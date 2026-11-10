"""Day 6 N13 · CompanionAgent ReAct 循环测试（dev-C 计划 C-7 量化验收）。

桩期口径（C-0-3 先桩后并）：LLM 与 ToolExecutor 全部用脚本化假实现注入，
不连任何外部服务；预算与熔断**直接复用** ``app.agents.guardrails.Guardrails``
（单轮 8 次 / 同 Tool 60s 第 3 次，AGENTS §7 数值），本文件不构造第二套预算。

覆盖六组断言（任务卡 C-7 / dev-C 计划 S2）：
    1. 第 9 次工具调用被 Guardrails 拒（前 8 次放行执行）；
    2. 同 Tool 60s 内第 3 次熔断 → 同样收口；
    3. 高危工具（改药单/停药/关工单/删数据）预拦截 → 不执行 + followup_required；
    4. 四类失败各一条回灌路径：bad_args 修正后成功 / transient 退避 2 次后降级
       话术 / not_found 如实告知 / forbidden 拒绝；
    5. 超预算收尾话术照 TD line 834 原文，且收口即终止（不再回灌模型）；
    6. 正常一轮：LLM 直接出文本，不触发任何工具调用。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence

import pytest

import app.agents.companion as companion_module
from app.agents.companion import (
    BUDGET_EXHAUSTED_TEXT,
    FORBIDDEN_FALLBACK_TEXT,
    HIGH_RISK_REFUSED_TEXT,
    HIGH_RISK_TOOL_NAMES,
    TRANSIENT_DEGRADED_TEXT,
    WEATHER_TOOL,
    CompanionAgent,
    FollowupTask,
    ToolResult,
)
from app.agents.guardrails import Guardrails
from app.core.runtime_context import Identity
from app.services.llm_service import LLMReply

# ---------------------------------------------------------------------------
# 常量与构造器（护栏参数照 AGENTS §7 / tests/test_guardrails.py 同款）
# ---------------------------------------------------------------------------

MAX = 8  # MAX_TOOL_CALLS_PER_TURN
WINDOW = 60  # 同 Tool 熔断窗口（秒）
THRESHOLD = 3  # 同 Tool 窗口内第 3 次熔断

DEVICE_ID = str(uuid.UUID("eeee1111-2222-3333-4444-555555555555"))
ELDER_ID = str(uuid.UUID("aaaa1111-2222-3333-4444-555555555555"))
TURN_ID = "turn-test-0001"


def _runtime() -> Identity:
    """服务端构造的设备身份（elder_id 只从这里注入，模型传入值一律丢弃）。"""
    return Identity(kind="device", device_id=DEVICE_ID, elder_id=ELDER_ID)


def _tool_call_text(tool: str, args: Mapping[str, object]) -> str:
    """ReAct 线协议：LLM 要调工具时输出的 JSON 文本。"""
    return json.dumps({"tool": tool, "args": dict(args)}, ensure_ascii=False)


def _ok(data: dict[str, object]) -> ToolResult:
    return ToolResult(status="succeeded", data=data, error_class=None, message=None)


# ---------------------------------------------------------------------------
# 桩：脚本化 LLM / 执行器 / 事件与跟进回调（脚本耗尽即响亮失败，防死循环漏网）
# ---------------------------------------------------------------------------


class ScriptedLLM:
    """按脚本依次回复的假 LLM（与 ``LLMService.chat`` 同签名）；记录每次入参。"""

    def __init__(self, replies: Sequence[LLMReply]) -> None:
        self._replies = list(replies)
        self.calls: list[list[Mapping[str, str]]] = []

    async def chat(self, messages: Sequence[Mapping[str, str]]) -> LLMReply:
        self.calls.append(list(messages))
        if not self._replies:
            raise AssertionError("ScriptedLLM 脚本耗尽：Agent 循环未按预期收敛")
        return self._replies.pop(0)


class ScriptedExecutor:
    """按脚本依次返回 ToolResult 的假执行器（contracts §5.3 签名）；记录每次入参。"""

    def __init__(self, results: Sequence[ToolResult]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, object]] = []

    async def execute(
        self,
        *,
        tool_name: str,
        args: dict[str, object],
        runtime: Identity,
        turn_id: str,
        idempotency_key: str,
    ) -> ToolResult:
        self.calls.append(
            {
                "tool_name": tool_name,
                "args": dict(args),
                "turn_id": turn_id,
                "idempotency_key": idempotency_key,
                "runtime_elder_id": runtime.elder_id,
            }
        )
        if not self._results:
            raise AssertionError("ScriptedExecutor 脚本耗尽：出现未被护栏拦住的工具调用")
        return self._results.pop(0)


class RecordingSinks:
    """收集事件与跟进任务的回调桩（落库归 HistoryStore / N14，桩期只收集）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object]]] = []
        self.followups: list[FollowupTask] = []

    async def on_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        self.events.append((event_type, payload))

    async def on_followup(self, task: FollowupTask) -> None:
        self.followups.append(task)


