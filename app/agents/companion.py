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
from app.core.metrics import Stopwatch, record_stage
from app.core.runtime_context import Identity
from app.services.llm_service import LLMReply
from app.system_one.privacy_context import SharedRoomProbe, current_privacy_scope

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

MEMORY_RECALL_TOOL: Final[ToolSpec] = ToolSpec(
    name="memory_recall",
    description="查询这位长者此前说过的偏好与事实（比如喜欢什么戏曲、儿女叫什么名字），用于个性化回应；查不到时返回空结果。",
)

MEDICATION_CONFIRM_TOOL: Final[ToolSpec] = ToolSpec(
    name="medication_confirm",
    description="记录长者对服药提醒的回答（吃了 / 没吃 / 稍后），推进用药任务状态。",
)

# Day 8 B4（dev-B）：mock-operations 两工具 + 戏曲点播，与 tool_registry 同步登记
WORK_ORDER_TOOL: Final[ToolSpec] = ToolSpec(
    name="create_work_order",
    description="为这位长者创建照护工单（Mock 落库），如上报异常、请求人工跟进。",
)

FAMILY_NOTIFICATION_TOOL: Final[ToolSpec] = ToolSpec(
    name="send_family_notification",
    description="给这位长者的家属发送通知（Mock 落库），如健康异常通报、日常报平安。",
)

PLAY_OPERA_TOOL: Final[ToolSpec] = ToolSpec(
    name="play_opera",
    description="按剧目或剧种检索戏曲并返回播放地址，给长者点播戏曲选段；查不到时如实告知。",
)

# Day 9 B3（dev-B）：健康采集五问（F14），与 tool_registry 的 health_checkup 同名
HEALTH_CHECKUP_TOOL: Final[ToolSpec] = ToolSpec(
    name="health_checkup",
    description=(
        "长者主动说身体不舒服、要量血压或做每日健康采集时，进入健康采集五问。"
        "首次调用不传 answer（返回第一问）；之后把长者的回答放进 answer 继续。"
    ),
)

#: 默认工具清单：七个 —— 天气（MCP）+ 用药确认 + 记忆召回 + 工单/家属通知（MCP，
#: Day 8 B4）+ 戏曲点播（本地，Day 8 B4）+ 健康采集（本地，Day 9 B3）。
#: 与 tool_registry.default_registry() 里的 ToolSpec 七条**同名**（两边独立定义，
#: companion.py 这一套只有 name + description，服务于 ReAct prompt 描述；
#: tool_registry 那套有完整 schema + executor，服务于实际执行）。
DEFAULT_TOOLS: Final[tuple[ToolSpec, ...]] = (
    WEATHER_TOOL,
    MEMORY_RECALL_TOOL,
    MEDICATION_CONFIRM_TOOL,
    WORK_ORDER_TOOL,
    FAMILY_NOTIFICATION_TOOL,
    PLAY_OPERA_TOOL,
    HEALTH_CHECKUP_TOOL,
)


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
#: 戏曲播放下行回调：``(device_id, payload)``，装配层接 signaling.deliver（D4/X8）
MediaPlaySink = Callable[[str, Mapping[str, object]], Awaitable[None]]
#: 播放控制下行回调：``(device_id, {"action": ...})`` → media_control 帧（D4/X8 ③）
MediaControlSink = Callable[[str, Mapping[str, object]], Awaitable[None]]

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
    identity: DialogueIdentity | None = None,
) -> list[dict[str, str]]:
    """[占位实现，将由 dev-B N06 的 ContextBuilder 替换]。

    N06（上下文构建）未开工：本占位参考 ``app.core.context.build_dialogue_messages``
    拼「系统人设 + 最近 5 轮 + 本轮输入」；身份来源统一按 ``device`` 处理（真实现
    应由 A1 的 identity 结论传入）、无记忆召回、无 ``waiting_for`` 任务态注入。
    替换时保持「返回 OpenAI 兼容 messages 列表、末条为本轮 user」的形态即可。
    """
    if identity is None:
        # 兜底：**从凭证主体推断身份结论是不可靠的**，只为本函数单独可调用而保留。
        # 生产路径（N14 统一出口）必须显式传入 identity（A1 IdentityDecision 产出）。
        #
        # 推断规则（按 runtime.kind 区分，原来的二元推断把 staff/guardian 全静默判 generic）：
        # - device + elder_id present → device（长者端正常）
        # - device + elder_id missing → generic（设备还没识别到长者）
        # - staff / staff_card → generic（员工端不该走这个 Agent，但真走了也不崩）
        # - guardian → generic（家属端同理）
        if runtime.kind == "device":
            inferred_source: Literal["device", "generic"] = (
                "device" if runtime.elder_id else "generic"
            )
        else:
            # 非 device 凭证（staff/guardian/staff_card）→ 一律 generic
            inferred_source = "generic"
        identity = DialogueIdentity(
            source=inferred_source,
            elder_id=runtime.elder_id if inferred_source != "generic" else None,
        )
        logger.warning(
            "companion_identity_inferred runtime_kind=%s inferred_source=%s elder_present=%s",
            runtime.kind,
            inferred_source,
            bool(runtime.elder_id),
        )
    return build_dialogue_messages(
        identity=identity,
        user_text=user_text,
        history=history,
        persona=None if identity.is_generic else persona,
        now=now,
    )


