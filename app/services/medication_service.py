"""用药事实的**唯一写库出口**（N10 的 service 侧）。

> 2026-11-09 更新：dev-B 的 `app/models/medication.py` 已合入，本文件从 `sa.table()`
> 临时映射**切到 ORM**（原 `TODO(E1)` 已偿清）。子图与调用方一行未动——这正是当初把
> 映射关在一个文件里的目的。

三条不变式（子图写不进去，只能在这里守）：

1. **幂等键**：`(plan_id, scheduled_at)` 唯一。同键**同内容**返回原 `reminder_id`；
   同键**异内容**抛 `MedicationConflict`（409 语义）——药单改版必须显式建新事件，
   否则「改了剂量但提醒还按老剂量发」没人能复盘。
2. **终态互斥**：`terminal_state` 五值只能定一次。已定终态再写不同值 → `TerminalStateConflict`；
   补确认走 `late_taken=True` 的追加语义，**不覆盖**原终态（AC-08）：原终态只允许从
   `missed_unconfirmed` / `notification_failed` 追加为 `late_taken_reported`，同事务写一条
   审计留原值（迁移 0020 / 契约 X8）。
3. **触达逐条留痕**：`(event_id, attempt_no)` 唯一，重复写同一次触达是**幂等无操作**，
   不是错误——scheduler 重试与图恢复都会重放这一步。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.medication import MedicationAttempt, MedicationEvent, MedicationPlan
from app.services.audit_log_service import record_audit

logger = logging.getLogger(__name__)

#: 终态五值（与 0020 的 CHECK 同源；第五值 `late_taken_reported` 是窗口外补确认，
#: 由 `set_terminal(late_taken=True)` 从「未确认 / 通知失败」追加，不覆盖原终态）
TERMINAL_STATES: tuple[str, ...] = (
    "taken_reported",
    "refused",
    "missed_unconfirmed",
    "notification_failed",
    "late_taken_reported",
)

#: 允许追加「补确认」的原终态（PRD line 320-325 / AC-08：只有这两种终态需要补报）
LATE_TAKEN_SOURCE_STATES: frozenset[str] = frozenset({"missed_unconfirmed", "notification_failed"})

#: 补确认写入的终态值（第五值）
LATE_TAKEN_STATE: str = "late_taken_reported"

#: 事件状态四值
EVENT_STATUSES: tuple[str, ...] = ("calling", "waiting_confirm", "confirmed", "escalated")

#: FR-UI-08 / AC-26：普通已服自报的撤销窗口（秒）
UNDO_WINDOW_SECONDS: int = 10

#: X12 允许的清理原因（契约 X12 枚举）
_PURGE_REASONS: frozenset[str] = frozenset({"checkout", "teaching_reset"})


class MedicationError(ValueError):
    """用药域异常基类。"""


class MedicationConflict(MedicationError):
    """同 `(plan_id, scheduled_at)` 已存在但内容不同（409 语义）。"""


class TerminalStateConflict(MedicationError):
    """终态已定，不允许改成另一个终态（补确认请用 `late_taken`）。"""


class MedicationEventNotFound(MedicationError):
    """事件不存在：说明建事件与推进图之间发生了回滚。"""


class MedicationUndoNotAllowed(MedicationError):
    """非「已服自报」终态不允许撤销（FR-UI-08：红色事件不能一键撤销）。"""


class MedicationUndoWindowExpired(MedicationError):
    """超过 10 秒撤销窗口（AC-26）。"""


def payload_hash(drug_snapshot: dict[str, Any]) -> str:
    """快照内容的稳定哈希（key 排序 + 紧凑分隔符）。"""
    canonical = json.dumps(drug_snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 写


async def create_event(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    elder_id: uuid.UUID,
    scheduled_at: datetime.datetime,
    drug_snapshot: dict[str, Any],
    triggered_by: str,
    plan_version: int = 1,
) -> uuid.UUID:
    """建（或取回）一条用药事件。幂等键 `(plan_id, scheduled_at, plan_version)`。

    plan_version 联动 medication_plans.version：改药单后 version+1，新事件自动带
    新版本号，不会与历史事件的 `(plan_id, scheduled_at)` 冲突——这是 G1 裁决落地
    的核心。见 0019_day8_medplan_version 迁移与 docs/plans/day08/dev-a-药单管理后端接口.md。
    """
    if triggered_by not in ("scheduler", "dialog"):
        raise MedicationError(f"unknown triggered_by: {triggered_by!r}")

    digest = payload_hash(drug_snapshot)
    existing = (
        (
            await session.execute(
                select(MedicationEvent.reminder_id, MedicationEvent.payload_hash).where(
                    MedicationEvent.plan_id == plan_id,
                    MedicationEvent.scheduled_at == scheduled_at,
                    MedicationEvent.plan_version == plan_version,
                )
            )
        )
        .mappings()
        .first()
    )

    if existing is not None:
        if existing.payload_hash != digest:
            raise MedicationConflict(
                "same (plan_id, scheduled_at, plan_version) already exists with a different drug snapshot"
            )
        # mappings() 取值仍是 Any，显式经 str 构造 UUID 而不是 cast——
        # 非法值会在这里炸响（宁可抛错也不要把脏数据静默往下传）
        return uuid.UUID(str(existing["reminder_id"]))

    reminder_id = uuid.uuid4()
    try:
        async with session.begin_nested():
            session.add(
                MedicationEvent(
                    reminder_id=reminder_id,
                    plan_id=plan_id,
                    elder_id=elder_id,
                    scheduled_at=scheduled_at,
                    plan_version=plan_version,
                    drug_snapshot=drug_snapshot,
                    payload_hash=digest,
                    triggered_by=triggered_by,
                    status="calling",
                    retry_count=0,
                    late_taken=False,
                )
            )
            await session.flush()
    except IntegrityError:
        # 并发建同一条：回查一次，按幂等处理（同内容返回、异内容冲突）
        row = (
            (
                await session.execute(
                    select(MedicationEvent.reminder_id, MedicationEvent.payload_hash).where(
                        MedicationEvent.plan_id == plan_id,
                        MedicationEvent.scheduled_at == scheduled_at,
                        MedicationEvent.plan_version == plan_version,
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise
        if row["payload_hash"] != digest:
            raise MedicationConflict("concurrent insert with different drug snapshot") from None
        return uuid.UUID(str(row["reminder_id"]))

    return reminder_id


async def record_attempt(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    attempt_no: int,
    receipt: str | None = None,
    tts_result: str | None = None,
    response: str | None = None,
    response_channel: str | None = None,
) -> None:
    """记一次触达。`(event_id, attempt_no)` 重复写是**幂等无操作**。"""
    if attempt_no < 1:
        raise MedicationError("attempt_no starts at 1")

    try:
        async with session.begin_nested():
            session.add(
                MedicationAttempt(
                    attempt_id=uuid.uuid4(),
                    event_id=event_id,
                    attempt_no=attempt_no,
                    receipt=receipt,
                    tts_result=tts_result,
                    response=response,
                    response_channel=response_channel,
                )
            )
            await session.flush()
    except IntegrityError:
        # 图恢复会重放「发送」这一步；重放不该多出一条触达记录。
        # 但 IntegrityError 不只有唯一键冲突——CHECK 违反也在其中。
        # 必须回查确认「真有同键记录」才算幂等；查不到说明是**新数据违规**，
        # 原样抛出。否则 CHECK 拒绝会被静默吞掉：触达记录凭空消失，
        # 日志还挂着误导性的 duplicate 名，排查时先去对时间戳才发现根本没插进去。
        duplicate = (
            await session.execute(
                select(MedicationAttempt.attempt_id).where(
                    MedicationAttempt.event_id == event_id,
                    MedicationAttempt.attempt_no == attempt_no,
                )
            )
        ).scalar_one_or_none()
        if duplicate is None:
            raise
        logger.info("medication_attempt_duplicate event_id=%s attempt_no=%s", event_id, attempt_no)


async def mark_attempt_played(
    session: AsyncSession, *, event_id: uuid.UUID, attempt_no: int
) -> bool:
    """触达语音「真被听到」的幂等落库（TD-2，receipt 白名单加值 `played`，迁移 0013）。

    只允许从投递态 `voice_dispatched` **升级**：`device_offline` / `no_bound_device`
    的触达音频根本没出过门，不能被回执「洗白」（宁缺勿假）；已 `played` 的行再收到
    重复/迟到回执是无操作。返回是否真的发生了升级（False = 幂等无操作，或行不存在、
    状态不符——调用方只记日志，不报错）。

    played 只做留痕闭环，**不参与**重试与终态判定（TD-2 明确不做该消费）。
    """
    if attempt_no < 1:
        raise MedicationError("attempt_no starts at 1")
    result = await session.execute(
        update(MedicationAttempt)
        .where(
            MedicationAttempt.event_id == event_id,
            MedicationAttempt.attempt_no == attempt_no,
            MedicationAttempt.receipt == "voice_dispatched",
        )
        .values(receipt="played")
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def get_attempt(
    session: AsyncSession, *, event_id: uuid.UUID, attempt_no: int
) -> dict[str, Any] | None:
    """查某一次触达是否**已经发生过**（重放判定用）。

    为什么需要它：图恢复会重放 `remind` 节点，而 `record_attempt` 的幂等只挡住
    **记录**、挡不住**投递**——不先问就会给长者再播一遍同一次提醒。
    返回 None 表示这次触达还没发生过（正常首触达）；返回 dict 表示是重放。
    """
    row = (
        (
            await session.execute(
                select(MedicationAttempt.attempt_no, MedicationAttempt.receipt).where(
                    MedicationAttempt.event_id == event_id,
                    MedicationAttempt.attempt_no == attempt_no,
                )
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


async def record_response(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    attempt_no: int,
    response: str,
    channel: str = "voice",
) -> None:
    """把长者的回答摘要补写回**那一次**触达记录（不改终态，终态由 `set_terminal` 写）。

    为什么是 UPDATE 而不是再插一条：一次触达的「发了什么、回了什么」是同一件事的两面，
    拆成两行会让「第 3 次提醒到底有没有回」需要 JOIN 才能回答。
    """
    await session.execute(
        update(MedicationAttempt)
        .where(
            MedicationAttempt.event_id == event_id,
            MedicationAttempt.attempt_no == attempt_no,
        )
        .values(response=response[:200], response_channel=channel)
    )


async def set_waiting_confirm(
    session: AsyncSession, *, event_id: uuid.UUID, graph_thread_id: str | None = None
) -> None:
    """把事件推进到「等确认」，并回指图状态（便于 `graph.get_state` 复盘）。"""
    values: dict[str, Any] = {"status": "waiting_confirm"}
    if graph_thread_id is not None:
        values["graph_thread_id"] = graph_thread_id
    await _update_event(session, event_id, values)


async def set_terminal(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    terminal_state: str,
    failure_reason: str | None = None,
    retry_count: int | None = None,
    confirm_channel: str | None = None,
    late_taken: bool = False,
) -> None:
    """写终态。已定终态再写不同值 → `TerminalStateConflict`（**不覆盖**）。

    **补确认例外**（PRD line 320-325 / AC-08 / 迁移 0020 / 契约 X8，主理人裁决 D8）：
    `late_taken=True` 且 `terminal_state="late_taken_reported"`、原终态 ∈
    {`missed_unconfirmed`, `notification_failed`} 时，允许把终态**追加**为第五值，
    并**同事务**写一条 `audit_logs`（action=`medication_late_taken_reported`，
    detail 留原终态）。工单不在这里关闭——核实归护工（AC-08）。
    重复补报同值幂等：不再写第二条审计。

    其他「不同终态」的组合仍是 `TerminalStateConflict`，既有互斥语义不变。
    """
    if terminal_state not in TERMINAL_STATES:
        raise MedicationError(f"unknown terminal_state: {terminal_state!r}")

    row = (
        await session.execute(
            select(MedicationEvent.terminal_state, MedicationEvent.status).where(
                MedicationEvent.reminder_id == event_id
            )
        )
    ).first()
    if row is None:
        raise MedicationEventNotFound(f"medication event not found: {event_id}")

    previous_terminal = row.terminal_state
    is_late_report = (
        late_taken
        and terminal_state == LATE_TAKEN_STATE
        and previous_terminal in LATE_TAKEN_SOURCE_STATES
    )

    if not is_late_report and previous_terminal is not None and previous_terminal != terminal_state:
        # AC-08：原终态不可被覆盖；补确认走 late_taken 的追加语义（见 docstring）
        raise TerminalStateConflict(
            f"event {event_id} already terminal={row.terminal_state!r}, "
            f"refuse to overwrite with {terminal_state!r}"
        )

    values: dict[str, Any] = {
        "terminal_state": terminal_state,
        "status": "confirmed" if terminal_state == "taken_reported" else "escalated",
        "late_taken": late_taken,
    }
    if failure_reason is not None:
        values["failure_reason"] = failure_reason
    if retry_count is not None:
        values["retry_count"] = retry_count
    if confirm_channel is not None:
        values["confirm_channel"] = confirm_channel
        values["confirmed_at"] = datetime.datetime.now(datetime.UTC)

    await _update_event(session, event_id, values)

    if is_late_report and previous_terminal != terminal_state:
        # 只在实际发生「追加」时留痕；重复补报同值走上面的幂等路径，不再多写审计
        await record_audit(
            session,
            actor_type="system",
            actor_id=None,
            action="medication_late_taken_reported",
            resource_type="medication_event",
            resource_id=event_id,
            detail={
                "previous_terminal_state": previous_terminal,
                "terminal_state": terminal_state,
                "late_taken": True,
                "confirm_channel": confirm_channel,
            },
        )


async def revoke_taken_report(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    reminder_id: uuid.UUID,
    now: datetime.datetime | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """FR-UI-08 / AC-26：普通「已服自报」10 秒内撤销，恢复为待确认。

    三条边界：

    1. **仅 `terminal_state='taken_reported'`** 可撤销；其他终态或未定终态 →
       `MedicationUndoNotAllowed`（红色事件只能追加「说错了」，不能撤销）。
    2. **10 秒窗口**（`UNDO_WINDOW_SECONDS`）→ 超时 `MedicationUndoWindowExpired`。
    3. **归属**：事件不属于该长者一律按「不存在」处理（`MedicationEventNotFound`），
       防止用他人 reminder_id 探测。

    撤销只回滚业务状态，**不删触达历史**（medication_attempts 只增不改）；
    同事务写审计 `medication_taken_report_revoked`，不 commit（事务归调用方）。
    """
    row = (
        await session.execute(
            select(
                MedicationEvent.elder_id,
                MedicationEvent.terminal_state,
                MedicationEvent.confirmed_at,
            ).where(MedicationEvent.reminder_id == reminder_id)
        )
    ).first()
    if row is None or row.elder_id != elder_id:
        raise MedicationEventNotFound(f"medication event not found: {reminder_id}")

    if row.terminal_state != "taken_reported" or row.confirmed_at is None:
        raise MedicationUndoNotAllowed("仅「已服自报」可撤销，红色事件请追加说明")

    current = now or datetime.datetime.now(datetime.UTC)
    confirmed_at = row.confirmed_at
    if confirmed_at.tzinfo is None:  # 兼容无时区读值，统一按 UTC 解读
        confirmed_at = confirmed_at.replace(tzinfo=datetime.UTC)
    elapsed = current - confirmed_at
    if elapsed > datetime.timedelta(seconds=UNDO_WINDOW_SECONDS):
        raise MedicationUndoWindowExpired(f"撤销窗口已过（{UNDO_WINDOW_SECONDS} 秒）")

    await _update_event(
        session,
        reminder_id,
        {
            "status": "waiting_confirm",
            "terminal_state": None,
            "late_taken": False,
            "confirmed_at": None,
            "confirm_channel": None,
        },
    )
    await record_audit(
        session,
        actor_type="system",
        actor_id=None,
        action="medication_taken_report_revoked",
        resource_type="medication_event",
        resource_id=reminder_id,
        detail={
            "elder_id": str(elder_id),
            "window_s": UNDO_WINDOW_SECONDS,
            "elapsed_ms": int(elapsed.total_seconds() * 1000),
            "device_id": device_id,
        },
    )
    return {
        "reminder_id": reminder_id,
        "status": "waiting_confirm",
        "terminal_state": None,
    }


async def _update_event(session: AsyncSession, event_id: uuid.UUID, values: dict[str, Any]) -> None:
    result = await session.execute(
        update(MedicationEvent).where(MedicationEvent.reminder_id == event_id).values(**values)
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise MedicationEventNotFound(f"medication event not found: {event_id}")


# ------------------------------------------------------------------ 读（复盘与测试用）


async def get_drug_name(session: AsyncSession, event_id: uuid.UUID) -> str | None:
    """取该提醒的药名（drug_snapshot 里的人读字段），话术用；无快照返回 None。"""
    row = (
        await session.execute(
            select(MedicationEvent.drug_snapshot).where(MedicationEvent.reminder_id == event_id)
        )
    ).scalar_one_or_none()
    if not isinstance(row, dict):
        return None
    name = row.get("drug_name")
    return str(name) if name else None


async def get_medication_card_snapshot(
    session: AsyncSession, event_id: uuid.UUID
) -> dict[str, str | int] | None:
    """pad 用药卡片快照（Day 7 L6#9 联调裁决）。

    只取 drug_snapshot 里**实际存在**的字段（不造 usage 之类没有的事实——
    快照无 usage 则前端显示为空），scheduled_at 转 Asia/Shanghai 展示口径。
    字段名与前端 MedicationSnapshot（camelCase）对齐，序列化后直接下发。
    """
    row = (
        await session.execute(
            select(MedicationEvent.scheduled_at, MedicationEvent.drug_snapshot).where(
                MedicationEvent.reminder_id == event_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    scheduled_at, drug_snapshot = row
    snapshot = drug_snapshot if isinstance(drug_snapshot, dict) else {}
    drug_name = str(snapshot.get("drug_name") or "")
    if not drug_name:
        return None
    local = scheduled_at.astimezone(ZoneInfo("Asia/Shanghai"))
    return {
        "drugName": drug_name,
        "dose": str(snapshot.get("dose") or ""),
        "scheduledAt": local.strftime("%H:%M"),
        "usage": str(snapshot.get("usage") or ""),
    }


async def list_bound_device_ids(session: AsyncSession, *, elder_id: uuid.UUID) -> list[uuid.UUID]:
    """长者绑定的设备 id（`devices.elder_id`）。

    可能多条（换新设备后旧档案未删）——**逐台下发**，任一台投进队列即算触达成功；
    这与「长者在哪台设备前不可预知」的现实一致，也避免误删旧设备导致提醒静默丢失。
    未绑定返回空列表：触达会如实记 `no_bound_device`，不抛错——
    设备没绑定是运维缺口，不是本轮该失败的原因。
    """
    from app.models.organization import Device

    rows = await session.execute(select(Device.device_id).where(Device.elder_id == elder_id))
    return [row[0] for row in rows.all()]


async def get_event(session: AsyncSession, event_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                select(
                    MedicationEvent.reminder_id,
                    MedicationEvent.plan_id,
                    MedicationEvent.elder_id,
                    MedicationEvent.status,
                    MedicationEvent.terminal_state,
                    MedicationEvent.retry_count,
                    MedicationEvent.late_taken,
                    MedicationEvent.graph_thread_id,
                ).where(MedicationEvent.reminder_id == event_id)
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


async def list_attempts(session: AsyncSession, event_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                MedicationAttempt.attempt_no,
                MedicationAttempt.receipt,
                MedicationAttempt.response,
                MedicationAttempt.response_channel,
            )
            .where(MedicationAttempt.event_id == event_id)
            .order_by(MedicationAttempt.attempt_no)
        )
    ).mappings()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ X12 清理钩子（退房 · 教学重置）


@dataclass(frozen=True, slots=True)
class PurgeRef:
    """清理结果（契约 X12：`affected_rows` / `archived_rows` / `domain`）。

    ⚠️ 契约 X4（记忆域）另给了一套 `deleted_chunks/deleted_vectors/audit_id` 字段。
    两处各自定义同名字面量：**不跨 service 横向 import**（AGENTS §2.1 单向依赖），
    A3 的流水线按属性名读取即可。`audit_id` 恒为 None —— 审计由 A 统一写（X12 明文）。
    """

    domain: str
    affected_rows: int
    archived_rows: int = 0
    deleted_chunks: int = 0
    deleted_vectors: int = 0
    audit_id: uuid.UUID | None = None


async def purge_elder_data(session: AsyncSession, *, elder_id: uuid.UUID, reason: str) -> PurgeRef:
    """**X12**：清空该长者的用药域数据（`attempts → events → plans` 顺序，FK 决定）。

    - `reason ∈ {checkout, teaching_reset}`，其余取值直接拒绝；
    - **幂等**：第二次调用命中 0 行，返回全 0（各步独立可重入，契约 X12 顺序口径）；
    - **不写审计**：由 A 的 `lifecycle_service` 逐步统一写（B 不重复写）；
    - 事务归调用方（只 `execute`，不 commit）；
    - `archived_rows=0`：用药域为物理清理，无脱敏归档（与健康域口径不同）。
    """
    if reason not in _PURGE_REASONS:
        raise MedicationError(f"invalid purge reason {reason!r}; allowed={sorted(_PURGE_REASONS)}")

    event_ids = select(MedicationEvent.reminder_id).where(MedicationEvent.elder_id == elder_id)

    attempts = await session.execute(
        delete(MedicationAttempt).where(MedicationAttempt.event_id.in_(event_ids))
    )
    events = await session.execute(
        delete(MedicationEvent).where(MedicationEvent.elder_id == elder_id)
    )
    plans = await session.execute(delete(MedicationPlan).where(MedicationPlan.elder_id == elder_id))

    affected = sum(int(r.rowcount or 0) for r in (attempts, events, plans))  # type: ignore[attr-defined]
    logger.info(
        "medication_purged",
        extra={
            "elder_id": str(elder_id),
            "reason": reason,
            "attempts": int(attempts.rowcount or 0),  # type: ignore[attr-defined]
            "events": int(events.rowcount or 0),  # type: ignore[attr-defined]
            "plans": int(plans.rowcount or 0),  # type: ignore[attr-defined]
        },
    )
    return PurgeRef(domain="medication", affected_rows=affected, archived_rows=0)


__all__ = [
    "EVENT_STATUSES",
    "LATE_TAKEN_SOURCE_STATES",
    "LATE_TAKEN_STATE",
    "TERMINAL_STATES",
    "UNDO_WINDOW_SECONDS",
    "MedicationConflict",
    "MedicationError",
    "MedicationEventNotFound",
    "MedicationUndoNotAllowed",
    "MedicationUndoWindowExpired",
    "PurgeRef",
    "TerminalStateConflict",
    "create_event",
    "get_attempt",
    "get_drug_name",
    "get_event",
    "list_attempts",
    "list_bound_device_ids",
    "mark_attempt_played",
    "payload_hash",
    "purge_elder_data",
    "record_attempt",
    "record_response",
    "revoke_taken_report",
    "set_terminal",
    "set_waiting_confirm",
]