def _agent(
    llm: ScriptedLLM,
    executor: ScriptedExecutor,
    sinks: RecordingSinks | None = None,
) -> CompanionAgent:
    sink = sinks if sinks is not None else RecordingSinks()
    return CompanionAgent(
        llm=llm,
        guardrails=Guardrails(
            max_tool_calls=MAX, repeat_window_s=WINDOW, repeat_threshold=THRESHOLD
        ),
        tool_executor=executor,
        tools=(WEATHER_TOOL,),
        on_event=sink.on_event,
        followup_sink=sink.on_followup,
    )


# ---------------------------------------------------------------------------
# ① 第 9 次工具调用被拒（前 8 次放行执行）
# ---------------------------------------------------------------------------


async def test_ninth_tool_call_rejected_by_budget() -> None:
    """9 个不同工具避开同 Tool 熔断：第 1–8 次执行，第 9 次被 Guardrails 拒。"""
    replies = [
        LLMReply(text=_tool_call_text(f"tool_{i}", {"n": i}), degraded=False) for i in range(9)
    ]
    llm = ScriptedLLM(replies)
    executor = ScriptedExecutor([_ok({"i": i}) for i in range(8)])
    agent = _agent(llm, executor)

    result = await agent.run(runtime=_runtime(), turn_id=TURN_ID, user_text="帮我连查九件事")

    assert len(executor.calls) == 8  # 第 9 次未到达执行器
    assert result.text == BUDGET_EXHAUSTED_TEXT
    assert result.degraded is True
    assert result.followup_required is False  # 预算拒绝不是高危跟进场景


# ---------------------------------------------------------------------------
# ② 同 Tool 60s 内第 3 次熔断
# ---------------------------------------------------------------------------


async def test_same_tool_circuit_break_on_third_call() -> None:
    """同一工具第 3 次调用（参数不同）被熔断：收口话术，执行器只调 2 次。"""
    llm = ScriptedLLM(
        [
            LLMReply(text=_tool_call_text("get_weather", {"city": "杭州"}), degraded=False),
            LLMReply(text=_tool_call_text("get_weather", {"city": "上海"}), degraded=False),
            LLMReply(text=_tool_call_text("get_weather", {"city": "北京"}), degraded=False),
        ]
    )
    executor = ScriptedExecutor([_ok({"temp": 18}), _ok({"temp": 20})])
    agent = _agent(llm, executor)

    result = await agent.run(runtime=_runtime(), turn_id=TURN_ID, user_text="各地天气如何")

    assert len(executor.calls) == 2
    assert result.text == BUDGET_EXHAUSTED_TEXT
    assert result.degraded is True


# ---------------------------------------------------------------------------
# ③ 高危工具预拦截：不执行 + followup_required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", sorted(HIGH_RISK_TOOL_NAMES))
async def test_high_risk_tool_refused_with_followup(tool_name: str) -> None:
    """高危名单内每个工具：预拦截（执行器零调用）+ followup_required=True + 原因。"""
    sinks = RecordingSinks()
    llm = ScriptedLLM([LLMReply(text=_tool_call_text(tool_name, {"id": "p1"}), degraded=False)])
    executor = ScriptedExecutor([])  # 高危调用绝不能到达执行器：一旦调用即响亮失败
    agent = _agent(llm, executor, sinks)

    result = await agent.run(runtime=_runtime(), turn_id=TURN_ID, user_text="帮我改一下药单")

    assert executor.calls == []
    assert result.text == HIGH_RISK_REFUSED_TEXT
    assert result.degraded is True
    assert result.followup_required is True
    assert result.followup_reason is not None
    assert tool_name in (result.followup_reason or "")
    # 跟进任务经回调外送：elder_id 取自 runtime（服务端注入），不由模型传入
    assert len(sinks.followups) == 1
    task = sinks.followups[0]
    assert task.tool_name == tool_name
    assert task.elder_id == ELDER_ID
    assert task.turn_id == TURN_ID
    # 事件留痕：tool_failed + forbidden（审计归执行器层，本层先留事件）
    failed = [payload for event, payload in sinks.events if event == "tool_failed"]
    assert failed and failed[0]["error_class"] == "forbidden"


