"""Day 8 dev-A：健康分级引擎 + 写库入口 + 复核入口。

分层硬约束（AGENTS §2）
-----------------------
本文件是**健康域唯一写库层**。`api/health.py`、`graphs/health_checkup_graph`、
`scheduler/health_collection` 一律不得直接操作 ORM 写数据，必须走这里。

架构
----
- `grade_health()` —— **纯函数**：无 DB、无 session 依赖，校验 + 分级。
  单测不 mock session 即可跑。签名 12:00 前冻结（X1，通知 B/C）。
- `create_health_record()` —— 写库入口：校验 → 分级 → 写 health_records
  → 按需建 risk_events + work_order。越权保护、版本链、黄升红全部在这里做。
- `review_health_record()` —— 复核入口：确认/纠错。纠错写新版本
  （superseded_reason="review_correction"，纯服务端写入，前端不传）。

分级规则（PRD §4.6 + AGENTS §7 硬性数值）
------------------------------------------
红色阈值：单分量即判红（AC-21）
  - 收缩压 ≥180 或 收缩压 ≤90
  - 舒张压 ≥120 或 舒张压 ≤60

黄色阈值（AC-09）：
  - 收缩压 140–179
  - 舒张压 90–119
  - 睡眠 <5h
  - mood_score ≤2
  - 任一字段缺失

绿色：收缩压 90–139 且 舒张压 60–89 且 睡眠 5–12h 且 mood 3–5 且 无缺失。

多项并存取最高（红 > 黄 > 绿）。

黄升红（AC-22）：
  同一 elder_id + 当日已有未关闭黄色 risk_event → 补录数据重算 → 若黄升红：
  原 risk_event.severity=red + status 保持 open（**不新建第二事件**）。
  work_order.severity 是建单快照，**不随后同步**（TD 明确）。

阈值改了必须 RULE_VERSION +1（写进 health_records.rule_version）。
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from typing import Final, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.health import HealthRecord
from app.models.medication import RiskEvent
from app.models.organization import Bed, Elder, Room
from app.models.relation import ElderStaffAssignment
from app.models.staff import StaffAccount
from app.services.work_order_service import (
    WorkOrderRef,
    open_work_order,
    resolve_primary_care_staff,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 分级常量（AGENTS §7 · 写死代码不写 prompt）

#: 分级规则版本（改阈值必须 +1，rule_version 写进 health_records）
RULE_VERSION: Final[str] = "g1"

#: 严重度三值（与 0017 迁移 CHECK 同源）
SEVERITY_GREEN: Final[str] = "green"
SEVERITY_YELLOW: Final[str] = "yellow"
SEVERITY_RED: Final[str] = "red"

#: 红色阈值（单分量即判红，AC-21）
SYSTOLIC_RED_UPPER: Final[int] = 180  # 收缩压 ≥180
SYSTOLIC_RED_LOWER: Final[int] = 90  # 收缩压 ≤90（低血压）
DIASTOLIC_RED_UPPER: Final[int] = 120  # 舒张压 ≥120
DIASTOLIC_RED_LOWER: Final[int] = 60  # 舒张压 ≤60

#: 黄色阈值（AC-09 口径）
SYSTOLIC_YELLOW_LOWER: Final[int] = 140  # 140–179 = 黄收缩压
DIASTOLIC_YELLOW_LOWER: Final[int] = 90  # 90–119 = 黄舒张压

#: 睡眠阈值
SLEEP_YELLOW_HOURS: Final[float] = 5.0  # <5h = 黄

#: 情绪阈值
MOOD_YELLOW: Final[int] = 2  # mood_score ≤2 = 黄

#: 正常范围上限（绿档）
SYSTOLIC_GREEN_UPPER: Final[int] = 139
DIASTOLIC_GREEN_UPPER: Final[int] = 89
SLEEP_GREEN_UPPER: Final[float] = 12.0  # 睡眠 5–12h = 绿


# ------------------------------------------------------------------ 返回值


@dataclasses.dataclass(frozen=True, slots=True)
class ComponentGrade:
    """单分量判定详情（AC-09/21 可追溯）。"""

    field: str  # systolic / diastolic / sleep_hours / mood_score
    value: float | int | None  # 校验后的有效值（None 表示缺失/无效）
    severity: str  # green / yellow / red
    reason: str  # 简短可追溯原因（如 ">=180", "missing"）


@dataclasses.dataclass(frozen=True, slots=True)
class HealthGradeResult:
    """分级结果（grade_health 纯函数返回值）。"""

    severity: str  # 三值之一
    rule_version: str  # 写回 health_records
    components: tuple[ComponentGrade, ...]  # 四个分量详情
    has_missing: bool  # 是否有缺失字段
    has_invalid: bool  # 是否有无效数据（非数字/超范围等）


@dataclasses.dataclass(frozen=True, slots=True)
class HealthRecordRef:
    """写库结果。"""

    record_id: uuid.UUID
    severity: str
    version: int
    risk_event_id: uuid.UUID | None  # 本次新建或升级的 risk_event
    work_order_id: uuid.UUID | None  # 本次新建的工单（黄档建单；红档若走 safety_service 则为 None）


# ------------------------------------------------------------------ 纯函数分级（无 DB 依赖，签名 12:00 X1 冻结）


def _validate_number(value: int | float | None, *, lo: float, hi: float) -> float | None:
    """校验数值：None / 非数字 / 越界 → 返回 None（记缺失）。"""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num < lo or num > hi:
        return None
    return num


def _grade_systolic(systolic: int | None) -> ComponentGrade:
    """收缩压分量分级。"""
    valid = _validate_number(systolic, lo=0, hi=300)
    if valid is None:
        return ComponentGrade("systolic", None, SEVERITY_YELLOW, "missing_or_invalid")
    if valid >= SYSTOLIC_RED_UPPER or valid <= SYSTOLIC_RED_LOWER:
        return ComponentGrade("systolic", int(valid), SEVERITY_RED, str(int(valid)))
    if valid >= SYSTOLIC_YELLOW_LOWER:
        return ComponentGrade("systolic", int(valid), SEVERITY_YELLOW, str(int(valid)))
    return ComponentGrade("systolic", int(valid), SEVERITY_GREEN, str(int(valid)))


def _grade_diastolic(diastolic: int | None) -> ComponentGrade:
    """舒张压分量分级。"""
    valid = _validate_number(diastolic, lo=0, hi=300)
    if valid is None:
        return ComponentGrade("diastolic", None, SEVERITY_YELLOW, "missing_or_invalid")
    if valid >= DIASTOLIC_RED_UPPER or valid <= DIASTOLIC_RED_LOWER:
        return ComponentGrade("diastolic", int(valid), SEVERITY_RED, str(int(valid)))
    if valid >= DIASTOLIC_YELLOW_LOWER:
        return ComponentGrade("diastolic", int(valid), SEVERITY_YELLOW, str(int(valid)))
    return ComponentGrade("diastolic", int(valid), SEVERITY_GREEN, str(int(valid)))


def _grade_sleep(sleep_hours: float | None) -> ComponentGrade:
    """睡眠分量分级。"""
    valid = _validate_number(sleep_hours, lo=0.0, hi=24.0)
    if valid is None:
        return ComponentGrade("sleep_hours", None, SEVERITY_YELLOW, "missing_or_invalid")
    if valid < SLEEP_YELLOW_HOURS:
        return ComponentGrade(
            "sleep_hours", round(valid, 1), SEVERITY_YELLOW, f"<{SLEEP_YELLOW_HOURS}h"
        )
    if valid > SLEEP_GREEN_UPPER:
        return ComponentGrade(
            "sleep_hours", round(valid, 1), SEVERITY_YELLOW, f">{SLEEP_GREEN_UPPER}h"
        )
    return ComponentGrade("sleep_hours", round(valid, 1), SEVERITY_GREEN, f"{valid}h")


def _grade_mood(mood_score: int | None) -> ComponentGrade:
    """情绪分量分级。"""
    valid = _validate_number(mood_score, lo=1, hi=5)
    if valid is None:
        return ComponentGrade("mood_score", None, SEVERITY_YELLOW, "missing_or_invalid")
    score = int(valid)
    if score <= MOOD_YELLOW:
        return ComponentGrade("mood_score", score, SEVERITY_YELLOW, f"≤{MOOD_YELLOW}")
    return ComponentGrade("mood_score", score, SEVERITY_GREEN, str(score))


def grade_health(
    *,
    systolic: int | None,
    diastolic: int | None,
    sleep_hours: float | None,
    mood_score: int | None,
    complaint: str | None,
) -> HealthGradeResult:
    """**纯函数** · 无 I/O · 无 DB 依赖。

    返回分级结果（severity + rule_version + 各分量判定详情）。
    单分量达红即红（AC-21），多项并存取最高。

    :param complaint: 主诉文本，当前版本仅参与日志，不单独触发升级。
    """
    comp_s = _grade_systolic(systolic)
    comp_d = _grade_diastolic(diastolic)
    comp_sl = _grade_sleep(sleep_hours)
    comp_m = _grade_mood(mood_score)
    components: tuple[ComponentGrade, ...] = (comp_s, comp_d, comp_sl, comp_m)

    # 取最高严重度
    severity_rank = {SEVERITY_RED: 3, SEVERITY_YELLOW: 2, SEVERITY_GREEN: 1}
    highest = max(components, key=lambda c: severity_rank[c.severity])
    final_severity = highest.severity

    has_missing = any(c.value is None for c in components)
    has_invalid = has_missing  # 本版本缺失与无效合并处理

    # 红档优先于黄档（AC-21）：只要有一个分量是 red → 整体 red
    if any(c.severity == SEVERITY_RED for c in components):
        final_severity = SEVERITY_RED

    # 有缺失 → 黄档（但如果已有红分量，红优先）
    if final_severity == SEVERITY_GREEN and has_missing:
        final_severity = SEVERITY_YELLOW

    if complaint:
        logger.debug("grade_health_complaint complaint_len=%d", len(complaint))

    return HealthGradeResult(
        severity=final_severity,
        rule_version=RULE_VERSION,
        components=components,
        has_missing=has_missing,
        has_invalid=has_invalid,
    )


# ------------------------------------------------------------------ 越权校验


async def _verify_staff_proxy(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    operator_id: uuid.UUID,
) -> None:
    """护工代填越权保护：operator 必须是该长者的 primary_care 护工（AC-32）。"""
    result = await session.execute(
        select(ElderStaffAssignment).where(
            ElderStaffAssignment.elder_id == elder_id,
            ElderStaffAssignment.staff_id == operator_id,
            ElderStaffAssignment.relation == "primary_care",
        )
    )
    if result.scalar_one_or_none() is None:
        raise PermissionError(f"staff {operator_id} is not primary_care for elder {elder_id}")


#: 对外别名：H-3 补启动 / 调度补推的范围过滤复用（task_dispatch_service），
#: 避免跨模块摸私有名；语义与 `_verify_staff_proxy` 完全一致。
verify_staff_proxy = _verify_staff_proxy


# ------------------------------------------------------------------ 黄升红查询


async def _find_open_yellow_risk_event(
    session: AsyncSession, *, elder_id: uuid.UUID, today_start: datetime.datetime
) -> RiskEvent | None:
    """查当日该长者未关闭的黄色 risk_event（AC-22 黄升红的前置条件）。

    可能存在多条开放黄档（如采集窗口内多次触发、或历史事件未及时闭合）——
    取**最新一条**复用即可：黄档去重的语义是「同长者当日黄档归并到一个事件」，
    不能因多行而炸（scalar_one_or_none 会 MultipleResultsFound → 500，
    2026-11-11 代填走查实锤）。
    """
    result = await session.execute(
        select(RiskEvent)
        .where(
            RiskEvent.elder_id == elder_id,
            RiskEvent.severity == "yellow",
            RiskEvent.status == "open",
            RiskEvent.created_at >= today_start,
        )
        .order_by(RiskEvent.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ------------------------------------------------------------------ 写库入口


async def create_health_record(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    source: str,
    systolic: int | None,
    diastolic: int | None,
    sleep_hours: float | None,
    mood_score: int | None,
    mood_note: str | None,
    complaint: str | None,
    operator_id: uuid.UUID | None,
    graph_thread_id: str | None = None,
) -> HealthRecordRef:
    """写库入口：校验 → 分级 → 写 health_records → 按需建 risk_events + work_order。

    越权保护：source=staff_* 时 operator_id 必填 且 operator 必须是该长者的 primary_care 护工。

    版本链：首次采集 version=1；补录场景由**复核入口** `review_health_record` 创建 version+1。
    本函数只负责创建新的采集记录（version=1，superseded_by=NULL）。
    """
    # --- 越权保护 ---
    if source.startswith("staff_"):
        if operator_id is None:
            raise ValueError("operator_id required when source is staff_*")
        await _verify_staff_proxy(session, elder_id=elder_id, operator_id=operator_id)

    # --- 分级 ---
    grade = grade_health(
        systolic=systolic,
        diastolic=diastolic,
        sleep_hours=sleep_hours,
        mood_score=mood_score,
        complaint=complaint,
    )

    # --- 写 health_records ---
    record = HealthRecord(
        record_id=uuid.uuid4(),
        elder_id=elder_id,
        operator_id=operator_id,
        reviewed_by=None,  # 首次采集未复核
        recorded_at=datetime.datetime.now(datetime.UTC),
        source=source,
        systolic=systolic,
        diastolic=diastolic,
        sleep_hours=sleep_hours,
        mood_score=mood_score,
        mood_note=mood_note,
        complaint=complaint,
        severity=grade.severity,
        rule_version=grade.rule_version,
        review_status="pending",  # 首次采集等待复核
        review_note=None,
        superseded_reason=None,
        version=1,
        superseded_by=None,
        graph_thread_id=graph_thread_id,
    )
    session.add(record)
    await session.flush()

    risk_event_id: uuid.UUID | None = None
    work_order_id: uuid.UUID | None = None

    # --- 按需建 risk_events + work_order ---
    if grade.severity in (SEVERITY_YELLOW, SEVERITY_RED):
        # AC-22 黄升红：查当日未关闭黄色 risk_event
        now_utc = datetime.datetime.now(datetime.UTC)
        today_start = datetime.datetime(
            now_utc.year, now_utc.month, now_utc.day, tzinfo=datetime.UTC
        )
        existing_yellow = await _find_open_yellow_risk_event(
            session, elder_id=elder_id, today_start=today_start
        )

        if existing_yellow is not None and grade.severity == SEVERITY_RED:
            # 黄升红：原事件升级，不新建
            existing_yellow.severity = "red"
            existing_yellow.source_id = record.record_id  # 更新来源指向最新记录
            existing_yellow.source_type = "health_record"
            risk_event_id = existing_yellow.event_id
            logger.info(
                "yellow_to_red_upgrade event_id=%s elder_id=%s record_id=%s",
                existing_yellow.event_id,
                elder_id,
                record.record_id,
            )
            await session.flush()
        else:
            # 新建 risk_event
            risk_event = RiskEvent(
                event_id=uuid.uuid4(),
                elder_id=elder_id,
                type="health_alert",
                severity=grade.severity,
                source_type="health_record",
                source_id=record.record_id,
                status="open",
            )
            session.add(risk_event)
            await session.flush()
            risk_event_id = risk_event.event_id

            # 黄档建工单；红档走 safety_service（dev-C），本函数不重复建单
            if grade.severity == SEVERITY_YELLOW:
                assignee_id = await resolve_primary_care_staff(session, elder_id=elder_id)
                order_ref = await open_work_order(
                    session,
                    elder_id=elder_id,
                    order_type="health_alert",
                    severity="yellow",
                    dedupe_token=f"health_record:{record.record_id}:yellow",
                    event_id=risk_event.event_id,
                    assignee_id=assignee_id,
                )
                work_order_id = order_ref.order_id

    logger.info(
        "health_record_created record_id=%s elder_id=%s severity=%s version=1 "
        "risk_event_id=%s work_order_id=%s",
        record.record_id,
        elder_id,
        grade.severity,
        risk_event_id,
        work_order_id,
    )

    return HealthRecordRef(
        record_id=record.record_id,
        severity=grade.severity,
        version=1,
        risk_event_id=risk_event_id,
        work_order_id=work_order_id,
    )


# ------------------------------------------------------------------ 复核入口


async def review_health_record(
    session: AsyncSession,
    *,
    record_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    action: Literal["confirm", "correct"],
    corrected_values: dict | None = None,
    review_note: str | None = None,
) -> HealthRecordRef:
    """黄档护工可确认/纠错；红档需医生确认（scope filter 在外层 API 做）。

    **纠错版本链**：
    - 原 record.review_status = "corrected"，原 record.superseded_by = 新版本 record_id
    - 新版本 version = 原 version + 1
    - 新版本 superseded_by = None（最新版本）
    - 新版本 superseded_reason = "review_correction"（**纯服务端写入，前端不传**）
    - 新版本 severity 重新计算（用 corrected_values）

    **确认（confirm）**：
    - 只改原 record.review_status = "confirmed"，不创建新版本。
    """
    old_record = (
        await session.execute(select(HealthRecord).where(HealthRecord.record_id == record_id))
    ).scalar_one_or_none()
    if old_record is None:
        raise ValueError(f"health record {record_id} not found")

    if action == "confirm":
        old_record.review_status = "confirmed"
        old_record.reviewed_by = reviewer_id
        if review_note is not None:
            old_record.review_note = review_note
        await session.flush()
        logger.info(
            "health_record_confirmed record_id=%s reviewer_id=%s",
            record_id,
            reviewer_id,
        )
        return HealthRecordRef(
            record_id=old_record.record_id,
            severity=old_record.severity,
            version=old_record.version,
            risk_event_id=None,
            work_order_id=None,
        )

    # --- correct：创建新版本 ---
    cv = corrected_values or {}
    new_systolic: int | None = cv.get("systolic", old_record.systolic)
    new_diastolic: int | None = cv.get("diastolic", old_record.diastolic)
    new_sleep: float | None = cv.get("sleep_hours", old_record.sleep_hours)
    new_mood: int | None = cv.get("mood_score", old_record.mood_score)

    grade = grade_health(
        systolic=new_systolic,
        diastolic=new_diastolic,
        sleep_hours=new_sleep,
        mood_score=new_mood,
        complaint=cv.get("complaint", old_record.complaint),
    )

    # 原记录标记为被取代
    old_record.review_status = "corrected"

    new_record = HealthRecord(
        record_id=uuid.uuid4(),
        elder_id=old_record.elder_id,
        operator_id=old_record.operator_id,
        reviewed_by=reviewer_id,
        recorded_at=datetime.datetime.now(datetime.UTC),
        source=old_record.source,
        systolic=new_systolic,
        diastolic=new_diastolic,
        sleep_hours=new_sleep,
        mood_score=new_mood,
        mood_note=cv.get("mood_note", old_record.mood_note),
        complaint=cv.get("complaint", old_record.complaint),
        severity=grade.severity,
        rule_version=grade.rule_version,
        review_status="confirmed",  # 纠错后直接确认
        review_note=review_note,
        superseded_reason="review_correction",  # 纯服务端写入
        version=old_record.version + 1,
        superseded_by=None,  # 最新版本
        graph_thread_id=old_record.graph_thread_id,
    )
    # 原记录 superseded_by → 新版本
    old_record.superseded_by = new_record.record_id

    session.add(new_record)
    await session.flush()

    # 如果纠错后 severity 变化 → 黄升红/红降级 逻辑
    risk_event_id: uuid.UUID | None = None
    if grade.severity == SEVERITY_RED and old_record.severity != SEVERITY_RED:
        now_utc = datetime.datetime.now(datetime.UTC)
        today_start = datetime.datetime(
            now_utc.year, now_utc.month, now_utc.day, tzinfo=datetime.UTC
        )
        existing = await _find_open_yellow_risk_event(
            session, elder_id=old_record.elder_id, today_start=today_start
        )
        if existing is not None:
            existing.severity = "red"
            existing.source_id = new_record.record_id
            risk_event_id = existing.event_id
            await session.flush()

    logger.info(
        "health_record_corrected old_id=%s new_id=%s reviewer_id=%s severity_old=%s severity_new=%s version=%d",
        old_record.record_id,
        new_record.record_id,
        reviewer_id,
        old_record.severity,
        grade.severity,
        new_record.version,
    )

    return HealthRecordRef(
        record_id=new_record.record_id,
        severity=grade.severity,
        version=new_record.version,
        risk_event_id=risk_event_id,
        work_order_id=None,
    )


# ------------------------------------------------------------------ 查询（dev-C · S3 联调段增量）


@dataclasses.dataclass(frozen=True, slots=True)
class HealthRecordRow:
    """复核视图行：记录本体 + JOIN 展示字段（契约 §3.5 / D-4「JOIN 展示字段服务端拼」）。

    口径偏差登记（计划 §10，待 dev-B 契约勘误）：
    - 契约 161 行 `reviewed_at` 引注「TD health_records.reviewed_at」，但 TD 17 列
      **无 `reviewed_at`**（dev-A 0017 按 TD 落列正确）→ 展示层以 None 透传；
    - 契约 169 行 chain「同 elder_id + 同 recorded_at」与 R-4「新行 recorded_at=纠错时刻、
      superseded_by 指向新行」自相矛盾 → 按 R-4/AC-22 口径以 `superseded_by` 链接。
    """

    record: HealthRecord
    elder_name: str
    room_no: str | None
    bed_no: str | None
    operator_name: str | None
    reviewer_name: str | None


def _view_select() -> object:
    """复核视图统一取数：记录 + 长者姓名 + 操作/复核人姓名。

    只 join 实际取列的表（操作/复核人姓名）；床位定位由 `_attach_location`
    二段批量补齐——不得把 Room/Bed 塞进本查询，否则无 join 条件即笛卡尔积。
    """
    operator = aliased(StaffAccount)
    reviewer = aliased(StaffAccount)
    return (
        select(HealthRecord, Elder.name, operator.name, reviewer.name)
        .join(Elder, Elder.elder_id == HealthRecord.elder_id)
        .outerjoin(operator, operator.staff_id == HealthRecord.operator_id)
        .outerjoin(reviewer, reviewer.staff_id == HealthRecord.reviewed_by)
    )


async def _attach_location(session: AsyncSession, rows: list[tuple]) -> list[HealthRecordRow]:
    """床位定位（beds.elder_id 反查 + rooms 房号）批量补齐，无床位记录时字段为 None。"""
    if not rows:
        return []
    elder_ids = {r[0].elder_id for r in rows}
    locations = await session.execute(
        select(Bed.elder_id, Bed.bed_no, Room.room_no)
        .join(Room, Room.room_id == Bed.room_id)
        .where(Bed.elder_id.in_(elder_ids))
    )
    bed_by_elder: dict[uuid.UUID, tuple[str | None, str | None]] = {
        elder_id: (bed_no, room_no) for elder_id, bed_no, room_no in locations.all()
    }
    return [
        HealthRecordRow(
            record=r[0],
            elder_name=r[1],
            room_no=bed_by_elder.get(r[0].elder_id, (None, None))[1],
            bed_no=bed_by_elder.get(r[0].elder_id, (None, None))[0],
            operator_name=r[2],
            reviewer_name=r[3],
        )
        for r in rows
    ]


async def list_pending_reviews(session: AsyncSession, *, role: str) -> list[HealthRecordRow]:
    """复核队列（R-1）：pending 记录，recorded_at 倒序。

    范围口径（任务卡 C1①）：红档仅 doctor/admin 可见——care_worker 视图过滤掉
    red 档（黄/绿档护工可复核；绿档无需复核但 historical pending 不会是绿）。
    """
    stmt = _view_select().where(HealthRecord.review_status == "pending")
    if role == "care_worker":
        stmt = stmt.where(HealthRecord.severity != "red")
    stmt = stmt.order_by(HealthRecord.recorded_at.desc())
    result = await session.execute(stmt)
    return await _attach_location(session, list(result.all()))


async def get_record_row(session: AsyncSession, *, record_id: uuid.UUID) -> HealthRecordRow | None:
    """单条记录详情（R-2）：不存在返回 None（API 层同口径 404）。"""
    result = await session.execute(_view_select().where(HealthRecord.record_id == record_id))
    first = result.first()
    if first is None:
        return None
    rows = await _attach_location(session, [first])
    return rows[0]


async def get_record_chain(session: AsyncSession, *, record_id: uuid.UUID) -> list[HealthRecordRow]:
    """版本链（R-2b）：沿 superseded_by 双向收集同链记录，version 升序。

    链接口径：纠错创建新行（version+1，superseded_by 指向新行）——AC-22
    「原值保留可查」；契约 169 行「同 recorded_at」与 R-4 矛盾，不采纳（见
    HealthRecordRow docstring 偏差登记）。
    """
    anchor = await session.get(HealthRecord, record_id)
    if anchor is None:
        return []

    ids = [anchor.record_id]
    # 向最新方向走
    cursor = anchor
    while cursor.superseded_by is not None:
        cursor = (
            await session.execute(
                select(HealthRecord).where(HealthRecord.record_id == cursor.superseded_by)
            )
        ).scalar_one()
        ids.append(cursor.record_id)
    # 向最早方向走
    back = anchor
    while True:
        prev = (
            await session.execute(
                select(HealthRecord).where(HealthRecord.superseded_by == back.record_id)
            )
        ).scalar_one_or_none()
        if prev is None:
            break
        ids.append(prev.record_id)
        back = prev

    result = await session.execute(
        _view_select().where(HealthRecord.record_id.in_(ids)).order_by(HealthRecord.version.asc())
    )
    return await _attach_location(session, list(result.all()))


async def get_health_trend(
    session: AsyncSession, *, elder_id: uuid.UUID, days: int = 14
) -> list[HealthRecord] | None:
    """近 N 天指标序列（医生趋势 / O1 前端数据源）。

    - 仅取**当前版本**（superseded_by IS NULL，被纠错取代的旧值不进图）；
    - recorded_at 升序返回，窗口含边界（now-days 起至今）；
    - 长者不存在返回 None（与详情 404 同口径）；存在但无数据返回空列表。
    """
    elder = (
        await session.execute(select(Elder.elder_id).where(Elder.elder_id == elder_id))
    ).scalar_one_or_none()
    if elder is None:
        return None

    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
    result = await session.execute(
        select(HealthRecord)
        .where(
            HealthRecord.elder_id == elder_id,
            HealthRecord.recorded_at >= since,
            HealthRecord.superseded_by.is_(None),
        )
        .order_by(HealthRecord.recorded_at.asc())
    )
    return list(result.scalars().all())


# ------------------------------------------------------------------ 调度域查询与漏采派单（B3 · Day 8）
# 三个入口只服务 app/scheduler/health_jobs.py 的 tick 驱动链；写库仍只经
# open_work_order（工单唯一建单口 A12），本文件不加第二条建单路径。


async def list_active_elder_ids(session: AsyncSession) -> list[uuid.UUID]:
    """全部 active 长者（19:00 触发 / 20:00 漏采判定的扫描面）。"""
    return list(
        (await session.execute(select(Elder.elder_id).where(Elder.status == "active"))).scalars()
    )


async def list_elder_ids_with_records_on(
    session: AsyncSession, *, day: datetime.date
) -> set[uuid.UUID]:
    """指定**上海日期**已有健康记录的长者集合（FR-HLT-08 漏采判定）。

    `recorded_at` 是 UTC timestamptz：先转 `Asia/Shanghai` 再取 date，
    与 report_service 的日窗口口径一致——漏采是按长者所在地「那一天」算的。
    """
    rows = await session.execute(
        select(HealthRecord.elder_id).where(
            func.date(func.timezone("Asia/Shanghai", HealthRecord.recorded_at)) == day
        )
    )
    return set(rows.scalars())


async def create_missed_collection_order(
    session: AsyncSession, *, elder_id: uuid.UUID, day: datetime.date
) -> WorkOrderRef:
    """漏采工单（FR-HLT-08）：窗口结束无任何记录 → `collection_missed` 派负责护工。

    - severity 固定 `yellow`：漏采不是急症，判红会稀释真红（与漏服同口径）；
    - 幂等 token `collection_missed:<elder_id>:<day>`：同日重跑 tick 必撞同一
      确定性单号返回原单（created=False），不重复派单；
    - 指派人 = 责任护工（`primary_care`），没配则 None（「未分派」是真实状态，
      不做 fallback，与 work_order_service 口径一致）。
    """
    assignee = await resolve_primary_care_staff(session, elder_id=elder_id)
    ref = await open_work_order(
        session,
        elder_id=elder_id,
        order_type="collection_missed",
        severity="yellow",
        dedupe_token=f"collection_missed:{elder_id}:{day:%Y%m%d}",
        assignee_id=assignee,
    )
    logger.info(
        "missed_collection_order elder_id=%s day=%s order_no=%s created=%s assignee=%s",
        elder_id,
        day,
        ref.order_no,
        ref.created,
        assignee,
    )
    return ref


__all__ = [
    "RULE_VERSION",
    "SEVERITY_GREEN",
    "SEVERITY_RED",
    "SEVERITY_YELLOW",
    "ComponentGrade",
    "HealthGradeResult",
    "HealthRecordRef",
    "HealthRecordRow",
    "create_health_record",
    "create_missed_collection_order",
    "get_health_trend",
    "get_record_chain",
    "get_record_row",
    "grade_health",
    "list_active_elder_ids",
    "list_elder_ids_with_records_on",
    "list_pending_reviews",
    "review_health_record",
]
