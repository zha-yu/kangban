"""Day 10 B3（F23）privacy_scope 信号链与消费点测试。

覆盖三段：
1. 信号产生：shadow 两题、mock 后端真三值语义（personal/shared/none）；
2. Router 透传：`_merge_privacy` 有果才并入 RouteDecision 并写通道，
   degraded / 无 shadow → `None` 不编造；每轮入口重置防跨轮残留；
3. Agent 消费：双人间 ∧ personal → 注入收敛指令；其余情况一律静默跳过。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from app.agents.companion import PRIVACY_DIRECTIVE, CompanionAgent
from app.agents.guardrails import Guardrails
from app.core.runtime_context import Identity
from app.router.decision import Router
from app.services.llm_service import LLMReply
from app.system_one.backend import MockBackend
from app.system_one.privacy_context import (
    current_privacy_scope,
    set_current_privacy_scope,
)
from app.system_one.questions import PRIVACY_QUESTION, build_shadow_questions
from app.system_one.schemas import SystemOneResult, degraded_result

DEVICE_ID = str(uuid.UUID("eeee1111-2222-3333-4444-555555555555"))
ELDER_ID = str(uuid.UUID("aaaa1111-2222-3333-4444-555555555555"))


# ---------------------------------------------------------------------------
# 1. 信号产生
# ---------------------------------------------------------------------------


def test_shadow_questions_now_two() -> None:
    """shadow 扩第二问：is_sensitive + privacy_scope（B3 方案①口径）。"""
    questions = build_shadow_questions()
    assert set(questions) == {"is_sensitive", "privacy_scope"}
    assert questions["privacy_scope"]["criteria"] == PRIVACY_QUESTION["criteria"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我血压有点高", "personal"),  # 含「我」→ personal
        ("我胸口疼得厉害", "personal"),  # 敏感词 → personal
        ("今天天气怎么样", "shared"),  # 公共话题 → shared
        ("晚上有个活动吗", "shared"),
        ("你好呀", "none"),  # 都沾不上 → none（旧实现永远不出 none）
    ],
)
def test_mock_backend_privacy_three_values(text: str, expected: str) -> None:
    backend = MockBackend()
    result = asyncio.run(
        backend.score(state={"asr_final": text}, questions={"privacy_scope": {"type": "choice"}})
    )
    assert result["privacy_scope"] == {"choice": expected}


# ---------------------------------------------------------------------------
# 2. Router 透传
# ---------------------------------------------------------------------------


class FakeSystemOne:
    """可编程桩：分别控制 fast 与 shadow（同 test_router_decision 款式）。"""

    def __init__(
        self,
        fast: SystemOneResult | None = None,
        shadow: SystemOneResult | None = None,
    ) -> None:
        self.fast = fast or SystemOneResult(intent="chat", confidence=0.93)
        self.shadow = shadow

    async def decide_fast(
        self, text: str, *, trace_id: str | None = None, state: Any = None
    ) -> SystemOneResult:
        return self.fast

    async def decide_shadow(
        self, text: str, *, trace_id: str | None = None, state: Any = None
    ) -> SystemOneResult | None:
        return self.shadow


class _Runtime:
    elder_id = ELDER_ID
    current_task = None
    media_status = None
    elder_name = None


async def _decide(system_one: FakeSystemOne) -> Any:
    router = Router(system_one=system_one)
    return await router.decide("陪我聊聊天", thread_id="t-1", runtime=_Runtime())


async def test_router_merges_shadow_privacy_personal() -> None:
    shadow = SystemOneResult(intent="other", confidence=0.0, privacy_scope="personal")
    decision = await _decide(FakeSystemOne(shadow=shadow))

    assert decision.privacy_scope == "personal"
    assert current_privacy_scope() == "personal"


async def test_router_no_shadow_means_privacy_none_not_fabricated() -> None:
    """shadow 缺席（短文本跳过/超预算取消）→ None，不得编造信号。"""
    decision = await _decide(FakeSystemOne(shadow=None))

    assert decision.privacy_scope is None
    assert current_privacy_scope() is None


async def test_router_degraded_shadow_means_privacy_none() -> None:
    """shadow 自身失败（degraded）= 「没问出来」，其默认 none 不得当真信号。"""
    decision = await _decide(FakeSystemOne(shadow=degraded_result()))

    assert decision.privacy_scope is None
    assert current_privacy_scope() is None


async def test_router_shadow_shared_value_flows_through() -> None:
    shadow = SystemOneResult(intent="other", confidence=0.0, privacy_scope="shared")
    decision = await _decide(FakeSystemOne(shadow=shadow))

    assert decision.privacy_scope == "shared"
    assert current_privacy_scope() == "shared"


async def test_privacy_channel_reset_between_turns() -> None:
    """上一轮 personal 不得泄漏进下一轮（decide 入口强制重置）。"""
    await _decide(
        FakeSystemOne(
            shadow=SystemOneResult(intent="other", confidence=0.0, privacy_scope="personal")
        )
    )
    assert current_privacy_scope() == "personal"

    await _decide(FakeSystemOne(shadow=None))
    assert current_privacy_scope() is None


def test_privacy_channel_rejects_unknown_values() -> None:
    """非法值一律落 None：宁可无信号，不传可疑值给消费方。"""
    set_current_privacy_scope(None)
    set_current_privacy_scope("public")  # 不在三值内
    assert current_privacy_scope() is None


# ---------------------------------------------------------------------------
# 3. Agent 消费（双人间收敛注入）
# ---------------------------------------------------------------------------


class OneShotLLM:
    """单轮自然语言回复的假 LLM；记录 messages 供断言。"""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def chat(self, messages: Any) -> LLMReply:
        self.calls.append(list(messages))
        return LLMReply(text="好的，我陪您聊聊。", degraded=False)


class _NoExecutor:
    async def execute(self, **kwargs: Any) -> Any:  # pragma: no cover - 不应被调用
        raise AssertionError("纯文本回复不应触发工具执行")


def _agent(probe: Any) -> CompanionAgent:
    return CompanionAgent(
        llm=OneShotLLM(),
        guardrails=Guardrails(max_tool_calls=8, repeat_window_s=60, repeat_threshold=3),
        tool_executor=_NoExecutor(),
        tools=(),
        privacy_room_probe=probe,
    )


def _device_identity() -> Identity:
    return Identity(kind="device", device_id=DEVICE_ID, elder_id=ELDER_ID)


async def _run_and_capture(agent: CompanionAgent) -> list[dict[str, str]]:
    await agent.run(
        runtime=_device_identity(),
        turn_id="turn-p-1",
        user_text="我今天血压怎么样",
    )
    assert isinstance(agent._llm, OneShotLLM)
    return agent._llm.calls[0]


async def test_injection_when_personal_and_shared_room() -> None:
    async def probe(*, elder_id: str) -> bool:
        assert elder_id == ELDER_ID
        return True

    set_current_privacy_scope("personal")
    messages = await _run_and_capture(_agent(probe))

    assert any(m["content"] == PRIVACY_DIRECTIVE for m in messages)
    # 指令必须在末条 user 之前（system 指令位置语义）
    assert messages[-1]["role"] == "user"


async def test_no_injection_when_room_is_single() -> None:
    """personal 但单人/未知房间 → 不收敛（宁可少收敛，不可误收敛）。"""

    async def probe(*, elder_id: str) -> bool:
        return False

    set_current_privacy_scope("personal")
    messages = await _run_and_capture(_agent(probe))

    assert all(m["content"] != PRIVACY_DIRECTIVE for m in messages)


async def test_no_injection_when_scope_missing() -> None:
    """None（没问出来）≠ "none"（确认无隐私）：无信号一律不注入。"""

    async def probe(*, elder_id: str) -> bool:  # pragma: no cover - 不应被调用
        raise AssertionError("无信号不应触发 DB 探测")

    set_current_privacy_scope(None)
    messages = await _run_and_capture(_agent(probe))

    assert all(m["content"] != PRIVACY_DIRECTIVE for m in messages)


async def test_no_injection_when_shared_scope() -> None:
    async def probe(*, elder_id: str) -> bool:  # pragma: no cover - 不应被调用
        raise AssertionError("shared 信号不应触发 DB 探测")

    set_current_privacy_scope("shared")
    messages = await _run_and_capture(_agent(probe))

    assert all(m["content"] != PRIVACY_DIRECTIVE for m in messages)


async def test_no_injection_when_probe_fails() -> None:
    """probe 抛异常 → 如实降级不注入，不拖垮轮次。"""

    async def probe(*, elder_id: str) -> bool:
        raise RuntimeError("db down")

    set_current_privacy_scope("personal")
    result = await _agent(probe).run(
        runtime=_device_identity(), turn_id="turn-p-2", user_text="我今天血压怎么样"
    )

    assert result.text == "好的，我陪您聊聊。"
    assert result.degraded is False


async def test_no_injection_when_probe_not_wired() -> None:
    """缺省装配（probe=None）= 功能关闭：无 DB 环境零副作用。"""
    agent = CompanionAgent(
        llm=OneShotLLM(),
        guardrails=Guardrails(max_tool_calls=8, repeat_window_s=60, repeat_threshold=3),
        tool_executor=_NoExecutor(),
        tools=(),
    )
    set_current_privacy_scope("personal")
    await agent.run(
        runtime=_device_identity(), turn_id="turn-p-3", user_text="我今天血压怎么样"
    )
    assert isinstance(agent._llm, OneShotLLM)
    assert all(m["content"] != PRIVACY_DIRECTIVE for m in agent._llm.calls[0])
