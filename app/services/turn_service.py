"""Day 6 N14 · 统一出口 handle_user_turn（对话事实的唯一提交边界）。

职责（task 卡 §3.3，**七步顺序即契约，不得改动顺序或语义**）：

    1. history.begin_turn        —— 事实先落（generic 只写事件，不建投影）
    2. state_center.snapshot/apply —— 读一致状态并应用轮次启动事件
    3. context_builder.build     —— 组装上下文
    4. agent.run                 —— 执行（工具事件经上下文缓冲后回灌 history）
    5. history.commit_response   —— 先记生成（generic 跳过投影，直写 response_completed 事件）
    6. state_center.apply        —— 再改状态
    7. response_stream.start     —— 最后下发

桩期协议（N05 StateCenter / N06 ContextBuilder 未到货）：
    - `StateCenterLike` / `ContextBuilderLike` Protocol 定义装配缝；模块级默认桩
      `MemoryStateCenter` / `MinimalContextBuilder` 提供最小语义（version 自增、
      最小 TurnContext）。N05/N06 到货后由装配层注入真身，**签名不变**。
    - N05 到位后：`StateSnapshot` 改为装配层提供的真实快照，下游读取
      `StateSnapshot.output.generation_epoch` 等字段（当前桩只有 version）。

分层约束（AGENTS §2）：本模块属 services（唯一写库层）；对 history_service /
response_service / CompanionAgent 只调用、不改其公开签名。
"""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import DialogueTurn, PersonaContext, get_trace_id
from app.core.runtime_context import Identity
from app.services.history_service import (
    TurnRef,
)
from app.services.history_service import (
    append_event as _hs_append_event,
)
from app.services.history_service import (
    begin_turn as _hs_begin_turn,
)
from app.services.history_service import (
    commit_response as _hs_commit_response,
)
from app.services.response_service import (
    ResponseHandle,
    ResponseService,
    TurnContext,
)

if TYPE_CHECKING:  # 避免运行时依赖 CompanionAgent 具体类型（结构化协议即可满足）
    from app.agents.companion import AgentTurnResult

logger = logging.getLogger("app.services.turn_service")

# ---------------------------------------------------------------- 常量（AGENTS §7 口径，不新增配置项）

SEEN_CAPACITY: Final[int] = 512  # 进程内幂等缓存容量，对齐 DialogueOrchestrator._seen_turns
DELIVERY_STATUS_PLAYING: Final[str] = (
    "playing"  # 第 5 步口径：生成已记、播放中（送达回执后由 N07 更新）
)


# ---------------------------------------------------------------- 桩期数据结构


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """桩期最小状态快照（N05 到位后由装配层换真；届时改读 `output.generation_epoch` 等真实字段）。"""

    thread_id: str
    version: int


class StateVersionConflict(Exception):
    """乐观并发冲突：apply 携带的 version 与当前版本不一致（桩期语义与 N05 对齐）。"""


@dataclass(frozen=True, slots=True)
class TurnRuntime:
    """一次对话轮的运行时身份束（服务端注入，禁止客户端/模型传入——AGENTS §4.9）。

    `elder_id` / `device_id` 从 `identity` 转发；`runtime.elder_id is None` 即
    generic（与 history_service 内部判定一致，双重判定防 R6 漂移）。
    """

    identity: Identity
    thread_id: str
    history: tuple[
        DialogueTurn, ...
    ] = ()  # 最近 5 轮缓存（Redis；durable 事实源在 history_service）
    persona: PersonaContext | None = None
    identity_source: str | None = None
    history_degraded: bool = False  # 缓存源不可用 → 第 2 步补 state_degraded 事件

    @property
    def elder_id(self) -> str | None:
        return self.identity.elder_id

    @property
    def device_id(self) -> str | None:
        return self.identity.device_id

    @property
    def is_generic(self) -> bool:
        """generic 判定：elder_id 为 None 即 generic（双重判定②，与 history 一致）。"""
        return self.elder_id is None


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """统一出口的生成事实面（orchestrator 组装 DialogueReply / 测试断言用）。

    `escalated_to_agent` 在统一出口内恒为 True——安全否决发生在 orchestrator
    前置层（N27），命中时不进入本服务。
    """

    turn_id: str
    response_id: str
    text: str
    degraded: bool
    escalated_to_agent: bool
    followup_required: bool
    followup_reason: str | None


