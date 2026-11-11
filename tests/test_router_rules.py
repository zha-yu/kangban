"""A3 · 规则层（`app/router/rules.py`）：条数上限、正负例、否定护栏、纯函数。

验收口径（计划 §6.6）：
- 规则 ≤10 条；
- **每条至少 1 正例 / 1 负例**（表驱动，新增规则不写例子就红）；
- 「不用停」这类否定句必须证明不误命中（误命中 = 直接执行错动作）；
- 命中时不该有任何 IO/模型调用 → 用源码断言钉死「无 httpx/session」。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.router.rules import (
    REASON_MISSING_SLOT,
    RULESET,
    RULESET_VERSION,
    Rule,
    extract_city,
    match_rule,
)

#: 规则 → (正例, 负例)。负例是本条规则最容易误伤的说法，不是随便找一句无关的话。
CASES: dict[str, tuple[str, str]] = {
    "R-04": ("你先别说话了，听我说", "我没有打断你的意思"),
    "R-01": ("戏曲先暂停一下", "不用暂停，接着放吧"),
    "R-02": ("继续播放刚才那段", "我不想继续听了"),
    "R-03": ("把收音机关掉", "不用停，我还听着呢"),
    "R-05": ("换一段听听", "这衣服怎么换颜色"),
    "R-06": ("声音太小了，大一点声", "声音不大也不小，正好"),
    "R-07": ("太吵了，小声点", "这戏真好看"),
    "R-08": ("再放一遍刚才那段", "我知道了，不用重复"),
    "R-09": ("北京今天冷不冷", "天气不错，咱们出去走走吧"),
    "R-10": ("我这药还剩多少", "我不想吃药"),
}


def test_ruleset_is_capped_and_versioned() -> None:
    assert len(RULESET) <= 10, "规则层超过 10 条就变成第二个分类器了（会没人 review）"
    assert len(RULESET) == len({rule.rule_id for rule in RULESET}), "rule_id 必须唯一"
    assert RULESET_VERSION == "r2"


def test_every_rule_has_positive_and_negative_case() -> None:
    """新增规则必须同时补正例与负例——否则这条规则没人知道它会误伤什么。"""
    missing = [rule.rule_id for rule in RULESET if rule.rule_id not in CASES]
    assert not missing, f"缺少正/负例的规则：{missing}"


@pytest.mark.parametrize("rule", RULESET, ids=[rule.rule_id for rule in RULESET])
def test_rule_positive_case_hits(rule: Rule) -> None:
    positive, _ = CASES[rule.rule_id]
    hit = match_rule(positive)

    assert hit is not None, f"{rule.rule_id} 正例未命中：{positive}"
    assert hit.rule_id == rule.rule_id


@pytest.mark.parametrize("rule", RULESET, ids=[rule.rule_id for rule in RULESET])
def test_rule_negative_case_does_not_hit_this_rule(rule: Rule) -> None:
    """负例不该被**本条**命中（允许被别的规则认领，那是规则间的优先级问题）。"""
    _, negative = CASES[rule.rule_id]
    hit = match_rule(negative)

    assert hit is None or hit.rule_id != rule.rule_id, f"{rule.rule_id} 负例误命中：{negative}"


@pytest.mark.parametrize(
    "text",
    ["不用停", "别暂停", "先不要换一段", "我不想继续了", "不用重复了"],
)
def test_negated_commands_never_hit(text: str) -> None:
    """否定护栏：长辈说「不用停」时，规则层**绝不能**发出停止播放的动作。"""
    hit = match_rule(text)
    assert hit is None, f"{text} 被误判成 {hit.rule_id if hit else None}"


def test_weather_rule_extracts_city_and_becomes_direct_tool() -> None:
    hit = match_rule("上海明天天气怎么样")

    assert hit is not None
    assert hit.intent == "weather"
    assert hit.action == "tool"
    assert hit.tool_name == "get_weather"
    assert hit.tool_args == {"city": "上海"}


def test_weather_without_city_degrades_to_agent_missing_slot() -> None:
    """抽不到城市就不直连：`get_weather` 的 city 必填，猜一个城市等于编数据。"""
    hit = match_rule("今天天气怎么样")

    assert hit is not None
    assert hit.action == "agent"
    assert hit.reason == REASON_MISSING_SLOT
    assert hit.tool_name is None


def test_extract_city_returns_none_when_absent() -> None:
    assert extract_city("今天天气怎么样") is None
    assert extract_city("杭州冷不冷") == "杭州"


def test_playback_rules_are_direct_tool_to_play_opera() -> None:
    """Day 8 A5：播放控制 8 条规则从 capability_not_ready 降级升级为直连 play_opera。

    等 B4 Tool 注册后经 ToolExecutor 调通；骨架先行，规则层已就位。
    """
    expected = {
        "暂停一下": ("R-01", "pause"),
        "换一段": ("R-05", "next"),
        "再大声点": ("R-06", "volume_up"),
        "再放一遍": ("R-08", "repeat"),
    }
    for text, (rule_id, action) in expected.items():
        hit = match_rule(text)
        assert hit is not None
        assert hit.rule_id == rule_id
        assert hit.action == "tool"
        assert hit.tool_name == "play_opera"
        assert hit.tool_args == {"action": action}


def test_empty_text_matches_nothing() -> None:
    assert match_rule("") is None
    assert match_rule("   ") is None


def test_rules_module_is_pure_no_io_no_db() -> None:
    """纯函数验收：规则层的 **import 图**里出现 httpx/models/config 就说明它开始"顺手查一下"了。

    用 AST 看 import 而不是 grep 全文：文档字符串里写着 `grep -nE "httpx..."` 这条纪律本身，
    全文匹配会把它自己也判成违规。
    """
    tree = ast.parse(Path("app/router/rules.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = ("httpx", "app.models", "app.core.config", "app.services", "sqlalchemy")
    assert not [name for name in imported if name.startswith(forbidden)], (
        f"rules.py 引入了不该有的依赖：{sorted(imported)}"
    )
