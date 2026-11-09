"""MCP 客户端：工具发现 / 调用 / 超时 / 越权审计（Day 6 · N25，契约 §5.4）。

身份只能从服务端 runtime_context 注入，模型自报的 elder_id 一律丢弃（AC-29）。
审计写入经 services 层唯一写库入口 `audit_log_service.record_audit`（默认
`DbAuditSink` 落库并独立提交；审计失败只告警，不阻断工具调用）——分层红线
「写库只走 services」不破。传输直接用仓库锁定的 mcp SDK streamable-http，
不自造第二套 JSON-RPC。
"""

from __future__ import annotations

from app.mcp_client.client import (
    ACTION_IDENTITY_OVERRIDE,
    ACTION_TOOL_CALL,
    AuditEntry,
    AuditSink,
    MCPClient,
    MCPResult,
    ToolErrorClass,
    tool_error_class,
)

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
