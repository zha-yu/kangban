"""Day 6 N14 · 统一出口 handle_user_turn（对话事实的唯一提交边界）。

职责（task 卡 §3.3，**七步顺序即契约，不得改动顺序或语义**）：

    1. history.begin_turn        —— 事实先落（generic 只写事件，不建投影）
    2. state_center.snapshot/apply —— 读一致状态并应用轮次启动事件
    3. context_builder.build     —— 组装上下文
    3.5 router.decide           —— 三级路由（规则 → System One → Agent；Day 7 A3 新增，
                                   **不改前后六步语义**，只在第 3 与第 4 步之间做分流）
    4. agent.run                 —— 执行（工具事件经上下文缓冲后回灌 history）
    5. response_id 发号          —— 只发号不再写库：`response_completed` 的唯一写口在
                                   响应层终态结算（dev-B P2-10；此前 A/B 双写靠幂等兜住，
                                   但 payload 恒为 `playing`，打断后状态就错了）。
                                   **例外**：generic 无投影，直写事件是它唯一写口，不许删。
    6. state_center.apply        —— 再改状态
    7. response_stream.start     —— 最后下发

装配缝与真身（N05/N06 已到货，Day 6 S3 对齐）：
    - `StateCenterLike` / `ContextBuilderLike` Protocol 定义装配缝；默认桩
      `MemoryStateCenter` / `MinimalContextBuilder` 保留（测试与未注入兜底用）。
    - N05 真身经 `CoordinatorStateCenterAdapter` 注入：snapshot 直通
      SessionCoordinator（per-thread 锁）；apply 显式 CAS 直调真 StateCenter
      （为什么不走 submit，见适配器 docstring）。真身返回
      `state_service.StateSnapshot`（thread_id/version 与桩同构，另有
      identity/input/output/media/task 真实字段）。
    - N06 真身 `ContextBuilder`：最小上下文 + 长期记忆召回注入
      （`memory_service.recall_active_chunks`，闸门②；召回失败不臆造）。

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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, get_args, get_type_hints

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import DialogueTurn, PersonaContext, get_trace_id
from app.core.runtime_context import Identity
from app.services.decision_log_service import record_decision as _record_decision_row
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
from app.services.memory_service import recall_active_chunks
from app.services.response_service import (
    ResponseHandle,
    ResponseService,
    TurnContext,
)
from app.services.state_service import (
    StateCenter,
    StateEvent,
)
from app.services.state_service import (
    StateSnapshot as CenterStateSnapshot,
)
from app.services.state_service import (
    StateVersionConflict as StateCenterVersionConflict,
)

if TYPE_CHECKING:  # 避免运行时依赖 CompanionAgent 具体类型（结构化协议即可满足）
    from app.agents.companion import AgentTurnResult

    # SessionCoordinator 仅作适配器构造参数标注（运行时鸭子类型调用 snapshot）
    from app.agents.session_coordinator import SessionCoordinator

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
    #: 本轮 ASR final 文本（`handle_user_turn` 入口统一写入，不信任调用方传参）。
    #: 记忆提取 / 上下文组装读这里，就不必各自再要一次文本（dev-B J2 patch，两处小改之一）。
    user_text: str | None = None

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

    Day 7 A3 只**追加**三个字段（`route_layer` / `intent` / `confidence`，B1 已确认
    响应流契约不变）：它们是「这轮走了哪一层」的服务端视图，与 `decision_logs`
    同一份结论，方便 orchestrator / 联调直接断言，不必回表查。
    """

    turn_id: str
    response_id: str
    text: str
    degraded: bool
    escalated_to_agent: bool
    followup_required: bool
    followup_reason: str | None
    #: 命中层：rule / system_one / agent / fallback（未装配 Router 时为 agent，
    #: 因为旧路径等价于「一直下沉 Agent」）
    route_layer: str = "agent"
    intent: str = "other"
    confidence: float = 0.0
    #: 本轮是否已澄清过（Router 判定的 clarify 轮为 True；供联调核对「只澄清一次」）
    clarified: bool = False


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


class RouterLike(Protocol):
    """三级路由装配缝（`app.router.decision.Router` 天然满足，测试可换桩）。

    只暴露两个动作：给结论、执行直连工具。Router 内部不碰 DB（本服务才是写库层），
    直连工具也由 Router 交给注入的 `ToolExecutor`（G2 唯一入口）。
    """

    async def decide(self, text: str, *, thread_id: str, runtime: TurnRuntime) -> Any: ...

    async def execute_tool(
        self, decision: Any, *, runtime: TurnRuntime, turn_id: str
    ) -> Any | None: ...


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


