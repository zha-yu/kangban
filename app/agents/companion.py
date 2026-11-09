"""受约束 CompanionAgent · ReAct Loop（Day 6 N13 · dev-C）。

实现口径（技术设计文档 §6.1 ReAct Loop / §6.4 四类失败；contracts.md §5.3 / §3）：

- 循环：``_build_turn_context``（占位，N06 替换）→ LLM（复用 ``LLMService`` 的
  调用方式，经 ``ChatClient`` 协议注入，不新建 provider）→ LLM 输出工具调用 →
  ``Guardrails.check`` → ``ToolExecutor.execute`` → 结果回灌再思考，直到产出
  文本或触顶；
- **预算与熔断唯一来源是注入的 ``guardrails.Guardrails``**（单轮 8 次、同 Tool
  60s 第 3 次熔断，常量由装配层从 Settings 构造传入）——本模块不定义任何预算 /
  熔断常量（dev-C 计划 S2 自检：grep 不到第二套）；
- **高危不越级**（FR-DLG-04：改药单/关工单/删数据一律拒绝）：高危名单为模块
  常量 ``HIGH_RISK_TOOL_NAMES``，命中即预拦截（不送执行器）；跟进任务以返回
  结构字段 ``AgentTurnResult.followup_required / followup_reason`` 承载（并经
  ``followup_sink`` 回调外送供事件流使用），**本模块不直接写库**（分层红线）——
  真实落 ``work_orders`` 在 N12/N26 接线时做（N14 统一出口串接），此边界以本
  docstring 为准；执行器返回 ``denied`` 的高危工具同样产出跟进任务（双保险）；
- 四类失败回灌（contracts §5.3）：``bad_args`` → 回传模型重试（重试将再次过
  guardrails，计预算）；``not_found`` → 回传模型如实告知不编造；``transient`` →
  同一幂等键退避重试 ≤2 次后降级话术；``forbidden`` → 拒绝 + ``tool_failed``
  事件留痕（审计）；
- 事件回调（``on_event``）产出 ``tool_started`` / ``tool_succeeded`` /
  ``tool_failed``（payload 照 contracts §3），本模块只调回调，落库归
  ``HistoryStore``（N14 接线）；``audit_logs`` 由工具执行器层负责（契约 §5.3）；
- 第 9 次工具调用被 Guardrails 拒 → 告知用户（超预算收尾话术，TD line 834 原话）；
- 工具注册表（桩期）：仅留 ``get_weather`` 注册位（名字照 contracts §5.4 /
  N00 冻结口径）——天气 MCP 调用属 dev-A 的 N26（mcp_client / weather server），
  本模块不实现天气分支，联调时由装配层注入真实执行器，本模块零改动。

桩期说明（先桩后并，C-0-3）：N06（上下文）、N12（工具执行器）未开工——
``ToolResult`` 在本模块按契约 §5.3 逐字定义，``ToolExecutor`` 用 ``Protocol``
注入，联调到达后只换 import / 注入实现，不改循环逻辑。

线协议：LLM 要调工具时只输出一个 JSON 对象 ``{"tool": "工具名", "args": {...}}``；
直接回答时输出普通文本。``_parse_tool_call`` 按此解析，解析不出即视为最终文本。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

from app.agents.guardrails import Guardrails
from app.core.context import (
    DialogueIdentity,
    DialogueTurn,
    PersonaContext,
    build_dialogue_messages,
    get_trace_id,
)
from app.core.runtime_context import Identity
from app.services.llm_service import LLMReply

logger = logging.getLogger("app.agents.companion")

# ---------------------------------------------------------------------------
# 常量（不含任何预算 / 熔断数值——那由注入的 Guardrails 唯一承载）
# ---------------------------------------------------------------------------

#: 超预算 / 护栏拒绝的收尾话术（TD line 834 原话，不得改动）
BUDGET_EXHAUSTED_TEXT: Final[str] = "这个我处理不了，帮您记下来稍后再试"
#: transient 重试耗尽后的降级话术（contracts §5.3：transient → 降级话术）
TRANSIENT_DEGRADED_TEXT: Final[str] = "这个我暂时查不到，帮您记下来"
#: forbidden / denied 且执行器未给话术时的兜底
FORBIDDEN_FALLBACK_TEXT: Final[str] = "这个操作需要护工帮忙处理，我已经把您的需求记下来了。"
#: 高危工具预拦截的播报话术
HIGH_RISK_REFUSED_TEXT: Final[str] = "这件事要医生或护工来处理，我已经帮您把需求转告护工了。"

#: 高危工具名单（FR-DLG-04：Agent 不得自动执行改药单/关工单/删数据）。
#: 桩期口径：实际工具名以 N12 / Day 8 mock-operations 注册为准，联调时对齐；
#: 名单写在代码而非 prompt（AGENTS §5）。
HIGH_RISK_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "update_medication_plan",  # 改药单
        "stop_medication_plan",  # 停药单
        "close_work_order",  # 关工单
        "delete_data",  # 删数据
    }
)

#: transient 退避重试上限（AGENTS §8「临时错误 → 退避重试 ≤2 次后降级话术」；
#: contracts §5.3 同口径）——这是失败重试预算，不是工具调用预算
_TRANSIENT_MAX_RETRIES: Final[int] = 2


async def _transient_backoff(attempt: int) -> None:
    """第 attempt 次 transient 重试前的退避；测试可 monkeypatch 本函数记录次数。"""
    await asyncio.sleep(0.25 * attempt)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具执行结果——contracts.md §5.3 的逐字桩期定义。

    dev-A 的 ``app/services/tool_executor.py`` 落地后应改为从该模块 import
    （字段已对齐契约，切换只改 import，不改循环逻辑）。
    """

    status: Literal["succeeded", "failed", "denied"]
    data: dict[str, object] | None
    error_class: Literal["bad_args", "not_found", "transient", "forbidden"] | None
    message: str | None  # 可直接播报的降级话术
    idempotent_replay: bool = False