# ---------------------------------------------------------------------------
# ④ 四类失败回灌路径
# ---------------------------------------------------------------------------


async def test_bad_args_retry_with_fixed_args_then_success() -> None:
    """bad_args：回灌模型修正参数重试（重试再次过护栏计预算），第二次成功。"""
    llm = ScriptedLLM(
        [
            LLMReply(text=_tool_call_text("get_weather", {}), degraded=False),
            LLMReply(text=_tool_call_text("get_weather", {"city": "杭州"}), degraded=False),
            LLMReply(text="杭州今天多云，18 度。", degraded=False),
        ]
    )
    executor = ScriptedExecutor(
        [
            ToolResult(status="failed", data=None, error_class="bad_args", message=None),
            _ok({"temp": 18}),
        ]
    )
    agent = _agent(llm, executor)

    result = await agent.run(runtime=_runtime(), turn_id=TURN_ID, user_text="杭州天气如何")

    assert result.text == "杭州今天多云，18 度。"
    assert result.degraded is False
    assert len(executor.calls) == 2
    # 第一次的观察结果回灌给模型：带 error_class=bad_args 与修正指引
    observation = llm.calls[1][-1]["content"]
    assert "bad_args" in observation
    assert "修正" in observation


async def test_transient_retries_twice_then_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """transient：同幂等键退避重试 ≤2 次（共 3 次执行），仍失败 → 降级话术。"""
    backoffs: list[int] = []

    async def _record_backoff(attempt: int) -> None:
        backoffs.append(attempt)

    monkeypatch.setattr(companion_module, "_transient_backoff", _record_backoff)

    transient = ToolResult(status="failed", data=None, error_class="transient", message=None)
    llm = ScriptedLLM(
        [LLMReply(text=_tool_call_text("get_weather", {"city": "杭州"}), degraded=False)]
    )
    executor = ScriptedExecutor([transient, transient, transient])
    agent = _agent(llm, executor)

    result = await agent.run(runtime=_runtime(), turn_id=TURN_ID, user_text="杭州天气如何")

    assert result.text == TRANSIENT_DEGRADED_TEXT == "这个我暂时查不到，帮您记下来"
    assert result.degraded is True
    assert len(executor.calls) == 3  # 首次 + 2 次重试
    assert backoffs == [1, 2]
    # 同一幂等键：重试是同一次逻辑调用的传输级重试，不构成新调用
    assert len({call["idempotency_key"] for call in executor.calls}) == 1


async def test_not_found_reported_truthfully() -> None:
    """not_found：回灌模型如实告知，不重试、不编造。"""
    truthful = "我这里没有查到您的用药记录，先帮您记下来，稍后再问问护工吧。"
    llm = ScriptedLLM(
        [
            LLMReply(
                text=_tool_call_text("query_medication_plans", {"drug": "降压药"}),
                degraded=False,
            ),
            LLMReply(text=truthful, degraded=False),
        ]
    )
    executor = ScriptedExecutor(
        [ToolResult(status="failed", data=None, error_class="not_found", message=None)]
    )
    agent = _agent(llm, executor)

    result = await agent.run(runtime=_runtime(), turn_id=TURN_ID, user_text="我的药还有吗")

    assert result.text == truthful
    assert result.degraded is False
    assert len(executor.calls) == 1  # 不重试
    observation = llm.calls[1][-1]["content"]
    assert "not_found" in observation
    assert "不要编造" in observation


async def test_forbidden_refused_without_retry() -> None:
    """forbidden：拒绝 + 告知（用执行器给的可播报话术），不重试。"""
    llm = ScriptedLLM(
        [
            LLMReply(
                text=_tool_call_text("query_medication_plans", {"drug": "安眠药"}), degraded=False
            )
        ]
    )
    executor = ScriptedExecutor(
        [
            ToolResult(
                status="denied",
                data=None,
                error_class="forbidden",
                message="这个操作需要护工帮忙处理。",
            )
        ]
    )
    agent = _agent(llm, executor)

    result = await agent.run(runtime=_runtime(), turn_id=TURN_ID, user_text="帮我开点安眠药")

    assert result.text == "这个操作需要护工帮忙处理。"
    assert result.degraded is True
    assert result.followup_required is False  # 非高危名单工具：不产出跟进任务
    assert len(executor.calls) == 1  # 不重试


