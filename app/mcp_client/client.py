"""MCP 客户端：工具发现 / 调用 / 超时 / 越权审计（Day 6 · N25，契约 §5.4）。

职责边界（TD §2.1 `app/mcp_client` 行 / FR-TC-03 / AC-29 / 闸门③）：

- 传输用仓库锁定的 mcp SDK streamable-http 客户端（与 `mcp-servers/weather` 共用
  同一协议实现，不自造第二套 JSON-RPC）；每次调用独立建连 → initialize → 调用 → 关闭，
  无跨调用会话状态，进程重启零恢复负担；
- 总超时读 `settings.mcp_call_timeout_seconds`（默认 5s，AGENTS §7），用
  `asyncio.timeout` 罩住建连 + initialize + 调用全程；
- **身份只能从服务端 `runtime_context` 注入**（PRD FR-ADM-06 ③ / AGENTS §4.9）：
  `args` 里携带的 `elder_id` 与 `runtime_context` 身份不一致、或上下文根本没有身份
  可校验时，调用被拒绝（`unauthorized`）并写 `audit_logs`；
- 每次工具调用写一条审计（工具名、耗时、结果分类，契约 §5.4）；审计写入经
  services 层唯一入口 `audit_log_service.record_audit`（分层硬约束：写库只走
  services），默认逐条独立事务；审计失败只 warning，不阻断工具调用本身；
- 错误四分类 `timeout / upstream_error / bad_response / unauthorized` 映射到工具层
  四类失败（`bad_args / not_found / transient / forbidden`）——映射关系唯一口径在
  `tool_error_class()` 的注释表里，调用方不得另立映射；
- 日志只含 trace_id / 工具名 / 耗时 / 分类，不含上游 URL、key、响应体。

`tools/list`（工具发现）不写审计：契约 §5.4 的审计口径是「工具名、耗时、结果分类」，
发现动作没有工具名语义。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent

from app.core.config import Settings
from app.core.context import get_trace_id
from app.core.runtime_context import get_identity
from app.db import session_factory
from app.services.audit_log_service import record_audit

logger = logging.getLogger("app.mcp_client.client")

# ---------------------------------------------------------------------------
# 类型与常量
# ---------------------------------------------------------------------------

#: 契约 §5.4 冻结的错误四分类
ErrorKind = Literal["timeout", "upstream_error", "bad_response", "unauthorized"]

#: 工具层四类失败（契约 §5.3 ToolResult.error_class）
ToolErrorClass = Literal["bad_args", "not_found", "transient", "forbidden"]

#: 传输工厂：返回一个 async 上下文管理器，进入后产出 (read_stream, write_stream)。
#: 默认实现是 mcp SDK 的 streamable-http 客户端；测试注入内存流工厂。
SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]

ACTION_TOOL_CALL: Final[str] = "mcp_tool_call"
# 与契约 §5.3 ToolExecutor 的越权审计动作同名：同一语义（模型/客户端塞身份 → 拒绝留痕）
ACTION_IDENTITY_OVERRIDE: Final[str] = "tool_identity_override_attempt"
RESOURCE_TYPE: Final[str] = "mcp_tool"


def _http_session_factory(endpoint: str) -> SessionFactory:
    """默认传输工厂：mcp SDK streamable-http 客户端，endpoint 形如 http://127.0.0.1:7101/mcp。"""

    def factory() -> AbstractAsyncContextManager[Any]:
        return streamable_http_client(endpoint)

    return factory


# ---------------------------------------------------------------------------
# 结果与审计载荷
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MCPResult:
    """一次 MCP 工具调用的结果（契约 §5.4，签名冻结：ok / data / error 三字段）。

    失败时 `data` 尽量保留服务端结构化载荷（如 {"ok": false, "error": "bad_request",
    "message": ...}），供 `tool_error_class()` 细化映射与上层拼可播报话术；
    拿不到载荷（协议形态异常 / 越权短路）时为 `None`。
    """

    ok: bool
    data: dict[str, Any] | None
    error: ErrorKind | None


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """一条待落库的审计记录（audit_logs 写入由 DbAuditSink 经 services 层完成）。"""

    action: str
    tool_name: str
    latency_ms: int
    outcome: str
    detail: dict[str, Any] = field(default_factory=dict)


class AuditSink(Protocol):
    """审计出口协议：默认 `DbAuditSink` 落库，测试注入内存收集器。"""

    async def write(self, entry: AuditEntry) -> None: ...


