"""会话级 Tool 调用护栏（Day 5 骨架）。

覆盖 PRD FR-DLG-10 / AGENTS §7 两项硬约束：
- 单轮最多 8 次 Tool 调用（MAX_TOOL_CALLS_PER_TURN）
- 同一 Tool 60s 内第 3 次熔断

骨架只处理**进程内内存**状态，不做跨进程持久化。Agent 进程重启后计数清零
是设计预期（TD §8 Day 5 明确）；重启恢复归 Day 9 audit_logs。

接口签名 **冻结**：`check(tool_name: str, args: Any | None = None) -> GuardrailDecision`
Day 6 Agent / Tool 注册表零改动接入本类。冻结注释见 `app/agents/__init__.py`。
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GuardrailDecision:
    """单步 Tool 调用的护栏判定结果。"""

    allow: bool
    reason: str | None = None


class Guardrails:
    """会话级 Tool 调用护栏实例。

    判定顺序（严格按此优先级；与 tests/test_guardrails.py 对齐）：
    1. 本轮累计调用已达 `max_tool_calls` → Deny（预算优先，先于熔断与同参短路）
    2. 同 Tool 在窗口内已达 `repeat_threshold` → Deny（熔断）
    3. 相同 Tool + 相同参数 → 短路放行（不消耗预算，但仍登记滑动窗，避免重复调用）
    4. 放行，登记本次调用

    注意：同参短路**不绕过**预算与熔断——预算/熔断判定在它之前。
    """

    def __init__(
        self,
        max_tool_calls: int,
        repeat_window_s: int,
        repeat_threshold: int,
    ) -> None:
        self._max_tool_calls = max_tool_calls
        self._repeat_window_s = repeat_window_s
        self._repeat_threshold = repeat_threshold

        # 同 Tool 滑动窗：每个 tool_name 对应一条 deque of (timestamp_monotonic, args_hash)
        self._repeat_windows: dict[str, deque[tuple[float, str]]] = {}
        # 本轮累计 Tool 调用次数（消耗到 max_tool_calls 为止）
        self._turn_total = 0

    # ---------- 内部工具 ----------

    @staticmethod
    def _hash_args(args: Any) -> str:
        """把任意 args 规范化成稳定可 hash 的字符串。

        dict 按 key 排序，list 保序，None → 'none'；其余类型走 repr。
        目标：相同语义的 args 产出相同 hash，不同语义尽量不同。
        """
        if args is None:
            return "none"
        if isinstance(args, dict):
            try:
                return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return repr(args)
        if isinstance(args, (list, tuple)):
            try:
                return json.dumps(list(args), ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return repr(args)
        return repr(args)

    def _evict_expired(self, tool_name: str, now: float) -> None:
        """弹出同 Tool 滑动窗外的旧记录。"""
        q = self._repeat_windows.get(tool_name)
        if q is None:
            return
        cutoff = now - self._repeat_window_s
        while q and q[0][0] < cutoff:
            q.popleft()
        if not q:
            del self._repeat_windows[tool_name]

    # ---------- 对外 API（签名冻结） ----------

    def check(self, tool_name: str, args: Any | None = None) -> GuardrailDecision:
        """判定本次 Tool 调用是否放行。

        Args:
            tool_name: Tool 注册表中的唯一标识符（如 "weather"、"create_work_order"）
            args: 传给 Tool 的结构化参数；可为任意类型，内部做 hash 比较相同参数

        Returns:
            GuardrailDecision(allow=True) 表示放行；allow=False 时 reason 写明拒因

        签名冻结说明：Day 6 Agent 注册 Tool 时只需要在此调用前置插入，
        不得新增参数或改变返回类型。需要额外维度（session_id、tool_kind 等）
        由调用方自行拼进 args 字典里，guardrails 本体不感知。
        """
        now = time.monotonic()
        args_hash = self._hash_args(args)

        # 1. 滑动窗过期清理
        self._evict_expired(tool_name, now)

        q = self._repeat_windows.get(tool_name)

        # 2. 本轮总预算 → 不管参数相同与否，预算满就 Deny
        if self._turn_total >= self._max_tool_calls:
            return GuardrailDecision(
                allow=False,
                reason=f"本轮工具调用已达上限（{self._max_tool_calls}）",
            )

        # 3. 同 Tool 熔断 → 不管参数相同与否，窗口内第 N 次就 Deny
        #    threshold=3 → 第 1、2 次放行，第 3 次熔断（PRD FR-DLG-10 原文）
        current_count = len(q) if q else 0
        if current_count >= self._repeat_threshold - 1:
            return GuardrailDecision(
                allow=False,
                reason=(
                    f"工具 {tool_name!r} 频繁调用已熔断"
                    f"（{self._repeat_window_s}s 内第 {current_count + 1} 次）"
                ),
            )

        # 4. 相同 Tool + 相同参数 → 短路放行（不消耗预算，但仍登记到滑动窗
        #    以便熔断判定正确——熔断看"调用次数"不看"是否消耗预算"）
        is_short_circuit = q is not None and any(
            stored_hash == args_hash for _, stored_hash in q
        )

        if not is_short_circuit:
            self._turn_total += 1

        # 无论是否短路，都登记本次调用到滑动窗
        self._repeat_windows.setdefault(tool_name, deque()).append((now, args_hash))
        return GuardrailDecision(allow=True)

    def reset(self) -> None:
        """会话结束时重置所有计数器。

        Day 6 会话生命周期钩子调用（thread 结束 / HITL 后新 turn）。
        """
        self._turn_total = 0
        self._repeat_windows.clear()
