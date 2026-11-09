"""事件账本与轮次投影的**唯一写库入口**（N04 / 契约 §5.1）。

三件事只在这里做，别处不要重复实现：

1. **`seq` 分配**：同事务内 `SELECT last_event_seq … FOR UPDATE` → +1 → 写回 → 插事件。
   **禁止** `SELECT max(seq)+1`：两个并发写会算出同一个号，轻则撞唯一索引，重则重排顺序，
   而事件流水一旦乱序，复盘「先说了什么、后改了什么口」就失去依据。
2. **只追加**：本模块不提供 update/delete 事件的函数。更正走新事件 + `ref_event_id`。
   想「顺手修一下旧事件」的调用方，请先读 `alembic/versions/0008_*.py` 的头注释。
3. **已送达语义**：`conversation_turns.assistant_delivered_text` 只接受**实际播出去**的文本；
   生成全文写进 `response_completed` 事件的 `payload_json`。两者混写，模型会以为自己说过
   被打断时其实没播的那半句，之后就会「顺着自己没说过的话」往下聊。

单写口约定（避免两个函数抢同一条状态）：

- `conversation_turns.status` 的终态**只由 `finish_turn` 写**；`commit_response` 收到 `status`
  只记进事件 payload。否则「先 completed 后 interrupted」时，终态会被迟到的 completed 覆盖。
- `assistant_delivered_text` 只由 `commit_response` 写。

generic 模式（`elder_id is None`，身份未确认，FR-VPR-03）：只写事件，不建轮次投影；
`read_turns` 返回空列表。这样既不会把「没确认是谁说的话」混进某个长者的上下文，
也不会因为没建投影就丢掉这一轮的事实（`user_text` 在 `turn_started` 的 payload 里）。

事务边界：本模块只 `flush()`，**不 commit**。提交由调用方（请求依赖 / turn_service）决定，
这样「历史写入 + 业务写入」才能落在同一个事务里，要么都成、要么都不成。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_trace_id
from app.core.security import canonical_uuid
from app.models.conversation import (
    EVENT_TYPES,
    ConversationEvent,
    ConversationThread,
    ConversationTurn,
)

logger = logging.getLogger(__name__)

#: 轮次终态三值（`open` 不是终态，`finish_turn` 不接受）
TERMINAL_TURN_STATUSES: tuple[str, ...] = ("completed", "interrupted", "failed")

TurnStatus = Literal["open", "completed", "interrupted", "failed"]
FinishStatus = Literal["completed", "interrupted", "failed"]


# ------------------------------------------------------------------ 异常


class HistoryError(ValueError):
    """历史域异常基类（属调用方写错，不是「数据不存在」那一类）。"""


class ThreadNotFound(HistoryError):
    """会话不存在：`thread_id` 未建或已被清理。

    调用方应当先走 `identity_service` 建会话；拿到本异常说明**跳过了建会话**，
    不是「查不到就新建」的信号。
    """


class ThreadMismatch(HistoryError):
    """传入的 `elder_id` / `device_id` 与 `conversation_threads` 上的不一致。

    这是闸门①的纵深防御：即使上游漏判，历史层也不会把 A 长者的对话写进 B 长者的会话。
    """


class InvalidEventType(HistoryError):
    """事件类型不在 12 值枚举内（写进去的库 CHECK 也会拒绝，但错误信息远不如这里清楚）。"""


# ------------------------------------------------------------------ 视图对象


@dataclass(frozen=True, slots=True)
class TurnRef:
    """`begin_turn` 的返回值。

    `seq` 为 `None` 表示**重复调用**（同 `turn_id` 已存在），此时没有新事件产生——
    给一个假序号会让人误以为「又写了一轮」。
    """

    turn_id: str
    seq: int | None
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class TurnView:
    """一轮对话的投影（只含**已送达**文本）。"""

    turn_id: str
    user_text: str
    assistant_delivered_text: str | None
    status: TurnStatus
    started_at: datetime.datetime
    ended_at: datetime.datetime | None


@dataclass(frozen=True, slots=True)
class EventView:
    """一条事件（只读视图；`payload` 已展开）。"""

    seq: int
    event_type: str
    occurred_at: datetime.datetime
    payload: dict[str, Any] = field(default_factory=dict)
    turn_id: str | None = None
    response_id: str | None = None
    task_id: str | None = None


# ------------------------------------------------------------------ 内部工具


def _as_text(value: uuid.UUID | str | None) -> str | None:
    """UUID / 文本统一成规范小写文本；非法值直接报错，不做「猜一个」的兜底。"""
    if value is None:
        return None
    return canonical_uuid(value)


async def _load_thread(
    session: AsyncSession, thread_id: str, *, require_elder: str | None = None
) -> ConversationThread:
    """取会话行并校验归属；不存在抛 `ThreadNotFound`，归属不符抛 `ThreadMismatch`。

    `require_elder` 传 `None` 有两种含义，调用方必须自己想清楚是哪一种：
    - 「这一轮是 generic，检查会话也必须是 generic」→ 用 `check_elder=True` 的严格版
      （见 `begin_turn`，那里显式比对两边是否同时为 None）。
    - 「不关心归属」→ 别传这个参数。
    """
    row = (
        await session.execute(
            select(ConversationThread).where(ConversationThread.thread_id == thread_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ThreadNotFound(f"conversation thread not found: {thread_id!r}")
    return row


def _validate_event_type(event_type: str) -> None:
    if event_type not in EVENT_TYPES:
        raise InvalidEventType(
            f"unknown event_type {event_type!r}; expected one of {list(EVENT_TYPES)}"
        )


# ------------------------------------------------------------------ 写：轮次开始


async def begin_turn(
    session: AsyncSession,
    *,
    thread_id: str,
    elder_id: uuid.UUID | str | None,
    device_id: uuid.UUID | str | None = None,
    call_id: str | None = None,
    turn_id: str,
    user_text: str,
    trace_id: str | None = None,
    identity_source: str | None = None,
) -> TurnRef:
    """开一轮：建轮次投影（仅已识别身份）+ 写 `turn_started` 事件。

    幂等：同 `turn_id` 二次调用返回 `TurnRef(duplicate=True)`，**不新增行、不新写事件**。
    为什么必须幂等：ASR final 可能因重连重发，重复处理会让同一句话在上下文里出现两次。

    `user_text` 必须是 **ASR final**；空串/纯空白直接拒绝（partial 由调用方拦截，
    本函数不接受 partial——一旦接受，投影里就会出现「说到一半的句子」）。
    """
    if not turn_id:
        raise HistoryError("turn_id is required")
    if not user_text or not user_text.strip():
        raise HistoryError("user_text must be a non-empty ASR final text")

    elder = _as_text(elder_id)
    device = _as_text(device_id)

    thread = await _load_thread(session, thread_id)

    # 闸门①的纵深防御：会话上记录的归属，必须与本次调用一致
    if _as_text(thread.elder_id) != elder:
        raise ThreadMismatch(
            f"elder_id mismatch on thread {thread_id!r}: thread={thread.elder_id!r}, call={elder!r}"
        )
    if device is not None and _as_text(thread.device_id) != device:
        raise ThreadMismatch(
            f"device_id mismatch on thread {thread_id!r}: "
            f"thread={thread.device_id!r}, call={device!r}"
        )

    # 已识别身份才建投影；generic 只留事件
    if elder is not None:
        existing = await session.get(ConversationTurn, turn_id)
        if existing is not None:
            logger.info("history_turn_duplicate turn_id=%s thread_id=%s", turn_id, thread_id)
            return TurnRef(turn_id=existing.turn_id, seq=None, duplicate=True)
        session.add(
            ConversationTurn(
                turn_id=turn_id,
                thread_id=thread_id,
                elder_id=uuid.UUID(elder),
                call_id=call_id,
                user_text=user_text,
                status="open",
            )
        )
        await session.flush()

    seq = await append_event(
        session,
        thread_id=thread_id,
        event_type="turn_started",
        payload={
            "user_text": user_text,
            "identity_source": identity_source or thread.identity_source,
        },
        turn_id=turn_id,
        call_id=call_id,
        trace_id=trace_id,
    )
    return TurnRef(turn_id=turn_id, seq=seq)


# ------------------------------------------------------------------ 写：事件追加


async def append_event(
    session: AsyncSession,
    *,
    thread_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    turn_id: str | None = None,
    response_id: str | None = None,
    task_id: str | None = None,
    event_id: uuid.UUID | None = None,
    ref_event_id: uuid.UUID | None = None,
    call_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    """追加一条事件，返回它在**本会话内**的 `seq`。

    `call_id` / `trace_id` 是契约 v1.1 新增的**可选**参数（向后兼容）：两列在 0008 里已存在，
    不传就落空会让「一次建连 / 一条链路」的排查断线。`trace_id` 缺省时取当前请求上下文。
    """
    _validate_event_type(event_type)

    # 行锁分配 seq：锁住会话行 → 读计数 → +1 → 写回 → 插事件，全程同一事务
    current = (
        await session.execute(
            select(ConversationThread.last_event_seq)
            .where(ConversationThread.thread_id == thread_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if current is None:
        raise ThreadNotFound(f"conversation thread not found: {thread_id!r}")

    next_seq = int(current) + 1
    await session.execute(
        update(ConversationThread)
        .where(ConversationThread.thread_id == thread_id)
        .values(last_event_seq=next_seq)
    )

    row = ConversationEvent(
        thread_id=thread_id,
        seq=next_seq,
        event_type=event_type,
        payload_json=payload or {},
        call_id=call_id,
        turn_id=turn_id,
        response_id=response_id,
        task_id=task_id,
        trace_id=trace_id or get_trace_id(),
        ref_event_id=ref_event_id,
    )
    if event_id is not None:
        row.event_id = event_id
    session.add(row)
    await session.flush()
    return next_seq


# ------------------------------------------------------------------ 写：回复提交与轮次收口


async def commit_response(
    session: AsyncSession,
    *,
    turn_id: str,
    response_id: str,
    generated_text: str,
    delivered_text: str | None,
    status: str,
) -> None:
    """提交一次回复：写已送达投影 + 记 `response_completed` 事件（同 `response_id` 幂等）。

    - `generated_text` 进事件 payload（审计用）；`delivered_text` 进投影（上下文用）。
    - `status` 只进 payload，**不改** `conversation_turns.status`：轮次终态统一由
      `finish_turn` 写，两个写口会让迟到的 completed 覆盖已发生的 interrupted。
    - 幂等判定用「该 `(turn_id, response_id)` 是否已有 `response_completed` 事件」，
      不另建幂等表——事件流水本身就是事实源。
    """
    if not response_id:
        raise HistoryError("response_id is required")

    # `completed` 却拿不到已送达文本，几乎一定是末段 ack 丢了（`<audio>.ended` 未到）。
    # 历史层不替调用方猜内容——猜错会把没播的话写进上下文；但必须留下可检索的痕迹，
    # 否则这条回复在复盘时就是"明明播完了，历史里一个字都没有"。
    if status == "completed" and not delivered_text:
        logger.warning(
            "history_completed_without_delivered turn_id=%s response_id=%s", turn_id, response_id
        )

    turn = await session.get(ConversationTurn, turn_id)
    if turn is None:
        raise HistoryError(
            f"conversation turn not found: {turn_id!r}（generic 模式不建投影，改用 append_event）"
        )

    already = (
        await session.execute(
            select(ConversationEvent.event_id)
            .where(
                ConversationEvent.turn_id == turn_id,
                ConversationEvent.response_id == response_id,
                ConversationEvent.event_type == "response_completed",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if already is not None:
        logger.info("history_response_duplicate turn_id=%s response_id=%s", turn_id, response_id)
        return

    # 防御性：正常路径上第二次调用已在上面幂等返回，走不到这里；但若事件被清理/迁移过，
    # 也不允许用更短的文本覆盖更长的已送达内容。
    if delivered_text is not None and (
        turn.assistant_delivered_text is None
        or len(delivered_text) >= len(turn.assistant_delivered_text)
    ):
        turn.assistant_delivered_text = delivered_text

    await append_event(
        session,
        thread_id=turn.thread_id,
        event_type="response_completed",
        payload={
            "generated_text": generated_text,
            "delivered_text": delivered_text,
            "response_status": status,
        },
        turn_id=turn_id,
        response_id=response_id,
    )


async def finish_turn(session: AsyncSession, *, turn_id: str, status: FinishStatus) -> None:
    """收口一轮：写终态与 `ended_at` + 记 `turn_completed` 事件。

    幂等且**不覆盖**：已是终态则直接返回（重连后又收到一次"结束"很常见）。
    generic 模式没有投影行：只补一条 `turn_completed` 事件，不报错——
    报错会逼调用方在 generic 分支里写特例，特例一多就会漏。
    """
    if status not in TERMINAL_TURN_STATUSES:
        raise HistoryError(
            f"finish_turn status must be one of {list(TERMINAL_TURN_STATUSES)}, got {status!r}"
        )

    turn = await session.get(ConversationTurn, turn_id)
    if turn is None:
        # generic 模式没有投影行，但轮次仍必须闭合：用 turn_id 索引回查它属于哪个会话。
        # 直接 return 会让通用模式的每一轮都停在 turn_started，重连补帧时看起来「永远没结束」。
        thread_id = (
            await session.execute(
                select(ConversationEvent.thread_id)
                .where(ConversationEvent.turn_id == turn_id)
                .order_by(ConversationEvent.seq.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if thread_id is None:
            logger.info("history_turn_missing_for_finish turn_id=%s status=%s", turn_id, status)
            return
        await append_event(
            session,
            thread_id=thread_id,
            event_type="turn_completed",
            payload={"turn_id": turn_id, "status": status, "projection": "none"},
            turn_id=turn_id,
        )
        return

    if turn.status != "open":
        logger.info("history_turn_already_finished turn_id=%s status=%s", turn_id, turn.status)
        return

    turn.status = status
    turn.ended_at = datetime.datetime.now(datetime.UTC)

    await append_event(
        session,
        thread_id=turn.thread_id,
        event_type="turn_completed",
        payload={"turn_id": turn_id, "status": status},
        turn_id=turn_id,
    )


# ------------------------------------------------------------------ 读


async def read_turns(
    session: AsyncSession,
    *,
    thread_id: str,
    elder_id: uuid.UUID | str | None,
    limit: int = 8,
) -> list[TurnView]:
    """读最近 `limit` 轮，**按对话先后正序**返回（最早的在前，便于直接拼进上下文）。

    排序依据是各轮 `turn_started` 事件的 `seq`，**不是** `started_at`：
    `started_at` 的默认值 `now()` 取的是**事务开始时间**，同一个事务里建的两轮会拿到
    完全相同的时间戳，按它排序等于让数据库随机决定先后——上下文一旦错序，
    「刚才那个」这类指代就会指到别处。

    - generic（`elder_id is None`）返回空：未确认身份的历史不进任何人的上下文。
    - 会话归属与 `elder_id` 不符时返回空，而不是抛异常——读路径不该因为「查错了会话」
      让整轮对话失败；写路径才需要硬失败（那里会写脏数据）。
    """
    elder = _as_text(elder_id)
    if elder is None:
        return []

    thread = (
        await session.execute(
            select(ConversationThread.elder_id).where(ConversationThread.thread_id == thread_id)
        )
    ).scalar_one_or_none()
    if thread is None or _as_text(thread) != elder:
        return []

    # 每轮的起始事件序号 → 真正的业务顺序（turn_id 上已有索引，join 成本可接受）
    start_seq = (
        select(
            ConversationEvent.turn_id.label("turn_id"),
            ConversationEvent.seq.label("start_seq"),
        )
        .where(
            ConversationEvent.thread_id == thread_id,
            ConversationEvent.event_type == "turn_started",
        )
        .subquery()
    )

    rows = (
        await session.execute(
            select(ConversationTurn)
            .outerjoin(start_seq, start_seq.c.turn_id == ConversationTurn.turn_id)
            .where(
                ConversationTurn.thread_id == thread_id,
                ConversationTurn.elder_id == uuid.UUID(elder),
            )
            .order_by(
                start_seq.c.start_seq.desc().nullslast(),
                ConversationTurn.started_at.desc(),
            )
            .limit(limit)
        )
    ).scalars()

    views = [
        TurnView(
            turn_id=row.turn_id,
            user_text=row.user_text,
            assistant_delivered_text=row.assistant_delivered_text,
            status=row.status,
            started_at=row.started_at,
            ended_at=row.ended_at,
        )
        for row in rows
    ]
    views.reverse()
    return views


async def read_events(
    session: AsyncSession, *, thread_id: str, after_seq: int = 0
) -> list[EventView]:
    """增量读事件（`seq > after_seq`），按 `seq` 正序。

    给重连补帧与复盘用：客户端记住已收到的最大 `seq`，重连后只要增量。
    """
    rows = (
        await session.execute(
            select(ConversationEvent)
            .where(
                ConversationEvent.thread_id == thread_id,
                ConversationEvent.seq > after_seq,
            )
            .order_by(ConversationEvent.seq.asc())
        )
    ).scalars()

    return [
        EventView(
            seq=row.seq,
            event_type=row.event_type,
            occurred_at=row.occurred_at,
            payload=dict(row.payload_json or {}),
            turn_id=row.turn_id,
            response_id=row.response_id,
            task_id=row.task_id,
        )
        for row in rows
    ]


__all__ = [
    "TERMINAL_TURN_STATUSES",
    "EventView",
    "HistoryError",
    "InvalidEventType",
    "ThreadMismatch",
    "ThreadNotFound",
    "TurnRef",
    "TurnView",
    "append_event",
    "begin_turn",
    "commit_response",
    "finish_turn",
    "read_events",
    "read_turns",
]