@dataclass(slots=True)
class _TurnRecord:
    """幂等缓存条目：首次调用的 ResponseHandle + 生成事实面。"""

    handle: ResponseHandle
    outcome: TurnOutcome


@dataclass(frozen=True, slots=True)
class _PendingEvent:
    """Agent 回灌事件缓冲条目（tool_started / tool_succeeded / tool_failed）。"""

    event_type: str
    payload: dict[str, object]


_pending_agent_events: ContextVar[list[_PendingEvent] | None] = ContextVar(
    "pending_agent_events", default=None
)


# ---------------------------------------------------------------- 装配缝 Protocol（N05/N06 到货后由装配层注入真身，签名不变）


class StateCenterLike(Protocol):
    """N05 StateCenter 装配缝：乐观并发（version）+ 事件应用。"""

    async def snapshot(self, thread_id: str) -> StateSnapshot: ...

    async def apply(self, thread_id: str, version: int, events: Sequence[str]) -> int:
        """应用事件并推进版本，返回新版本号；版本不匹配抛 StateVersionConflict。"""
        ...


class ContextBuilderLike(Protocol):
    """N06 ContextBuilder 装配缝：由运行时 + 快照 + 已落事实组装下发上下文。"""

    async def build(
        self, runtime: TurnRuntime, snapshot: StateSnapshot, turn: TurnRef
    ) -> TurnContext: ...


class AgentLike(Protocol):
    """N13 CompanionAgent 装配缝（结构化，CompanionAgent 天然满足）。"""

    def run(
        self,
        *,
        runtime: Identity,
        turn_id: str,
        user_text: str,
        history: Sequence[DialogueTurn] = (),
        persona: PersonaContext | None = None,
        now: datetime | None = None,
    ) -> Awaitable[AgentTurnResult]: ...


class HistoryLike(Protocol):
    """N04 history_service 装配缝（默认 HistoryGateway 直连模块函数；测试可换内存实现）。"""

    async def begin_turn(
        self,
        session: AsyncSession,
        *,
        thread_id: str,
        elder_id: str | None,
        device_id: str | None = None,
        call_id: str | None = None,
        turn_id: str,
        user_text: str,
        trace_id: str | None = None,
        identity_source: str | None = None,
    ) -> TurnRef: ...

    async def append_event(
        self,
        session: AsyncSession,
        *,
        thread_id: str,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        turn_id: str | None = None,
        response_id: str | None = None,
        task_id: str | None = None,
        call_id: str | None = None,
        trace_id: str | None = None,
    ) -> int: ...

    async def commit_response(
        self,
        session: AsyncSession,
        *,
        turn_id: str,
        response_id: str,
        generated_text: str,
        delivered_text: str | None,
        status: str,
    ) -> None: ...


# ---------------------------------------------------------------- 默认桩 / 默认网关


class HistoryGateway:
    """history_service 模块函数的会话式包装（默认注入；签名与 HistoryLike 一致）。"""

    async def begin_turn(
        self,
        session: AsyncSession,
        *,
        thread_id: str,
        elder_id: str | None,
        device_id: str | None = None,
        call_id: str | None = None,
        turn_id: str,
        user_text: str,
        trace_id: str | None = None,
        identity_source: str | None = None,
    ) -> TurnRef:
        return await _hs_begin_turn(
            session,
            thread_id=thread_id,
            elder_id=elder_id,
            device_id=device_id,
            call_id=call_id,
            turn_id=turn_id,
            user_text=user_text,
            trace_id=trace_id,
            identity_source=identity_source,
        )

    async def append_event(
        self,
        session: AsyncSession,
        *,
        thread_id: str,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        turn_id: str | None = None,
        response_id: str | None = None,
        task_id: str | None = None,
        call_id: str | None = None,
        trace_id: str | None = None,
    ) -> int:
        return await _hs_append_event(
            session,
            thread_id=thread_id,
            event_type=event_type,
            payload=payload,
            turn_id=turn_id,
            response_id=response_id,
            task_id=task_id,
            call_id=call_id,
            trace_id=trace_id,
        )

    async def commit_response(
        self,
        session: AsyncSession,
        *,
        turn_id: str,
        response_id: str,
        generated_text: str,
        delivered_text: str | None,
        status: str,
    ) -> None:
        return await _hs_commit_response(
            session,
            turn_id=turn_id,
            response_id=response_id,
            generated_text=generated_text,
            delivered_text=delivered_text,
            status=status,
        )


