"""mock-operations MCP server 入口（Day 8 范围提前件，端口 7102）。

两个工具把 Agent 的对外副作用收敛为「Mock 落库」（技术设计文档 §1.1）：
`create_work_order`（工单落 `work_orders`，护工看板可查）与
`send_family_notification`（家属通知落 `notifications`，`is_mock=true`，
AC-38）。Mock 落库可查是验收口径，唯一不做的是真实外发（红线：不接
真实微信 / 短信 / 电话）。

工具 schema 冻结口径（contracts.md v1.8 / 计划
docs/plans/adhoc-20261110-mockoperations提前实现.md）：

- `create_work_order(subject_elder_id, order_type, severity, dedupe_token?, event_id?, note?)`
- `send_family_notification(subject_elder_id, notification_type, payload, dedupe_key?, channel?)`

身份口径（闸门③）：`subject_elder_id` 由后端 ToolExecutor 从 runtime_context
注入（Day 8 接线），模型传入无效；`elder_id` 键会被 MCPClient 在客户端丢弃。

协议用仓库锁定的 mcp SDK（pyproject `mcp>=1.1`，锁装的 2.x 中 `FastMCP`
已更名为 `MCPServer`，`mcp.server.mcpserver`），与 weather server 同构，
不自造第二套 JSON-RPC。

两种传输（MCP 官方形态，选一个启动）：

- `--stdio`（默认）：进程 stdin/stdout 走 JSON-RPC，供 MCP 客户端以子进程方式拉起；
  此时**日志必须走 stderr**，stdout 是协议通道，混入日志即毁帧；
- `--http`：streamable-http 监听 `127.0.0.1:7102`（AGENTS §3 端口约定：
  mock-operations-mcp 7102），`stateless_http + json_response` 让每次请求独立、
  响应为纯 JSON，curl 可直接调试。

启动（仓库根执行）：

    uv run python mcp-servers/mock-operations/server.py            # stdio
    uv run python mcp-servers/mock-operations/server.py --http     # 127.0.0.1:7102

脚本以文件方式运行（目录名含连字符不能作包名），因此先做 sys.path 引导：
仓库根（导入 app.*）与本级目录（导入 operations_core）。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent
for _path in (str(_REPO_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from mcp.server.mcpserver import MCPServer  # noqa: E402  （mcp 2.x：FastMCP 更名 MCPServer）
from operations_core import OperationsService  # noqa: E402

from app.core.config import get_settings  # noqa: E402  （sys.path 引导后才能 import）
from app.core.logging import configure_logging  # noqa: E402

# AGENTS §3 端口约定：mock-operations-mcp 7102；本地进程只绑环回，不裸奔公网
SERVER_NAME = "silvermate-mock-operations"
BIND_HOST = "127.0.0.1"
BIND_PORT = 7102
# 端口覆盖变量：仅本地进程运维参数，不进 Settings（同 weather server 的豁免口径）；
# 全局配置基线仍以 .env.example 为准，此处散读是唯一豁免点。
PORT_ENV_VAR = "MOCK_OPERATIONS_MCP_PORT"

CREATE_WORK_ORDER_DESCRIPTION = (
    "为一位长者创建一张照护工单（红色健康事件或漏服药时使用）。"
    "severity 必须与事件的分级一致；同一事件重复调用会返回已有工单，不会重复派单。"
    "工单由照护台护工处理，本工具不能指定处理人。"
)

SEND_FAMILY_NOTIFICATION_DESCRIPTION = (
    "给一位长者绑定的家属发送站内通知（Mock：落库即可查，不真实外发）。"
    "通知对象由系统按家属绑定解析，不能指定个人；"
    "相同 dedupe_key 的重复调用只落一条记录。"
)


def build_server() -> tuple[MCPServer, OperationsService]:
    """装配 MCP server；写库全部委托 app/services（唯一写库层）。"""
    from app.db import session_factory

    service = OperationsService(session_factory=session_factory)

    server = MCPServer(
        SERVER_NAME,
        instructions="SilverMate 工单与家属通知工具（Mock 落库，非真实外发）。",
    )

    @server.tool(name="create_work_order", description=CREATE_WORK_ORDER_DESCRIPTION)
    async def create_work_order(
        subject_elder_id: str,
        order_type: str,
        severity: str,
        dedupe_token: str | None = None,
        event_id: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """创建照护工单；失败返回 {"ok": false, ...}，不抛异常、不编造结果。"""
        return await service.create_work_order(
            {
                "subject_elder_id": subject_elder_id,
                "order_type": order_type,
                "severity": severity,
                "dedupe_token": dedupe_token,
                "event_id": event_id,
                "note": note,
            }
        )

    @server.tool(name="send_family_notification", description=SEND_FAMILY_NOTIFICATION_DESCRIPTION)
    async def send_family_notification(
        subject_elder_id: str,
        notification_type: str,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """发送家属通知（落库）；失败返回 {"ok": false, ...}，不抛异常、不编造结果。"""
        return await service.send_family_notification(
            {
                "subject_elder_id": subject_elder_id,
                "notification_type": notification_type,
                "payload": payload,
                "dedupe_key": dedupe_key,
                "channel": channel,
            }
        )

    return server, service


def _run_stdio(server: MCPServer) -> None:
    """stdio 传输：stdout 只承载 JSON-RPC 帧，日志一律写 stderr。"""
    settings = get_settings()
    logging.basicConfig(
        stream=sys.stderr,
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("mcp_servers.mock_operations").info(
        "mock_operations_mcp_starting", extra={"transport": "stdio"}
    )
    server.run("stdio")


def _resolve_bind_port() -> int:
    """HTTP 绑定端口：默认 7102，可用 `MOCK_OPERATIONS_MCP_PORT` 环境变量覆盖。

    非法值（非整数）不崩进程：告警后回落默认端口，避免一次手误把工单通道打挂。
    """
    raw = os.environ.get(PORT_ENV_VAR, "").strip()
    if not raw:
        return BIND_PORT
    try:
        return int(raw)
    except ValueError:
        logging.getLogger("mcp_servers.mock_operations").warning(
            "mock_operations_mcp_port_invalid_fallback_default",
            extra={"trace_id": "-", "env_var": PORT_ENV_VAR},
        )
        return BIND_PORT


def _run_http(server: MCPServer) -> None:
    """streamable-http 传输：绑定环回 7102，纯 JSON 响应便于 curl 调试。"""
    configure_logging(service="silvermate-mock-operations")
    port = _resolve_bind_port()
    logging.getLogger("mcp_servers.mock_operations").info(
        "mock_operations_mcp_starting",
        extra={"transport": "streamable-http", "bind_host": BIND_HOST, "bind_port": port},
    )
    server.run(
        "streamable-http",
        host=BIND_HOST,
        port=port,
        stateless_http=True,
        json_response=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SilverMate mock-operations MCP server（create_work_order / send_family_notification）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--stdio", action="store_true", help="stdio 传输（默认，MCP 客户端子进程拉起）"
    )
    mode.add_argument("--http", action="store_true", help="streamable-http，监听 127.0.0.1:7102")
    args = parser.parse_args()

    server, _service = build_server()
    if args.http:
        _run_http(server)
    else:
        _run_stdio(server)


if __name__ == "__main__":
    main()
