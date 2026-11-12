"""L2/L3 路由决策（Day 7 · A3）：规则 → System One → Agent，外加 G1 安全复核。

**顺序固定，不得交换**（PRD FR-DLG-02/03、day07 README §1.4）：

```
G0 安全一票否决（在 orchestrator 里，Router 之前）
  → L1 规则层（rules.py，命中即返回，0 次模型调用）
  → L2 System One 快路径（只问 intent，200ms）
  → G1 敏感信号复核（shadow 的 is_sensitive，不阻塞路由）
  → L3 Agent（低置信澄清一次、能力未就绪、闲聊兜底）
```

**依赖全部注入**：本模块不 import 数据库会话、HTTP 客户端、MCP 客户端，也不 import
工具执行器的实现（只认结构化 Protocol）。这不是洁癖：一旦 Router 能自己查库/发 HTTP，
同一个工具就会出现第二套权限判断，`audit_logs` 也就再也说不清「这次是谁放行的」。
`tests/test_router_decision.py` 用 AST 断言这条约束，不依赖人工 grep。

三条验收口径（对应 §5 总表）：

- `action="escalate"` ⇒ `is_sensitive=True` ∧ `tool_name is None`（敏感命中不得执行工具、不进 Agent）；
- 低置信（<0.85）：**动作型意图**同 `thread_id` 首次 `clarify`、第二次 `agent`；
  `chat` / `other` 不澄清、直接下沉 Agent（澄清只为防错执行工具，见 `NO_CLARIFY_INTENTS`）；
  域外一律澄清一次（那是话术问题，不是执行问题）；
- System One 不可用 → `route_layer="fallback"`，**不计入** `system_one` 命中率。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.router.rules import (
    REASON_CAPABILITY_NOT_READY,
    REASON_MISSING_SLOT,
    RuleHit,
    extract_city,
    match_rule,
)
from app.router.safety import SEVERITY_YELLOW, safety_check
from app.system_one.schemas import Intent, SystemOneResult

logger = logging.getLogger("app.router.decision")

#: 路由分层（`decision_logs.route_layer` 的四值；`laya` 是历史只读值，新代码不产出）
RouteLayer = Literal["rule", "system_one", "agent", "fallback"]
#: 四种动作：直连工具 / 下沉 Agent / 只澄清一次 / 安全升级
RouteAction = Literal["tool", "agent", "clarify", "escalate"]

#: **不澄清**的意图：闲聊与「其他」。
#:
#: 为什么（2026-11-10 主理人裁决，方案 B）：澄清的唯一产品目的是**防止执行错工具**
#: （问错城市、确认错药单），不是为了"确认对方在聊什么"。闲聊/其他本来就不映射任何工具，
#: 多问一句只会让长辈把同一句话再说一遍——实测 50 条普通样本里澄清率 28%，其中 7 条
#: 其实判对了，正确样本的闲聊置信度系统性偏低（0.3~0.6，熵大）。
#: 这几类低置信时直接下沉 Agent（它本来就是话术兜底），0.85 阈值只约束**动作型**意图。
NO_CLARIFY_INTENTS: Final[frozenset[str]] = frozenset({"chat", "other"})

#: shadow 等待硬上限（秒）。PRD:547 的 200ms 是**整条路由**的预算，等待预算不得吃掉它。
#: 2026-11-12 实测（jev/cpu）：fast 单问 p50 187ms、shadow 单问 p50 100ms；两者并发时
#: fast 被 CPU 争抢推到 214ms，而 shadow 每次都先于 fast 完成——即「等 shadow」几乎不耗时，
#: 真正的尾部是 fast 本身。因此本上限只需兜住长文本/抖动的极端尾巴，不能靠放大等待换准确率。
_SHADOW_WAIT_BUDGET_S: Final[float] = 0.080

#: 低置信澄清话术（**不调 LLM**：澄清本身不该再花 200ms 和一次模型调用）
CLARIFY_TEXT: Final[str] = "我没太听明白，您是想听段戏、问问天气，还是想让我陪您说说话？"
#: 域外澄清话术（PRD：域外不硬答，但也不冷场）
CLARIFY_TEXT_OUT_OF_DOMAIN: Final[str] = "这个我说不好，我可以陪您聊聊天，也可以帮您叫护工。"

#: reason 取值（写日志与 bad case 归因用；不进库，`decision_logs` 没有 reason 列）
REASON_LOW_CONFIDENCE: Final[str] = "low_confidence"
REASON_OUT_OF_DOMAIN: Final[str] = "out_of_domain"
REASON_SIDECAR_FAILED: Final[str] = "sidecar_failed"
REASON_SENSITIVE_G1: Final[str] = "sensitive_g1"
REASON_DISABLED: Final[str] = "system_one_disabled"
REASON_MULTI_INTENT: Final[str] = "multi_intent_dynamic"

#: 意图 → 直连工具。**只放"参数能从这句话里稳定抽出来"的工具**：
#: - `weather` → `get_weather(city)`：城市可从词表抽；抽不到就转 Agent 追问（不猜城市）。
#: - `medication` 本日**不直连**：`medication_confirm` 需要 `answer` 归一化（吃了/没吃/稍后），
#:   C1 尚未给出该契约（J4 待定），在 Router 里用关键词猜参数就是第二套规则表。
DIRECT_TOOL_BY_INTENT: Final[dict[str, str]] = {"weather": "get_weather"}

#: 能力未就绪的意图：识别得出来，但本日没有可执行端点（媒体库/用药查询）。
#: `health_checkup` 已于 B3 放开（F14：健康语音入口经 Agent 调 `health_checkup` 工具）；
#: `opera`/`movie` 仍不动——是否放开取决于 C4/D5 的 media 接线完成情况，不许顺手全放开。
NOT_READY_INTENTS: Final[frozenset[str]] = frozenset({"opera", "movie", "medication"})

#: 多意图子句分隔符（保守：只认明确的顺承词与标点）
_CLAUSE_SEPARATORS: Final[tuple[str, ...]] = ("然后", "接着", "顺便", "；", ";", "，", ",")


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """一轮路由的结论。字段与 `contracts-system-one.md` §2 逐字一致。

    不变量（构造后由测试钉死）：
    - `action != "tool"` ⇒ `tool_name is None`；
    - `action == "clarify"` ⇒ `clarify_text` 非空；
    - `action == "escalate"` ⇒ `is_sensitive is True` ∧ `tool_name is None`。
    """

    route_layer: RouteLayer
    action: RouteAction
    intent: Intent = "other"
    confidence: float = 0.0
    is_sensitive: bool = False
    escalated_to_s2: bool = False
    reason: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    clarify_text: str | None = None
    multi_intent: bool = False
    sub_intents: tuple[Intent, ...] = ()
    #: 路由层自身耗时，**包含 shadow 等待**（2026-11-10 主理人定口径：
    #: 影子分类只要还在决定「这一轮允许做什么」，它的等待就是用户真实等待，不能剔出计时。
    #: 因此 `decision_logs.latency_ms` 与评测 p95 一律含 shadow，不做"剔除 shadow 的重新定基线"）
    latency_ms: int = 0
    #: 规则命中证据（`R-09` 等），只进日志，不进库（库里没有这一列）
    rule_id: str | None = None


# ------------------------------------------------------------------ 注入缝（全部 Protocol）


class SystemOneLike(Protocol):
    """`SystemOneClient` 的消费面（测试用假实现即可，不加载模型）。"""

    async def decide_fast(
        self, text: str, *, trace_id: str | None = None, state: Any = None
    ) -> SystemOneResult: ...

    async def decide_shadow(
        self, text: str, *, trace_id: str | None = None, state: Any = None
    ) -> SystemOneResult: ...


class EscalateLike(Protocol):
    """G1 升级口（装配层传既有 `_escalate_safety` 的同款包装）。

    只收「疑似」信号，**不收急症类别**——分类器给不出 `chest_pain` 这种临床分类，
    在 Router 里编一个就等于伪造病历。
    """

    async def __call__(
        self, *, elder_id: str | None, thread_id: str, probability: float | None, reason: str
    ) -> Any: ...


class WasClarifiedLike(Protocol):
    """同 `thread_id` 上一轮是否已经澄清过（只读查询，走 `decision_logs` 索引）。"""

    async def __call__(self, thread_id: str) -> bool: ...


class ToolExecutorLike(Protocol):
    """`ToolExecutor.execute()` 的消费面（直连工具唯一入口，不复制权限判断）。"""

    async def execute(
        self,
        *,
        tool_name: str,
        args: dict[str, Any] | None,
        runtime: Identity,
        turn_id: str | None,
        idempotency_key: str,
    ) -> Any: ...


ToolExecutorFactory = Callable[[], ToolExecutorLike]


class BoundaryReviewLike(Protocol):
    """FR-DLG-07 黄档复核出口（装配层注入 `work_order_service.open_work_order` 包装）。

    Router 不持有 session、不 import services：黄档复核的落库只走这一个注入口。
    `dedupe_token` 由 Router 生成（`boundary:<turn_id>`），保证同一轮只建一单；
    **不建 risk_events**（避免污染红色统计）。
    """

    async def __call__(
        self,
        *,
        elder_id: str | None,
        thread_id: str,
        turn_id: str,
        dedupe_token: str,
        reason: str,
        matched_rule: str | None,
    ) -> Any: ...


# ------------------------------------------------------------------ 多意图工具函数


def split_clauses(text: str) -> tuple[str, ...]:
    """按顺承词/标点切子句。**保守**：切完任一子句短于 2 字就整句当单意图。

    "换一段，再大声点" → ("换一段", "再大声点")；"我爱看戏" → 单子句（不切）。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ()
    parts: list[str] = []
    buffer = cleaned
    for separator in _CLAUSE_SEPARATORS:
        buffer = buffer.replace(separator, "\x00")
    parts = [part.strip(" \x00") for part in buffer.split("\x00")]
    clauses = tuple(part for part in parts if part)
    if len(clauses) < 2 or any(len(part) < 2 for part in clauses):
        return (cleaned,)
    return clauses