class ToolExecutor(Protocol):
    """工具执行器协议——签名照 contracts.md §5.3（N12 未开工，桩注入）。"""

    async def execute(
        self,
        *,
        tool_name: str,
        args: dict[str, object],
        runtime: Identity,
        turn_id: str,
        idempotency_key: str,
    ) -> ToolResult: ...


class ChatClient(Protocol):
    """LLM 客户端协议：与 ``LLMService.chat`` 同签名（生产注入 LLMService）。"""

    async def chat(self, messages: Sequence[Mapping[str, str]]) -> LLMReply: ...


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """注册给 Agent 的工具说明（进入 ReAct 协议 prompt）。"""

    name: str
    description: str


# ---------------------------------------------------------------------------
# 工具注册表（桩期；N12 执行器落地后由装配层按此登记，本模块不写第二套注册逻辑）
# ---------------------------------------------------------------------------

#: N26 天气分支的注册位：工具名与 contracts.md 一致（``get_weather``）。
#: 桩期只登记不实现——MCP 调用属 dev-A 的 N26（mcp_client / weather server），
#: 联调时由装配层把真实执行器接到 :class:`ToolExecutor` 协议上，本模块零改动。
WEATHER_TOOL: Final[ToolSpec] = ToolSpec(
    name="get_weather",
    description="查询城市当前天气；查不到时如实告知长者，不编造。",
)

#: 默认注册表：桩期仅天气一个注册位；用药查询等工具随 N12 执行器落地后追加。
DEFAULT_TOOLS: Final[tuple[ToolSpec, ...]] = (WEATHER_TOOL,)


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    """一轮 Agent 的最终输出。

    ``followup_required`` / ``followup_reason``：高危工具被拒时承载护工跟进任务的
    描述（工具执行器未接入前的载体）；真实落 ``work_orders`` 在 N12/N26 接线时做
    （N14 统一出口串接），本模块不直接写库。
    """

    text: str
    degraded: bool = False
    followup_required: bool = False
    followup_reason: str | None = None


@dataclass(frozen=True, slots=True)
class FollowupTask:
    """高危工具被拒后的护工跟进任务（内存结构）。

    经 ``followup_sink`` 回调产出，**不直接写库**（分层红线，AGENTS §2）；
    由 N14 接线落 ``work_orders``。
    """

    elder_id: str | None
    tool_name: str
    reason: str | None
    turn_id: str


#: 事件回调：``(event_type, payload)``，payload 照 contracts §3；落库归 HistoryStore（N14）
EventSink = Callable[[str, Mapping[str, object]], Awaitable[None]]
#: 跟进任务回调：落库由 N14 接线（写 work_orders）
FollowupSink = Callable[[FollowupTask], Awaitable[None]]

