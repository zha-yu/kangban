"""System Two：受约束 CompanionAgent（ReAct + Tool Calling）、工具注册表、预算与死循环熔断（Day 5-6）。

`MAX_TOOL_CALLS_PER_TURN=8`、同一 Tool 1 分钟内第 3 次熔断（PRD FR-DLG-10）。
"""

from app.agents.guardrails import GuardrailDecision, Guardrails

__all__ = ["GuardrailDecision", "Guardrails"]


# ---------------------------------------------------------------------------
# 接口签名冻结（v2026-11-07 · Day 5 dev-B）
# ---------------------------------------------------------------------------
# Guardrails.check 签名冻结如下，Day 6 Agent / Tool 注册表零改动接入：
#
#     def check(self, tool_name: str, args: Any | None = None) -> GuardrailDecision:
#         """判定本次 Tool 调用是否放行。
#
#         Day 6 Agent 注册 Tool 时只需在调用前置插入：
#             decision = guardrails.check(tool_name="weather", args={"city": "北京"})
#             if not decision.allow:
#                 # 告知用户 + 记日志 + 返回
#         """
#
# 冻结意味着：不得新增位置参数、不得改变返回类型、不得改变判定优先级。
# 需要额外维度（session_id、tool_kind、role 等）由调用方自行拼进 args 字典，
# guardrails 本体不感知，内部只做 dict 的 json.dumps 规范化 hash。
# ---------------------------------------------------------------------------