# ------------------------------------------------------------------ Router


@dataclass(frozen=True, slots=True)
class Router:
    """三级路由。构造后只读；所有外部能力都是注入的（见 §依赖全部注入）。"""

    system_one: SystemOneLike | None = None
    escalate: EscalateLike | None = None
    was_clarified: WasClarifiedLike | None = None
    tool_executor_factory: ToolExecutorFactory | None = None
    confidence_threshold: float = 0.85
    #: shadow 等多久：快路径已并行发出，这里只是「愿不愿意为安全多等一点」。
    #: 默认 80ms（2026-11-12 从 150ms 下调；实测 shadow 单问 ~100ms 且总先于 fast 完成，
    #: 等待上限只兜尾部，压到 80ms 后敏感召回在 110 条固定集上仍 20/20）。
    shadow_grace_s: float = 0.080
    enabled: bool = True
    #: 测试注入点（生产用 `time.perf_counter`）
    clock: Callable[[], float] = field(default=time.perf_counter)
    #: D4（X8 口径③）：直连播放控制下行回调 ``(device_id, 完整帧)``。
    #: play_opera 控制动作/换源 → media_control 帧推 pad（装配层接 signaling.deliver）
    media_frame_sink: Callable[[str, Mapping[str, object]], Awaitable[None]] | None = None
    #: F22 黄档复核出口（FR-DLG-07 历史/他人/模糊）：装配层注 `open_work_order` 包装。
    boundary_review: BoundaryReviewLike | None = None

    # -------------------------------------------------------------- 决策主流程

    async def decide(
        self,
        text: str,
        *,
        thread_id: str,
        runtime: Any,
    ) -> RouteDecision:
        """一轮路由。`runtime` 是 turn_service 的 `TurnRuntime`（只读 `elder_id`/`state`）。"""
        started = self.clock()
        trace_id = get_trace_id()
        cleaned = (text or "").strip()

        # ---- FR-DLG-07 黄档复核（历史/他人/模糊）：建 follow_up，不打断本轮 ----
        # 位置在 L1 之前：这三类表达可能同时命中固定意图（如「我爸爸胸痛，暂停一下」），
        # 复核任务与继续路由互不排斥；**不 return**，避免把进行中的用药/健康任务打断。
        await self._maybe_open_boundary_review(cleaned, thread_id=thread_id, runtime=runtime)

        # ---- L1 规则层：命中即返回，0 次模型调用（验收：命中时 System One 调用 = 0） ----
        rule_decision = self._decide_by_rules(cleaned, started)
        if rule_decision is not None:
            self._log(rule_decision, trace_id, thread_id)
            return rule_decision

        # ---- L2 System One ----
        if not self.enabled or self.system_one is None:
            decision = RouteDecision(
                route_layer="fallback",
                action="agent",
                reason=REASON_DISABLED,
                escalated_to_s2=True,
                latency_ms=self._elapsed_ms(started),
            )
            self._log(decision, trace_id, thread_id)
            return decision

        state = self._build_state(cleaned, runtime)
        shadow_task: asyncio.Task[SystemOneResult] | None = None
        if self._shadow_worth_calling(cleaned):
            shadow_task = asyncio.create_task(self._shadow(cleaned, trace_id, state))

        fast = await self._system_one(cleaned, trace_id, state)
        shadow = await self._consume_shadow(shadow_task)

        # ---- G1 敏感信号复核（分类器只给信号，处置走既有升级口） ----
        if shadow is not None and shadow.is_sensitive:
            # 契约 §6.1：否定/历史/他人/模糊四类边界**由 safety.py 判定，不由分类器裁决**。
            # 分类器只读「这句话像不像急症」，读不出「我爸爸胸痛」是他人主体、
            # 「昨天疼现在好了」是历史。safety.py 的护栏已经给过结论，这里直接采信：
            # 它判"边界"就不升级（G1 不得绕过 FR-DLG-07 的四类边界）。
            guard = safety_check(cleaned)
            if not guard.is_sensitive and guard.reason is not None:
                logger.info(
                    "router_g1_suppressed_by_boundary trace_id=%s thread_id=%s reason=%s",
                    trace_id or "-",
                    thread_id,
                    guard.reason,
                )
                shadow = None

        if shadow is not None and shadow.is_sensitive:
            escalated = await self._escalate_g1(
                runtime=runtime,
                thread_id=thread_id,
                probability=shadow.sensitive_probability,
            )
            decision = RouteDecision(
                route_layer="fallback" if fast.degraded else "system_one",
                action="escalate",
                intent=fast.intent if not fast.degraded else "other",
                confidence=fast.confidence,
                is_sensitive=True,
                escalated_to_s2=True,
                reason=REASON_SENSITIVE_G1,
                clarify_text=None,
                latency_ms=self._elapsed_ms(started),
            )
            logger.warning(
                "router_g1_escalated trace_id=%s thread_id=%s probability=%s escalate_wired=%s",
                trace_id or "-",
                thread_id,
                shadow.sensitive_probability,
                escalated,
            )
            self._log(decision, trace_id, thread_id)
            return decision

        if fast.degraded:
            decision = RouteDecision(
                route_layer="fallback",
                action="agent",
                intent="other",
                confidence=0.0,
                escalated_to_s2=True,
                reason=REASON_SIDECAR_FAILED,
                latency_ms=self._elapsed_ms(started),
            )
            self._log(decision, trace_id, thread_id)
            return decision

        decision = await self._decide_by_intent(
            text=cleaned,
            fast=fast,
            thread_id=thread_id,
            started=started,
        )
        self._log(decision, trace_id, thread_id)
        return decision

    # -------------------------------------------------------------- F22 黄档复核

    async def _maybe_open_boundary_review(self, text: str, *, thread_id: str, runtime: Any) -> None:
        """安全边界（历史/他人/模糊）→ 黄档 follow_up 复核任务。

        - **否定不建单**：`severity != "yellow"` 一律跳过（否定是 `severity=None`）；
        - **不建 risk_events**：出口只调 `open_work_order`（order_type=follow_up）；
        - 失败只记日志：复核单建不出来不能拖垮这一轮对话；
        - `dedupe_token=boundary:<turn_id>`：同一轮重复路由/重试只建一单。
        """
        if self.boundary_review is None or not text:
            return
        guard = safety_check(text)
        if guard.severity != SEVERITY_YELLOW or guard.reason is None:
            return
        turn_id = getattr(runtime, "turn_id", None) or thread_id
        try:
            await self.boundary_review(
                elder_id=getattr(runtime, "elder_id", None),
                thread_id=thread_id,
                turn_id=turn_id,
                dedupe_token=f"boundary:{turn_id}",
                reason=guard.reason,
                matched_rule=guard.matched_rule,
            )
        except Exception as exc:
            logger.warning(
                "router_boundary_review_failed trace_id=%s thread_id=%s reason=%s error=%s",
                get_trace_id() or "-",
                thread_id,
                guard.reason,
                type(exc).__name__,
            )
            return
        logger.info(
            "router_boundary_review_opened trace_id=%s thread_id=%s reason=%s matched=%s",
            get_trace_id() or "-",
            thread_id,
            guard.reason,
            guard.matched_rule or "-",
        )

    # -------------------------------------------------------------- 直连工具

    async def execute_tool(
        self,
        decision: RouteDecision,
        *,
        runtime: Any,
        turn_id: str,
    ) -> Any | None:
        """执行 `action="tool"` 的直连工具。**只走注入的 ToolExecutor**（G2 唯一入口）。

        返回 `ToolResult`（或不执行时 None）。不重试、不兜底：重试策略在 ToolExecutor 内部，
        这里再来一套就会出现「同一句话重试两次、工单两条」。
        """
        if decision.action != "tool" or not decision.tool_name:
            return None
        if self.tool_executor_factory is None:
            logger.warning("router_tool_executor_missing tool=%s", decision.tool_name)
            return None
        executor = self.tool_executor_factory()
        result = await executor.execute(
            tool_name=decision.tool_name,
            args=decision.tool_args,
            runtime=runtime.identity,
            turn_id=turn_id,
            idempotency_key=f"{turn_id}:{decision.tool_name}:1",
        )
        # D4（X8 口径③）：直连播放控制经 sink 转 media_control 帧推 pad——
        # 七动作在 pad 本地执行；next（换一段）随帧携带新 media（换源）。
        # agents/router 不横向 import audio，deliver 由 main 注入。
        if (
            decision.tool_name == "play_opera"
            and self.media_frame_sink is not None
            and getattr(result, "status", "") == "succeeded"
        ):
            data = getattr(result, "data", None) or {}
            control = data.get("control")
            media = data.get("media") or {}
            device_id = getattr(runtime.identity, "device_id", None)
            if control:
                # §6.5 冻结：interrupt 由 pad 端 barge-in 收口，不走 WS 媒体帧
                if str(control) != "interrupt":
                    await self.media_frame_sink(
                        device_id, {"type": "media_control", "action": str(control)}
                    )
            elif data.get("found") and media.get("media_url"):
                await self.media_frame_sink(
                    device_id,
                    {
                        "type": "media_control",
                        "action": "next_track",
                        "media_url": media["media_url"],
                        "title": media.get("title") or "",
                        "media_id": str(media.get("media_id") or ""),
                        "duration": media.get("duration"),
                    },
                )
        return result

    # -------------------------------------------------------------- L1 规则

    def _decide_by_rules(self, text: str, started: float) -> RouteDecision | None:
        clauses = split_clauses(text)

        # 多意图：**只在每个子句都能被规则认出来时**展开，否则整句交给后面的层
        # （拆错子句比不拆更糟：半句话被判成指令会直接执行错动作）
        if len(clauses) >= 2:
            hits = [match_rule(clause) for clause in clauses]
            if all(hit is not None for hit in hits):
                typed_hits = [hit for hit in hits if hit is not None]
                intents = tuple(hit.intent for hit in typed_hits)
                if len(set(intents)) == 1:
                    # 同一意图的顺承动作（"换一段，再大声点"）：按首个子句的动作走，
                    # 但把 multi_intent/sub_intents 如实标出来（评测要用）
                    return self._decision_from_hit(
                        typed_hits[0],
                        started,
                        multi_intent=True,
                        sub_intents=intents,
                    )
                # 不同意图的顺序组合 = 需要按结果动态决策 → 直接下沉 Agent
                return RouteDecision(
                    route_layer="rule",
                    action="agent",
                    intent=intents[0],
                    confidence=1.0,
                    escalated_to_s2=True,
                    reason=REASON_MULTI_INTENT,
                    multi_intent=True,
                    sub_intents=intents,
                    latency_ms=self._elapsed_ms(started),
                )

        hit = match_rule(text)
        if hit is None:
            return None
        return self._decision_from_hit(hit, started)

    def _decision_from_hit(
        self,
        hit: RuleHit,
        started: float,
        *,
        multi_intent: bool = False,
        sub_intents: tuple[Intent, ...] = (),
    ) -> RouteDecision:
        if hit.action == "tool" and hit.tool_name and hit.tool_args:
            return RouteDecision(
                route_layer="rule",
                action="tool",
                intent=hit.intent,
                confidence=hit.confidence,
                escalated_to_s2=False,
                tool_name=hit.tool_name,
                tool_args=hit.tool_args,
                multi_intent=multi_intent,
                sub_intents=sub_intents,
                latency_ms=self._elapsed_ms(started),
                rule_id=hit.rule_id,
            )
        # 规则认出来了但能力没就绪（媒体库/用药查询）：保留 rule 层的识别价值，
        # 执行交 Agent。**不假装**工具存在（计划 §10-2 裁决）。
        return RouteDecision(
            route_layer="rule",
            action="agent",
            intent=hit.intent,
            confidence=hit.confidence,
            escalated_to_s2=True,
            reason=hit.reason or REASON_CAPABILITY_NOT_READY,
            multi_intent=multi_intent,
            sub_intents=sub_intents,
            latency_ms=self._elapsed_ms(started),
            rule_id=hit.rule_id,
        )

    # -------------------------------------------------------------- L2 之后

    async def _decide_by_intent(
        self,
        *,
        text: str,
        fast: SystemOneResult,
        thread_id: str,
        started: float,
    ) -> RouteDecision:
        """意图 → 动作。低置信/域外先过「是否已澄清过」这一关。"""
        latency = self._elapsed_ms(started)
        layer: RouteLayer = "system_one"
        low_confidence = fast.confidence < self.confidence_threshold

        if low_confidence and not fast.is_out_of_domain and fast.intent in NO_CLARIFY_INTENTS:
            # 闲聊/其他不澄清：直接下沉 Agent。省掉一次 was_clarified 查询与一轮往返，
            # 也不占用「只澄清一次」的额度（动作型意图该澄清时照样能澄清）。
            return RouteDecision(
                route_layer=layer,
                action="agent",
                intent=fast.intent,
                confidence=fast.confidence,
                escalated_to_s2=True,
                reason=f"{REASON_LOW_CONFIDENCE}:no_clarify_intent",
                latency_ms=latency,
            )

        if low_confidence or fast.is_out_of_domain:
            already = await self._already_clarified(thread_id)
            reason = REASON_OUT_OF_DOMAIN if fast.is_out_of_domain else REASON_LOW_CONFIDENCE
            if not already:
                return RouteDecision(
                    route_layer=layer,
                    action="clarify",
                    intent=fast.intent,
                    confidence=fast.confidence,
                    escalated_to_s2=False,
                    reason=reason,
                    clarify_text=(
                        CLARIFY_TEXT_OUT_OF_DOMAIN if fast.is_out_of_domain else CLARIFY_TEXT
                    ),
                    latency_ms=latency,
                )
            # 第二次：不再追问，交 Agent 用话术兜（PRD：只澄清一次）
            return RouteDecision(
                route_layer=layer,
                action="agent",
                intent=fast.intent,
                confidence=fast.confidence,
                escalated_to_s2=True,
                reason=f"{reason}:clarified_once",
                latency_ms=latency,
            )

        tool_name = DIRECT_TOOL_BY_INTENT.get(fast.intent)
        if tool_name is not None:
            tool_args = self._args_for_tool(tool_name, text)
            if tool_args is not None:
                return RouteDecision(
                    route_layer=layer,
                    action="tool",
                    intent=fast.intent,
                    confidence=fast.confidence,
                    escalated_to_s2=False,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    latency_ms=latency,
                )
            return RouteDecision(
                route_layer=layer,
                action="agent",
                intent=fast.intent,
                confidence=fast.confidence,
                escalated_to_s2=True,
                reason=f"{REASON_MISSING_SLOT}:{tool_name}",
                latency_ms=latency,
            )

        reason = REASON_CAPABILITY_NOT_READY if fast.intent in NOT_READY_INTENTS else None
        return RouteDecision(
            route_layer=layer,
            action="agent",
            intent=fast.intent,
            confidence=fast.confidence,
            escalated_to_s2=True,
            reason=reason,
            latency_ms=latency,
        )

    # -------------------------------------------------------------- 小工具

    @staticmethod
    def _args_for_tool(tool_name: str, text: str) -> dict[str, Any] | None:
        """从文本抽工具参数。**抽不到就返回 None**（宁可追问，不猜默认值）。"""
        if tool_name == "get_weather":
            city = extract_city(text)
            return {"city": city} if city else None
        return None

    @staticmethod
    def _build_state(text: str, runtime: Any) -> dict[str, Any]:
        """交给分类器的 state：必须带 `current_task` / `media_status`。

        20 条实测里 3 个错例（"换一段""声音小一点""今天几号"）全因 state 缺上下文
        （spike README §5）。这两项从 turn 运行时取；取不到就不写，不编造。
        """
        state: dict[str, Any] = {"asr_final": text}
        for field_name in ("current_task", "media_status", "elder_name"):
            value = getattr(runtime, field_name, None)
            if isinstance(value, str) and value:
                state[field_name] = value
        return state

    @staticmethod
    def _shadow_worth_calling(text: str) -> bool:
        """极短寒暄（"嗯""好"）不值得为它多打一次模型——省 CPU 也省 shadow 预算。"""
        return len(text) >= 4

    async def _system_one(
        self, text: str, trace_id: str | None, state: dict[str, Any]
    ) -> SystemOneResult:
        assert self.system_one is not None  # 调用前已判 None
        try:
            return await self.system_one.decide_fast(text, trace_id=trace_id, state=state)
        except Exception as exc:  # 客户端承诺不抛；这里兜住假实现/未来回归
            logger.error(
                "router_system_one_fast_raised trace_id=%s error=%s",
                trace_id or "-",
                type(exc).__name__,
            )
            from app.system_one.schemas import degraded_result  # 局部导入，避免模块级耦合

            return degraded_result()

    async def _shadow(
        self, text: str, trace_id: str | None, state: dict[str, Any]
    ) -> SystemOneResult | None:
        assert self.system_one is not None
        try:
            return await self.system_one.decide_shadow(text, trace_id=trace_id, state=state)
        except Exception as exc:
            logger.warning(
                "router_system_one_shadow_raised trace_id=%s error=%s",
                trace_id or "-",
                type(exc).__name__,
            )
            return None

    async def _consume_shadow(
        self, task: asyncio.Task[SystemOneResult | None] | None
    ) -> SystemOneResult | None:
        """等 shadow 最多 `shadow_grace_s`（且不超过 `_SHADOW_WAIT_BUDGET_S`）。

        为什么要有硬上限：调用方可能把它配成 sidecar 的请求超时（如 400ms），
        那会把「等安全信号」变成吃掉整条 200ms 路由预算的黑洞。实测 shadow 通常
        先于 fast 完成，真正的尾部是 fast 本身，故缩小等待不损召回。
        """
        if task is None:
            return None
        budget = min(self.shadow_grace_s, _SHADOW_WAIT_BUDGET_S)
        try:
            done, _ = await asyncio.wait({task}, timeout=budget)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not done:
            task.cancel()
            return None
        return task.result()

    async def _escalate_g1(
        self,
        *,
        runtime: Any,
        thread_id: str,
        probability: float | None,
    ) -> bool:
        """调升级口。**未装配时保守按敏感处理**（action 仍是 escalate，不降级成普通意图）。"""
        if self.escalate is None:
            logger.warning("router_escalate_not_wired thread_id=%s", thread_id)
            return False
        try:
            await self.escalate(
                elder_id=getattr(runtime, "elder_id", None),
                thread_id=thread_id,
                probability=probability,
                reason=REASON_SENSITIVE_G1,
            )
        except Exception as exc:  # 升级失败不改变"已判敏感"的事实，只留证
            logger.error(
                "router_escalate_failed thread_id=%s error=%s", thread_id, type(exc).__name__
            )
            return False
        return True

    async def _already_clarified(self, thread_id: str) -> bool:
        if self.was_clarified is None:
            return False
        try:
            return bool(await self.was_clarified(thread_id))
        except Exception as exc:  # 查询失败不该把长者卡在"再问一次"
            logger.warning(
                "router_was_clarified_failed thread_id=%s error=%s",
                thread_id,
                type(exc).__name__,
            )
            return False

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self.clock() - started) * 1000))

    def _log(self, decision: RouteDecision, trace_id: str | None, thread_id: str) -> None:
        logger.info(
            "router_decided trace_id=%s thread_id=%s layer=%s action=%s intent=%s "
            "confidence=%.3f sensitive=%s reason=%s tool=%s latency_ms=%d",
            trace_id or "-",
            thread_id,
            decision.route_layer,
            decision.action,
            decision.intent,
            decision.confidence,
            decision.is_sensitive,
            decision.reason or "-",
            decision.tool_name or "-",
            decision.latency_ms,
        )


__all__ = [
    "CLARIFY_TEXT",
    "CLARIFY_TEXT_OUT_OF_DOMAIN",
    "DIRECT_TOOL_BY_INTENT",
    "NOT_READY_INTENTS",
    "NO_CLARIFY_INTENTS",
    "REASON_CAPABILITY_NOT_READY",
    "REASON_LOW_CONFIDENCE",
    "REASON_OUT_OF_DOMAIN",
    "REASON_SENSITIVE_G1",
    "REASON_SIDECAR_FAILED",
    "BoundaryReviewLike",
    "RouteAction",
    "RouteDecision",
    "RouteLayer",
    "Router",
    "split_clauses",
]