class _NullAuditSink:
    """审计空实现：仅供测试注入（生产默认 `DbAuditSink` 落库，审计不可关）。"""

    async def write(self, entry: AuditEntry) -> None:
        return None


class DbAuditSink:
    """默认审计落库：经 services 层唯一写库入口 `record_audit` 写 audit_logs。

    审计失败不阻断工具调用（只 warning 留痕）——越权拒绝在调用侧已经生效，
    不因审计库抖动放大成对话失败；审计缺口由 warning 日志兜底可查。
    """

    async def write(self, entry: AuditEntry) -> None:
        try:
            async with session_factory() as session:
                await record_audit(
                    session,
                    actor_type="system",
                    actor_id=None,
                    action=entry.action,
                    resource_type=RESOURCE_TYPE,
                    resource_id=None,
                    detail={
                        "tool_name": entry.tool_name,
                        "latency_ms": entry.latency_ms,
                        "outcome": entry.outcome,
                        **entry.detail,
                    },
                )
                await session.commit()
        except Exception:
            logger.warning(
                "mcp_audit_write_failed",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "action": entry.action,
                    "tool_name": entry.tool_name,
                },
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# 身份闸门（闸门③：工具身份只从 runtime_context 读）
# ---------------------------------------------------------------------------


def _same_uuid(left: str, right: str) -> bool:
    """UUID 文本等值比较：大小写不敏感（auth 侧 canonical 小写，模型侧大小写随意）。"""
    return left.strip().lower() == right.strip().lower()


