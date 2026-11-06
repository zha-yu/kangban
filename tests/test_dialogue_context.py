"""Day 4 A2 · 纯上下文组装测试（A2-08 context 部分）。

覆盖：trace API 回归、固定前缀字节稳定、最近 5 轮截断、本轮 user 只追加一次、
generic 无个人称呼/人设/历史、非法身份构造拒绝。全部为纯函数测试，不依赖 DB/Redis。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from app.core.context import (
    DIALOGUE_MAX_ROUNDS,
    FIXED_SYSTEM_PROMPT,
    GENERIC_SYSTEM_PROMPT,
    DialogueIdentity,
    DialogueTurn,
    PersonaContext,
    build_dialogue_messages,
    get_trace_id,
    is_valid_trace_id,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
)


def _personal(display_name: str | None = "张*芬") -> DialogueIdentity:
    return DialogueIdentity(source="voice", elder_id="elder-a", display_name=display_name)


def _history(count: int) -> list[DialogueTurn]:
    return [DialogueTurn(user=f"问题{i}", assistant=f"回答{i}") for i in range(1, count + 1)]


# ---------------------------------------------------------------------------
# trace API 回归（原 behavior 不变）
# ---------------------------------------------------------------------------


def test_trace_api_roundtrip() -> None:
    generated = new_trace_id()
    assert len(generated) == 32

    token = set_trace_id(generated)
    try:
        assert get_trace_id() == generated
    finally:
        reset_trace_id(token)
    assert get_trace_id() is None

    assert is_valid_trace_id("abc-123_XYZ.9") is True
    assert is_valid_trace_id("short") is False
    assert is_valid_trace_id(None) is False
    assert is_valid_trace_id("bad\nvalue") is False


# ---------------------------------------------------------------------------
# 固定前缀与动态上下文分离
# ---------------------------------------------------------------------------


def test_fixed_prefix_is_byte_stable_across_identities_and_time() -> None:
    now = datetime(2026, 11, 6, 9, 30, tzinfo=UTC)
    messages_a = build_dialogue_messages(
        identity=_personal("张*芬"),
        user_text="今天天气怎么样",
        history=_history(3),
        persona=PersonaContext(name="小满", personality="温暖耐心"),
        now=now,
    )
    messages_b = build_dialogue_messages(
        identity=_personal("李*贵"),
        user_text="我想听戏",
        history=[],
        persona=None,
        now=None,
    )

    assert messages_a[0] == {"role": "system", "content": FIXED_SYSTEM_PROMPT}
    assert messages_b[0] == {"role": "system", "content": FIXED_SYSTEM_PROMPT}
    digest_a = hashlib.sha256(messages_a[0]["content"].encode("utf-8")).hexdigest()
    digest_b = hashlib.sha256(messages_b[0]["content"].encode("utf-8")).hexdigest()
    assert digest_a == digest_b

    # 固定前缀不承载任何动态/个人内容
    assert "张*芬" not in FIXED_SYSTEM_PROMPT
    assert "李*贵" not in FIXED_SYSTEM_PROMPT
    assert "2026-11-06" not in FIXED_SYSTEM_PROMPT
    assert "今天天气怎么样" not in FIXED_SYSTEM_PROMPT
    assert "医疗诊断" in FIXED_SYSTEM_PROMPT


def test_persona_and_time_live_in_dynamic_context() -> None:
    now = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    messages = build_dialogue_messages(
        identity=_personal("张*芬"),
        user_text="今天几号",
        persona=PersonaContext(
            name="小满",
            personality="温暖、耐心、爱听戏",
            tone_examples=("奶奶您早", "咱们慢慢说", "我陪您", "第四条不该出现"),
        ),
        now=now,
    )
    dynamic = messages[1]["content"]

    assert "对长者的称呼：张*芬" in dynamic
    assert "人设：小满" in dynamic
    assert "性格：温暖、耐心、爱听戏" in dynamic
    assert "奶奶您早" in dynamic
    assert "第四条不该出现" not in dynamic  # tone_examples 最多 3 条
    # 09:00 UTC → 17:00 Asia/Shanghai
    assert "当前时间：2026-11-06 17:00" in dynamic
    assert "当前时间" not in messages[0]["content"]


def test_no_time_line_when_now_is_none() -> None:
    messages = build_dialogue_messages(
        identity=_personal(),
        user_text="你好",
        now=None,
    )
    assert "当前时间" not in messages[1]["content"]


# ---------------------------------------------------------------------------
# 最近 5 轮与当前 user
# ---------------------------------------------------------------------------


def test_only_recent_five_rounds_expanded_to_ten_messages() -> None:
    messages = build_dialogue_messages(
        identity=_personal(),
        user_text="本轮提问",
        history=_history(10),
    )

    # 2 条 system + 10 条历史 + 1 条本轮 user
    assert len(messages) == 2 + 2 * DIALOGUE_MAX_ROUNDS + 1
    history_messages = messages[2:-1]
    assert [m["role"] for m in history_messages] == ["user", "assistant"] * DIALOGUE_MAX_ROUNDS
    # 只保留最后 5 轮（问题6..10），前 5 轮必须被截断
    assert history_messages[0]["content"] == "问题6"
    assert history_messages[-2]["content"] == "问题10"
    user_contents = [m["content"] for m in history_messages if m["role"] == "user"]
    assert user_contents == [f"问题{i}" for i in range(6, 11)]


def test_current_user_appended_once_at_tail() -> None:
    messages = build_dialogue_messages(
        identity=_personal(),
        user_text="  今天天气怎么样  ",
        history=_history(2),
    )
    assert messages[-1] == {"role": "user", "content": "今天天气怎么样"}
    assert sum(1 for m in messages if m["content"] == "今天天气怎么样") == 1


# ---------------------------------------------------------------------------
# generic 隐私收敛
# ---------------------------------------------------------------------------


def test_generic_ignores_history_persona_and_display_name() -> None:
    identity = DialogueIdentity(source="generic", elder_id=None, display_name="张*芬")
    # generic 即使误传个人称呼也一律丢弃
    assert identity.display_name is None

    messages = build_dialogue_messages(
        identity=identity,
        user_text="我吃过药了",
        history=_history(5),
        persona=PersonaContext(name="小满", personality="温暖耐心"),
        now=datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
    )

    generic_context = messages[1]["content"]
    assert GENERIC_SYSTEM_PROMPT in generic_context
    assert "张*芬" not in generic_context
    assert "小满" not in generic_context
    assert "温暖耐心" not in generic_context
    assert "问题1" not in generic_context
    assert "回答1" not in generic_context
    # 历史整体被忽略：只剩 2 条 system + 本轮 user
    assert len(messages) == 3
    assert messages[-1] == {"role": "user", "content": "我吃过药了"}
    # generic 明确要求健康/用药先确认身份
    assert "先确认身份" in generic_context


def test_generic_with_elder_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="generic identity must not carry elder_id"):
        DialogueIdentity(source="generic", elder_id="elder-a")


@pytest.mark.parametrize("source", ["device", "voice", "confirm"])
def test_personal_identity_requires_elder_id(source: str) -> None:
    with pytest.raises(ValueError, match="requires elder_id"):
        DialogueIdentity(source=source, elder_id=None)  # type: ignore[arg-type]


def test_device_source_is_personal_not_generic() -> None:
    identity = DialogueIdentity(source="device", elder_id="elder-a")
    assert identity.is_generic is False
    assert identity.elder_id == "elder-a"


# ---------------------------------------------------------------------------
# 输入边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_empty_user_text_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="user_text must be non-empty"):
        build_dialogue_messages(identity=_personal(), user_text=text)


@pytest.mark.parametrize(("user", "assistant"), [("", "回答"), ("问题", "  ")])
def test_dialogue_turn_rejects_empty_fields(user: str, assistant: str) -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        DialogueTurn(user=user, assistant=assistant)