class MemoryStateCenter:
    """桩期默认 StateCenter：version 每次 apply 自增一代，事件按线程累积（测试/调试检视用）。

    N05 到位后由装配层替换注入，本类退役（签名与 StateCenterLike 一致）。
    """

    def __init__(self) -> None:
        self._versions: dict[str, int] = {}
        self._applied: dict[str, list[tuple[int, str]]] = {}

    async def snapshot(self, thread_id: str) -> StateSnapshot:
        return StateSnapshot(thread_id=thread_id, version=self._versions.get(thread_id, 0))

    async def apply(self, thread_id: str, version: int, events: Sequence[str]) -> int:
        current = self._versions.get(thread_id, 0)
        if version != current:
            raise StateVersionConflict(
                f"state version conflict on thread {thread_id!r}: expected {current}, got {version}"
            )
        new_version = current + 1
        self._versions[thread_id] = new_version
        applied = self._applied.setdefault(thread_id, [])
        for name in events:
            applied.append((new_version, name))
        return new_version

    @property
    def applied(self) -> Mapping[str, list[tuple[int, str]]]:
        """检视面（测试断言 state_degraded 等应用顺序）。"""
        return self._applied


class MinimalContextBuilder:
    """桩期默认 ContextBuilder：返回最小 TurnContext 供第 7 步下发。

    N06 到位后由装配层替换注入；届时此处承载记忆召回/任务态注入，并改读
    StateSnapshot 的真实字段（桩期注释留痕，见模块 docstring）。
    """

    async def build(
        self, runtime: TurnRuntime, snapshot: StateSnapshot, turn: TurnRef
    ) -> TurnContext:
        return TurnContext(
            thread_id=runtime.thread_id,
            turn_id=turn.turn_id,
            device_id=runtime.device_id or "",
            elder_id=runtime.elder_id,
        )


