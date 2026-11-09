"""Day 6 N14 · 统一出口 handle_user_turn 测试（task 卡六类场景 + 桩期协议）。

覆盖判据：
① 事件流水按序完整（turn_started → asr_final_committed → response_completed，seq 单调）；
② generic 不落 conversation_turns 投影，但 conversation_events 事件闭合（含生成文本）；
③ 同 (device_id, turn_id) 幂等返回首次 ResponseHandle 且 LLM 只调一次；
   history 层 duplicate 且内存无缓存 → 告警 + ValueError，不二次调 LLM；
④ 工具失败四类各有可读话术（transient/forbidden 终态收尾，bad_args/not_found 回灌模型）；
⑤ 安全红档否决：escalated_to_agent=False、Agent.run 未被调用、N27 升级面被调用（generic 跳过）；
⑥ 超预算收尾话术 + 桩期协议（StateCenter 冲突 / state_degraded / ContextBuilder 缝 /
   HistoryEventSink 落库）。

DB 隔离沿用 test_history_store 写法：db_engine + savepoint，用例结束整体回滚；
无库时 skip（不许静默通过）。工具失败类用内存桩（不依赖 DB），幂等/顺序类用真库。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents.companion import (
    BUDGET_EXHAUSTED_TEXT,
    TRANSIENT_DEGRADED_TEXT,
    AgentTurnResult,
    CompanionAgent,
    ToolResult,
)
from app.agents.guardrails import Guardrails
from app.core.context import DialogueIdentity, DialogueTurn
from app.core.runtime_context import Identity
from app.models.conversation import (
    EVENT_TYPES,
    ConversationEvent,
    ConversationThread,
    ConversationTurn,
)
from app.models.organization import Device, Elder, Room
from app.router.safety import SafetyDecision
from app.services.dialogue_service import (
    SAFETY_VETO_REPLY_TEXT,
    DialogueOrchestrator,
    IdentityThreadResult,
)
from app.services.llm_service import LLMReply
from app.services.response_service import ResponseService
from app.services.tool_executor import MESSAGES
from app.services.turn_service import (
    HistoryEventSink,
    MemoryStateCenter,
    MinimalContextBuilder,
    StateVersionConflict,
    TurnRuntime,
    TurnService,
)

_DEVICE_TOKEN = "test-device-token-hash"
_ELDER_ID = "0197f6a2-3b7c-7c05-b3a0-9d4c1a2b3c5e"
_DEVICE_ID = "0197f6a2-3b7c-7c05-b3a0-9d4c1a2b3c4d"


# ---------------------------------------------------------------------------
# 会话与数据夹具（同 test_history_store：db_engine + savepoint，结束整体回滚）
# ---------------------------------------------------------------------------


@pytest.fixture
async def turn_db(db_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """每用例一个绑定外层事务的 session 工厂；统一出口内部的 commit 只释放 savepoint。"""
    try:
        conn = await db_engine.connect()
    except Exception as exc:  # 任何连接失败都算「无库」
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")
    trans = await conn.begin()
    sf = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield sf
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def seeded(turn_db: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    """一位长者（personal thread）+ 一个 generic thread，同一台设备。"""
    async with turn_db() as session:
        room = Room(room_no=f"H-{uuid.uuid4().hex[:6]}")
        session.add(room)
        await session.flush()

        device = Device(room_id=room.room_id, token_hash=_DEVICE_TOKEN, status="offline")
        session.add(device)
        await session.flush()

        elder = Elder(name="张桂芬", status="active")
        session.add(elder)
        await session.flush()

        thread = ConversationThread(
            thread_id=f"t-a-{uuid.uuid4().hex[:6]}",
            elder_id=elder.elder_id,
            device_id=device.device_id,
            identity_source="voice",
        )
        thread_generic = ConversationThread(
            thread_id=f"t-g-{uuid.uuid4().hex[:6]}",
            elder_id=None,
            device_id=device.device_id,
            identity_source="generic",
        )
        session.add_all([thread, thread_generic])
        await session.flush()
        await session.commit()  # 只释放 savepoint（外层事务仍在），否则 close 时回滚丢数据

        return {
            "device_id": str(device.device_id),
            "elder_id": str(elder.elder_id),
            "thread_id": thread.thread_id,
            "thread_generic": thread_generic.thread_id,
        }


async def _read_events(
    turn_db: async_sessionmaker[AsyncSession], thread_id: str
) -> list[ConversationEvent]:
    async with turn_db() as session:
        rows = (
            await session.execute(
                select(ConversationEvent)
                .where(ConversationEvent.thread_id == thread_id)
                .order_by(ConversationEvent.seq)
            )
        ).scalars()
        return list(rows)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


@dataclass
class _EventRec:
    seq: int
    thread_id: str
    event_type: str
    payload: dict[str, object]
    turn_id: str | None
    response_id: str | None


@dataclass(frozen=True, slots=True)
class _TurnRefLike:
    """history_service.TurnRef 的内存同构（不 import 真库类型，保持桩独立）。"""

    turn_id: str
    seq: int | None
    duplicate: bool = False


class _RecordingHistory:
    """内存 HistoryLike：事件类型校验走 12 值枚举（不得发明新类型），begin_turn 判重。"""

    def __init__(self) -> None:
        self.seq = 0
        self.turns: set[str] = set()
        self.thread_of_turn: dict[str, str] = {}
        self.events: list[_EventRec] = []
        self.commits: list[dict[str, Any]] = []

    def _append(
        self,
        *,
        thread_id: str,
        event_type: str,
        payload: Mapping[str, object] | None,
        turn_id: str | None,
        response_id: str | None,
    ) -> int:
        assert event_type in EVENT_TYPES, f"event_type 不在 12 值枚举内：{event_type!r}"
        self.seq += 1
        self.events.append(
            _EventRec(
                seq=self.seq,
                thread_id=thread_id,
                event_type=event_type,
                payload=dict(payload or {}),
                turn_id=turn_id,
                response_id=response_id,
            )
        )
        return self.seq

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
    ) -> _TurnRefLike:
        if turn_id in self.turns:
            return _TurnRefLike(turn_id=turn_id, seq=None, duplicate=True)
        self.turns.add(turn_id)
        self.thread_of_turn[turn_id] = thread_id
        self._append(
            thread_id=thread_id,
            event_type="turn_started",
            payload={"user_text": user_text, "elder_id": elder_id},
            turn_id=turn_id,
            response_id=None,
        )
        return _TurnRefLike(turn_id=turn_id, seq=len(self.turns), duplicate=False)

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
        return self._append(
            thread_id=thread_id,
            event_type=event_type,
            payload=payload,
            turn_id=turn_id,
            response_id=response_id,
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
        self.commits.append(
            {
                "turn_id": turn_id,
                "response_id": response_id,
                "generated_text": generated_text,
                "delivered_text": delivered_text,
                "status": status,
            }
        )
        self._append(
            thread_id=self.thread_of_turn[turn_id],
            event_type="response_completed",
            payload={
                "generated_text": generated_text,
                "delivered_text": delivered_text,
                "response_status": status,
            },
            turn_id=turn_id,
            response_id=response_id,
        )


class _NullSession:
    """内存桩会话壳：只提供统一出口在事务边界调用的 no-op 方法。"""

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _NullSessionFactory:
    """内存桩用会话工厂壳：HistoryLike 替身不触碰真会话。"""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[AsyncSession]:
        yield _NullSession()  # type: ignore[return-value]


class _UnboundAgent:
    """占位 Agent：真 CompanionAgent 在构造后回填（见 _react_service）。"""

    async def run(self, **_: Any) -> AgentTurnResult:
        raise AssertionError("占位 Agent 不应被调用")


class _ScriptedAgent:
    """AgentLike 替身：按脚本吐 AgentTurnResult，记录 run 入参。"""

    def __init__(self, results: Sequence[AgentTurnResult]) -> None:
        self._results = list(results)
        self.run_calls: list[dict[str, Any]] = []

    async def run(
        self,
        *,
        runtime: Identity,
        turn_id: str,
        user_text: str,
        history: Sequence[DialogueTurn] = (),
        persona: Any = None,
        now: Any = None,
    ) -> AgentTurnResult:
        self.run_calls.append(
            {
                "runtime": runtime,
                "turn_id": turn_id,
                "user_text": user_text,
                "history": tuple(history),
                "persona": persona,
            }
        )
        if not self._results:
            raise AssertionError("ScriptedAgent 脚本耗尽（不该再调 LLM/Agent）")
        return self._results.pop(0)


class _ScriptedLLM:
    """ChatClient 替身：按脚本吐 LLMReply（工具调用走 JSON 线协议）。"""

    def __init__(self, replies: Sequence[LLMReply]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    async def chat(self, messages: Sequence[Mapping[str, str]]) -> LLMReply:
        self.calls.append([dict(m) for m in messages])
        if not self._replies:
            raise AssertionError("ScriptedLLM 脚本耗尽")
        return self._replies.pop(0)


def _tool_call(tool: str = "get_weather") -> LLMReply:
    return LLMReply(
        text=json.dumps({"tool": tool, "args": {"city": "上海"}}, ensure_ascii=False),
        degraded=False,
    )


class _ScriptedExecutor:
    """ToolExecutor 替身：按脚本吐 ToolResult，记录执行入参。"""

    def __init__(self, results: Sequence[ToolResult]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self,
        *,
        tool_name: str,
        args: dict[str, object],
        runtime: Identity,
        turn_id: str,
        idempotency_key: str,
    ) -> ToolResult:
        self.calls.append({"tool_name": tool_name, "args": dict(args), "turn_id": turn_id})
        if not self._results:
            raise AssertionError("ScriptedExecutor 脚本耗尽")
        return self._results.pop(0)


class _RecordingDeliver:
    """ResponseService.deliver 替身：记录 (device_id, message)，恒成功。"""

    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    def __call__(self, device_id: str, message: Mapping[str, object]) -> bool:
        self.messages.append((device_id, dict(message)))
        return True


class _RecordingSink:
    """N07 EventSink 替身：记录下发侧事件。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append_event(
        self,
        *,
        thread_id: str,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        turn_id: str | None = None,
        response_id: str | None = None,
    ) -> int:
        self.events.append(
            {
                "thread_id": thread_id,
                "event_type": event_type,
                "payload": dict(payload or {}),
                "turn_id": turn_id,
                "response_id": response_id,
            }
        )
        return len(self.events)


