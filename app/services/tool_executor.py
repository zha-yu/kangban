"""工具执行器（N12 / 契约 §5.3）—— 白名单、四类失败、幂等、审计。

一条工具调用的完整顺序（顺序本身是语义的一部分，别重排）：

```
白名单 → 参数消毒（丢弃模型自报身份 + 审计）→ 护栏预算/熔断
      → 参数校验 → 幂等查重 → 落 running 行 → 执行（transient 退避≤2）→ 收口
```

四个决定的理由：

1. **身份只从 `runtime` 读**。`args` 里出现 `elder_id` / `thread_id` / `device_id`，
   说明模型在试图自报身份（或提示词被注入），一律**丢弃并写审计**后继续执行——
   直接报错会让一句「帮 3 号床的老人查一下」把整轮对话打断，丢弃更稳。
2. **白名单不通过就 `denied`**。未注册 = 模型幻觉；已注册未绑定 = 本日还没接线。
   两者都不该落到「让模型自己编答案」。
3. **`side_effect=True` 必须落 `tool_executions`**，靠 `UNIQUE(idempotency_key)` 兜底。
   同键**同参**返回上次结果（`idempotent_replay=True`）；同键**异参**直接拒绝——
   后者几乎一定是调用方拼键写错，静默执行会重复产生副作用（重复建单、重复播报）。
4. **预算与熔断复用 `Guardrails.check()`**，不新建第二套计数器（`AGENTS.md` §4-8）。
   两套计数器意味着「一个放行、一个拦住」，最后没人说得清到底限制是多少。

本模块是 `app/services/` 下唯一写 `tool_executions` 与工具类审计的地方；只 `flush()`，
**不 commit**，事务边界交给调用方（与 `history_service` 同口径）。
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.guardrails import Guardrails
from app.agents.tool_registry import ToolRegistry, ToolSpec, default_registry
from app.core.runtime_context import Identity
from app.models.conversation import TOOL_ERROR_CLASSES, ConversationTurn, ToolExecution
from app.services.audit_log_service import record_audit

logger = logging.getLogger(__name__)

#: 模型不得自报的身份字段：出现在 args 里一律丢弃并审计
IDENTITY_KEYS: frozenset[str] = frozenset({"elder_id", "thread_id", "device_id"})

#: 受管服务对象参数（Day 8 B4 / AC-39）：工具 schema 声明了该参数时，
#: 执行链步骤 1.5 一律以运行时身份的 `elder_id` 注入覆盖——
#: 模型传了别的长者 → 越权拒绝 + 审计；没传 → 注入；`runtime.elder_id`
#: 为 None（staff/guardian 身份）→ 拒绝（宁可不做，不可做错对象）。
SUBJECT_ELDER_KEY = "subject_elder_id"

ToolStatus = Literal["succeeded", "failed", "denied"]
ErrorClass = Literal["bad_args", "not_found", "transient", "forbidden"]

#: 可直接播报的降级话术（四类失败各一句；不要在这里写技术细节）
MESSAGES: dict[str, str] = {
    "bad_args": "我理解得不太对，您能再说一次吗？",
    "not_found": "这个我这儿查不到，帮您记下来。",
    "transient": "这个我暂时查不到，帮您记下来。",
    "forbidden": "这个我不能替您做。",
}

#: `Identity.kind` → `audit_logs.actor_type`（该列 CHECK 只有 staff/guardian/system）
_ACTOR_TYPE: dict[str, str] = {
    "staff": "staff",
    "staff_card": "staff",
    "guardian": "guardian",
    "device": "system",
}


# ------------------------------------------------------------------ 结果与异常


@dataclass(frozen=True, slots=True)
class ToolResult:
    """一次工具调用的结论。`message` 是**可直接播给长者听**的话，不是给开发看的。"""

    status: ToolStatus
    data: dict[str, Any] | None = None
    error_class: ErrorClass | None = None
    message: str | None = None
    idempotent_replay: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


class ToolError(Exception):
    """工具域异常基类：执行函数用它表达「属于哪一类失败」。"""

    error_class: ErrorClass = "transient"


class ToolArgumentError(ToolError):
    """入参不合法（类型错、缺必填、多传字段）。模型可据此重试。"""

    error_class: ErrorClass = "bad_args"


class ToolNotFoundError(ToolError):
    """目标资源不存在（查不到某位长者、某个计划）。如实告知，不重试。"""

    error_class: ErrorClass = "not_found"


class ToolTransientError(ToolError):
    """上游临时故障（超时、5xx）。退避重试，仍失败则降级话术。"""

    error_class: ErrorClass = "transient"


class ToolForbiddenError(ToolError):
    """越权（他人 `elder_id`、超出范围）。拒绝 + 审计，不重试。"""

    error_class: ErrorClass = "forbidden"


# ------------------------------------------------------------------ 参数校验


def _validate_args(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    """最小 JSON Schema 子集校验；返回错误说明，`None` 表示通过。

    只认 `type` / `required` / `properties` / `additionalProperties` / `minLength`。
    多传字段默认拒绝（`additionalProperties: False`）：模型很喜欢补一个 `elder_id` 或
    `reason`，放过去就会在半年后变成「谁都能传点什么」。
    """
    props: dict[str, Any] = schema.get("properties", {}) or {}
    for key in schema.get("required", []) or []:
        if key not in args:
            return f"missing required parameter: {key}"

    if schema.get("additionalProperties") is False:
        extra = sorted(set(args) - set(props))
        if extra:
            return f"unexpected parameters: {extra}"

    for key, value in args.items():
        rule = props.get(key) or {}
        expected = rule.get("type")
        if expected == "string":
            if not isinstance(value, str):
                return f"{key} must be a string"
            min_len = rule.get("minLength")
            if isinstance(min_len, int) and len(value) < min_len:
                return f"{key} is too short (min {min_len})"
        elif expected == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return f"{key} must be an integer"
        elif expected == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{key} must be a number"
        elif expected == "boolean":
            if not isinstance(value, bool):
                return f"{key} must be a boolean"
        elif expected == "object":
            if not isinstance(value, dict):
                return f"{key} must be an object"
        elif expected == "array":
            if not isinstance(value, list):
                return f"{key} must be an array"
    return None


def _actor(identity: Identity) -> tuple[str, uuid.UUID | None]:
    """身份 → 审计的 `(actor_type, actor_id)`；`actor_id` 不是 UUID 就写 None。

    审计里塞一个非法 UUID 会让整条写入失败，进而让「本该拒绝的调用」变成 500 ——
    审计的意义是留痕，不是把主流程拖垮。
    """
    actor_type = _ACTOR_TYPE.get(identity.kind, "system")
    raw = identity.principal_id
    try:
        return actor_type, uuid.UUID(raw)
    except (ValueError, TypeError):
        return actor_type, None


# ------------------------------------------------------------------ 执行器


class ToolExecutor:
    """工具执行的唯一入口。一个会话一个实例（`Guardrails` 是会话级状态）。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        registry: ToolRegistry | None = None,
        guardrails: Guardrails | None = None,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.05,
        mcp_client: Any | None = None,
    ) -> None:
        self._session = session
        self._registry = registry if registry is not None else default_registry()
        self._guardrails = guardrails
        self._max_attempts = max(1, max_attempts)
        self._backoff = retry_backoff_seconds
        #: `app/mcp_client.MCPClient`（或同协议的测试替身）。为 None 时 MCP 工具按「未接线」处理。
        self._mcp = mcp_client

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    async def execute(
        self,
        *,
        tool_name: str,
        args: dict[str, Any] | None,
        runtime: Identity,
        turn_id: str | None,
        idempotency_key: str,
    ) -> ToolResult:
        """执行一次工具调用。**不抛业务异常**：所有失败都变成 `ToolResult`。

        这一点是刻意的：Agent 主循环拿到异常会中断整轮，而「查不到天气」不该中断对话。
        只有编程错误（如 session 挂了）才会往上抛。
        """
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        raw_args = dict(args or {})

        spec = self._registry.get(tool_name)
        if spec is None:
            await self._audit(
                runtime,
                action="tool_not_registered",
                detail={"tool_name": tool_name},
            )
            logger.warning("tool_not_registered tool=%s", tool_name)
            return ToolResult(
                status="denied",
                error_class="not_found",
                message=MESSAGES["not_found"],
            )

        # 1) 身份消毒：模型自报的身份一律丢弃（先审计，再执行）
        cleaned, dropped = self._strip_identity(raw_args)
        if dropped:
            await self._audit(
                runtime,
                action="tool_identity_override_attempt",
                detail={"tool_name": tool_name, "dropped_keys": dropped},
            )
            logger.warning("tool_identity_override_attempt tool=%s keys=%s", tool_name, dropped)

        # 1.5) 服务对象注入（AC-39）：schema 声明 subject_elder_id 的工具，
        # 服务对象只认运行时身份；模型试图指定别的长者 → 越权拒绝 + 审计
        if SUBJECT_ELDER_KEY in (spec.parameters_schema.get("properties") or {}):
            context_elder = getattr(runtime, "elder_id", None)
            claimed = cleaned.get(SUBJECT_ELDER_KEY)
            if context_elder is None:
                await self._audit(
                    runtime,
                    action="tool_subject_context_missing",
                    detail={"tool_name": tool_name, "claimed": claimed},
                )
                logger.warning("tool_subject_context_missing tool=%s", tool_name)
                return ToolResult(
                    status="denied",
                    error_class="forbidden",
                    message=MESSAGES["forbidden"],
                )
            if claimed is not None and str(claimed) != context_elder:
                await self._audit(
                    runtime,
                    action="tool_cross_elder_attempt",
                    detail={
                        "tool_name": tool_name,
                        "claimed": str(claimed),
                        "context": context_elder,
                    },
                )
                logger.warning("tool_cross_elder_attempt tool=%s claimed=%s", tool_name, claimed)
                return ToolResult(
                    status="denied",
                    error_class="forbidden",
                    message=MESSAGES["forbidden"],
                )
            cleaned[SUBJECT_ELDER_KEY] = context_elder

        # 2) 护栏：预算优先于熔断，熔断优先于同参短路（判定顺序在 Guardrails 内）
        if self._guardrails is not None:
            decision = self._guardrails.check(tool_name, cleaned)
            if not decision.allow:
                await self._audit(
                    runtime,
                    action="tool_guardrail_denied",
                    detail={"tool_name": tool_name, "reason": decision.reason},
                )
                return ToolResult(
                    status="denied",
                    error_class="forbidden",
                    message=decision.reason or MESSAGES["forbidden"],
                )

        # 3) 参数校验（schema 由注册表给，不在执行器里写死任何工具名）
        problem = _validate_args(spec.parameters_schema, cleaned)
        if problem is not None:
            return ToolResult(
                status="failed",
                error_class="bad_args",
                message=f"{MESSAGES['bad_args']}（{problem}）",
            )

        # 4) MCP 工具走客户端（失败分类由 mcp_client.tool_error_class 统一决定）
        if spec.transport == "mcp":
            return await self._run_mcp(spec=spec, args=cleaned, runtime=runtime)

        # 5) 本地工具：已注册但未绑定实现
        if not spec.wired:
            return ToolResult(
                status="failed",
                error_class="not_found",
                message=MESSAGES["not_found"],
            )

        # 5) 幂等查重（只对有副作用的工具）
        if spec.side_effect:
            replay = await self._replay_if_duplicated(
                spec=spec,
                key=idempotency_key,
                request=cleaned,
                runtime=runtime,
            )
            if replay is not None:
                return replay

        # 6) 落 running 行（副作用工具才记账，只读工具不灌表）
        row: ToolExecution | None = None
        if spec.side_effect:
            if turn_id is not None:
                # **插入前**校验轮次存在：外键违反发生在 flush 里，SAVEPOINT 救不了
                # ——flush 失败会把 session 打成 rollback 状态，调用方拿到的会是
                # PendingRollbackError（原始异常被包住，排查时反而看不清）。
                # 轮次不存在是**调用方的参数错**，按 bad_args 明确返回，
                # 不让它走到 DB 约束那一层。
                turn_exists = (
                    await self._session.execute(
                        select(ConversationTurn.turn_id).where(ConversationTurn.turn_id == turn_id)
                    )
                ).scalar_one_or_none()
                if turn_exists is None:
                    logger.warning(
                        "tool_execution_unknown_turn tool=%s turn_id=%s", tool_name, turn_id
                    )
                    return ToolResult(
                        status="failed",
                        error_class="bad_args",
                        message=MESSAGES["bad_args"],
                    )
            row = ToolExecution(
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                request_json=cleaned,
                status="running",
                turn_id=turn_id,
            )
            self._session.add(row)
            try:
                # 用 SAVEPOINT 兜唯一键冲突：整段 `rollback()` 会顺手回滚同一事务里的
                # 业务写入（历史事件、用药事实），一次并发撞键不该牵连别人的数据
                async with self._session.begin_nested():
                    await self._session.flush()
            except IntegrityError:
                # 并发同键：另一个请求刚插进去，让调用方按 transient 处理即可。
                # 但 IntegrityError 不只有唯一键冲突——CHECK 违反（tool_name 不在白名单等）
                # 与外键违反（turn_id 指向不存在的轮次）也在其中。必须回查确认
                # 「真有同键记录」才算并发；查不到说明是**数据违规**，原样抛出，
                # 否则「turn_id 无效」会被伪装成「并发冲突」，排查时完全找错方向。
                existing = (
                    await self._session.execute(
                        select(ToolExecution.execution_id).where(
                            ToolExecution.idempotency_key == idempotency_key
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                return ToolResult(
                    status="failed",
                    error_class="transient",
                    message=MESSAGES["transient"],
                )

        # 7) 执行（transient 退避重试，其余失败一次即出）
        result = await self._run(spec=spec, args=cleaned, runtime=runtime)

        # 8) 收口记账
        if row is not None:
            row.status = (
                "succeeded" if result.ok else ("denied" if result.status == "denied" else "failed")
            )
            row.error_class = result.error_class
            row.result_json = result.data if result.ok else {"message": result.message}
            row.finished_at = datetime.datetime.now(datetime.UTC)
            await self._session.flush()

        if not result.ok:
            await self._audit(
                runtime,
                action="tool_failed",
                detail={
                    "tool_name": tool_name,
                    "error_class": result.error_class,
                    "idempotency_key": idempotency_key,
                },
            )
        return result

    async def _run_mcp(self, *, spec: ToolSpec, args: dict[str, Any], runtime: Any) -> ToolResult:
        """走 MCP 客户端调用远端工具，把 `MCPResult` 收敛成 `ToolResult`。

        三件事只在这里做：

        1. **不做第二层缓存**。weather server 的 `WeatherService` 已按
           `weather_cache_ttl_seconds` 做了 TTL 缓存 + 过期兜底，并回 `cached` 标记。
           A 侧再缓存会出现两个 TTL、两套命中率，且进程内缓存不跨 worker——
           排查「为什么这台机器数据旧」会变成玄学。
        2. **失败分类只用 `tool_error_class()`**（C 的 `app/mcp_client/client.py`），
           本函数不另立映射表；否则同一个超时在不同层会有两个 error_class。
        3. **成功也把 `cached` / `observed_at` 原样透传**：Agent 要据此播「（数据时间 08:12）」，
           这里改写字段等于让播报说错时间。

        失败时本层**不再写 `tool_failed` 审计**：MCP 客户端在 `call_tool` 内对每次调用都写
        `audit_logs`（契约 §5.4），这里再写一条会让同一次超时出现两条记录，
        排查时反而要先去对时间戳。
        """
        if self._mcp is None:
            # 未注入客户端 = N25 还没接线；按 not_found 处理，绝不让模型自己编天气
            logger.warning("tool_mcp_not_wired tool=%s", spec.name)
            return ToolResult(
                status="failed",
                error_class="not_found",
                message=MESSAGES["not_found"],
            )

        from app.mcp_client.client import tool_error_class

        try:
            result = await self._mcp.call_tool(spec.name, args)
        except Exception:  # 客户端自身异常（连接被拒等）也不该掀翻整轮对话
            logger.exception("tool_mcp_client_crashed tool=%s", spec.name)
            return ToolResult(
                status="failed",
                error_class="transient",
                message=MESSAGES["transient"],
            )

        if result.ok:
            data = dict(result.data or {})
            data.setdefault("cached", False)  # server 未给标记时按「实时」理解
            return ToolResult(status="succeeded", data=data)

        error_class = tool_error_class(result) or "transient"
        logger.info(
            "tool_mcp_failed tool=%s error=%s error_class=%s",
            spec.name,
            getattr(result, "error", None),
            error_class,
        )
        return ToolResult(
            status="denied" if error_class == "forbidden" else "failed",
            error_class=error_class,
            message=MESSAGES[error_class],
        )

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _strip_identity(args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        dropped = sorted(k for k in args if k in IDENTITY_KEYS)
        if not dropped:
            return args, []
        return {k: v for k, v in args.items() if k not in IDENTITY_KEYS}, dropped

    async def _run(self, *, spec: ToolSpec, args: dict[str, Any], runtime: Identity) -> ToolResult:
        assert spec.executor is not None  # 上一步已判 wired
        last: ToolError | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                data = await spec.executor(args, runtime)
                if not isinstance(data, dict):
                    # 执行器返回非 dict 属实现错误，按上游坏响应处理，别把异常抛给 Agent
                    raise ToolTransientError("tool executor returned non-dict payload")
                return ToolResult(status="succeeded", data=data)
            except ToolError as exc:
                last = exc
                if exc.error_class != "transient" or attempt == self._max_attempts:
                    break
                await asyncio.sleep(self._backoff * (2 ** (attempt - 1)))
            except Exception as exc:  # 执行器里的任何意外都不该掀翻整轮对话
                logger.exception("tool_executor_crashed tool=%s", spec.name)
                last = ToolTransientError(str(exc))
                if attempt == self._max_attempts:
                    break
                await asyncio.sleep(self._backoff * (2 ** (attempt - 1)))

        assert last is not None
        error_class = last.error_class
        assert error_class in TOOL_ERROR_CLASSES  # 与 DB CHECK 同源，防止拼错
        return ToolResult(
            status="denied" if error_class == "forbidden" else "failed",
            error_class=error_class,
            message=MESSAGES[error_class],
        )

    async def _replay_if_duplicated(
        self,
        *,
        spec: ToolSpec,
        key: str,
        request: dict[str, Any],
        runtime: Identity,
    ) -> ToolResult | None:
        """同键已存在时的处理；返回 `None` 表示「没重复，继续执行」。"""
        existing = (
            await self._session.execute(
                select(ToolExecution).where(ToolExecution.idempotency_key == key)
            )
        ).scalar_one_or_none()
        if existing is None:
            return None

        if existing.tool_name != spec.name or dict(existing.request_json or {}) != request:
            # 同键异参/异工具：拼键写错，静默执行会重复产生副作用
            await self._audit(
                runtime,
                action="tool_idempotency_conflict",
                detail={
                    "tool_name": spec.name,
                    "stored_tool": existing.tool_name,
                    "idempotency_key": key,
                },
            )
            logger.warning("tool_idempotency_conflict tool=%s key=%s", spec.name, key)
            return ToolResult(
                status="denied",
                error_class="forbidden",
                message=MESSAGES["forbidden"],
            )

        if existing.status == "succeeded":
            logger.info("tool_idempotent_replay tool=%s key=%s", spec.name, key)
            return ToolResult(
                status="succeeded",
                data=dict(existing.result_json or {}),
                idempotent_replay=True,
            )

        if existing.status == "running":
            return ToolResult(
                status="failed",
                error_class="transient",
                message=MESSAGES["transient"],
            )

        # 上次是 failed / denied：允许用同一把键重试（否则一次抖动就永远补不上）
        return None

    async def _audit(self, runtime: Identity, *, action: str, detail: dict[str, Any]) -> None:
        actor_type, actor_id = _actor(runtime)
        await record_audit(
            self._session,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            resource_type="tool_execution",
            resource_id=None,
            detail=detail,
        )


__all__ = [
    "IDENTITY_KEYS",
    "MESSAGES",
    "SUBJECT_ELDER_KEY",
    "ToolArgumentError",
    "ToolError",
    "ToolExecutor",
    "ToolForbiddenError",
    "ToolNotFoundError",
    "ToolResult",
    "ToolTransientError",
]
