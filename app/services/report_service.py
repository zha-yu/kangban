"""家属日报服务（Day 8 · C2）。

**daily_reports 表唯一写库入口**（TD §3.5：api / audio / scheduler / graphs
一律经本文件写日报，AC-13 生成 / AC-25 版本链）。

X2 口径（S2 冻结，与 dev-A A3 落库基准同款三段式，提交 99e09d1 双留痕）：
- 「日报已更新」家属通知 dedupe_key = ``daily_report:{report_id}:update``
  （notify_service 逐监护人补 ``:<guardian_id>`` 后缀，同一 report_id 只发一次）；
- 日报**版本链以行为单位**：补录（regenerate）生成新行（version+1、新 report_id），
  旧版保留可查（UNIQUE(elder_id, report_date, version) 兜底，AC-25）——
  因此「每版只发一次」天然成立：新版本 = 新 report_id = 新 dedupe 锚点。

内容口径（PRD §4.7 / TD §3.5）：
- 仅取结构化数据：健康采集记录（当日最新版）+ 用药事件终态四类分列 +
  关注事件（risk_events 当日计数）；**不接记忆检索层**（红线：日报不引长期记忆）；
- 未答项写「未采集」（20:00 强制结束口径，PRD §4.6）；
- 固定拼入「本日报为教学模拟信息，不构成医疗诊断。」（PRD §1.4 非目标声明）。
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import logging
import struct
import uuid
import zoneinfo
from typing import Any, Final, Protocol, runtime_checkable

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health import HealthRecord
from app.models.medication import MedicationEvent, RiskEvent
from app.models.organization import Elder
from app.models.relation import ElderGuardian
from app.models.report import DailyReport
from app.services.notify_service import (
    NotificationTargetNotFound,
    PurgeRef,
    send_guardian_notifications,
)

logger = logging.getLogger(__name__)

#: 报表日期按 Asia/Shanghai 自然日切分（展示口径），落库 UTC（AGENTS §8）
_TZ_SHANGHAI: Final = zoneinfo.ZoneInfo("Asia/Shanghai")

#: 「日报已更新」通知类型（notifications.type 无 CHECK 枚举，三段式键见模块 docstring）
NOTIFICATION_TYPE_DAILY_REPORT: Final = "daily_report_updated"

#: 免责声明（PRD §1.4：不构成医疗诊断）
DISCLAIMER: Final = "本日报为教学模拟信息，不构成医疗诊断。"

#: 用药终态五类（models/medication.py terminal_state CHECK 约束口径；B4 已落库
#: late_taken_reported，本处对齐——family 五键联调 D11 14:00，2026-11-12 dev-B 提出）
_MED_TERMINAL_STATES: Final = (
    "taken_reported",
    "refused",
    "missed_unconfirmed",
    "notification_failed",
    "late_taken_reported",
)

# ------------------------------------------------------------------ F30 日报语音条
# services 禁反向 import app/audio（response_service 同款红线），故此处只定义
# **结构协议**（与 app.audio.asr.TTSProvider 的 ``synthesize(text)->bytes`` 同形），
# 由调用方（scheduler 装配 / 测试）注入任意实现；合成失败一律降级纯文本（模型注释口径）。


@runtime_checkable
class ReportTtsSynthesizer(Protocol):
    """日报语音条合成协议（F30，FR-RPT-04）：文本 → 16kHz/16bit/单声道 s16le PCM。"""

    async def synthesize(self, text: str) -> bytes: ...


#: PCM 口径与 app/audio/tts.py C1 契约一致（本地常量，避免反向 import）
_PCM_SAMPLE_RATE: Final[int] = 16_000
_PCM_SAMPLE_WIDTH: Final[int] = 2
_PCM_CHANNELS: Final[int] = 1


def pcm_to_wav_bytes(pcm: bytes) -> bytes:
    """裸 s16le PCM 包装为 WAV 容器（RIFF 头 + 数据；F30 语音条承载格式）。"""
    byte_rate = _PCM_SAMPLE_RATE * _PCM_CHANNELS * _PCM_SAMPLE_WIDTH
    block_align = _PCM_CHANNELS * _PCM_SAMPLE_WIDTH
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        _PCM_CHANNELS,
        _PCM_SAMPLE_RATE,
        byte_rate,
        block_align,
        _PCM_SAMPLE_WIDTH * 8,
        b"data",
        len(pcm),
    )
    return header + pcm


def _tts_data_url(wav: bytes) -> str:
    """WAV → ``data:audio/wav;base64,…``（教学/演示期免对象存储；D10 上线存储后换 URL 口径）。"""
    return "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")


async def synthesize_report_audio(tts: ReportTtsSynthesizer, summary_text: str) -> str | None:
    """日报摘要 → ``audio_url``（F30）。合成失败降级纯文本（返回 None，不抛出、不阻断日报）。"""
    try:
        pcm = await tts.synthesize(summary_text)
    except Exception:
        logger.warning("report_tts_failed report_len=%s", len(summary_text))
        return None
    if not pcm:
        return None
    return _tts_data_url(pcm_to_wav_bytes(pcm))


@dataclasses.dataclass(frozen=True, slots=True)
class DailyReportRef:
    """日报生成结果引用（写库后的最小回执）。"""

    report_id: uuid.UUID
    elder_id: uuid.UUID
    report_date: datetime.date
    version: int
    status: str
    created: bool  # False = 幂等返回既有版本，未新建行
    notification_created: bool  # 「日报已更新」通知是否新落（dedupe 命中为 False）


def _day_window_utc(report_date: datetime.date) -> tuple[datetime.datetime, datetime.datetime]:
    """Asia/Shanghai 自然日 → UTC 半开区间 [00:00, 次日 00:00)。"""
    start = datetime.datetime(
        report_date.year, report_date.month, report_date.day, tzinfo=_TZ_SHANGHAI
    )
    end = start + datetime.timedelta(days=1)
    return start.astimezone(datetime.UTC), end.astimezone(datetime.UTC)


def _today_shanghai() -> datetime.date:
    return datetime.datetime.now(_TZ_SHANGHAI).date()


async def _latest_version(
    session: AsyncSession, *, elder_id: uuid.UUID, report_date: datetime.date
) -> int:
    """该长者当日已存在的最大版本号（无则 0）。"""
    result = await session.execute(
        select(func.max(DailyReport.version)).where(
            DailyReport.elder_id == elder_id,
            DailyReport.report_date == report_date,
        )
    )
    return int(result.scalar_one() or 0)


async def _get_by_version(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    report_date: datetime.date,
    version: int,
) -> DailyReport | None:
    result = await session.execute(
        select(DailyReport).where(
            DailyReport.elder_id == elder_id,
            DailyReport.report_date == report_date,
            DailyReport.version == version,
        )
    )
    return result.scalar_one_or_none()


async def _collect_medication_stats(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    window: tuple[datetime.datetime, datetime.datetime],
) -> dict[str, Any]:
    """当日用药事件按终态四类分列（AC-13；空态 total=0）。"""
    result = await session.execute(
        select(
            MedicationEvent.terminal_state,
            MedicationEvent.drug_snapshot,
            MedicationEvent.scheduled_at,
        ).where(
            MedicationEvent.elder_id == elder_id,
            MedicationEvent.scheduled_at >= window[0],
            MedicationEvent.scheduled_at < window[1],
        )
    )
    counters = dict.fromkeys(_MED_TERMINAL_STATES, 0)
    details: list[dict[str, Any]] = []
    for terminal_state, drug_snapshot, scheduled_at in result.all():
        state = terminal_state if terminal_state in counters else None
        if state is not None:
            counters[state] += 1
        details.append(
            {
                "drug": drug_snapshot.get("drug_name", "未知药品"),
                "dose": drug_snapshot.get("dose", ""),
                "scheduled_at": scheduled_at.isoformat(),
                "terminal_state": state,  # 未到终态（calling/waiting）→ null
            }
        )
    counters["total"] = len(details)
    counters["details"] = details
    return counters


async def _collect_health_summary(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    window: tuple[datetime.datetime, datetime.datetime],
) -> str:
    """当日健康采集摘要段：仅取**当前版本**（superseded_by IS NULL），未采集如实声明。"""
    result = await session.execute(
        select(HealthRecord)
        .where(
            HealthRecord.elder_id == elder_id,
            HealthRecord.recorded_at >= window[0],
            HealthRecord.recorded_at < window[1],
            HealthRecord.superseded_by.is_(None),
        )
        .order_by(HealthRecord.recorded_at.desc())
    )
    records = list(result.scalars().all())
    if not records:
        return "健康采集：未采集（当日无有效采集记录）。"
    latest = records[0]
    parts = [
        f"健康采集：收缩压 {latest.systolic if latest.systolic is not None else '未采集'}"
        f"/舒张压 {latest.diastolic if latest.diastolic is not None else '未采集'} mmHg，"
        f"睡眠 {latest.sleep_hours if latest.sleep_hours is not None else '未采集'} 小时，"
        f"情绪评分 {latest.mood_score if latest.mood_score is not None else '未采集'}/5。"
    ]
    if latest.complaint:
        parts.append(f"长者自述：{latest.complaint}")
    if len(records) > 1:
        parts.append(f"（当日共 {len(records)} 次采集，取最新一次）")
    return "".join(parts)


async def _collect_risk_summary(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    window: tuple[datetime.datetime, datetime.datetime],
) -> str:
    """当日关注事件摘要段（risk_events 计数，红/黄分列）。"""
    result = await session.execute(
        select(RiskEvent.severity, func.count())
        .where(
            RiskEvent.elder_id == elder_id,
            RiskEvent.created_at >= window[0],
            RiskEvent.created_at < window[1],
        )
        .group_by(RiskEvent.severity)
    )
    counts = {"red": 0, "yellow": 0}
    for severity, count in result.all():
        if severity in counts:
            counts[severity] = int(count)
    total = counts["red"] + counts["yellow"]
    if total == 0:
        return "关注事件：无。"
    return (
        f"关注事件：{total} 起（红档 {counts['red']}、黄档 {counts['yellow']}），"
        "异常值已同步人工处理，详情以照护台记录为准。"
    )


def _build_summary_text(
    *,
    report_date: datetime.date,
    health_section: str,
    stats: dict[str, Any],
    risk_section: str,
) -> str:
    """家属可读摘要（结构化统计另存 medication_stats，TD §3.5）。"""
    if stats["total"] == 0:
        med_section = "用药情况：当日无用药记录。"
    else:
        med_section = (
            f"用药情况：共 {stats['total']} 次——按时服用 {stats['taken_reported']}、"
            f"拒服 {stats['refused']}、漏服待补 {stats['missed_unconfirmed']}、"
            f"通知失败 {stats['notification_failed']}。"
        )
    return (
        f"【{report_date.isoformat()} 健康日报】\n"
        f"{health_section}\n{med_section}\n{risk_section}\n{DISCLAIMER}"
    )


async def _notify_daily_report(session: AsyncSession, *, ref: DailyReportRef) -> bool:
    """「日报已更新」家属通知（每版一次，X2 三段式键）；无 active 绑定不阻塞生成。

    payload 按家属端契约 S-02 三键组装（docs/接口契约_家属端数据.md §2.2）：
    ``elder_name`` / ``event_brief`` / ``report_id``——家属卡片渲染与日报跳转
    都靠这三键；Q-D9 实测 {title,body,summary_text} 落法卡片空、点不动日报。
    ``title`` 为向前兼容保留键（家属端忽略未知键）；日报全文经 report_id
    拉详情，不进 payload（S-02 脱敏口径）。
    """
    elder_name = (
        await session.execute(select(Elder.name).where(Elder.elder_id == ref.elder_id))
    ).scalar_one_or_none()
    try:
        refs = await send_guardian_notifications(
            session,
            elder_id=ref.elder_id,
            ntype=NOTIFICATION_TYPE_DAILY_REPORT,
            payload={
                "title": "日报已更新",
                "elder_name": elder_name or "您的家人",
                "event_brief": (
                    f"{ref.report_date.isoformat()} 健康日报（第 {ref.version} 版）已生成，点击查看详情"
                ),
                "report_id": str(ref.report_id),
            },
            dedupe_key=f"daily_report:{ref.report_id}:update",
        )
    except NotificationTargetNotFound:
        logger.info("日报 %s 无 active 家属绑定，跳过通知", ref.report_id, extra={"trace_id": "-"})
        return False
    return any(r.created for r in refs)


async def generate_daily_report(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    report_date: datetime.date | None = None,
    regenerate: bool = False,
    tts: ReportTtsSynthesizer | None = None,
) -> DailyReportRef:
    """生成（或补录）某长者某日日报。

    - ``regenerate=False``（首版/幂等）：当日已有版本则**返回原版不重建、
      不重发通知**（dedupe_key 双保险）；
    - ``regenerate=True``（补录）：生成新行 version=max+1、旧版保留（AC-25），
      并随新 report_id 发一次「日报已更新」。

    Raises:
        ValueError: 长者不存在。
    """
    from app.models.organization import Elder  # 局部导入避免潜在循环依赖

    elder = (
        await session.execute(select(Elder.elder_id).where(Elder.elder_id == elder_id))
    ).scalar_one_or_none()
    if elder is None:
        raise ValueError(f"elder {elder_id} not found")

    day = report_date or _today_shanghai()
    window = _day_window_utc(day)

    if not regenerate:
        existing = await _get_by_version(session, elder_id=elder_id, report_date=day, version=1)
        if existing is not None:
            return DailyReportRef(
                report_id=existing.report_id,
                elder_id=existing.elder_id,
                report_date=existing.report_date,
                version=existing.version,
                status=existing.status,
                created=False,
                notification_created=False,  # 幂等命中：不重发
            )

    stats = await _collect_medication_stats(session, elder_id=elder_id, window=window)
    health_section = await _collect_health_summary(session, elder_id=elder_id, window=window)
    risk_section = await _collect_risk_summary(session, elder_id=elder_id, window=window)
    summary_text = _build_summary_text(
        report_date=day, health_section=health_section, stats=stats, risk_section=risk_section
    )

    version = (
        await _latest_version(session, elder_id=elder_id, report_date=day) + 1 if regenerate else 1
    )
    audio_url = await synthesize_report_audio(tts, summary_text) if tts is not None else None
    report = DailyReport(
        elder_id=elder_id,
        report_date=day,
        version=version,
        status="normal",
        summary_text=summary_text,
        medication_stats=stats,
        audio_url=audio_url,
        pushed_at=datetime.datetime.now(datetime.UTC),  # 生成即推送（通知随后落）
    )
    session.add(report)
    await session.flush()

    ref = DailyReportRef(
        report_id=report.report_id,
        elder_id=elder_id,
        report_date=day,
        version=version,
        status="normal",
        created=True,
        notification_created=False,
    )
    ref = dataclasses.replace(
        ref,
        notification_created=await _notify_daily_report(session, ref=ref),
    )
    return ref


async def get_daily_report(
    session: AsyncSession, *, elder_id: uuid.UUID, report_date: datetime.date
) -> DailyReport | None:
    """查询某长者某日**最新版**日报（家属端/回放读取口；不存在返回 None）。"""
    result = await session.execute(
        select(DailyReport)
        .where(
            DailyReport.elder_id == elder_id,
            DailyReport.report_date == report_date,
        )
        .order_by(DailyReport.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_daily_report_versions(
    session: AsyncSession, *, elder_id: uuid.UUID, report_date: datetime.date
) -> list[DailyReport]:
    """某长者某日全部版本（version 升序，AC-25 补录历史可查）。"""
    result = await session.execute(
        select(DailyReport)
        .where(
            DailyReport.elder_id == elder_id,
            DailyReport.report_date == report_date,
        )
        .order_by(DailyReport.version.asc())
    )
    return list(result.scalars().all())


# ------------------------------------------------------------------ 家属端读侧（契约 F1/F2/F3，dev-D D1 依赖）

#: F1 分页上限（契约 S-10：page_size 默认 20，上限 100）
_PAGE_SIZE_MAX: Final[int] = 100

#: attention_events.brief 兜底短标（type 字段输出 risk_events.type 五枚举原值；契约 F2 示例值系示意）
_RISK_TYPE_LABELS: Final[dict[str, str]] = {
    "red_emergency": "紧急事件",
    "health_alert": "健康异常提醒",
    "med_risk": "用药风险",
    "sensitive": "敏感内容提醒",
    "sos": "SOS 呼叫",
}


class GuardianReportNotFound(LookupError):
    """家属日报不存在（F1/F2 → 404，不与 403 混淆）。"""


class GuardianReportForbidden(PermissionError):
    """日报所属长者不在该监护人 active 绑定范围（F1/F2 → 403；审计由 API 层 catch 后写，AGENTS §8）。"""


async def _guardian_active_elder_ids(
    session: AsyncSession, *, guardian_id: uuid.UUID
) -> list[uuid.UUID]:
    """监护人 active 绑定长者集合（elder_guardians status='active'；AC-24 解绑即出范围）。"""
    result = await session.execute(
        select(ElderGuardian.elder_id).where(
            ElderGuardian.guardian_id == guardian_id,
            ElderGuardian.status == "active",
        )
    )
    return list(result.scalars().all())


async def list_bound_elders_for_guardian(
    session: AsyncSession, *, guardian_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, str]]:
    """监护人绑定长者清单（契约 F0 首页长者卡片）：[(elder_id, name, relation)]。

    仅 status='active'；unbound 不出（AC-24 解绑保痕但对家属不可见）。
    """
    result = await session.execute(
        select(Elder.elder_id, Elder.name, ElderGuardian.relation)
        .join(Elder, Elder.elder_id == ElderGuardian.elder_id)
        .where(
            ElderGuardian.guardian_id == guardian_id,
            ElderGuardian.status == "active",
        )
        .order_by(Elder.name.asc())
    )
    return [(row[0], row[1], row[2]) for row in result.all()]


async def list_reports_for_guardian(
    session: AsyncSession,
    *,
    guardian_id: uuid.UUID,
    elder_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[DailyReport], int]:
    """家属日报列表（契约 F1）：绑定范围过滤 + 每日只出最大版本（S-07）+ 分页作用在去重后。

    - 范围： elder_guardians status='active'；无绑定返回 ([], 0)（S-08，200 空列表）；
    - elder_id 可选：契约 F1 按长者路径（/api/family/elders/{elder_id}/reports）。
      过滤进**去重子查询内部**（外层过滤后再分页会错 total/页）；指定长者不在
      绑定范围同样返回 ([], 0)（越权分流由 API 层按 F0 绑定清单裁决）；
    - 去重： 同一 (elder_id, report_date) 仅 version 最大者，report_date 倒序；
    - 分页： S-10——page≥1、page_size∈[1,100]（服务端钳制兜底，入参校验在 API 层），
      作用在去重后的行集上；total = 去重后总行数（与 page 无关）。
    """
    elder_ids = await _guardian_active_elder_ids(session, guardian_id=guardian_id)
    if not elder_ids or (elder_id is not None and elder_id not in elder_ids):
        return [], 0
    scoped_ids = [elder_id] if elder_id is not None else elder_ids
    page = max(1, page)
    page_size = min(max(1, page_size), _PAGE_SIZE_MAX)

    latest = (
        select(
            DailyReport.elder_id,
            DailyReport.report_date,
            func.max(DailyReport.version).label("max_version"),
        )
        .where(DailyReport.elder_id.in_(scoped_ids))
        .group_by(DailyReport.elder_id, DailyReport.report_date)
        .subquery()
    )
    total = int((await session.execute(select(func.count()).select_from(latest))).scalar_one())
    result = await session.execute(
        select(DailyReport)
        .join(
            latest,
            and_(
                DailyReport.elder_id == latest.c.elder_id,
                DailyReport.report_date == latest.c.report_date,
                DailyReport.version == latest.c.max_version,
            ),
        )
        .order_by(DailyReport.report_date.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def get_report_detail_for_guardian(
    session: AsyncSession,
    *,
    guardian_id: uuid.UUID,
    report_id: uuid.UUID,
) -> tuple[DailyReport, str, list[dict[str, Any]]]:
    """家属日报详情（契约 F2）：(report, health_summary, attention_events)，查询时组装（S-09）。

    - 任意版本（含旧版）均可查（F3 自裁 S-10）；
    - health_summary 复用生成侧口径（当日窗口、仅当前版本采集）；
    - attention_events：当日窗口 risk_events 倒序；level=severity、type=五枚举原值、
      brief 真实优先——health_record 来源取实测血压，其余用类型中文短标（不编造细节）；
    - 404/403 分离：不存在→GuardianReportNotFound；存在但不属绑定范围→GuardianReportForbidden。
    """
    report = await session.get(DailyReport, report_id)
    if report is None:
        raise GuardianReportNotFound(f"daily_report {report_id} not found")
    elder_ids = await _guardian_active_elder_ids(session, guardian_id=guardian_id)
    if report.elder_id not in elder_ids:
        raise GuardianReportForbidden(
            f"daily_report {report_id} elder {report.elder_id} not bound to guardian {guardian_id}"
        )

    window = _day_window_utc(report.report_date)
    health_summary = await _collect_health_summary(session, elder_id=report.elder_id, window=window)

    result = await session.execute(
        select(RiskEvent)
        .where(
            RiskEvent.elder_id == report.elder_id,
            RiskEvent.created_at >= window[0],
            RiskEvent.created_at < window[1],
        )
        .order_by(RiskEvent.created_at.desc())
    )
    events: list[dict[str, Any]] = []
    for risk in result.scalars().all():
        brief = _RISK_TYPE_LABELS.get(risk.type, risk.type)
        if risk.source_type == "health_record" and risk.source_id is not None:
            source = await session.get(HealthRecord, risk.source_id)
            if source is not None and (source.systolic is not None or source.diastolic is not None):
                systolic = source.systolic if source.systolic is not None else "未采集"
                diastolic = source.diastolic if source.diastolic is not None else "未采集"
                brief = f"血压 {systolic}/{diastolic} mmHg"
        events.append(
            {
                "level": risk.severity,
                "type": risk.type,
                "brief": brief,
                "occurred_at": risk.created_at.isoformat(),
            }
        )
    return report, health_summary, events


# ------------------------------------------------------------------ X12 域清理钩子


async def purge_elder_data(session: AsyncSession, *, elder_id: uuid.UUID, reason: str) -> PurgeRef:
    """日报域清理（X12）：daily_reports 全版本物理清除（checkout／teaching_reset 同口径）。

    日报是派生物（可由 health/medication 原始域重算），无归档价值；健康原始行
    的归档语义归 health_service.purge_elder_data。幂等：重复调用第二遍计数为 0。
    本钩子不写审计（A 的 lifecycle 统一写）。
    """
    if reason not in ("checkout", "teaching_reset"):
        raise ValueError(f"unknown purge reason: {reason!r}")
    from sqlalchemy import delete as sa_delete

    result = await session.execute(sa_delete(DailyReport).where(DailyReport.elder_id == elder_id))
    affected = int(result.rowcount or 0)
    if affected:
        await session.flush()
    logger.info(
        "report_purge_elder_data elder_id=%s reason=%s affected=%d", elder_id, reason, affected
    )
    return PurgeRef(domain="daily_reports", affected_rows=affected, archived_rows=0)


__all__ = [
    "DISCLAIMER",
    "NOTIFICATION_TYPE_DAILY_REPORT",
    "DailyReportRef",
    "GuardianReportForbidden",
    "GuardianReportNotFound",
    "generate_daily_report",
    "get_daily_report",
    "get_report_detail_for_guardian",
    "list_bound_elders_for_guardian",
    "list_daily_report_versions",
    "list_reports_for_guardian",
    "purge_elder_data",
]