async def test_forbidden_without_message_uses_fallback_text() -> None:
    """forbidden 且执行器未给话术：用兜底模板收尾，不把内部细节回显给长者。"""
    llm = ScriptedLLM(
        [LLMReply(text=_tool_call_text("get_weather", {"city": "杭州"}), degraded=False)]
    )
    executor = ScriptedExecutor(
        [ToolResult(status="failed", data=None, error_class="forbidden", message=None)]
    )
    agent = _agent(llm, executor)

    result = await agent.run(runtime=_runtime(), turn_id=TURN_ID, user_text="杭州天气如何")

    assert result.text == FORBIDDEN_FALLBACK_TEXT
    assert result.degraded is True


# ---------------------------------------------------------------------------
# ⑤ 超预算收尾话术（TD line 834 原文；收口即终止）
# ---------------------------------------------------------------------------


async def test_budget_exhausted_closing_text_is_terminal() -> None:
    """超预算收尾：话术照 TD 原文；拒绝后立即收口，不再向模型追加一轮。"""
    replies = [
        LLMReply(text=_tool_call_text(f"tool_{i}", {"n": i}), degraded=False) for i in range(9)
    ]
    llm = ScriptedLLM(replies)
    executor = ScriptedExecutor([_ok({"i": i}) for i in range(8)])
    agent = _agent(llm, executor)

    result = await agent.run(runtime=_runtime(), turn_id=TURN_ID, user_text="帮我连查九件事")

    assert BUDGET_EXHAUSTED_TEXT == "这个我处理不了，帮您记下来稍后再试"  # TD line 834 原话
    assert result.text == BUDGET_EXHAUSTED_TEXT
    assert len(llm.calls) == 9  # 第 9 次请求被拒后不再有第 10 轮 LLM 调用
    assert len(executor.calls) == 8


# ---------------------------------------------------------------------------
# ⑥ 正常一轮：LLM 直接出文本，不走工具
# ---------------------------------------------------------------------------


async def test_plain_text_reply_without_tools() -> None:
    """LLM 首轮即出自然语言：原样返回，不触发工具调用与事件。"""
    sinks = RecordingSinks()
    llm = ScriptedLLM([LLMReply(text="今天阳光不错，您想吃点什么？", degraded=False)])
    executor = ScriptedExecutor([])
    agent = _agent(llm, executor, sinks)

    result = await agent.run(runtime=_runtime(), turn_id=TURN_ID, user_text="陪我聊聊天")

    assert result.text == "今天阳光不错，您想吃点什么？"
    assert result.degraded is False
    assert result.followup_required is False
    assert executor.calls == []
    assert sinks.events == []
    assert sinks.followups == []
    # 首轮上下文：本轮 user 在末条；get_weather 注册位已进入工具协议说明
    assert llm.calls[0][-1]["role"] == "user"
    assert llm.calls[0][-1]["content"] == "陪我聊聊天"
    assert any("get_weather" in message["content"] for message in llm.calls[0])


def test_default_tools_has_three_entries() -> None:
    """Day 7 C1：DEFAULT_TOOLS 从 1 个扩到 3 个——与 tool_registry.default_registry() 三条 spec 同名对齐。

    companion.py 的 ToolSpec 只有 name + description（给 LLM 看 ReAct prompt）；
    tool_registry 那套有完整 schema + executor（给执行器用）。两边**同名**，不同步。
    """
    from app.agents.companion import DEFAULT_TOOLS
    from app.agents.tool_registry import default_registry

    # 数量：weather + memory_recall + medication_confirm
    assert len(DEFAULT_TOOLS) == 3, f"DEFAULT_TOOLS count={len(DEFAULT_TOOLS)}, expect 3"

    # 名称：三条工具都在
    default_names = {t.name for t in DEFAULT_TOOLS}
    assert default_names == {"get_weather", "memory_recall", "medication_confirm"}, default_names

    # 与 tool_registry.default_registry() 的 spec 同名对齐
    registry_names = set(default_registry().names)
    assert default_names == registry_names, (
        f"companion DEFAULT_TOOLS names mismatch registry: {default_names} vs {registry_names}"
    )


def test_memory_recall_executor_factory_returns_callable() -> None:
    """Day 7 C1：make_memory_recall_executor 工厂返回值是 callable。

    实际调用需要正确的 async_sessionmaker 实例（且 B2 recall_for_turn 没合进来）——
    那个路径已被 tool_impls.py 里的 ImportError 降级覆盖（代码审查可见）。
    本测试只验证工厂构造不崩、返回 callable。
    """
    from contextlib import asynccontextmanager

    from app.services.tool_impls import make_memory_recall_executor

    class _FakeSession:
        pass

    @asynccontextmanager
    async def _session_factory():
        yield _FakeSession()

    fn = make_memory_recall_executor(session_factory=_session_factory)
    assert callable(fn), "factory should return a callable executor"
