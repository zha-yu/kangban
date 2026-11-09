"""用药事实的**唯一写库出口**（N10 的 service 侧）。

> 2026-11-09 更新：dev-B 的 `app/models/medication.py` 已合入，本文件从 `sa.table()`
> 临时映射**切到 ORM**（原 `TODO(E1)` 已偿清）。子图与调用方一行未动——这正是当初把
> 映射关在一个文件里的目的。

三条不变式（子图写不进去，只能在这里守）：

1. **幂等键**：`(plan_id, scheduled_at)` 唯一。同键**同内容**返回原 `reminder_id`；
   同键**异内容**抛 `MedicationConflict`（409 语义）——药单改版必须显式建新事件，
   否则「改了剂量但提醒还按老剂量发」没人能复盘。
2. **终态互斥**：`terminal_state` 四值只能定一次。已定终态再写不同值 → `TerminalStateConflict`；
   补确认走 `late_taken=True` 的追加语义，**不覆盖**原终态（AC-08）。
3. **触达逐条留痕**：`(event_id, attempt_no)` 唯一，重复写同一次触达是**幂等无操作**，
   不是错误——scheduler 重试与图恢复都会重放这一步。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.medication import MedicationAttempt, MedicationEvent

logger = logging.getLogger(__name__)

#: 终态四值（与 0010 的 CHECK 同源）
TERMINAL_STATES: tuple[str, ...] = (
    "taken_reported",
    "refused",
    "missed_unconfirmed",
    "notification_failed",
)

#: 事件状态四值
EVENT_STATUSES: tuple[str, ...] = ("calling", "waiting_confirm", "confirmed", "escalated")


class MedicationError(ValueError):
    """用药域异常基类。"""


class MedicationConflict(MedicationError):
    """同 `(plan_id, scheduled_at)` 已存在但内容不同（409 语义）。"""


class TerminalStateConflict(MedicationError):
    """终态已定，不允许改成另一个终态（补确认请用 `late_taken`）。"""


class MedicationEventNotFound(MedicationError):
    """事件不存在：说明建事件与推进图之间发生了回滚。"""


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
) -> uuid.UUID:
    """建（或取回）一条用药事件。幂等键 `(plan_id, scheduled_at)`。"""
    if triggered_by not in ("scheduler", "dialog"):
        raise MedicationError(f"unknown triggered_by: {triggered_by!r}")

    digest = payload_hash(drug_snapshot)
    existing = (
        (
            await session.execute(
                select(MedicationEvent.reminder_id, MedicationEvent.payload_hash).where(
                    MedicationEvent.plan_id == plan_id,
                    MedicationEvent.scheduled_at == scheduled_at,
                )
            )
        )
        .mappings()
        .first()
    )

    if existing is not None:
        if existing.payload_hash != digest:
            raise MedicationConflict(
                "same (plan_id, scheduled_at) already exists with a different drug snapshot"
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
    """写终态。已定终态再写不同值 → `TerminalStateConflict`（**不覆盖**）。"""
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

    if row.terminal_state is not None and row.terminal_state != terminal_state:
        # AC-08：原终态不可被覆盖；补确认走 late_taken 的追加语义
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


__all__ = [
    "EVENT_STATUSES",
    "TERMINAL_STATES",
    "MedicationConflict",
    "MedicationError",
    "MedicationEventNotFound",
    "TerminalStateConflict",
    "create_event",
    "get_drug_name",
    "get_event",
    "list_attempts",
    "list_bound_device_ids",
    "payload_hash",
    "record_attempt",
    "record_response",
    "set_terminal",
    "set_waiting_confirm",
]