def _resolve_args_identity(args: Mapping[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    """闸门③前置校验。返回 (清洗后的 args, 越权时声称的 elder_id 或 None)。

    - `args` 不带 `elder_id` → 原样放行（工具入参由业务字段构成，与身份无关）；
    - 带 `elder_id` 且与 `runtime_context` 身份一致 → **丢弃该字段**再发送：
      模型传入的身份字段即使碰巧一致也不可信（AGENTS §4.9「传入一律丢弃」）；
    - 带 `elder_id` 但与身份不一致 / 上下文没有身份可校验 → 拒绝，
      返回声称的 elder_id 供审计留痕。
    """
    if not args:
        return {}, None
    cleaned = dict(args)
    claimed = cleaned.get("elder_id")
    if claimed is None:
        return cleaned, None
    identity = get_identity()
    expected = identity.elder_id if identity is not None else None
    if expected is not None and isinstance(claimed, str) and _same_uuid(claimed, expected):
        cleaned.pop("elder_id", None)
        return cleaned, None
    return cleaned, str(claimed)


# ---------------------------------------------------------------------------
# 异常分类与结果提取（纯函数，便于单测）
# ---------------------------------------------------------------------------


def _unwrap_group(exc: BaseException) -> list[BaseException]:
    """摊平 anyio 任务组的 ExceptionGroup，逐叶分类。"""
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in exc.exceptions:
            leaves.extend(_unwrap_group(sub))
        return leaves
    return [exc]


def _classify_exception(exc: Exception) -> ErrorKind:
    """异常 → 四分类。只看异常类别，不读异常消息原文（防内部细节外泄进结果）。"""
    leaves = _unwrap_group(exc)
    if any(isinstance(leaf, TimeoutError) for leaf in leaves):
        # asyncio.TimeoutError 在 3.11+ 即内置 TimeoutError；SDK 读超时最终也落在它上
        return "timeout"
    if any(isinstance(leaf, MCPError) for leaf in leaves):
        # 服务端返回了 JSON-RPC error（方法不存在 / 参数不符 schema 等协议级拒绝）
        return "bad_response"
    # 连接拒绝、TLS、HTTP 非 2xx、SDK 内部错误……一律按上游临时故障，可退避重试
    return "upstream_error"


def _extract_payload(result: CallToolResult) -> tuple[Any | None, bool]:
    """从 CallToolResult 提取业务载荷 → (payload, shape_ok)。

    取值顺序：structuredContent（SDK 2.x 对 dict 返回值自动生成）→ 首个 TextContent
    的 JSON 文本 → 兜底 {"text": 原文}；两处皆空视为协议形态异常（bad_response）。
    """
    structured = result.structured_content
    if structured is not None:
        return structured, True
    for item in result.content:
        if isinstance(item, TextContent):
            try:
                return json.loads(item.text), True
            except ValueError:
                return {"text": item.text}, True
    return None, False


def _result_from_call(result: CallToolResult) -> MCPResult:
    """CallToolResult → MCPResult（把 SDK 形态收敛到契约 §5.4）。"""
    payload, shape_ok = _extract_payload(result)

    if result.is_error:
        # SDK 层工具执行失败：保留服务端载荷供 bad_request 细化；无载荷只给分类
        if isinstance(payload, dict) and payload.get("ok") is False:
            return MCPResult(ok=False, data=payload, error="upstream_error")
        return MCPResult(ok=False, data=None, error="upstream_error")

    if not shape_ok or payload is None:
        logger.warning(
            "mcp_bad_response_shape",
            extra={"trace_id": get_trace_id() or "-", "content_items": len(result.content)},
        )
        return MCPResult(ok=False, data=None, error="bad_response")

    if isinstance(payload, dict) and payload.get("ok") is False:
        # 服务端业务失败（本仓 weather server 约定：失败也作为正常结果返回，ok 字段区分）
        return MCPResult(ok=False, data=payload, error="upstream_error")

    if isinstance(payload, dict):
        return MCPResult(ok=True, data=payload, error=None)
    # 非 dict 成功载荷（字符串/列表等）：包一层保持 data 恒为 dict 的调用方假设
    return MCPResult(ok=True, data={"result": payload}, error=None)


def tool_error_class(result: MCPResult) -> ToolErrorClass | None:
    """MCPResult 四分类 → 工具层四类失败。**映射唯一口径在本函数**，调用方不得另立。

    | MCPResult.error | ToolResult.error_class | 依据（TD §6.4 / 契约 §5.3/§5.4） |
    | --------------- | ---------------------- | -------------------------------- |
    | timeout         | transient              | 上游慢/超时：退避重试 ≤2 后降级话术（TD §6.4 天气超时示例、AC-02） |
    | upstream_error  | transient              | 上游临时故障：同上；但服务端业务码 `bad_request` 细化为 `bad_args` |
    | bad_response    | transient              | 协议形态异常：模型改参数无济于事，按临时故障降级，不让模型空转烧预算 |
    | unauthorized    | forbidden              | 越权：拒绝执行 + 审计 + trace_id（AC-29、闸门③） |

    `bad_request` 细化说明：城市名无法解析这类**参数错**应「错误回传模型重试并计入
    预算」（TD §6.4 参数错行）；weather server 以 {"ok": false, "error": "bad_request"}
    表达此语义，MCPResult.data 里带着它时映射为 bad_args 而非 transient。
    `not_found` 由工具层对具体业务（曲目不存在等）判定，MCP 传输层不产生该分类。
    """
    if result.ok:
        return None
    if result.error == "unauthorized":
        return "forbidden"
    if (
        isinstance(result.data, dict)
        and result.data.get("ok") is False
        and result.data.get("error") == "bad_request"
    ):
        return "bad_args"
    return "transient"


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class MCPClient:
    """MCP 工具客户端（发现 + 调用 + 超时 + 越权审计）。

    Args:
        settings: 全量配置（mcp_call_timeout_seconds 等）。
        server_url: 目标 MCP server 基地址；缺省取 `settings.mcp_weather_server_url`。
            Day 8 的 mock-operations 可传 `settings.mcp_operations_server_url` 复用本类。
        session_factory: 传输工厂注入点（测试用内存流，默认 streamable-http）。
        audit_sink: 审计出口注入点（缺省 `DbAuditSink` 落库；测试注入空实现/收集器）。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        server_url: str | None = None,
        session_factory: SessionFactory | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._settings = settings
        base = server_url if server_url is not None else settings.mcp_weather_server_url
        self._endpoint = base.strip().rstrip("/") + "/mcp"
        # 总超时兜底（AGENTS §7 硬性数值经 Settings 注入，不散写数字）
        self._timeout_seconds = float(settings.mcp_call_timeout_seconds)
        self._session_factory: SessionFactory = (
            session_factory
            if session_factory is not None
            else _http_session_factory(self._endpoint)
        )
        # 审计不可关（安全要求）：缺省落库，测试经注入点替换
        self._audit_sink: AuditSink = audit_sink if audit_sink is not None else DbAuditSink()

    # ---------- 对外 API ----------

    async def list_tools(self) -> MCPResult:
        """工具发现：返回 {"tools": [{"name", "description"}]}；失败按四分类。"""
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with (
                    self._session_factory() as streams,
                    ClientSession(
                        streams[0], streams[1], read_timeout_seconds=self._timeout_seconds
                    ) as session,
                ):
                    await session.initialize()
                    listed = await session.list_tools()
        except TimeoutError:
            logger.warning("mcp_discover_timeout", extra={"trace_id": get_trace_id() or "-"})
            return MCPResult(ok=False, data=None, error="timeout")
        except Exception as exc:
            kind = _classify_exception(exc)
            logger.warning(
                "mcp_discover_failed",
                extra={"trace_id": get_trace_id() or "-", "error_kind": kind},
            )
            return MCPResult(ok=False, data=None, error=kind)

        data = {
            "tools": [
                {"name": tool.name, "description": tool.description or ""} for tool in listed.tools
            ]
        }
        return MCPResult(ok=True, data=data, error=None)

    async def call_tool(self, tool_name: str, args: Mapping[str, Any] | None = None) -> MCPResult:
        """调用一个 MCP 工具；越权短路、超时、审计全在本方法收口。

        顺序：闸门③身份校验（不建连，越权零成本拒绝）→ SDK 建连调用（总超时罩住）
        → 计耗时 → 写审计（每次调用必写，契约 §5.4）→ 返回 MCPResult。
        """
        started = time.perf_counter()
        cleaned_args, claimed_elder_id = _resolve_args_identity(args)
        if claimed_elder_id is not None:
            result: MCPResult = MCPResult(ok=False, data=None, error="unauthorized")
        else:
            result = await self._invoke(tool_name, cleaned_args)
        latency_ms = int((time.perf_counter() - started) * 1000)
        outcome = result.error or "ok"

        if claimed_elder_id is not None:
            identity = get_identity()
            await self._audit_sink.write(
                AuditEntry(
                    action=ACTION_IDENTITY_OVERRIDE,
                    tool_name=tool_name,
                    latency_ms=latency_ms,
                    outcome=outcome,
                    detail={
                        "claimed_elder_id": claimed_elder_id,
                        "expected_elder_id": identity.elder_id if identity is not None else None,
                    },
                )
            )
        else:
            await self._audit_sink.write(
                AuditEntry(
                    action=ACTION_TOOL_CALL,
                    tool_name=tool_name,
                    latency_ms=latency_ms,
                    outcome=outcome,
                )
            )

        logger.info(
            "mcp_tool_call",
            extra={
                "trace_id": get_trace_id() or "-",
                "tool_name": tool_name,
                "latency_ms": latency_ms,
                "outcome": outcome,
            },
        )
        return result

    # ---------- 内部 ----------

    async def _invoke(self, tool_name: str, args: Mapping[str, Any]) -> MCPResult:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with (
                    self._session_factory() as streams,
                    ClientSession(
                        streams[0], streams[1], read_timeout_seconds=self._timeout_seconds
                    ) as session,
                ):
                    await session.initialize()
                    call_result = await session.call_tool(
                        tool_name,
                        arguments=dict(args) if args else None,
                        read_timeout_seconds=self._timeout_seconds,
                    )
        except TimeoutError:
            logger.warning(
                "mcp_call_timeout",
                extra={"trace_id": get_trace_id() or "-", "tool_name": tool_name},
            )
            return MCPResult(ok=False, data=None, error="timeout")
        except Exception as exc:
            kind = _classify_exception(exc)
            logger.warning(
                "mcp_call_failed",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "tool_name": tool_name,
                    "error_kind": kind,
                    "exc_type": type(exc).__name__,
                },
            )
            return MCPResult(ok=False, data=None, error=kind)

        if not isinstance(call_result, CallToolResult):
            # 2.x call_tool 的返回联合里还有 InputRequiredResult / 通用 Result；
            # Day 6 工具面不涉及 HITL elicitation，收到即视为协议形态异常。
            logger.warning(
                "mcp_unexpected_result_type",
                extra={"trace_id": get_trace_id() or "-", "tool_name": tool_name},
            )
            return MCPResult(ok=False, data=None, error="bad_response")
        return _result_from_call(call_result)


__all__ = [
    "ACTION_IDENTITY_OVERRIDE",
    "ACTION_TOOL_CALL",
    "AuditEntry",
    "AuditSink",
    "MCPClient",
    "MCPResult",
    "ToolErrorClass",
    "tool_error_class",
]