#: Day 10 B3（F23）：双人间隐私收敛指令（PRD「privacy_scope 用于隐私收敛」的消费落点）。
#: 触发条件 = Router 本轮 shadow 判 `personal`（本人私密内容）∧ 长者当前房间 ≥2 人在住。
#: 只注入话术约束，不拦截/改写生成文本——硬拦截对 LLM 输出做手术风险高（方案①口径）。
PRIVACY_DIRECTIVE: Final[str] = (
    "（隐私提醒）长者住在多人间，说话时室友可能就在旁边。"
    "这句话涉及本人健康、用药或身体等私密内容，回复时请注意："
    "不要复述具体的健康数值或敏感细节，用关心的语气简短回应；"
    "如需详细讨论，建议长者和护工或家属单独说。"
)


def _tool_protocol_message(tools: Sequence[ToolSpec]) -> dict[str, str]:
    """ReAct 输出协议说明（只描述格式，不含任何阈值——阈值在代码里强制）。

    Day 8 追加 few-shot：实测 minimax 对 play_opera 偶发自创 ``title`` 参数
    （schema 只有 ``action``/``query``，bad_args 烧额度，同 Tool 60s 内 3 次即
    触发熔断 → 长者听到「这个我处理不了」）。示例锚定 play_opera 的正确入参
    形状与「参数被拒按 schema 修正后重试」的行为，把首调错参率打下去；
    示例只描述入参形状，不新增 schema 字段（X8 口径② 冻结契约不动）。
    """
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
            "工具查不到的信息要如实告知长者，不要编造。\n"
            "\n"
            "示例（戏曲点播 play_opera）：检索词必须放进 args 的 query 字段，"
            "不要自创参数名。例如长者说「我要听茉莉雨」，输出：\n"
            '{"tool": "play_opera", "args": {"action": "play", "query": "茉莉雨"}}\n'
            '若工具结果提示 unexpected parameters（如 ["title"]），说明参数名传错了，'
            "请对照上面示例改成 query 后重试一次，不要换别的参数名。"
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
        on_media_play: MediaPlaySink | None = None,
        on_media_control: MediaControlSink | None = None,
        #: Day 10 B3（F23）：双人间判定 ``(elder_id) -> bool``。生产装配层注
        #: `privacy_context.is_shared_room_for_elder`；缺省 None = 不做收敛注入
        #: （测试/无 DB 环境零副作用）。
        privacy_room_probe: SharedRoomProbe | None = None,
    ) -> None:
        self._llm = llm
        self._guardrails = guardrails
        self._executor = tool_executor
        self._tools = tuple(tools)
        self._on_event = on_event
        self._followup_sink = followup_sink
        self._on_media_play = on_media_play
        self._on_media_control = on_media_control
        self._privacy_room_probe = privacy_room_probe

    async def _maybe_inject_privacy_directive(
        self,
        messages: list[dict[str, str]],
        *,
        runtime: Identity,
        identity: DialogueIdentity | None,
    ) -> None:
        """双人间收敛注入（Day 10 B3 · F23 消费点，主理人批准的方案①）。

        全部前置任一不满足即静默跳过：通道无信号（None=没问出来）、信号非
        `personal`、generic 身份、未装配 probe。命中时 probe 查一次 DB，
        查失败记 warning 不收敛——**宁可少收敛，不可误收敛**。
        """
        scope = current_privacy_scope()
        if scope != "personal" or self._privacy_room_probe is None:
            return
        elder_id = (identity.elder_id if identity is not None else runtime.elder_id) or None
        is_generic = identity.is_generic if identity is not None else elder_id is None
        if is_generic or not elder_id:
            return
        try:
            shared_room = await self._privacy_room_probe(elder_id=elder_id)
        except Exception as exc:
            logger.warning(
                "privacy_probe_failed_skip_directive elder_present=%s error_type=%s",
                bool(elder_id),
                type(exc).__name__,
            )
            return
        if not shared_room:
            return
        logger.info("privacy_directive_injected scope=personal room=shared")
        # 与工具协议消息同样的位置语义：末条 user 之前的 system 指令
        messages.insert(len(messages) - 1, {"role": "system", "content": PRIVACY_DIRECTIVE})

    async def run(
        self,
        *,
        runtime: Identity,
        turn_id: str,
        user_text: str,
        history: Sequence[DialogueTurn] = (),
        persona: PersonaContext | None = None,
        now: datetime | None = None,
        identity: DialogueIdentity | None = None,
    ) -> AgentTurnResult:
        """跑一轮 ReAct：上下文 → LLM ⇄ 工具 → 最终文本。

        ``runtime`` 是服务端构造的 ``Identity``（工具层只从这里读身份，
        PRD FR-ADM-06 ③）；``history`` / ``persona`` / ``now`` 由调用方
        （N14 统一出口）提供，本方法不读 Redis / DB。

        ``identity`` 是**本次会话的身份结论**（A1 `IdentityDecision` 的产物，
        见 `identity_decision_to_result()`），决定人设、历史与记忆开关。
        它与 ``runtime`` 是两个概念：前者说「这次认出来是谁」，后者说「哪个凭证在连」。
        不传时按 `runtime.elder_id` 推断并打 `companion_identity_inferred` 告警——
        员工/家属身份的 `elder_id` 恒为 None，推断结果会退化成 generic。
        """
        text = user_text.strip()
        if not text:
            raise ValueError("user_text must be non-empty")

        # 预算是「单轮」口径（MAX_TOOL_CALLS_PER_TURN=8）：无论装配层给的是
        # 新实例还是会话级实例，进入本轮先复位，保证 PRD FR-DLG-10 的每轮上限。
        self._guardrails.reset()

        messages = _build_turn_context(
            runtime=runtime,
            user_text=text,
            history=history,
            persona=persona,
            now=now,
            identity=identity,
        )
        if self._tools:
            messages.insert(len(messages) - 1, _tool_protocol_message(self._tools))

        # Day 10 B3（F23）：privacy_scope 消费点——双人间 ∧ 本轮 shadow 判 personal
        # → 注入收敛指令。信号经 `privacy_context` 通道（Router 同 task 写入）；
        # 判定惰性：只有信号命中才查一次 DB（路由 200ms 热路径零查询）。
        # probe 失败不收敛（如实降级，不拖垮轮次）；generic 身份无个人上下文，不注入。
        await self._maybe_inject_privacy_directive(messages, runtime=runtime, identity=identity)

        # Day 6 补回 llm_first_token 打点：原在 dialogue_service.respond 内，
        # N14 后对话走 CompanionAgent 直连 LLM 绕过了该点（S4 试跑实测 0 行）。
        # 口径同自裁 Z1：非流式首 token ≈ 整段返回时刻；只记每轮首次调用
        # （端到端延迟关心首包，工具轮的后续调用属编排成本，不重复计）。
        llm_sw = Stopwatch()
        first_llm_call = True
        while True:
            reply = await self._llm.chat(messages)
            if first_llm_call:
                first_llm_call = False
                record_stage(
                    "llm_first_token",
                    llm_sw.elapsed_ms(),
                    source="non_stream",
                )
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
                # D4（X8 口径②/③）：play_opera 结果 → 装配层注入的 sink 下发
                # media_play（LLM 点播）或 media_control（控制动作）帧
                # （agents 不横向 import audio，deliver 由 main 注入）
                if call.tool == "play_opera":
                    data = result.data or {}
                    control = data.get("control")
                    if control and self._on_media_control is not None:
                        await self._on_media_control(runtime.device_id, {"action": str(control)})
                    elif self._on_media_play is not None:
                        media = data.get("media") or {}
                        if data.get("found") and media.get("media_url"):
                            await self._on_media_play(
                                runtime.device_id,
                                {
                                    "media_url": media["media_url"],
                                    "title": media.get("title") or "",
                                    "media_id": str(media.get("media_id") or ""),
                                    "duration": media.get("duration"),
                                },
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
    "HEALTH_CHECKUP_TOOL",
    "HIGH_RISK_REFUSED_TEXT",
    "HIGH_RISK_TOOL_NAMES",
    "MEDICATION_CONFIRM_TOOL",
    "MEMORY_RECALL_TOOL",
    "TRANSIENT_DEGRADED_TEXT",
    "WEATHER_TOOL",
    "AgentTurnResult",
    "ChatClient",
    "CompanionAgent",
    "EventSink",
    "FollowupSink",
    "FollowupTask",
    "MediaControlSink",
    "MediaPlaySink",
    "ToolExecutor",
    "ToolResult",
    "ToolSpec",
]