# ---------------------------------------------------------------------------
# 上下文组装（占位实现）与 ReAct 线协议
# ---------------------------------------------------------------------------


def _build_turn_context(
    *,
    runtime: Identity,
    user_text: str,
    history: Sequence[DialogueTurn] = (),
    persona: PersonaContext | None = None,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """[占位实现，将由 dev-B N06 的 ContextBuilder 替换]。

    N06（上下文构建）未开工：本占位参考 ``app.core.context.build_dialogue_messages``
    拼「系统人设 + 最近 5 轮 + 本轮输入」；身份来源统一按 ``device`` 处理（真实现
    应由 A1 的 identity 结论传入）、无记忆召回、无 ``waiting_for`` 任务态注入。
    替换时保持「返回 OpenAI 兼容 messages 列表、末条为本轮 user」的形态即可。
    """
    if runtime.elder_id:
        identity = DialogueIdentity(source="device", elder_id=runtime.elder_id)
    else:
        identity = DialogueIdentity(source="generic")
    return build_dialogue_messages(
        identity=identity,
        user_text=user_text,
        history=history,
        persona=None if identity.is_generic else persona,
        now=now,
    )


def _tool_protocol_message(tools: Sequence[ToolSpec]) -> dict[str, str]:
    """ReAct 输出协议说明（只描述格式，不含任何阈值——阈值在代码里强制）。"""
    listed = "\n".join(f"- {spec.name}：{spec.description}" for spec in tools)
    return {
        "role": "system",
        "content": (
            "你可以按需使用下面这些工具：\n"
            f"{listed}\n"
            "需要用工具时，只输出一个 JSON 对象，格式："
            '{"tool": "工具名", "args": {参数}}，不要输出其他文字。\n'
            "不需要工具时，直接用自然语言回答，不要输出 JSON。\n"
            "工具结果会以「工具执行结果：…」的消息发给你；请根据结果继续回答，"
            "或决定是否再次调用工具。\n"
            "工具查不到的信息要如实告知长者，不要编造。"
        ),
    }


@dataclass(frozen=True, slots=True)
class _ToolCall:
    """从 LLM 输出解析出的一次工具调用意图。"""

    tool: str
    args: dict[str, object]


def _parse_tool_call(text: str) -> _ToolCall | None:
    """把 LLM 输出解析为工具调用；解析不出（非 JSON / 无 tool 字段）视为最终文本。

    容错口径：``tool`` 必须是非空字符串；``args`` 不是 dict 时按空参数处理，
    交由执行器判 ``bad_args``（回灌模型重试），不在解析层吞掉。
    """
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        payload: object = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    tool = payload.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        return None
    raw_args = payload.get("args", {})
    args: dict[str, object] = dict(raw_args) if isinstance(raw_args, dict) else {}
    return _ToolCall(tool=tool.strip(), args=args)


def _observation(call: _ToolCall, result: ToolResult) -> str:
    """工具结果 → 回灌给模型的消息（JSON + 失败类别的行为指引）。"""
    payload: dict[str, object] = {"tool": call.tool, "status": result.status}
    if result.status == "succeeded":
        payload["data"] = result.data
    else:
        payload["error_class"] = result.error_class
        if result.message:
            payload["message"] = result.message

    directive = ""
    if result.error_class == "bad_args":
        directive = " 参数有误，请修正参数后重新调用工具。"
    elif result.error_class == "not_found":
        directive = " 没有查到对应信息，请如实告知长者，不要编造。"
    return "工具执行结果：" + json.dumps(payload, ensure_ascii=False, sort_keys=True) + directive


# ---------------------------------------------------------------------------
# CompanionAgent
# ---------------------------------------------------------------------------


class CompanionAgent:
    """受约束的 ReAct 主控 Agent（技术设计文档 §6.1/§6.2/§6.4）。

    终止条件（循环必然收敛）：
    1. LLM 输出非 JSON 文本 → 作为最终回复返回；
    2. LLM 返回 degraded（LLMService 降级模板）→ 原样返回；
    3. Guardrails 拒绝（单轮预算满 / 同 Tool 熔断）→ 超预算收尾话术；
    4. 高危工具 → 预拦截拒绝 + 跟进任务；
    5. 执行器返回 denied / forbidden / transient 耗尽 → 确定性话术收尾。
    预算上界由注入的 Guardrails 保证：每次放行要么消耗预算槽，要么登记滑动窗
    （同参短路不耗预算但计入熔断窗口），因此不存在无限循环。
    """

    def __init__(
        self,
        *,
        llm: ChatClient,
        guardrails: Guardrails,
        tool_executor: ToolExecutor,
        tools: Sequence[ToolSpec] = (),
        on_event: EventSink | None = None,
        followup_sink: FollowupSink | None = None,
    ) -> None:
        self._llm = llm
        self._guardrails = guardrails
        self._executor = tool_executor
        self._tools = tuple(tools)
        self._on_event = on_event
        self._followup_sink = followup_sink

    async def run(
        self,
        *,
        runtime: Identity,
        turn_id: str,
        user_text: str,
        history: Sequence[DialogueTurn] = (),
        persona: PersonaContext | None = None,
        now: datetime | None = None,
    ) -> AgentTurnResult:
        """跑一轮 ReAct：上下文 → LLM ⇄ 工具 → 最终文本。

        ``runtime`` 是服务端构造的 ``Identity``（工具层只从这里读身份，
        PRD FR-ADM-06 ③）；``history`` / ``persona`` / ``now`` 由调用方
        （N14 统一出口）提供，本方法不读 Redis / DB。
        """
        text = user_text.strip()
        if not text:
            raise ValueError("user_text must be non-empty")

        # 预算是「单轮」口径（MAX_TOOL_CALLS_PER_TURN=8）：无论装配层给的是
        # 新实例还是会话级实例，进入本轮先复位，保证 PRD FR-DLG-10 的每轮上限。
        self._guardrails.reset()

        messages = _build_turn_context(
            runtime=runtime, user_text=text, history=history, persona=persona, now=now
        )
        if self._tools:
            messages.insert(len(messages) - 1, _tool_protocol_message(self._tools))

        while True:
            reply = await self._llm.chat(messages)
            if reply.degraded:
                return AgentTurnResult(text=reply.text, degraded=True)

            call = _parse_tool_call(reply.text)
            if call is None:
                return AgentTurnResult(text=reply.text, degraded=False)

            # 高危预拦截先于预算判定：高危调用根本没有「放行执行」的路径（FR-DLG-04）
            if call.tool in HIGH_RISK_TOOL_NAMES:
                return await self._refuse_high_risk(runtime=runtime, turn_id=turn_id, call=call)

            decision = self._guardrails.check(call.tool, call.args)
            if not decision.allow:
                logger.warning(
                    "react_guardrail_denied",
                    extra={
                        "trace_id": get_trace_id() or "-",
                        "turn_id": turn_id,
                        "tool_name": call.tool,
                        "reason": decision.reason,
                    },
                )
                await self._emit_event(
                    "tool_failed", {"tool_name": call.tool, "error_class": "forbidden"}
                )
                return AgentTurnResult(text=BUDGET_EXHAUSTED_TEXT, degraded=True)

            idempotency_key = uuid.uuid4().hex
            await self._emit_event(
                "tool_started", {"tool_name": call.tool, "idempotency_key": idempotency_key}
            )
            started_at = time.monotonic()
            result = await self._executor.execute(
                tool_name=call.tool,
                args=call.args,
                runtime=runtime,
                turn_id=turn_id,
                idempotency_key=idempotency_key,
            )

            # transient：同一幂等键退避重试 ≤2 次（AGENTS §8）。重试不重复过
            # guardrails——预算/熔断针对模型发起的工具调用次数，同一逻辑调用的
            # 传输级重试不构成新调用（否则熔断会吞掉 ≤2 次重试语义）。
            retries = 0
            while (
                result.status == "failed"
                and result.error_class == "transient"
                and retries < _TRANSIENT_MAX_RETRIES
            ):
                retries += 1
                await _transient_backoff(retries)
                result = await self._executor.execute(
                    tool_name=call.tool,
                    args=call.args,
                    runtime=runtime,
                    turn_id=turn_id,
                    idempotency_key=idempotency_key,
                )
            latency_ms = int((time.monotonic() - started_at) * 1000)

            if result.status == "succeeded":
                await self._emit_event(
                    "tool_succeeded", {"tool_name": call.tool, "latency_ms": latency_ms}
                )
                messages.append({"role": "assistant", "content": reply.text})
                messages.append({"role": "user", "content": _observation(call, result)})
                continue

            error_class = result.error_class or (
                "forbidden" if result.status == "denied" else "transient"
            )
            await self._emit_event(
                "tool_failed", {"tool_name": call.tool, "error_class": error_class}
            )

            if result.status == "denied" or error_class == "forbidden":
                # 拒绝：不重试、不回灌模型自由发挥，按执行器话术或兜底模板收尾。
                # 高危工具被拒（双保险的第二道：N12 也可能先行拒绝）→ 产出跟进任务。
                is_high_risk = call.tool in HIGH_RISK_TOOL_NAMES
                if is_high_risk:
                    await self._emit_followup(
                        runtime=runtime,
                        turn_id=turn_id,
                        tool_name=call.tool,
                        reason=result.message,
                    )
                denied_text = result.message or (
                    HIGH_RISK_REFUSED_TEXT if is_high_risk else FORBIDDEN_FALLBACK_TEXT
                )
                return AgentTurnResult(
                    text=denied_text,
                    degraded=True,
                    followup_required=is_high_risk,
                    followup_reason=result.message if is_high_risk else None,
                )

            if error_class == "transient":
                transient_text = result.message or TRANSIENT_DEGRADED_TEXT
                return AgentTurnResult(text=transient_text, degraded=True)

            # bad_args / not_found：回灌模型（bad_args 的重试将再次过 guardrails，
            # 计入预算；not_found 由模型如实告知，观察消息已带「不要编造」指引）。
            messages.append({"role": "assistant", "content": reply.text})
            messages.append({"role": "user", "content": _observation(call, result)})

    # ------------------------------------------------------------------ 回调

    async def _refuse_high_risk(
        self, *, runtime: Identity, turn_id: str, call: _ToolCall
    ) -> AgentTurnResult:
        """高危工具预拦截：事件留痕 + 跟进任务 + 确定性话术，执行器零调用。"""
        logger.warning(
            "react_high_risk_refused",
            extra={
                "trace_id": get_trace_id() or "-",
                "turn_id": turn_id,
                "tool_name": call.tool,
                "elder_id": runtime.elder_id or "-",
            },
        )
        await self._emit_event("tool_failed", {"tool_name": call.tool, "error_class": "forbidden"})
        reason = f"高危工具 {call.tool} 被 Agent 拒绝执行（FR-DLG-04）"
        await self._emit_followup(
            runtime=runtime,
            turn_id=turn_id,
            tool_name=call.tool,
            reason=reason,
        )
        return AgentTurnResult(
            text=HIGH_RISK_REFUSED_TEXT,
            degraded=True,
            followup_required=True,
            followup_reason=reason,
        )

    async def _emit_event(self, event_type: str, payload: dict[str, object]) -> None:
        """事件只经回调外送（落库归 HistoryStore，N14 接线）；回调故障不拖垮本轮。"""
        if self._on_event is None:
            return
        try:
            await self._on_event(event_type, payload)
        except Exception:
            logger.warning(
                "react_event_sink_failed",
                extra={"trace_id": get_trace_id() or "-", "event_type": event_type},
            )

    async def _emit_followup(
        self, *, runtime: Identity, turn_id: str, tool_name: str, reason: str | None
    ) -> None:
        """跟进任务只经回调外送（N14 落 work_orders）；未接线时 warning 留痕。"""
        if self._followup_sink is None:
            logger.warning(
                "react_followup_sink_missing",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "turn_id": turn_id,
                    "tool_name": tool_name,
                },
            )
            return
        task = FollowupTask(
            elder_id=runtime.elder_id,
            tool_name=tool_name,
            reason=reason,
            turn_id=turn_id,
        )
        try:
            await self._followup_sink(task)
        except Exception:
            logger.warning(
                "react_followup_sink_failed",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "turn_id": turn_id,
                    "tool_name": tool_name,
                },
            )


__all__ = [
    "BUDGET_EXHAUSTED_TEXT",
    "DEFAULT_TOOLS",
    "FORBIDDEN_FALLBACK_TEXT",
    "HIGH_RISK_REFUSED_TEXT",
    "HIGH_RISK_TOOL_NAMES",
    "TRANSIENT_DEGRADED_TEXT",
    "WEATHER_TOOL",
    "AgentTurnResult",
    "ChatClient",
    "CompanionAgent",
    "EventSink",
    "FollowupSink",
    "FollowupTask",
    "ToolExecutor",
    "ToolResult",
    "ToolSpec",
]
