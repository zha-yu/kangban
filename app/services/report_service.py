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

import dataclasses
import datetime
import logging
import uuid
import zoneinfo
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health import HealthRecord
from app.models.medication import MedicationEvent, RiskEvent
from app.models.report import DailyReport
from app.services.notify_service import (
    NotificationTargetNotFound,
    send_guardian_notifications,
)

logger = logging.getLogger(__name__)

#: 报表日期按 Asia/Shanghai 自然日切分（展示口径），落库 UTC（AGENTS §8）
_TZ_SHANGHAI: Final = zoneinfo.ZoneInfo("Asia/Shanghai")

#: 「日报已更新」通知类型（notifications.type 无 CHECK 枚举，三段式键见模块 docstring）
NOTIFICATION_TYPE_DAILY_REPORT: Final = "daily_report_updated"

#: 免责声明（PRD §1.4：不构成医疗诊断）
DISCLAIMER: Final = "本日报为教学模拟信息，不构成医疗诊断。"

#: 用药终态四类（models/medication.py MedicationEvent.terminal_state 注释口径）
_MED_TERMINAL_STATES: Final = (
    "taken_reported",
    "refused",
    "missed_unconfirmed",
    "notification_failed",
)


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


async def _notify_daily_report(
    session: AsyncSession, *, ref: DailyReportRef, summary_text: str
) -> bool:
    """「日报已更新」家属通知（每版一次，X2 三段式键）；无 active 绑定不阻塞生成。"""
    try:
        refs = await send_guardian_notifications(
            session,
            elder_id=ref.elder_id,
            ntype=NOTIFICATION_TYPE_DAILY_REPORT,
            payload={
                "title": "日报已更新",
                "body": f"{ref.report_date.isoformat()} 健康日报（第 {ref.version} 版）已生成",
                "summary_text": summary_text,
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
    report = DailyReport(
        elder_id=elder_id,
        report_date=day,
        version=version,
        status="normal",
        summary_text=summary_text,
        medication_stats=stats,
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
        notification_created=await _notify_daily_report(
            session, ref=ref, summary_text=summary_text
        ),
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


__all__ = [
    "DISCLAIMER",
    "NOTIFICATION_TYPE_DAILY_REPORT",
    "DailyReportRef",
    "generate_daily_report",
    "get_daily_report",
    "list_daily_report_versions",
]
