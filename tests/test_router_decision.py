"""A3 · 路由决策（`app/router/decision.py`）：分层顺序、G1、澄清一次、多意图、G2。

全部用注入的假件（假 System One / 假执行器 / 假升级口），**不加载模型、不连库、不发 HTTP**。
每条用例对应验收总表的一行：

- 规则命中 → System One 调用 0；tool 型规则 → Agent 调用 0；
- ≥0.85 → `system_one`；<0.85 首次 `clarify`、二次 `agent`；
- sidecar 失败 → `fallback`；
- G1 命中 → `escalate`、Agent 0、工具 0；
- 直连工具与 Agent 共用同一执行器（Router 内无第二套权限）。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.router.decision import (
    CLARIFY_TEXT,
    CLARIFY_TEXT_OUT_OF_DOMAIN,
    REASON_SENSITIVE_G1,
    REASON_SIDECAR_FAILED,
    RouteDecision,
    Router,
    split_clauses,
)
from app.system_one.schemas import SystemOneResult, degraded_result

# ------------------------------------------------------------------ 假件


@dataclass
class FakeRuntime:
    elder_id: str | None = "elder-1"
    current_task: str | None = None
    media_status: str | None = None
    identity: Any = None


@dataclass
class FakeSystemOne:
    """可编程的 System One 桩：分别控制快路径与 shadow，并记录调用次数。"""

    fast: SystemOneResult = field(
        default_factory=lambda: SystemOneResult(intent="chat", confidence=0.93)
    )
    shadow: SystemOneResult = field(
        default_factory=lambda: SystemOneResult(intent="other", confidence=0.0)
    )
    fast_error: bool = False
    fast_calls: int = 0
    shadow_calls: int = 0
    seen_states: list[dict[str, Any]] = field(default_factory=list)

    async def decide_fast(self, text: str, *, trace_id: str | None = None, state: Any = None):
        self.fast_calls += 1
        self.seen_states.append(dict(state or {}))
        if self.fast_error:
            raise RuntimeError("boom")
        return self.fast

    async def decide_shadow(self, text: str, *, trace_id: str | None = None, state: Any = None):
        self.shadow_calls += 1
        return self.shadow


@dataclass
class FakeToolResult:
    status: str = "succeeded"
    message: str | None = None
    data: dict[str, Any] | None = None
    error_class: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result: FakeToolResult = FakeToolResult(message="上海现在多云，气温 18 度左右。")

    async def execute(self, **kwargs: Any) -> FakeToolResult:
        self.calls.append(kwargs)
        return self.result


def _router(**kwargs: Any) -> tuple[Router, FakeSystemOne, list[dict[str, Any]]]:
    system_one = kwargs.pop("system_one", FakeSystemOne())
    escalations: list[dict[str, Any]] = []

    async def escalate(**payload: Any) -> bool:
        escalations.append(payload)
        return True

    router = Router(
        system_one=system_one,
        escalate=kwargs.pop("escalate", escalate),
        was_clarified=kwargs.pop("was_clarified", None),
        tool_executor_factory=kwargs.pop("tool_executor_factory", None),
        **kwargs,
    )
    return router, system_one, escalations


# ------------------------------------------------------------------ 规则层优先


async def test_rule_hit_skips_system_one_and_agent() -> None:
    """规则命中时 System One 一次都不该调（验收：命中时 System One 调用 = 0）。"""
    router, system_one, _ = _router()

    decision = await router.decide("暂停一下", thread_id="th-1", runtime=FakeRuntime())

    assert decision.route_layer == "rule"
    assert decision.action == "agent"  # 媒体库未就绪 → 能力下沉
    assert decision.intent == "opera"
    assert decision.reason == "capability_not_ready"
    assert system_one.fast_calls == 0
    assert system_one.shadow_calls == 0
    assert decision.latency_ms >= 0


async def test_weather_rule_routes_direct_tool() -> None:
    executor = FakeExecutor()
    router, system_one, _ = _router(tool_executor_factory=lambda: executor)

    decision = await router.decide("上海明天天气怎么样", thread_id="th-1", runtime=FakeRuntime())

    assert decision.route_layer == "rule"
    assert decision.action == "tool"
    assert decision.tool_name == "get_weather"
    assert decision.tool_args == {"city": "上海"}
    assert decision.escalated_to_s2 is False
    assert system_one.fast_calls == 0

    result = await router.execute_tool(decision, runtime=FakeRuntime(), turn_id="turn-1")
    assert result is not None and result.ok
    assert executor.calls[0]["tool_name"] == "get_weather"
    assert executor.calls[0]["idempotency_key"] == "turn-1:get_weather:1"


# ------------------------------------------------------------------ System One 分层


async def test_high_confidence_goes_system_one() -> None:
    router, _, _ = _router(
        system_one=FakeSystemOne(fast=SystemOneResult(intent="chat", confidence=0.93))
    )

    decision = await router.decide("我今天心里挺高兴的", thread_id="th-1", runtime=FakeRuntime())

    assert decision.route_layer == "system_one"
    assert decision.intent == "chat"
    assert decision.action == "agent"
    assert decision.escalated_to_s2 is True


async def test_low_confidence_action_intent_clarifies_once_then_agents() -> None:
    """动作型意图（这里用 weather）低置信时才澄清——澄清是为防错执行工具。"""
    flagged: list[str] = []

    async def was_clarified(thread_id: str) -> bool:
        return thread_id in flagged

    router, system_one, _ = _router(
        system_one=FakeSystemOne(fast=SystemOneResult(intent="weather", confidence=0.42)),
        was_clarified=was_clarified,
    )

    first = await router.decide("明天出门穿什么好", thread_id="th-1", runtime=FakeRuntime())
    assert first.action == "clarify"
    assert first.clarify_text == CLARIFY_TEXT
    assert first.escalated_to_s2 is False

    flagged.append("th-1")  # 同一 thread 已澄清过
    second = await router.decide("明天出门穿什么好", thread_id="th-1", runtime=FakeRuntime())
    assert second.action == "agent"
    assert second.escalated_to_s2 is True
    assert second.reason is not None and second.reason.endswith("clarified_once")
    assert system_one.fast_calls == 2


@pytest.mark.parametrize("intent", ["chat", "other"])
async def test_low_confidence_chat_and_other_never_clarify(intent: str) -> None:
    """方案 B（2026-11-10 主理人裁决）：闲聊/其他低置信直接下沉，不再反问一句。

    理由：澄清只为防错执行工具；chat/other 不映射任何工具，多问只会让长辈重复一遍
    （实测 50 条普通样本澄清率 28%，其中 7 条其实判对了）。
    """
    asked: list[str] = []

    async def was_clarified(thread_id: str) -> bool:
        asked.append(thread_id)
        return False

    router, _, _ = _router(
        system_one=FakeSystemOne(fast=SystemOneResult(intent=intent, confidence=0.31)),
        was_clarified=was_clarified,
    )

    decision = await router.decide("我今天有点闷", thread_id="th-1", runtime=FakeRuntime())

    assert decision.action == "agent"
    assert decision.clarify_text is None
    assert decision.escalated_to_s2 is True
    assert decision.reason == "low_confidence:no_clarify_intent"
    # 关键：连「是否澄清过」都不用查（少一次 DB 往返），也不占用只澄清一次的额度
    assert asked == []


async def test_out_of_domain_still_clarifies_even_for_other_intent() -> None:
    """域外是话术问题不是执行问题：即便 intent=other，也要澄清一次（域外话术）。"""
    router, _, _ = _router(
        system_one=FakeSystemOne(
            fast=SystemOneResult(intent="other", confidence=0.2, is_out_of_domain=True)
        )
    )

    decision = await router.decide("帮我看看这段代码", thread_id="th-1", runtime=FakeRuntime())

    assert decision.action == "clarify"
    assert decision.clarify_text == CLARIFY_TEXT_OUT_OF_DOMAIN


async def test_out_of_domain_uses_its_own_clarify_text() -> None:
    router, _, _ = _router(
        system_one=FakeSystemOne(
            fast=SystemOneResult(intent="other", confidence=0.9, is_out_of_domain=True)
        )
    )

    decision = await router.decide("帮我写一段 Python 爬虫", thread_id="th-1", runtime=FakeRuntime())

    assert decision.action == "clarify"
    assert decision.clarify_text == CLARIFY_TEXT_OUT_OF_DOMAIN


async def test_sidecar_failure_falls_back_to_agent() -> None:
    router, _, _ = _router(system_one=FakeSystemOne(fast=degraded_result()))

    decision = await router.decide("我今天想吃点面条", thread_id="th-1", runtime=FakeRuntime())

    assert decision.route_layer == "fallback"
    assert decision.action == "agent"
    assert decision.reason == REASON_SIDECAR_FAILED
    assert decision.escalated_to_s2 is True


async def test_system_one_raised_is_treated_as_fallback() -> None:
    """客户端承诺不抛异常；即便抛了，路由也必须落到 fallback 而不是崩掉这一轮。"""
    router, _, _ = _router(system_one=FakeSystemOne(fast_error=True))

    decision = await router.decide("我今天想吃点面条", thread_id="th-1", runtime=FakeRuntime())

    assert decision.route_layer == "fallback"


async def test_disabled_router_goes_fallback() -> None:
    router, system_one, _ = _router(enabled=False)

    decision = await router.decide("我今天想吃点面条", thread_id="th-1", runtime=FakeRuntime())

    assert decision.route_layer == "fallback"
    assert system_one.fast_calls == 0


# ------------------------------------------------------------------ G1 安全复核


async def test_shadow_sensitive_escalates_without_agent_or_tool() -> None:
    executor = FakeExecutor()
    router, _, escalations = _router(
        system_one=FakeSystemOne(
            fast=SystemOneResult(intent="weather", confidence=0.95),
            shadow=SystemOneResult(
                intent="other", confidence=0.0, is_sensitive=True, sensitive_probability=0.91
            ),
        ),
        tool_executor_factory=lambda: executor,
    )

    # 刻意选用规则层接不住的说法（"冷不冷"会被 R-09 接走，那样测不到 G1）
    decision = await router.decide("明天出门用不用加件外套", thread_id="th-1", runtime=FakeRuntime())

    assert decision.action == "escalate"
    assert decision.is_sensitive is True
    assert decision.tool_name is None
    assert decision.reason == REASON_SENSITIVE_G1
    assert escalations and escalations[0]["probability"] == pytest.approx(0.91)
    # 敏感命中后不得执行工具
    assert await router.execute_tool(decision, runtime=FakeRuntime(), turn_id="t-1") is None
    assert executor.calls == []


async def test_escalate_not_wired_still_escalates() -> None:
    """升级口未装配时保守按敏感处理：action 仍是 escalate，不降级成普通意图。"""
    router, _, _ = _router(
        escalate=None,
        system_one=FakeSystemOne(
            fast=SystemOneResult(intent="chat", confidence=0.9),
            shadow=SystemOneResult(
                intent="other", confidence=0.0, is_sensitive=True, sensitive_probability=0.88
            ),
        ),
    )

    decision = await router.decide("我心里难受得很", thread_id="th-1", runtime=FakeRuntime())

    assert decision.action == "escalate"
    assert decision.is_sensitive is True


# ------------------------------------------------------------------ 多意图


def test_split_clauses_keeps_single_intent_whole() -> None:
    assert split_clauses("换一段，再大声点") == ("换一段", "再大声点")
    assert split_clauses("我爱看戏") == ("我爱看戏",)
    assert split_clauses("") == ()


async def test_multi_intent_same_intent_merges_to_one_action() -> None:
    router, system_one, _ = _router()

    decision = await router.decide("换一段，再大声点", thread_id="th-1", runtime=FakeRuntime())

    assert decision.multi_intent is True
    assert decision.sub_intents == ("opera", "opera")
    assert decision.route_layer == "rule"
    assert system_one.fast_calls == 0


async def test_multi_intent_different_intents_goes_agent() -> None:
    router, _, _ = _router()

    decision = await router.decide("换一段戏，顺便问问今天天气", thread_id="th-1", runtime=FakeRuntime())

    assert decision.multi_intent is True
    assert decision.action == "agent"
    assert decision.reason == "multi_intent_dynamic"
    assert set(decision.sub_intents) == {"opera", "weather"}


# ------------------------------------------------------------------ 契约不变量 / 分层纪律


async def test_decision_invariants_hold_across_actions() -> None:
    executor = FakeExecutor()
    router, _, _ = _router(tool_executor_factory=lambda: executor)
    runtime = FakeRuntime()

    for text in (
        "上海明天天气怎么样",
        "暂停一下",
        "我今天有点闷",
        "换一段，再大声点",
    ):
        decision = await router.decide(text, thread_id="th-1", runtime=runtime)
        assert isinstance(decision, RouteDecision)
        if decision.action != "tool":
            assert decision.tool_name is None
        if decision.action == "clarify":
            assert decision.clarify_text
        if decision.action == "escalate":
            assert decision.is_sensitive and decision.tool_name is None
        assert decision.route_layer in {"rule", "system_one", "agent", "fallback"}


async def test_state_carries_current_task_and_media_status() -> None:
    """3 个实测错例都因 state 缺上下文（"换一段""声音小一点""今天几号"）。"""
    system_one = FakeSystemOne()
    router, _, _ = _router(system_one=system_one)

    # 用规则层接不住的说法（"声音小一点"会被 R-07 接走，压根不进 System One）
    await router.decide(
        "今天几号了",
        thread_id="th-1",
        runtime=FakeRuntime(current_task="opera_playback", media_status="playing"),
    )
    state = system_one.seen_states[0]
    assert state["current_task"] == "opera_playback"
    assert state["media_status"] == "playing"


def test_decision_module_has_no_db_http_or_executor_import() -> None:
    """G2 证据：Router 内没有第二套权限判断/HTTP/DB —— 直连工具只能经注入的执行器。"""
    tree = ast.parse(Path("app/router/decision.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = ("httpx", "app.models", "app.services.tool_executor", "app.mcp_client", "sqlalchemy")
    assert not [name for name in imported if name.startswith(forbidden)], sorted(imported)
