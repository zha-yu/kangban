"""天气 MCP server 入口（Day 6 · N24，本地进程，端口 7101）。

工具 schema 严格照冻结契约（contracts.md §5.4 / N00）：`get_weather(city)`，
单一 `city` 字符串参数。协议用仓库锁定的 mcp SDK（pyproject `mcp>=1.1`，
锁装的 2.x 中 `FastMCP` 已更名为 `MCPServer`，`mcp.server.mcpserver`），
客户端与 mcp-servers 共用同一协议实现，不自造第二套 JSON-RPC。

两种传输（MCP 官方形态，选一个启动）：

- `--stdio`（默认）：进程 stdin/stdout 走 JSON-RPC，供 MCP 客户端以子进程方式拉起；
  此时**日志必须走 stderr**，stdout 是协议通道，混入日志即毁帧；
- `--http`：streamable-http 监听 `127.0.0.1:7101`（AGENTS §3 端口约定：weather-mcp 7101），
  对外暴露由 nginx/Compose 收口，留 Day 10。`stateless_http + json_response` 让每次
  请求独立、响应为纯 JSON（无 SSE 帧），curl 可直接调试。

启动（仓库根执行）：

    uv run python mcp-servers/weather/server.py            # stdio
    uv run python mcp-servers/weather/server.py --http     # 127.0.0.1:7101

需要的 `.env` 配置（全部已有占位，不新增配置项）：

    WEATHER_API_PROVIDER=...        # 上游供应商标识（未冻结，仅记录用）
    WEATHER_API_BASE_URL=...        # 上游 current-weather 端点
    WEATHER_API_KEY=...             # 上游 key；为空时 get_weather 一律明确失败
    WEATHER_CACHE_TTL_SECONDS=600   # 进程内缓存 TTL（契约 §5.4 默认 600s）

脚本以文件方式运行（目录名含连字符不能作包名），因此先做 sys.path 引导：
仓库根（导入 app.core.*）与本级目录（导入 weather_core）。
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
from weather_core import WeatherService  # noqa: E402

from app.core.config import get_settings  # noqa: E402  （sys.path 引导后才能 import）
from app.core.logging import configure_logging  # noqa: E402

# AGENTS §3 端口约定：weather-mcp 7101；本地进程只绑环回，不裸奔公网
SERVER_NAME = "silvermate-weather"
BIND_HOST = "127.0.0.1"
BIND_PORT = 7101
# 端口覆盖变量：仅本地进程运维参数，不进 Settings（任务口径「不要动 Settings」）；
# 全局配置基线仍以 .env.example 的 17 组变量为准，此处散读是唯一豁免点。
PORT_ENV_VAR = "WEATHER_MCP_PORT"

TOOL_DESCRIPTION = (
    "查询一个城市当前的天气（温度、天气状况与数据时间戳）。"
    "无可用数据源或上游失败时返回明确的失败结果，不会编造天气。"
)


def build_server() -> tuple[MCPServer, WeatherService]:
    """装配 MCP server：配置全部经 Settings 读取，禁止 os.environ 散读。"""
    settings = get_settings()
    service = WeatherService(
        api_key=settings.weather_api_key,
        base_url=settings.weather_api_base_url,
        cache_ttl_seconds=settings.weather_cache_ttl_seconds,
        provider=settings.weather_api_provider,
    )

    server = MCPServer(
        SERVER_NAME,
        instructions="SilverMate 天气工具：唯一真实外部接口（PRD 边界）。",
    )

    @server.tool(name="get_weather", description=TOOL_DESCRIPTION)
    async def get_weather(city: str) -> dict[str, Any]:
        """查询城市当前天气；失败返回 {"ok": false, ...}，不抛异常、不编造数据。"""
        return await service.fetch_city(city)

    return server, service


def _run_stdio(server: MCPServer) -> None:
    """stdio 传输：stdout 只承载 JSON-RPC 帧，日志一律写 stderr。"""
    settings = get_settings()
    logging.basicConfig(
        stream=sys.stderr,
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("mcp_servers.weather").info(
        "weather_mcp_starting", extra={"transport": "stdio"}
    )
    server.run("stdio")


def _resolve_bind_port() -> int:
    """HTTP 绑定端口：默认 7101，可用 `WEATHER_MCP_PORT` 环境变量覆盖。

    非法值（非整数）不崩进程：告警后回落默认端口，避免一次手误把天气工具打挂。
    """
    raw = os.environ.get(PORT_ENV_VAR, "").strip()
    if not raw:
        return BIND_PORT
    try:
        return int(raw)
    except ValueError:
        logging.getLogger("mcp_servers.weather").warning(
            "weather_mcp_port_invalid_fallback_default",
            extra={"trace_id": "-", "env_var": PORT_ENV_VAR},
        )
        return BIND_PORT


def _run_http(server: MCPServer) -> None:
    """streamable-http 传输：绑定环回 7101，纯 JSON 响应便于 curl 调试。"""
    configure_logging(service="silvermate-weather")
    port = _resolve_bind_port()
    logging.getLogger("mcp_servers.weather").info(
        "weather_mcp_starting",
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
    parser = argparse.ArgumentParser(description="SilverMate 天气 MCP server（get_weather）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--stdio", action="store_true", help="stdio 传输（默认，MCP 客户端子进程拉起）"
    )
    mode.add_argument("--http", action="store_true", help="streamable-http，监听 127.0.0.1:7101")
    args = parser.parse_args()

    server, _service = build_server()
    if args.http:
        _run_http(server)
    else:
        _run_stdio(server)


if __name__ == "__main__":
    main()