# ---------------------------------------------------------------- N05 真身适配器（装配层注入 TurnService）


_STATE_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    get_args(get_type_hints(StateEvent)["event_type"])
)
"""state_service.StateEvent 冻结的 12 值事件枚举。

单一事实源：直接从真身 dataclass 的 Literal 注解取值，本模块不手抄第二份——
state_service 增删事件时适配器自动跟随，不会出现「状态中心已认、适配器漏映射」。
"""


class CoordinatorStateCenterAdapter:
    """真 StateCenter / SessionCoordinator → `StateCenterLike` 适配器。

    为什么 apply 走 `StateCenter.apply` 而不是 `SessionCoordinator.submit`：
    submit 内部会**重取最新版本**并在冲突时自动重试，这会绕过七步契约的显式
    版本语义——第 2 步按 snapshot 版本 CAS 应用启动事件，第 6 步必须以第 2 步
    返回的版本号**原样**再 CAS；若经 submit，两步之间的并发写入会被静默重试
    吞掉（lost update 不再显性失败）。版本不符时真身的
    `state_service.StateVersionConflict` **冒泡不翻译不吞**：本侧只留日志再
    上抛，事务回滚由 `TurnService.handle_user_turn` 已有的
    `except BaseException` 兜住。

    `snapshot` 直通 `SessionCoordinator.snapshot`（per-thread 锁保证与提交方
    读写一致），返回真 `state_service.StateSnapshot`：thread_id/version 与桩期
    `StateSnapshot` 同构，结构上满足装配缝；下游可按需读 output.generation_epoch、
    input.status 等真实字段。

    ⚠️ `StateVersionConflict.actual == -2` 不是「版本落后」：`state_service` 用 -2 表示
    Redis 里的状态 JSON **已损坏/不可解析**（见 `CAS_LUA`），此刻重取快照得到的版本号
    没有意义。调用方**不要盲目重试**（会连续冲突直到连接超时），应按「状态不可信」处理：
    落 `state_degraded`、让本轮走降级路径，并报警由人清理该 thread 的 key。
    """

    def __init__(self, *, coordinator: SessionCoordinator, state_center: StateCenter) -> None:
        self._coordinator = coordinator
        self._center = state_center

    async def snapshot(self, thread_id: str) -> CenterStateSnapshot:
        return await self._coordinator.snapshot(thread_id)

    async def apply(self, thread_id: str, version: int, events: Sequence[str]) -> int:
        state_events: list[StateEvent] = []
        for name in events:
            if name not in _STATE_EVENT_TYPES:
                # 12 值契约外的字符串事件（如 state_degraded）：durable 留痕在
                # history_service 事件流水，状态中心跳过，不发明第 13 种事件
                logger.warning(
                    "state_event_skipped_not_in_center_contract",
                    extra={
                        "trace_id": get_trace_id() or "-",
                        "thread_id": thread_id,
                        "event_type": name,
                    },
                )
                continue
            # StateEvent 必填字段仅 event_type；payload 缺省空 dict——
            # turn_started / response_completed 等事件的状态字段语义由真身
            # `_apply_event` 映射表维护，适配器不臆造负载
            state_events.append(StateEvent(event_type=name))
        try:
            new_snap = await self._center.apply(
                thread_id=thread_id,
                expected_version=version,
                events=state_events,
            )
        except StateCenterVersionConflict as exc:
            logger.warning(
                "state_apply_version_conflict",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "thread_id": thread_id,
                    "expected": exc.expected,
                    "actual": exc.actual,
                },
            )
            raise
        return new_snap.version


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
    """默认 ContextBuilder：返回最小 TurnContext 供第 7 步下发（测试/未注入兜底用）。

    N06 真身 `ContextBuilder` 由装配层注入；生产路径应使用真身（承载记忆召回）。

    本轮用户文本从 `runtime.user_text` 读（`handle_user_turn` 入口已回填），
    不额外开参数：`TurnContext` 目前没有 `user_text` 字段，加字段属响应层（dev-B 写锁），
    本类只保证「文本在运行时里一定拿得到」。
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


class _MemoryRecallFn(Protocol):
    """`memory_service.recall_active_chunks` 的消费面（闸门②：强制 elder_id + status=active + 有效期）。"""

    async def __call__(
        self,
        *,
        elder_id: str | None,
        is_generic: bool,
        limit: int = 5,
        min_importance: float | None = None,
        session: Any = None,
    ) -> Sequence[Any]: ...


class ContextBuilder(MinimalContextBuilder):
    """N06 真身构建器：最小上下文 + 长期记忆召回注入（dev-B memory_service 到货）。

    召回走 `recall_active_chunks`（闸门②在真身内部强制：elder_id + status=active +
    有效期过滤）。PRD 口径：**召回失败（异常/空）→ 本轮不加载记忆，绝不臆造**——
    捕获后日志并继续组装，产物仅缺 memory_notes，不拖垮轮次。

    `recall` 可注入（测试绑定用例事务；缺省为真函数，内部自开 `app.db.get_session`）。
    """

    def __init__(self, *, recall: _MemoryRecallFn | None = None, limit: int = 5) -> None:
        self._recall: _MemoryRecallFn = recall if recall is not None else recall_active_chunks
        self._limit = limit

    async def build(
        self, runtime: TurnRuntime, snapshot: StateSnapshot, turn: TurnRef
    ) -> TurnContext:
        memory_notes: tuple[str, ...] = ()
        if runtime.elder_id is not None:
            try:
                chunks = await self._recall(
                    elder_id=runtime.elder_id, is_generic=False, limit=self._limit
                )
                memory_notes = tuple(str(chunk.content) for chunk in chunks)
            except Exception as exc:  # 召回失败不拖垮轮次（PRD：本轮不加载记忆、绝不臆造）
                logger.warning(
                    "memory_recall_failed_skip_memory_this_turn",
                    extra={
                        "trace_id": get_trace_id() or "-",
                        "thread_id": runtime.thread_id,
                        "error_type": type(exc).__name__,
                    },
                )
            else:
                logger.info(
                    "context_memory_loaded",
                    extra={
                        "trace_id": get_trace_id() or "-",
                        "thread_id": runtime.thread_id,
                        "memory_count": len(memory_notes),
                    },
                )
        context = await super().build(runtime, snapshot, turn)
        if not memory_notes:
            return context
        return replace(context, memory_notes=memory_notes)


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


def _is_route_degraded(decision: Any, result: Any) -> bool:
    """D6（route_degraded 冻结 v1.0）：fallback 路由且 L2 正常生成才为 true。

    澄清、拒答、生成降级轮一律 false——与 `degraded` 正交，不做互相推断。
    """
    route_layer = str(getattr(decision, "route_layer", "agent"))
    route_action = str(getattr(decision, "action", "agent"))
    return (
        route_layer == "fallback"
        and route_action == "agent"
        and not bool(getattr(result, "degraded", False))
    )


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
        router: RouterLike | None = None,
        model_version: str = "",
        confidence_threshold: float = 0.85,
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
        #: Day 7 A3：三级路由（未注入时行为与 Day 6 完全一致——一直走 Agent）
        self._router = router
        #: 写入 `decision_logs.model_version`（装配层传 settings.system_one_model_version）
        self._model_version = model_version
        #: 与 Router 同一阈值（澄清判据在 `was_clarified` 侧使用）
        self._confidence_threshold = confidence_threshold
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
        audio_sink: Callable[[str], Awaitable[bool]] | None = None,
    ) -> ResponseHandle:
        """七步顺序执行（模块 docstring）；返回值只做下发句柄，生成事实面见 `outcome_of`。

        `audio_sink` 透传给第 7 步 ResponseService.start：注入时走分节奏下发
        （段音频开始流送才发对应 tts_text），None 保持一次性全量下发（测试/旧路径）。
        """
        text = (asr_final or "").strip()
        if not text:
            # ASR 空文本前置拦截：orchestrator 已拦（Day 4），此处兜底防脏数据越界
            raise ValueError("asr_final must be non-empty ASR final text")
        if not turn_id:
            raise ValueError("turn_id is required")
        # 文本统一回填运行时：下游（记忆提取 / ContextBuilder 真身）读 `runtime.user_text`，
        # 不再各自持有第二份入参（dev-B J2 patch）。
        runtime = replace(runtime, user_text=text)

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

                # ---- 第 3.5 步：三级路由（Day 7 A3；规则 → System One → Agent） ----
                # 位置严格在「上下文已就绪、Agent 未启动」之间：路由需要 thread_id/runtime，
                # 但绝不能抢在事实落库（第 1/2 步）之前——否则决策日志会指向不存在的轮次。
                decision: Any | None = None
                tool_result: Any | None = None
                if self._router is not None:
                    decision = await self._router.decide(
                        text, thread_id=runtime.thread_id, runtime=runtime
                    )
                    # 路由结论先落库（同一事务）：工具再慢也不能让这一轮「查不到走了哪一层」
                    if not is_generic:
                        await self._record_decision(session, runtime, text, decision)
                    if decision.action == "tool":
                        # 直连工具**只**经 Router → ToolExecutor（G2 唯一入口）
                        tool_result = await self._router.execute_tool(
                            decision, runtime=runtime, turn_id=turn_id
                        )
                        if tool_result is not None:
                            await self._append_tool_events(
                                session, runtime, turn_id, decision, tool_result, call_id
                            )

                # ---- 第 4 步：执行（工具事件经上下文缓冲，结束后回灌 history） ----
                # clarify / escalate 不调 Agent（验收：G1 命中 → Agent 调用 0、工具执行 0）；
                # tool 有结果时也不调 Agent（同一工具不得出现第二套实现）。
                short_circuit = self._short_circuit_result(decision, tool_result)
                if short_circuit is not None:
                    result = short_circuit
                else:
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

                # ---- 第 5 步：只发号（生成事实的落库归响应层终态结算，P2-10） ----
                # 为什么不再调 commit_response：
                #   1) `response_completed` 的**唯一写口**在响应层（末段 played / interrupt 时
                #      按同一 response_id 写终态）。A 这里再写一次是双写，只靠 history 层的
                #      幂等兜住；
                #   2) 更要紧的是 payload：谁先写谁定 `response_status`。A 在生成时只能写
                #      `playing`，于是「打断后 status 仍是 playing」——终态对不上（P2-10）。
                # 生成事实不会丢：响应层结算时用同一 response_id 写 `response_completed`
                # （generated_text 在响应层手里），投影也由它一并补。
                response_id = uuid.uuid4().hex
                if is_generic:
                    # generic 没有 ConversationTurn 投影（commit_response 会抛 HistoryError），
                    # 直写事件是它**唯一**的写口——不要删。
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

                # ---- 第 6 步：再改状态 ----
                await self._state.apply(runtime.thread_id, state_version, ("response_completed",))

                # 事实与状态已闭合，先提交再下发。第 7 步 ResponseService.start 内部的
                # HistoryEventSink 开独立短事务，对同一 conversation_threads 行
                # SELECT … FOR UPDATE 分配 seq；若在本会话事务窗口内调用，该短事务会
                # 锁等本会话未提交的行锁——应用层循环等待，PG 不检测、无 lock_timeout，
                # 静默挂死（trace a5dc0863：response_started 后 40s 零段下发 →
                # ws_dead 45s 判死）。commit 后行锁释放，下发短事务立即可进。
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

        # ---- 第 7 步：最后下发（事务窗口之外；response_id 与第 5 步同一枚，事实与下发对账一致） ----
        # 下发失败不回滚已闭合的事实（只追加口径）；客户端侧由 ws_dead / 重连兜底。
        # D6（route_degraded 冻结 v1.0）：仅「fallback 路由 → L2 正常生成」为 true；
        # clarify / 拒答 / 生成降级轮一律 false（与 degraded 正交）。
        route_layer = str(getattr(decision, "route_layer", "agent"))
        route_degraded = _is_route_degraded(decision, result)
        handle = await self._response_stream.start(
            context,
            result,
            response_id=response_id,
            audio_sink=audio_sink,
            route_degraded=route_degraded,
        )

        outcome = TurnOutcome(
            turn_id=turn_id,
            response_id=response_id,
            text=result.text,
            degraded=result.degraded,
            escalated_to_agent=True,
            followup_required=result.followup_required,
            followup_reason=result.followup_reason,
            route_layer=route_layer,
            intent=getattr(decision, "intent", "other"),
            confidence=float(getattr(decision, "confidence", 0.0) or 0.0),
            clarified=bool(decision is not None and decision.action == "clarify"),
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

    # -- Day 7 A3：路由结论落库 / 短路结果 / 直连工具事件 --------------------

    async def _record_decision(
        self,
        session: AsyncSession,
        runtime: TurnRuntime,
        text: str,
        decision: Any,
    ) -> None:
        """写 `decision_logs`（每轮恰好 1 行；失败只降级记录，不影响对话）。"""
        await _record_decision_row(
            session,
            thread_id=runtime.thread_id,
            elder_id=runtime.elder_id,
            trace_id=get_trace_id(),
            input_text=text,
            decision=decision,
            model_version=self._model_version,
        )

    def _short_circuit_result(self, decision: Any, tool_result: Any) -> AgentTurnResult | None:
        """把路由结论转成「可播报的轮次结果」；返回 None 表示继续走 Agent。

        - `clarify`：用 Router 给的常量话术（**不调 LLM**：澄清本身不该再花 200ms）；
        - `escalate`：G1 敏感命中，复用安全否决话术（同一句话只说一份）；
        - `tool`：把 `ToolResult.message` 直接播（失败话术已由 ToolExecutor 四分类给出）。
        """
        if decision is None:
            return None
        from app.agents.companion import AgentTurnResult  # 局部导入：避免模块级耦合 Agent

        if decision.action == "clarify":
            return AgentTurnResult(text=decision.clarify_text or "您再说一遍好吗？", degraded=False)
        if decision.action == "escalate":
            from app.services.dialogue_service import (
                SAFETY_VETO_REPLY_TEXT,  # 局部导入：避模块级循环（response_service → dialogue_service）
            )

            return AgentTurnResult(text=SAFETY_VETO_REPLY_TEXT, degraded=True)
        if decision.action == "tool":
            if tool_result is None:
                # 执行器未装配：不假装查到了，交 Agent 用常识兜住（并留日志）
                logger.warning(
                    "route_tool_not_executed_fallback_agent tool=%s",
                    getattr(decision, "tool_name", "-"),
                )
                return None
            ok = bool(getattr(tool_result, "ok", False))
            return AgentTurnResult(
                text=self._tool_speech(decision.tool_name, tool_result),
                degraded=not ok,
            )
        return None

    @staticmethod
    def _tool_speech(tool_name: str | None, tool_result: Any) -> str:
        """直连工具的播报文本。`ToolResult.message` 有值直接用（失败四分类都在里面）。"""
        message = getattr(tool_result, "message", None)
        if isinstance(message, str) and message:
            return message
        data = getattr(tool_result, "data", None) or {}
        if tool_name == "play_opera" and isinstance(data, dict):
            # D4（X8 口径③）：控制动作 → 简短确认；换一段 → 报新曲名
            control = data.get("control")
            if control:
                return _PLAY_CONTROL_SPEECH.get(str(control), "好的。")
            media = data.get("media") or {}
            if data.get("found") and media.get("title"):
                return f"好的，为您换了一首《{media['title']}》，请欣赏。"
        if tool_name == "get_weather" and isinstance(data, dict):
            city = data.get("city") or "当地"
            condition = data.get("condition") or ""
            temperature = data.get("temperature_c")
            if condition and isinstance(temperature, (int, float)):
                return f"{city}现在{condition}，气温{round(float(temperature))}度左右。"
        # 成功但形状不认识：不编数据，如实请长者再问一次
        return "我查到了，不过没太看懂，您再问我一次好吗？"

    async def _append_tool_events(
        self,
        session: AsyncSession,
        runtime: TurnRuntime,
        turn_id: str,
        decision: Any,
        tool_result: Any,
        call_id: str | None,
    ) -> None:
        """直连工具也写事件流水（与 Agent 路径同一套 12 值枚举，复盘才对得上）。"""
        status = getattr(tool_result, "status", "failed")
        event_type = "tool_succeeded" if getattr(tool_result, "ok", False) else "tool_failed"
        await self._history.append_event(
            session,
            thread_id=runtime.thread_id,
            event_type=event_type,
            payload={
                "tool_name": getattr(decision, "tool_name", None),
                "status": status,
                "error_class": getattr(tool_result, "error_class", None),
                "route_layer": getattr(decision, "route_layer", "rule"),
            },
            turn_id=turn_id,
            call_id=call_id,
        )


#: 直连播放控制的播报确认（契约 §6.3 七动作；interrupt 让位后由后续轮次接管话术）
_PLAY_CONTROL_SPEECH: Final[dict[str, str]] = {
    "pause": "好的，先停在这里。",
    "resume": "好的，继续播放。",
    "stop": "好的，已停止播放。",
    "replay": "好的，重新播放一遍。",
    "interrupt": "好的。",
    "volume_up": "好的，音量调大了。",
    "volume_down": "好的，音量调小了。",
}

__all__ = [
    "DELIVERY_STATUS_PLAYING",
    "SEEN_CAPACITY",
    "AgentLike",
    "ContextBuilder",
    "ContextBuilderLike",
    "CoordinatorStateCenterAdapter",
    "HistoryEventSink",
    "HistoryGateway",
    "HistoryLike",
    "MemoryStateCenter",
    "MinimalContextBuilder",
    "RouterLike",
    "StateCenterLike",
    "StateSnapshot",
    "StateVersionConflict",
    "TurnOutcome",
    "TurnRuntime",
    "TurnService",
]
