"""guardrails 骨架单测（假工具，不 import MCP / LangGraph）。

覆盖 dev-b 计划 B1-4 五项判定 + 常量取值断言 + reset()。
计时全部用 `unittest.mock.patch(time.monotonic)` 做确定性注入，
不依赖真实 sleep / wall-clock，零 flaky。
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.agents import GuardrailDecision, Guardrails

# ---------------------------------------------------------------------------
# 测试夹具：默认参数对齐 config.py 常量（AGENTS §7 / PRD FR-DLG-10）
# ---------------------------------------------------------------------------

MAX = 8  # max_tool_calls_per_turn
WINDOW = 60  # tool_repeat_window_seconds
THRESHOLD = 3  # tool_repeat_break_threshold


@pytest.fixture
def gr() -> Guardrails:
    return Guardrails(
        max_tool_calls=MAX,
        repeat_window_s=WINDOW,
        repeat_threshold=THRESHOLD,
    )


# ---------------------------------------------------------------------------
# case 1：第 9 次调用被拒（MAX_TOOL_CALLS_PER_TURN=8）
# ---------------------------------------------------------------------------


def test_max_tool_calls_rejected_on_9th(gr: Guardrails) -> None:
    """同一轮内 8 次 Allow，第 9 次 Deny。"""
    for i in range(8):
        d = gr.check(f"tool_{i}")
        assert d.allow, f"第 {i + 1} 次应放行"

    denied = gr.check("tool_one_more")
    assert denied.allow is False
    assert "上限（8）" in (denied.reason or "")


# ---------------------------------------------------------------------------
# case 2：同 Tool 60s 内第 3 次熔断
# ---------------------------------------------------------------------------


def test_same_tool_breaker_3rd_within_window(gr: Guardrails) -> None:
    """同 tool_name 连续 3 次，第 3 次 Deny。"""
    for _ in range(2):
        d = gr.check("weather", args={"city": "北京"})
        assert d.allow

    denied = gr.check("weather", args={"city": "北京"})
    assert denied.allow is False
    assert "熔断" in (denied.reason or "")


# ---------------------------------------------------------------------------
# case 3：窗口过期后计数复位
# ---------------------------------------------------------------------------


def test_breaker_resets_after_window_expires(gr: Guardrails) -> None:
    """熔断触发后，时间推进到窗口外，下一次又 Allow。"""
    t0 = 1000.0

    with patch.object(time, "monotonic", return_value=t0):
        for _ in range(2):
            gr.check("weather", args={"city": "北京"})
        denied = gr.check("weather", args={"city": "北京"})
        assert denied.allow is False

    # 61 秒后，窗口过期
    with patch.object(time, "monotonic", return_value=t0 + WINDOW + 1):
        allowed = gr.check("weather", args={"city": "北京"})
        assert allowed.allow is True


# ---------------------------------------------------------------------------
# case 4：相同参数短路（不消耗预算）
# ---------------------------------------------------------------------------


def test_same_args_short_circuit_does_not_consume_budget(gr: Guardrails) -> None:
    """相同 tool_name + 相同 args 连续调用，第二次不消耗 _turn_total。

    注意：短路放行**仍然登记到滑动窗**（保证熔断判定正确——熔断看"调用次数"
    不看"是否消耗预算"）。所以 weather 连续调第 3 次不管参数都可能触发熔断，
    短路验证必须在熔断阈值内完成。
    """
    # 用不同 Tool 名绕开 weather 熔断，只验证 _turn_total 不增加
    d1 = gr.check("weather", args={"city": "北京"})
    assert d1.allow
    total_after_first = gr._turn_total

    # 相同参数 → 短路放行，_turn_total 不增
    d2 = gr.check("weather", args={"city": "北京"})
    assert d2.allow
    assert gr._turn_total == total_after_first

    # 换一个 Tool 名继续 → 消耗预算（_turn_total 增加）
    d3 = gr.check("llm_search", args={"query": "今天天气"})
    assert d3.allow
    assert gr._turn_total == total_after_first + 1


# ---------------------------------------------------------------------------
# case 5：常量取值与 PRD / AGENTS §7 文字一致
# ---------------------------------------------------------------------------


def test_constant_values_match_prd() -> None:
    """构造参数断言硬约束值，防私自篡改（AGENTS §7 写死在常量里）。"""
    from app.core.config import get_settings

    s = get_settings()
    assert s.max_tool_calls_per_turn == 8, (
        "PRD FR-DLG-10 / AGENTS §7 写死 MAX_TOOL_CALLS_PER_TURN=8"
    )
    assert s.tool_repeat_window_seconds == 60, "AGENTS §7 写死窗口 60s"
    assert s.tool_repeat_break_threshold == 3, "AGENTS §7 写死熔断阈值 3"


# ---------------------------------------------------------------------------
# 附加：reset() 清空所有状态
# ---------------------------------------------------------------------------


def test_reset_clears_all_state(gr: Guardrails) -> None:
    gr.check("weather", args={"city": "北京"})
    gr.check("llm_search")
    assert gr._turn_total == 2
    assert len(gr._repeat_windows) == 2

    gr.reset()
    assert gr._turn_total == 0
    assert gr._repeat_windows == {}

    # reset 后可以再用
    assert gr.check("weather", args={"city": "北京"}).allow


# ---------------------------------------------------------------------------
# T1（并发安全）：asyncio 协程并发 check（单线程 asyncio 下安全）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_checks_are_safe(gr: Guardrails) -> None:
    """多个协程并发调 check，计数仍正确（asyncio 单线程协程不会在
    Python 字节码之间被中断，纯内存状态机是安全的）。

    关键设计：weather 用**不同 args**（避免短路放行干扰熔断计数），
    并且 weather 协程先并发完再 llm 并发（确定性调度顺序）。
    """
    import asyncio

    async def one_check(tool_name: str, args: dict | None = None) -> GuardrailDecision:
        return gr.check(tool_name, args=args)

    # Phase 1：5 个 weather 协程并发（不同 args 避免短路放行）
    # weather 前 2 次 Allow，第 3 次熔断 Deny，后续 2 次继续 Deny
    weather_tasks = [one_check("weather", args={"city": f"北京_{i}"}) for i in range(5)]
    weather_results = list(await asyncio.gather(*weather_tasks))

    # weather：前 2 次 Allow，后 3 次 Deny（熔断）
    assert sum(1 for r in weather_results if r.allow) == 2
    assert sum(1 for r in weather_results if not r.allow) == 3

    # Phase 2：weather 已熔断，但不同 tool_name 继续放行
    # 注意：熔断是 per-tool 的，所以 Phase 2 用不同 tool_name 避免各自也触发熔断
    phase2_tools = ["llm_search", "weather_query", "api_call"]
    llm_tasks = [one_check(name, args={"query": f"q{i}"}) for i, name in enumerate(phase2_tools)]
    llm_results = list(await asyncio.gather(*llm_tasks))

    # 全部 Allow（每个 tool 只调了 1 次，未达熔断阈值）
    assert all(r.allow for r in llm_results), (
        f"Phase 2 全部应放行，实际 {[r.allow for r in llm_results]}"
    )

    # _turn_total = 2(weather 放行) + 3(Phase 2 放行) = 5
    # 熔断 Deny 的 weather 不扣预算
    assert gr._turn_total == 5, f"_turn_total 应为 5，实际 {gr._turn_total}"


# ---------------------------------------------------------------------------
# T2（熔断不阻塞其他 tool）
# ---------------------------------------------------------------------------


def test_breaker_only_blocks_same_tool(gr: Guardrails) -> None:
    """weather 触发熔断后，llm_search 应该继续放行。"""
    for _ in range(2):
        assert gr.check("weather", args={"city": "北京"}).allow

    # 第 3 次 weather → 熔断 Deny
    denied = gr.check("weather", args={"city": "北京"})
    assert denied.allow is False
    assert "熔断" in (denied.reason or "")

    # 同 tool 后续调用继续 Deny
    assert gr.check("weather", args={"city": "上海"}).allow is False

    # 不同 tool → 继续放行
    assert gr.check("llm_search", args={"query": "今天天气"}).allow
    assert gr.check("tool_c").allow


# ---------------------------------------------------------------------------
# T3（预算优先级高于熔断）
# ---------------------------------------------------------------------------


def test_budget_trumps_breaker(gr: Guardrails) -> None:
    """同一 turn 内预算先耗尽时，即使熔断条件也满足，应该返回预算拒而不是熔断拒。

    优先级在代码里：_turn_total >= max_tool_calls 检查在 current_count >= threshold-1 之前。
    """
    # 用不同 tool 名先消耗掉 8 次预算
    for i in range(8):
        assert gr.check(f"tool_{i}").allow

    # 第 9 次：预算已耗尽，返回预算拒（reason 含"上限"而非"熔断"）
    denied = gr.check("weather", args={"city": "北京"})
    assert denied.allow is False
    assert "上限" in (denied.reason or ""), f"应返回预算拒，实际 reason={denied.reason}"

    # 即使 weather 熔断条件也满足（理论上需要 3 次同 tool 调用），
    # 预算先拒 → 熔断逻辑根本不会走到
    # 再加个熔断验证：reset 后先触发熔断，再检查预算优先级
    gr.reset()

    # 先让 weather 触发熔断
    for _ in range(2):
        gr.check("weather", args={"city": "北京"})
    # 触发熔断
    assert gr.check("weather", args={"city": "北京"}).allow is False

    # reset 后场景：先满预算，再调 weather（weather 还没触发过，不会先触发熔断）
    gr.reset()
    for i in range(8):
        gr.check(f"tool_{i}")

    # weather 只调过 0 次，熔断条件完全不满足；但预算满了 → 预算拒
    denied2 = gr.check("weather", args={"city": "北京"})
    assert denied2.allow is False
    assert "上限" in (denied2.reason or "")
