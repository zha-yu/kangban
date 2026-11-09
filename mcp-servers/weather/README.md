# 天气 MCP server（Day 6 · N24）

`get_weather(city)` 工具的 MCP server：银伴系统**唯一真实外部接口**（其余外部系统一律 Mock）。
协议实现用仓库锁定的 mcp SDK（pyproject `mcp>=1.1`，锁装 2.x，`MCPServer`），
本地进程只绑环回（AGENTS §3 端口约定：weather-mcp 7101；对外暴露由 nginx / Compose
收口，留 Day 10）。

## 启动（仓库根执行，二选一）

```bash
uv run python mcp-servers/weather/server.py            # stdio 传输（默认）：stdout 走 JSON-RPC，日志走 stderr
uv run python mcp-servers/weather/server.py --http     # streamable-http：127.0.0.1:7101，纯 JSON 响应
```

`--http` 模式端口默认 7101（AGENTS §3 端口约定），可用环境变量 `WEATHER_MCP_PORT`
覆盖（仅本地运维参数，不进 `Settings`，不新增配置项）；非法值回落默认端口。

stdio 模式可直接用一行 JSON-RPC 冒烟（`initialize` 为例）：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
  | uv run python mcp-servers/weather/server.py --stdio
```

`--http` 模式（stateless + 纯 JSON）curl 调试：

```bash
curl -s http://127.0.0.1:7101/mcp -H 'Content-Type: application/json' -d \
  '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_weather","arguments":{"city":"杭州"}}}'
```

## `.env` 需要的配置（占位符已在 `.env.example`，不新增配置项）

| 变量 | 说明 |
| --- | --- |
| `WEATHER_API_PROVIDER` | 上游供应商标识（未冻结，仅记录用） |
| `WEATHER_API_BASE_URL` | 上游 current-weather 端点（OpenWeather 兼容查询串形态，见下方假设） |
| `WEATHER_API_KEY` | 上游 key；**为空时 get_weather 一律明确失败，不编造天气** |
| `WEATHER_CACHE_TTL_SECONDS` | 进程内缓存 TTL（契约 §5.4，默认 600） |

key 一律经 Pydantic Settings（`app/core/config.py`）读取，禁止写死。

## 协议与工具契约

- MCP 方法面：`initialize` / `tools/list` / `tools/call`（由 SDK 实现，客户端与
  `app/mcp_client/` 共用同一 SDK，不自造第二套 JSON-RPC）。
- 工具名：`get_weather`；入参：`city`（string，必填，非空、≤64 字符、限安全字符）。
- 成功：`{"ok": true, "city", "condition", "temperature_c", "observed_at", "cached"}`
  —— `observed_at` 是**数据时间戳**（上游观测时间，缓存命中不刷新）。
- 失败：`{"ok": false, "error", "message"}`
  - `error` ∈ `bad_request` / `missing_api_key` / `upstream_timeout` /
    `upstream_unavailable` / `bad_upstream_response`；
  - `message` 是长者可读话术，**不含**完整 URL、key、上游响应体等内部细节。
- 降级顺序（TD §6.4 / AC-02）：上游失败 → 有旧缓存先回退旧缓存（`cached=true`，
  原 `observed_at` 如实标注数据时间）→ 无缓存才明确失败；任何路径不编造天气。

## 假设与边界

- **假设（自裁，TD 未定义 provider）**：上游按 OpenWeatherMap 兼容 current-weather
  形态解析（`weather[0].description` / `main.temp` / `dt`）；换真实供应商只需替换
  `weather_core.httpx_fetch` 或注入自定义 fetch，核心逻辑零改动。
- 除天气上游 API 外**无任何外网访问**（AGENTS §4.5）。
- 缓存为进程内 TTL 缓存（重启即清空），不是长期记忆存储。

## 测试

```bash
uv run pytest tests/test_mcp_weather.py -q
```

测试用注入的假 fetch（不连真实外网、不用真实 key），覆盖：正常返回 / 无 key 明确
失败 / 上游超时明确失败（含超时回退旧缓存）/ 入参校验 / 缓存命中带数据时间戳。