class HistoryEventSink:
    """N07 EventSink 协议 → history_service.append_event 适配（N04 到位后装配层接线用）。

    每次追加开独立短事务（response_started 等下发侧事件不占轮次会话的事务窗口）；
    返回全局 seq（与 EventSink 协议一致）。
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def append_event(
        self,
        *,
        thread_id: str,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        turn_id: str | None = None,
        response_id: str | None = None,
    ) -> int:
        async with self._session_factory() as session:
            seq = await _hs_append_event(
                session,
                thread_id=thread_id,
                event_type=event_type,
                payload=payload,
                turn_id=turn_id,
                response_id=response_id,
            )
            await session.commit()
            return seq


# ---------------------------------------------------------------- 统一出口


class TurnService:
    """统一事实提交边界：七步顺序即契约（见模块 docstring）。

    幂等（任务卡 §2）：同 `(device_id, turn_id)` 第二次调用直接返回首次
    ResponseHandle，不重复调 LLM。进程内 dict 为主路径（对齐
    DialogueOrchestrator._seen_turns 现状）；history.begin_turn 返回
    duplicate=True 且内存无缓存（如进程重启后 ASR 重发）时：不重跑 LLM，
    日志告警后抛 ValueError。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        agent: AgentLike,
        response_stream: ResponseService,
        history: HistoryLike | None = None,
        state_center: StateCenterLike | None = None,
        context_builder: ContextBuilderLike | None = None,
        now_provider: Callable[[], datetime] | None = None,
        seen_capacity: int = SEEN_CAPACITY,
    ) -> None:
        self._session_factory = session_factory
        self._agent = agent
        self._response_stream = response_stream
        self._history: HistoryLike = history if history is not None else HistoryGateway()
        self._state: StateCenterLike = (
            state_center if state_center is not None else MemoryStateCenter()
        )
        self._builder: ContextBuilderLike = (
            context_builder if context_builder is not None else MinimalContextBuilder()
        )
        self._now_provider = (
            now_provider if now_provider is not None else (lambda: datetime.now(UTC))
        )
        self._seen_capacity = seen_capacity
        self._records: OrderedDict[tuple[str, str], _TurnRecord] = OrderedDict()

    # -- 幂等检视面 ------------------------------------------------------

    def outcome_of(self, turn_id: str) -> TurnOutcome | None:
        """按 turn_id 查首次调用的生成事实面（orchestrator 组装 DialogueReply 用）。"""
        for record in self._records.values():
            if record.outcome.turn_id == turn_id:
                return record.outcome
        return None

    # -- Agent 事件回灌面 ------------------------------------------------

    async def agent_event_sink(self, event_type: str, payload: Mapping[str, object]) -> None:
        """注入 `CompanionAgent(on_event=...)`：工具事件先缓冲，第 4 步结束后统一落 history。

        事件类型必须落在 history_service 的 12 值枚举内（落库时校验，不发明新类型）。
        """
        buffer = _pending_agent_events.get()
        if buffer is None:
            logger.warning(
                "agent_event_outside_turn_discarded",
                extra={"event_type": event_type},
            )
            return
        buffer.append(_PendingEvent(event_type=event_type, payload=dict(payload)))

    # -- 七步契约 --------------------------------------------------------

    async def handle_user_turn(
        self,
        runtime: TurnRuntime,
        asr_final: str,
        *,
        call_id: str | None = None,
        turn_id: str,
    ) -> ResponseHandle:
        """七步顺序执行（模块 docstring）；返回值只做下发句柄，生成事实面见 `outcome_of`。"""
        text = (asr_final or "").strip()
        if not text:
            # ASR 空文本前置拦截：orchestrator 已拦（Day 4），此处兜底防脏数据越界
            raise ValueError("asr_final must be non-empty ASR final text")
        if not turn_id:
            raise ValueError("turn_id is required")

        # 幂等主路径：进程内命中直接返回首次 ResponseHandle，不重复调 LLM
        key = (runtime.device_id or "-", turn_id)
        cached = self._records.get(key)
        if cached is not None:
            logger.info(
                "turn_idempotent_hit",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "turn_id": turn_id,
                    "device_id": runtime.device_id,
                },
            )
            return cached.handle

        # 双重判定②：elder_id 为 None 即 generic，与 history_service 内部判定一致（R6）
        elder_id = runtime.elder_id
        is_generic = elder_id is None

        async with self._session_factory() as session:
            try:
                # ---- 第 1 步：事实先落（generic 只写事件，不建 ConversationTurn 投影） ----
                turn_ref = await self._history.begin_turn(
                    session,
                    thread_id=runtime.thread_id,
                    elder_id=elder_id,
                    device_id=runtime.device_id,
                    call_id=call_id,
                    turn_id=turn_id,
                    user_text=text,
                    identity_source=runtime.identity_source,
                )
                if turn_ref.duplicate:
                    # 兜底判重：无内存缓存（进程重启后 ASR 重发）→ 不重跑 LLM，显性失败
                    logger.warning(
                        "turn_duplicate_no_cached_handle",
                        extra={
                            "trace_id": get_trace_id() or "-",
                            "turn_id": turn_id,
                            "device_id": runtime.device_id,
                            "thread_id": runtime.thread_id,
                        },
                    )
                    raise ValueError(f"duplicate turn_id without cached handle: {turn_id!r}")

                # ASR final 事实事件（12 值枚举内；begin_turn 已写 turn_started，此处补 final 事实）
                await self._history.append_event(
                    session,
                    thread_id=runtime.thread_id,
                    event_type="asr_final_committed",
                    payload={"turn_id": turn_id, "text_len": len(text)},
                    turn_id=turn_id,
                    call_id=call_id,
                )

                # ---- 第 2 步：读一致状态 + 应用轮次启动事件 ----
                snapshot = await self._state.snapshot(runtime.thread_id)
                state_events: list[str] = ["turn_started", "asr_final_committed"]
                if runtime.history_degraded:
                    # Redis 不可用 → 闲聊退化单进程短会话，留 state_degraded（§4 失败分支）
                    state_events.append("state_degraded")
                state_version = await self._state.apply(
                    runtime.thread_id, snapshot.version, tuple(state_events)
                )

                # ---- 第 3 步：组装上下文 ----
                context = await self._builder.build(runtime, snapshot, turn_ref)

                # ---- 第 4 步：执行（工具事件经上下文缓冲，结束后回灌 history） ----
                buffer: list[_PendingEvent] = []
                token = _pending_agent_events.set(buffer)
                try:
                    result = await self._agent.run(
                        runtime=runtime.identity,
                        turn_id=turn_id,
                        user_text=text,
                        history=runtime.history,
                        persona=runtime.persona,
                        now=self._now_provider(),
                    )
                finally:
                    _pending_agent_events.reset(token)
                for pending in buffer:
                    await self._history.append_event(
                        session,
                        thread_id=runtime.thread_id,
                        event_type=pending.event_type,
                        payload=pending.payload,
                        turn_id=turn_id,
                        call_id=call_id,
                    )

                # ---- 第 5 步：先记生成（delivered_text 传 None：此时尚无送达事实，投影只留已送达） ----
                response_id = uuid.uuid4().hex
                if is_generic:
                    # commit_response 依赖 ConversationTurn 投影（generic 无投影会抛 HistoryError）
                    # → generic 改直写 response_completed 事件，生成事实仍闭合
                    await self._history.append_event(
                        session,
                        thread_id=runtime.thread_id,
                        event_type="response_completed",
                        payload={
                            "generated_text": result.text,
                            "delivered_text": None,
                            "response_status": DELIVERY_STATUS_PLAYING,
                        },
                        turn_id=turn_id,
                        response_id=response_id,
                    )
                else:
                    await self._history.commit_response(
                        session,
                        turn_id=turn_id,
                        response_id=response_id,
                        generated_text=result.text,
                        delivered_text=None,
                        status=DELIVERY_STATUS_PLAYING,
                    )

                # ---- 第 6 步：再改状态 ----
                await self._state.apply(runtime.thread_id, state_version, ("response_completed",))

                # ---- 第 7 步：最后下发（response_id 与第 5 步同一枚，事实与下发对账一致） ----
                handle = await self._response_stream.start(context, result, response_id=response_id)

                await session.commit()
            except BaseException:
                await session.rollback()
                raise

        outcome = TurnOutcome(
            turn_id=turn_id,
            response_id=response_id,
            text=result.text,
            degraded=result.degraded,
            escalated_to_agent=True,
            followup_required=result.followup_required,
            followup_reason=result.followup_reason,
        )
        self._records[key] = _TurnRecord(handle=handle, outcome=outcome)
        while len(self._records) > self._seen_capacity:
            self._records.popitem(last=False)
        logger.info(
            "turn_committed_via_unified_exit",
            extra={
                "trace_id": get_trace_id() or "-",
                "turn_id": turn_id,
                "thread_id": runtime.thread_id,
                "device_id": runtime.device_id,
                "elder_id": elder_id,
                "generic": is_generic,
                "response_id": response_id,
                "degraded": result.degraded,
                "followup_required": result.followup_required,
            },
        )
        return handle


__all__ = [
    "DELIVERY_STATUS_PLAYING",
    "SEEN_CAPACITY",
    "AgentLike",
    "ContextBuilderLike",
    "HistoryEventSink",
    "HistoryGateway",
    "HistoryLike",
    "MemoryStateCenter",
    "MinimalContextBuilder",
    "StateCenterLike",
    "StateSnapshot",
    "StateVersionConflict",
    "TurnOutcome",
    "TurnRuntime",
    "TurnService",
]
