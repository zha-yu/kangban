"""用药事实的**唯一写库出口**（N10 的 service 侧）。

> **TODO(E1)**：`medication_*` 六张表的 ORM（`app/models/medication.py`）归 dev-B，
> 本文件在它落地前用 `sa.table()` 临时映射顶着。B 交付后**本文件是唯一需要改的地方**：
> 把 `_events` / `_attempts` 换成 `from app.models.medication import MedicationEvent, ...`，
> 调用方（子图）一行不动。
> 为什么不自建 ORM 类：那样会出现两份 `medication_events` 定义，谁先 import 谁生效，
> 且 `Base.metadata` 里多一张同名表，`alembic check` 会开始报「重复表」。
> **签字**：2026-11-09 主理人确认接受该过渡方案（`docs/plans/day06/dev-a-DAG.md` 回退条款）。

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

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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

_UUID = postgresql.UUID(as_uuid=True)

#: TODO(E1)：B 的 `app/models/medication.py` 到位后删除以下两个临时映射
_events = sa.table(
    "medication_events",
    sa.column("reminder_id", _UUID),
    sa.column("plan_id", _UUID),
    sa.column("elder_id", _UUID),
    sa.column("scheduled_at", sa.DateTime(timezone=True)),
    sa.column("drug_snapshot", postgresql.JSONB()),
    sa.column("payload_hash", sa.Text()),
    sa.column("triggered_by", sa.Text()),
    sa.column("status", sa.Text()),
    sa.column("terminal_state", sa.Text()),
    sa.column("late_taken", sa.Boolean()),
    sa.column("failure_reason", sa.Text()),
    sa.column("retry_count", sa.Integer()),
    sa.column("confirmed_at", sa.DateTime(timezone=True)),
    sa.column("confirm_channel", sa.Text()),
    sa.column("graph_thread_id", sa.Text()),
)

_attempts = sa.table(
    "medication_attempts",
    sa.column("attempt_id", _UUID),
    sa.column("event_id", _UUID),
    sa.column("attempt_no", sa.Integer()),
    sa.column("sent_at", sa.DateTime(timezone=True)),
    sa.column("receipt", sa.Text()),
    sa.column("tts_result", sa.Text()),
    sa.column("response", sa.Text()),
    sa.column("response_channel", sa.Text()),
)


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
        await session.execute(
            sa.select(_events.c.reminder_id, _events.c.payload_hash).where(
                _events.c.plan_id == plan_id,
                _events.c.scheduled_at == scheduled_at,
            )
        )
    ).first()

    if existing is not None:
        if existing.payload_hash != digest:
            raise MedicationConflict(
                "same (plan_id, scheduled_at) already exists with a different drug snapshot"
            )
        return existing.reminder_id

    reminder_id = uuid.uuid4()
    try:
        async with session.begin_nested():
            await session.execute(
                sa.insert(_events).values(
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
    except IntegrityError:
        # 并发建同一条：回查一次，按幂等处理（同内容返回、异内容冲突）
        row = (
            await session.execute(
                sa.select(_events.c.reminder_id, _events.c.payload_hash).where(
                    _events.c.plan_id == plan_id,
                    _events.c.scheduled_at == scheduled_at,
                )
            )
        ).first()
        if row is None:
            raise
        if row.payload_hash != digest:
            raise MedicationConflict("concurrent insert with different drug snapshot") from None
        return row.reminder_id

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
            await session.execute(
                sa.insert(_attempts).values(
                    attempt_id=uuid.uuid4(),
                    event_id=event_id,
                    attempt_no=attempt_no,
                    receipt=receipt,
                    tts_result=tts_result,
                    response=response,
                    response_channel=response_channel,
                )
            )
    except IntegrityError:
        # 图恢复会重放「发送」这一步；重放不该多出一条触达记录
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
        sa.update(_attempts)
        .where(_attempts.c.event_id == event_id, _attempts.c.attempt_no == attempt_no)
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
            sa.select(_events.c.terminal_state, _events.c.status).where(
                _events.c.reminder_id == event_id
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
        sa.update(_events).where(_events.c.reminder_id == event_id).values(**values)
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise MedicationEventNotFound(f"medication event not found: {event_id}")


# ------------------------------------------------------------------ 读（复盘与测试用）


async def get_event(session: AsyncSession, event_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                sa.select(
                    _events.c.reminder_id,
                    _events.c.plan_id,
                    _events.c.elder_id,
                    _events.c.status,
                    _events.c.terminal_state,
                    _events.c.retry_count,
                    _events.c.late_taken,
                    _events.c.graph_thread_id,
                ).where(_events.c.reminder_id == event_id)
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


async def list_attempts(session: AsyncSession, event_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            sa.select(
                _attempts.c.attempt_no,
                _attempts.c.receipt,
                _attempts.c.response,
                _attempts.c.response_channel,
            )
            .where(_attempts.c.event_id == event_id)
            .order_by(_attempts.c.attempt_no)
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
    "get_event",
    "list_attempts",
    "payload_hash",
    "record_attempt",
    "record_response",
    "set_terminal",
    "set_waiting_confirm",
]
