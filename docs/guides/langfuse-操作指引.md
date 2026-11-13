# Langfuse 操作指引（Day 10 B1 · v4 共享实例）

> 面向本项目成员的 Langfuse 日常操作手册。覆盖：接入、取 key、查 trace、常见坑。
> 实例形态出处：dev-B 计划件 §8.1-⑨（2026-11-13 裁决：v4 全家桶常驻 dev-B 主机）。

## 1. 实例形态与连接

| 项 | 值 |
| --- | --- |
| 版本 | Langfuse **v4**（六容器全家桶：web / worker / postgres / redis / clickhouse / minio） |
| 地址 | `http://<dev-B的局域网IP>:3000`（dev-B 本机即 `http://localhost:3000`） |
| 项目 | **`kangban`**（本项目 traces 落这里；`ai-eval` 是历史冒烟项目，勿混用） |
| 后端写入 | OTel SDK → OTLP/HTTP，端点 `{LANGFUSE_HOST}/api/public/otel/v1/traces`（`app/core/observability.py`） |
| 自起兜底 | `docker compose --profile langfuse up`（repo 编排，dev-A 落地；profile 化默认不启动） |

> ⚠️ **两套容器名不要混**：dev-B 主机当前跑的实例是独立起的（容器名 `langfuse-*-1`）；
> repo 内 compose 起的是 `silvermate-langfuse-*`。同一台机**只能起一套**（3000 端口冲突）。

## 2. 首次接入五步

1. **注册**：浏览器开 `http://<dev-B的局域网IP>:3000` → Sign up（本地实例，邮箱+密码自注册；dev-B 主机实例无需邀请码）。
2. **选项目**：登录后进入 **kangban** 项目（不是 ai-eval）。
3. **生成 key**：Settings → **API Keys** → Create new API keys → **立即复制 pk/sk**
   （`pk-lf-…` 公钥、`sk-lf-…` 密钥；**sk 只在创建时完整显示一次**，关窗即不可再看，丢了只能重新生成）。
4. **填 `.env`**（四个变量，真值只进本地 `.env`，**绝不进 git / 群聊 / 文档**）：

   ```bash
   LANGFUSE_ENABLED=true
   LANGFUSE_HOST=http://<dev-B的局域网IP>:3000
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   ```

5. **重启后端**：`uv run uvicorn app.main:app --port 8000`（观测初始化在 lifespan，
   改 key 必须重启才生效）。启动日志无报错即接入成功；发起一次对话即可在 UI Traces 页看到。

> 排错快捷键：key 无效时后端日志会反复出现 `Failed to export` + 401。
> 先确认 pk 前缀在 kangban 项目里（不是 ai-eval），再确认 HOST 可达。

## 3. 日常操作

### 3.1 UI 查 trace

- Traces 页顶部搜索框直接粘贴 **trace_id**（32 位 hex；与后端日志/接口响应里的系统 trace_id
  **同值同名**，Day 10 AC-31 已验证互查）。
- 点进 trace 看 span 链：`http` → `asr` → `route` → `llm_first_token` → `llm_usage`(GENERATION)
  → `tool` → `tts_first_packet` → `response_segment`，每个 span 有真实耗时与属性。
- `llm_usage` 属性含 provider/model/prompt_tokens/completion_tokens/cache_hit/miss_tokens。

### 3.2 ClickHouse 直查（v4 验数通道）

v4 实例开启 events_only 模式（legacy 查询 API 禁用），精确验数走 ClickHouse：

```bash
# 某条 trace 的完整 span 链与耗时
docker exec langfuse-clickhouse-1 clickhouse-client --query \
  "SELECT type, name, dateDiff('millisecond', start_time, end_time) AS ms
   FROM default.events_core WHERE trace_id = '<trace_id>' ORDER BY start_time"

# 最近 5 分钟的 trace 概览
docker exec langfuse-clickhouse-1 clickhouse-client --query \
  "SELECT trace_id, count() AS spans FROM default.events_core
   WHERE start_time > now() - INTERVAL 5 MINUTE GROUP BY trace_id ORDER BY spans DESC"
```

常用列名：`metadata_names` / `metadata_values`（属性键值对）、`start_time`/`end_time`。

## 4. 已知坑（联调实测）

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| 手工推 OTLP 后 UI 里 trace **延迟约 10 分钟**才出现 | v4 需请求头 `x-langfuse-ingestion-version: 4` 才实时摄入 | 后端代码已带该 header（`observability.py`）；自己手工推时记得补 |
| span 批次间歇性丢失（某 trace 缺尾部 1-2 个 span） | 宿主资源紧张时 worker 的 redis 连接 socket timeout，摄入管道停滞 | `docker restart langfuse-langfuse-worker-1` 后即恢复；重跑一轮对话补证 |
| UI/legacy API 查询报错 | events_only 模式禁用 legacy 查询 | 走 §3.2 ClickHouse 直查 |
| 宿主 FunASR 容器重启循环拖垮全机 | 内存挤占 | 停闲置容器（曾停 `silvermate-funasr` 缓解）；Langfuse 本身六容器常驻约 3-4GB |
| token 数字显示 `***` | **已知 bug**：`redact.py` `_SECRET_KEY_RE` 误伤 `*_tokens` 字段名（待 dev-A 修复） | `/api/metrics` 快照数字不受影响；修复后明细恢复 |

## 5. 安全红线

1. **key 真值只进本地 `.env`**（已 gitignore）；文档/群聊/commit 里一律写 `pk-lf-…` 占位。
2. pk/sk 通过 dev-B 实例 UI **各自生成**，不要把别人的 key 复制进自己的 `.env`（按人可审计）。
3. span 内容经 `app/core/redact.py` 统一脱敏（手机号/身份证/房间号/明文密钥），**不要**为方便排查在 attrs 里塞原始 PII；脱敏验证口径见 `eval/reports/day10_langfuse_trace.md`。
4. 演示前跑一遍 §3.2 的 ClickHouse 查询确认数据新鲜，避免现场空页。

## 6. 关联文档

- 观测验收证据：`eval/reports/day10_langfuse_trace.md`
- 成本口径与素材：`eval/reports/day10_cost_brief.md`
- 变量定义：`.env.example` Langfuse 段
- 容器编排：`docker-compose.yml`（`langfuse` profile）
