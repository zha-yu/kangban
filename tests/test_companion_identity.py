"""身份结论注入的回归用例（A 侧复核 C 报的 `companion.py` generic 缺陷）。

背景：`_build_turn_context` 原先用 `if runtime.elder_id:` 推断「这次有没有认出人」。
两个概念被混用：

- `runtime: Identity` = 本次连接的**凭证主体**（device / staff / guardian / staff_card）；
- `DialogueIdentity` = 本次会话的**身份结论**（device / voice / confirm / generic）。

`Identity` 对 `staff` / `guardian` 强制 `elder_id=None`，所以员工发起的每一轮都会被判成
generic——不报错、无人设、无历史，NLU 上完全看不出问题。这些用例把两条路径都钉住。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.agents.companion import CompanionAgent, _build_turn_context
from app.agents.guardrails import Guardrails
from app.core.context import DialogueIdentity
from app.core.runtime_context import Identity
from app.services.llm_service import LLMReply

_DEVICE = "11111111-1111-1111-1111-111111111111"
_ELDER = "22222222-2222-2222-2222-222222222222"
_STAFF = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def device_runtime() -> Identity:
    """设备连上来但没认出人（generic 场景的真实来源）。"""
    return Identity(kind="device", device_id=_DEVICE)


@pytest.fixture
def staff_runtime() -> Identity:
    """员工身份：`Identity` 强制 elder_id 为 None。"""
    return Identity(kind="staff", staff_id=_STAFF, role="care_worker")


class _StubLLM:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] | None = None

    async def chat(self, messages: Any) -> LLMReply:
        self.messages = list(messages)
        return LLMReply(text="好的，我记下了。", degraded=False)


class _NoopExecutor:
    async def execute(self, **_kwargs: Any) -> Any:  # pragma: no cover - 本文件不调工具
        raise AssertionError("本用例不应触发工具调用")


def _agent() -> tuple[CompanionAgent, _StubLLM]:
    llm = _StubLLM()
    agent = CompanionAgent(
        llm=llm,
        guardrails=Guardrails(max_tool_calls=8, repeat_window_s=60, repeat_threshold=3),
        tool_executor=_NoopExecutor(),
        tools=(),
    )
    return agent, llm


# ------------------------------------------------------------------ 1. 崩溃已修


def test_generic_branch_no_longer_raises(
    device_runtime: Identity, caplog: pytest.LogCaptureFixture
) -> None:
    """设备连上来但没认出人 → 必须能构建上下文（原先这里 `TypeError`）。"""
    with caplog.at_level(logging.WARNING, logger="app.agents.companion"):
        messages = _build_turn_context(runtime=device_runtime, user_text="你是谁呀")

    assert messages and messages[-1] == {"role": "user", "content": "你是谁呀"}
    assert "companion_identity_inferred" in caplog.text, "推断路径必须留痕，否则无从发现"


# ------------------------------------------------------------------ 2. 员工身份不再静默降级


def test_staff_runtime_with_explicit_identity_keeps_persona(
    staff_runtime: Identity, caplog: pytest.LogCaptureFixture
) -> None:
    """传了身份结论就用它：员工问了「张奶奶今天怎么样」也不该被当成陌生人。"""
    from app.core.context import PersonaContext

    persona = PersonaContext(name="小伴", personality="温和", tone_examples=())
    identity = DialogueIdentity(source="device", elder_id=_ELDER)

    with caplog.at_level(logging.WARNING, logger="app.agents.companion"):
        messages = _build_turn_context(
            runtime=staff_runtime,
            identity=identity,
            user_text="张奶奶今天怎么样",
            persona=persona,
        )

    assert "companion_identity_inferred" not in caplog.text
    # 人设落在第二条 system（`_dynamic_context`），不是固定前缀那条
    system = "".join(m["content"] for m in messages if m["role"] == "system")
    assert "小伴" in system, "明确身份时必须带人设"


def test_staff_runtime_without_identity_is_visible_in_logs(
    staff_runtime: Identity, caplog: pytest.LogCaptureFixture
) -> None:
    """不传身份结论时至少要吵：这条告警就是「员工端静默降智」的唯一线索。"""
    with caplog.at_level(logging.WARNING, logger="app.agents.companion"):
        _build_turn_context(runtime=staff_runtime, user_text="今天吃药了吗")

    assert "companion_identity_inferred" in caplog.text
    assert "runtime_kind=staff" in caplog.text


# ------------------------------------------------------------------ 3. run 透传


async def test_run_forwards_identity(
    staff_runtime: Identity, caplog: pytest.LogCaptureFixture
) -> None:
    """`run(identity=...)` 必须一路透传到上下文构建（否则上面两条都白改）。"""
    agent, llm = _agent()
    persona_name = "小伴"
    from app.core.context import PersonaContext

    with caplog.at_level(logging.WARNING, logger="app.agents.companion"):
        await agent.run(
            runtime=staff_runtime,
            identity=DialogueIdentity(source="device", elder_id=_ELDER),
            turn_id="turn-1",
            user_text="她今天怎么样",
            persona=PersonaContext(name=persona_name, personality="温和", tone_examples=()),
        )

    assert llm.messages is not None
    system = "".join(m["content"] for m in llm.messages if m["role"] == "system")
    assert persona_name in system
    assert "companion_identity_inferred" not in caplog.text