class _EscalatorSpy:
    """N27 升级面替身：记录调用；fail=True 时模拟落库后端故障。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *, elder_id: str, decision: SafetyDecision, thread_id: str) -> bool:
        if self.fail:
            raise RuntimeError("escalation backend down")
        self.calls.append(
            {"elder_id": elder_id, "category": decision.category, "thread_id": thread_id}
        )
        return True


class _DialogueServiceStub:
    """遗留 respond() 不会被走到（turn_service 已注入）；N14 分支只用缓存读写。"""

    async def load_cached_history(
        self, *, thread_id: str, identity: DialogueIdentity
    ) -> tuple[list[DialogueTurn], bool]:
        return [], False

    async def cache_turn(
        self,
        *,
        thread_id: str,
        identity: DialogueIdentity,
        user_text: str,
        assistant_text: str,
    ) -> None:
        return None


# ---------------------------------------------------------------------------
# 组装辅助
# ---------------------------------------------------------------------------


def _runtime(
    thread_id: str,
    *,
    elder_id: str | None = None,
    device_id: str | None = None,
    history: Sequence[DialogueTurn] = (),
    history_degraded: bool = False,
) -> TurnRuntime:
    return TurnRuntime(
        identity=Identity(kind="device", device_id=device_id or _DEVICE_ID, elder_id=elder_id),
        thread_id=thread_id,
        history=tuple(history),
        identity_source="voice" if elder_id else "generic",
        history_degraded=history_degraded,
    )


def _memory_service(
    agent: Any,
    *,
    history: _RecordingHistory | None = None,
    state_center: MemoryStateCenter | None = None,
    context_builder: Any = None,
) -> tuple[TurnService, _RecordingHistory]:
    real_history = history if history is not None else _RecordingHistory()
    service = TurnService(
        session_factory=_NullSessionFactory(),
        agent=agent,
        response_stream=ResponseService(deliver=_RecordingDeliver(), events=_RecordingSink()),
        history=real_history,
        state_center=state_center,
        context_builder=context_builder,
    )
    return service, real_history


def _react_service(
    llm: _ScriptedLLM,
    executor: _ScriptedExecutor,
    history: _RecordingHistory,
    *,
    max_tool_calls: int = 8,
) -> TurnService:
    """真 CompanionAgent + 统一出口装配（agent 与 service 互相引用：先占位后回填）。"""
    service = TurnService(
        session_factory=_NullSessionFactory(),
        agent=_UnboundAgent(),
        response_stream=ResponseService(deliver=_RecordingDeliver(), events=_RecordingSink()),
        history=history,
    )
    agent = CompanionAgent(
        llm=llm,
        guardrails=Guardrails(
            max_tool_calls=max_tool_calls, repeat_window_s=60, repeat_threshold=3
        ),
        tool_executor=executor,
        on_event=service.agent_event_sink,
    )
    service._agent = agent  # 测试期回填（构造顺序解耦）
    return service


@pytest.fixture
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """CompanionAgent 的 transient 退避不真睡，缩短用例时长。"""

    async def _skip(attempt: int) -> None:
        return None

    monkeypatch.setattr("app.agents.companion._transient_backoff", _skip)


def _personal_result(thread_id: str, elder_id: str | None) -> IdentityThreadResult:
    return IdentityThreadResult(
        identity=DialogueIdentity(source="voice" if elder_id else "generic", elder_id=elder_id),
        thread_id=thread_id,
    )


def _make_orchestrator(
    agent: Any,
    *,
    elder_id: str | None,
    thread_id: str,
    device_id: str,
    escalator: _EscalatorSpy | None = None,
) -> tuple[DialogueOrchestrator, TurnService]:
    """N14 分支编排器：resolver / DialogueService 用桩替身，统一出口用内存桩历史。"""
    service, _ = _memory_service(agent)
    orchestrator = DialogueOrchestrator(
        service=_DialogueServiceStub(),  # type: ignore[arg-type]
        identity_thread_resolver=None,  # type: ignore[arg-type]
        turn_service=service,
        safety_escalator=escalator,
    )

    async def _resolver(**_: Any) -> IdentityThreadResult:
        return _personal_result(thread_id, elder_id)

    orchestrator._identity_thread_resolver = _resolver  # type: ignore[attr-defined]
    return orchestrator, service


# ---------------------------------------------------------------------------
# ① 事件流水按序完整（DB）
# ---------------------------------------------------------------------------


async def test_event_flow_ordered_and_monotonic(
    turn_db: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    agent = _ScriptedAgent([AgentTurnResult(text="好的，我陪您聊聊。")])
    sink = _RecordingSink()
    deliver = _RecordingDeliver()
    service = TurnService(
        session_factory=turn_db,
        agent=agent,
        response_stream=ResponseService(deliver=deliver, events=sink),
    )
    runtime = _runtime(
        seeded["thread_id"], elder_id=seeded["elder_id"], device_id=seeded["device_id"]
    )

    handle = await service.handle_user_turn(
        runtime, "今天天气怎么样", call_id=None, turn_id="turn-o1"
    )

    # 七步产物：handle 是下发快照；生成事实面在 outcome_of
    assert handle.turn_id == "turn-o1"
    assert handle.total >= 1
    outcome = service.outcome_of("turn-o1")
    assert outcome is not None
    assert outcome.text == "好的，我陪您聊聊。"
    assert outcome.escalated_to_agent is True
    assert handle.response_id == outcome.response_id  # 第 5 步与第 7 步同一枚 response_id

    # DB 事件流水：顺序即契约
    events = await _read_events(turn_db, seeded["thread_id"])
    assert [e.event_type for e in events] == [
        "turn_started",
        "asr_final_committed",
        "response_completed",
    ]
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # seq 单调且无重号

    # 投影：user 文本已落，assistant 送达文本为空（尚未 ack，只存已送达）
    async with turn_db() as session:
        turn_row = (
            await session.execute(
                select(ConversationTurn).where(ConversationTurn.turn_id == "turn-o1")
            )
        ).scalar_one()
    assert turn_row.user_text == "今天天气怎么样"
    assert turn_row.assistant_delivered_text is None

    # 第 7 步分段下发：发给本设备
    assert deliver.messages and deliver.messages[0][0] == seeded["device_id"]
    assert sink.events[0]["event_type"] == "response_started"


# ---------------------------------------------------------------------------
# ② generic 不落投影、事件闭合
# ---------------------------------------------------------------------------


async def test_generic_skips_projection_but_writes_events(
    turn_db: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    agent = _ScriptedAgent([AgentTurnResult(text="您好，我在。")])
    service = TurnService(
        session_factory=turn_db,
        agent=agent,
        response_stream=ResponseService(deliver=_RecordingDeliver(), events=_RecordingSink()),
    )
    runtime = _runtime(seeded["thread_generic"], elder_id=None, device_id=seeded["device_id"])

    await service.handle_user_turn(runtime, "你好", call_id=None, turn_id="turn-g1")

    async with turn_db() as session:
        turn_rows = (
            await session.execute(
                select(ConversationTurn).where(ConversationTurn.turn_id == "turn-g1")
            )
        ).scalars()
        assert list(turn_rows) == []  # generic 不建 ConversationTurn 投影

    events = await _read_events(turn_db, seeded["thread_generic"])
    assert [e.event_type for e in events] == [
        "turn_started",
        "asr_final_committed",
        "response_completed",
    ]
    # generic 的生成事实直写 response_completed 事件（commit_response 依赖投影，跳过）
    completed = events[-1]
    assert completed.payload_json["generated_text"] == "您好，我在。"
    assert completed.payload_json["response_status"] == "playing"
    assert completed.turn_id == "turn-g1"


# ---------------------------------------------------------------------------
# ③ 幂等
# ---------------------------------------------------------------------------


async def test_duplicate_turn_id_returns_first_handle_llm_called_once(
    turn_db: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    agent = _ScriptedAgent([AgentTurnResult(text="第一次的回复")])
    service = TurnService(
        session_factory=turn_db,
        agent=agent,
        response_stream=ResponseService(deliver=_RecordingDeliver(), events=_RecordingSink()),
    )
    runtime = _runtime(
        seeded["thread_id"], elder_id=seeded["elder_id"], device_id=seeded["device_id"]
    )

    first = await service.handle_user_turn(runtime, "你好", call_id=None, turn_id="turn-d1")
    second = await service.handle_user_turn(runtime, "你好", call_id=None, turn_id="turn-d1")

    assert second == first  # 直接返回首次 ResponseHandle，不重复调 LLM
    assert len(agent.run_calls) == 1
    # 事实层也不重复落轮
    events = await _read_events(turn_db, seeded["thread_id"])
    assert [e.event_type for e in events].count("turn_started") == 1


async def test_history_duplicate_without_cached_handle_raises(
    turn_db: async_sessionmaker[AsyncSession],
    seeded: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent_a = _ScriptedAgent([AgentTurnResult(text="第一次的回复")])
    service_a = TurnService(
        session_factory=turn_db,
        agent=agent_a,
        response_stream=ResponseService(deliver=_RecordingDeliver(), events=_RecordingSink()),
    )
    runtime = _runtime(
        seeded["thread_id"], elder_id=seeded["elder_id"], device_id=seeded["device_id"]
    )
    await service_a.handle_user_turn(runtime, "你好", call_id=None, turn_id="turn-d2")

    # 第二个实例（内存无缓存，如进程重启后 ASR 重发）重放同 turn_id
    agent_b = _ScriptedAgent([])
    service_b = TurnService(
        session_factory=turn_db,
        agent=agent_b,
        response_stream=ResponseService(deliver=_RecordingDeliver(), events=_RecordingSink()),
    )
    with (
        caplog.at_level(logging.WARNING, logger="app.services.turn_service"),
        pytest.raises(ValueError, match="turn-d2"),
    ):
        await service_b.handle_user_turn(runtime, "你好", call_id=None, turn_id="turn-d2")

    assert len(agent_b.run_calls) == 0  # 不二次调 LLM
    assert any(r.getMessage() == "turn_duplicate_no_cached_handle" for r in caplog.records)


# ---------------------------------------------------------------------------
# ④ 工具失败四类各有可读话术（内存桩 + 真 CompanionAgent）
# ---------------------------------------------------------------------------


async def test_tool_failure_terminal_transient(
    _no_backoff: None, caplog: pytest.LogCaptureFixture
) -> None:
    """transient 连续失败 →「这个我暂时查不到，帮您记下来」+ tool_failed(transient)。"""
    history = _RecordingHistory()
    executor = _ScriptedExecutor(
        [ToolResult(status="failed", data=None, error_class="transient", message=None)] * 3
    )
    llm = _ScriptedLLM([_tool_call()])
    service = _react_service(llm, executor, history)

    await service.handle_user_turn(
        _runtime("t-react-a"), "今天天气怎么样", call_id=None, turn_id="turn-t1"
    )

    outcome = service.outcome_of("turn-t1")
    assert outcome is not None
    assert outcome.text == TRANSIENT_DEGRADED_TEXT
    assert outcome.degraded is True
    failed = [e for e in history.events if e.event_type == "tool_failed"]
    assert failed and failed[0].payload["error_class"] == "transient"


async def test_tool_failure_terminal_forbidden(_no_backoff: None) -> None:
    """forbidden →「这个我不能替您做。」+ tool_failed(forbidden)。"""
    history = _RecordingHistory()
    executor = _ScriptedExecutor(
        [
            ToolResult(
                status="denied",
                data=None,
                error_class="forbidden",
                message=MESSAGES["forbidden"],
            )
        ]
    )
    llm = _ScriptedLLM([_tool_call()])
    service = _react_service(llm, executor, history)

    await service.handle_user_turn(
        _runtime("t-react-f"), "今天天气怎么样", call_id=None, turn_id="turn-t2"
    )

    outcome = service.outcome_of("turn-t2")
    assert outcome is not None
    assert outcome.text == MESSAGES["forbidden"]
    assert outcome.degraded is True
    failed = [e for e in history.events if e.event_type == "tool_failed"]
    assert failed and failed[0].payload["error_class"] == "forbidden"


@pytest.mark.parametrize("error_class", ["bad_args", "not_found"])
async def test_tool_failure_feed_back_classes_go_back_to_model(
    _no_backoff: None, error_class: str
) -> None:
    """bad_args / not_found：回灌模型再答，观察消息带可读话术，事件留 tool_failed。"""
    history = _RecordingHistory()
    executor = _ScriptedExecutor(
        [
            ToolResult(
                status="failed",
                data=None,
                error_class=error_class,
                message=MESSAGES[error_class],
            )
        ]
    )
    final_text = "这个结果我没查到准确信息，您先别急。"
    llm = _ScriptedLLM([_tool_call(), LLMReply(text=final_text, degraded=False)])
    service = _react_service(llm, executor, history)

    await service.handle_user_turn(
        _runtime("t-react-b"), "今天天气怎么样", call_id=None, turn_id="turn-t3"
    )

    outcome = service.outcome_of("turn-t3")
    assert outcome is not None
    assert outcome.text == final_text
    # 回灌：第二次 LLM 调用的末条消息包含执行器可读话术（观察消息）
    assert MESSAGES[error_class] in llm.calls[1][-1]["content"]
    failed = [e for e in history.events if e.event_type == "tool_failed"]
    assert failed and failed[0].payload["error_class"] == error_class


# ---------------------------------------------------------------------------
# ⑤ 安全一票否决前置
# ---------------------------------------------------------------------------


async def test_safety_red_veto_skips_agent_and_escalates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = _ScriptedAgent([AgentTurnResult(text="不应被调用")])
    escalator = _EscalatorSpy()
    orchestrator, _ = _make_orchestrator(
        agent,
        elder_id=_ELDER_ID,
        thread_id=f"{_ELDER_ID}:1",
        device_id=_DEVICE_ID,
        escalator=escalator,
    )

    reply = await orchestrator.handle_asr_final(
        device_id=_DEVICE_ID,
        trace_id="t" * 32,
        turn_id="turn-s1",
        text="我胸口疼得厉害，喘不上气",
    )

    assert reply is not None
    assert reply.escalated_to_agent is False  # 断言面：Agent 未被调用
    assert reply.text == SAFETY_VETO_REPLY_TEXT
    assert reply.degraded is True
    assert len(agent.run_calls) == 0  # Agent.run 未被调用
    assert len(escalator.calls) == 1  # N27 三表落库面被调用
    assert escalator.calls[0]["elder_id"] == _ELDER_ID
    assert any(r.getMessage() == "safety_veto_triggered" for r in caplog.records)


async def test_safety_red_veto_generic_skips_escalation() -> None:
    agent = _ScriptedAgent([AgentTurnResult(text="不应被调用")])
    escalator = _EscalatorSpy()
    orchestrator, _ = _make_orchestrator(
        agent,
        elder_id=None,  # generic：elder_id 不可得，无法落三表
        thread_id="generic-thread-1",
        device_id=_DEVICE_ID,
        escalator=escalator,
    )

    reply = await orchestrator.handle_asr_final(
        device_id=_DEVICE_ID,
        trace_id="t" * 32,
        turn_id="turn-s2",
        text="我胸口疼得厉害，喘不上气",
    )

    assert reply is not None
    assert reply.escalated_to_agent is False
    assert reply.text == SAFETY_VETO_REPLY_TEXT
    assert len(agent.run_calls) == 0
    assert escalator.calls == []  # generic 跳过三表落库（risk_events elder_id 非空约束）


async def test_safety_red_veto_survives_escalator_failure() -> None:
    """升级落库失败不拖垮否决短路（长者侧优先收到安抚与 SOS 指引）。"""
    agent = _ScriptedAgent([AgentTurnResult(text="不应被调用")])
    escalator = _EscalatorSpy(fail=True)
    orchestrator, _ = _make_orchestrator(
        agent,
        elder_id=_ELDER_ID,
        thread_id=f"{_ELDER_ID}:1",
        device_id=_DEVICE_ID,
        escalator=escalator,
    )

    reply = await orchestrator.handle_asr_final(
        device_id=_DEVICE_ID,
        trace_id="t" * 32,
        turn_id="turn-s3",
        text="我胸口疼得厉害，喘不上气",
    )

    assert reply is not None and reply.escalated_to_agent is False
    assert reply.text == SAFETY_VETO_REPLY_TEXT
    assert len(agent.run_calls) == 0


async def test_safety_non_red_does_not_veto() -> None:
    """非红档（is_sensitive=False）不短路，正常进统一出口并走完七步。"""
    agent = _ScriptedAgent([AgentTurnResult(text="好的，我陪您聊聊。")])
    escalator = _EscalatorSpy()
    orchestrator, _ = _make_orchestrator(
        agent,
        elder_id=_ELDER_ID,
        thread_id=f"{_ELDER_ID}:1",
        device_id=_DEVICE_ID,
        escalator=escalator,
    )

    reply = await orchestrator.handle_asr_final(
        device_id=_DEVICE_ID,
        trace_id="t" * 32,
        turn_id="turn-s4",
        text="今天天气怎么样",
    )

    assert reply is not None
    assert reply.escalated_to_agent is True
    assert reply.text == "好的，我陪您聊聊。"
    assert escalator.calls == []
    assert len(agent.run_calls) == 1


# ---------------------------------------------------------------------------
# ⑥ 超预算收尾话术
# ---------------------------------------------------------------------------


async def test_budget_exhausted_returns_final_talkback(_no_backoff: None) -> None:
    """LLM 连续要求调工具 → 预算熔断 →「这个我处理不了，帮您记下来稍后再试」。"""
    history = _RecordingHistory()
    executor = _ScriptedExecutor(
        [
            ToolResult(
                status="succeeded",
                data={"city": "上海", "weather": "晴"},
                error_class=None,
                message=None,
            )
        ]
    )
    llm = _ScriptedLLM([_tool_call(), _tool_call()])
    service = _react_service(llm, executor, history, max_tool_calls=1)  # 预算=1

    await service.handle_user_turn(
        _runtime("t-budget"), "今天天气怎么样", call_id=None, turn_id="turn-b1"
    )

    outcome = service.outcome_of("turn-b1")
    assert outcome is not None
    assert outcome.text == BUDGET_EXHAUSTED_TEXT
    assert outcome.degraded is True
    failed = [e for e in history.events if e.event_type == "tool_failed"]
    assert failed and failed[0].payload["error_class"] == "forbidden"
    assert len(executor.calls) == 1  # 第二次调用被预算拦截，未到执行器


# ---------------------------------------------------------------------------
# 桩期协议（N05/N06 缝）
# ---------------------------------------------------------------------------


async def test_state_center_stub_versions_and_conflict() -> None:
    state = MemoryStateCenter()
    snapshot = await state.snapshot("thread-x")
    assert snapshot.version == 0
    assert await state.apply("thread-x", 0, ("turn_started", "asr_final_committed")) == 1
    assert await state.apply("thread-x", 1, ("response_completed",)) == 2
    with pytest.raises(StateVersionConflict):
        await state.apply("thread-x", 0, ("response_completed",))
    assert [name for _, name in state.applied["thread-x"]] == [
        "turn_started",
        "asr_final_committed",
        "response_completed",
    ]


async def test_history_degraded_applies_state_degraded_event() -> None:
    """Redis 不可用 → 闲聊退化单进程短会话 + state_degraded（§4 失败分支）。"""
    agent = _ScriptedAgent([AgentTurnResult(text="您好，我在。")])
    state = MemoryStateCenter()
    service, real_history = _memory_service(agent, state_center=state)

    await service.handle_user_turn(
        _runtime("t-degraded", history_degraded=True),
        "你好",
        call_id=None,
        turn_id="turn-dd1",
    )

    applied = [name for _, name in state.applied["t-degraded"]]
    assert applied == [
        "turn_started",
        "asr_final_committed",
        "state_degraded",
        "response_completed",
    ]
    assert real_history.events[0].event_type == "turn_started"


async def test_context_builder_stub_seam_receives_snapshot_and_turn() -> None:
    """ContextBuilderLike 注入缝：收到 runtime + snapshot + turn，产出下发上下文。"""

    class _SpyBuilder(MinimalContextBuilder):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[tuple[str, int, str]] = []

        async def build(self, runtime: Any, snapshot: Any, turn: Any) -> Any:
            self.seen.append((runtime.thread_id, snapshot.version, turn.turn_id))
            return await super().build(runtime, snapshot, turn)

    builder = _SpyBuilder()
    agent = _ScriptedAgent([AgentTurnResult(text="好的。")])
    service, _ = _memory_service(agent, context_builder=builder)
    await service.handle_user_turn(
        _runtime("t-builder", elder_id=_ELDER_ID),
        "你好",
        call_id=None,
        turn_id="turn-cb1",
    )

    assert builder.seen == [("t-builder", 0, "turn-cb1")]
    assert service.outcome_of("turn-cb1") is not None


async def test_agent_receives_runtime_identity_and_history_cache(
    turn_db: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    """AgentLike 收到的是服务端 Identity + 最近 5 轮缓存（Redis 只作缓存，闸门③注入）。"""
    agent = _ScriptedAgent([AgentTurnResult(text="好的。")])
    service = TurnService(
        session_factory=turn_db,
        agent=agent,
        response_stream=ResponseService(deliver=_RecordingDeliver(), events=_RecordingSink()),
    )
    runtime = _runtime(
        seeded["thread_id"],
        elder_id=seeded["elder_id"],
        device_id=seeded["device_id"],
        history=(DialogueTurn(user="昨天聊了戏曲", assistant="好的"),),
    )
    await service.handle_user_turn(runtime, "今天呢", call_id=None, turn_id="turn-h1")

    assert len(agent.run_calls) == 1
    call = agent.run_calls[0]
    assert call["runtime"].elder_id == seeded["elder_id"]  # 服务端注入，不收客户端值
    assert call["user_text"] == "今天呢"
    assert call["history"] == (DialogueTurn(user="昨天聊了戏曲", assistant="好的"),)


async def test_history_event_sink_writes_to_db(
    turn_db: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    """N07 EventSink → history_service.append_event 适配（N04 到位后的装配面）。"""
    sink = HistoryEventSink(turn_db)
    seq = await sink.append_event(
        thread_id=seeded["thread_id"],
        event_type="response_started",
        payload={"response_id": "r-1", "degraded": False},
        turn_id="turn-sink",
        response_id="r-1",
    )
    assert seq >= 1

    events = await _read_events(turn_db, seeded["thread_id"])
    assert [e.event_type for e in events] == ["response_started"]
    assert events[0].response_id == "r-1"


async def test_empty_asr_final_rejected_at_unified_exit(
    turn_db: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    """ASR 空文本兜底拦截（orchestrator 已前置拦，统一出口再兜一层）。"""
    agent = _ScriptedAgent([])
    service = TurnService(
        session_factory=turn_db,
        agent=agent,
        response_stream=ResponseService(deliver=_RecordingDeliver(), events=_RecordingSink()),
    )
    runtime = _runtime(
        seeded["thread_id"], elder_id=seeded["elder_id"], device_id=seeded["device_id"]
    )
    with pytest.raises(ValueError, match="non-empty"):
        await service.handle_user_turn(runtime, "   ", call_id=None, turn_id="turn-e1")
    assert agent.run_calls == []
