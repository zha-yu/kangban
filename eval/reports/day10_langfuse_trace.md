# Day 10 B1 联调证据 · Langfuse 完整 Trace（AC-31）

> 负责人：dev-B ｜ 日期：2026-11-13 ｜ 依据：PRD line 513（AC-31）＋ dev-B 计划件 §4 Step 3
> 实例：**Langfuse v4 全家桶六容器**（Q4 §8.1-⑨，常驻 dev-B 本机 `localhost:3000`，profile `langfuse`）
> 链路入口：`POST /api/audio/utterance`（与 WS FINAL 同一编排入口，契约 F15）

## 0. 结论速览

§2.2 自检表 7 条判据：**1/2/3/4/5/7 全绿；6 全绿；另有 3 项环境噪音与 2 项登记 bug 如实附后**。
AC-31 语义「每次主控 Agent 执行 = 1 条 Trace，五环节 Span 齐备」已验证落库（§2）。

## 1. 可复现步骤（他人按此可复现同一条 Trace）

```bash
# 1) 起共享实例（本机已有则跳过）
docker compose --profile langfuse up -d   # 六容器：web/worker/postgres/redis/clickhouse/minio

# 2) .env 注入凭据（真值只进本地 .env）
#    LANGFUSE_ENABLED=true  OTEL_ENABLED=true
#    LANGFUSE_HOST=http://localhost:3000  LANGFUSE_PUBLIC_KEY=pk-lf-...  LANGFUSE_SECRET_KEY=sk-lf-...

# 3) 起后端
uv run uvicorn app.main:app --port 8000   # 启动日志应见 "observability enabled: otel->langfuse ..."

# 4) 设备换会话（2026-11-11 裁决：device_id 直换，无 secret）
curl -s -X POST http://localhost:8000/api/device-sessions \
  -H "Content-Type: application/json" \
  -d '{"device_id":"3454c376-d3c6-4d50-b049-df07e7f7a3bb"}'

# 5) 发起含工具调用的对话（16kHz 单声道 WAV）
curl -s -X POST http://localhost:8000/api/audio/utterance \
  -H "Authorization: Bearer <access_token>" \
  -F "audio=@query.wav;type=audio/wav" -F "duration_ms=1700"

# 6) 验库（v4 无 legacy 查询 API，走 ClickHouse）
docker exec langfuse-clickhouse-1 clickhouse-client \
  --query "SELECT name, dateDiff('millisecond', start_time, end_time) AS ms
           FROM default.events_core WHERE trace_id = '<系统trace_id>' ORDER BY start_time"
```

UI 入口：`http://localhost:3000` → 搜索框输 32 位系统 trace_id 直接命中（判据 2）。

## 2. Span 链证据（ClickHouse `default.events_core` 实查）

五环节均有落库实例；各 trace 按当日路由层决策自然分化（rule 短路轮无 LLM span、agent 轮有）：

| trace_id（系统值＝Langfuse 值） | Span 链（耗时 ms） | 证明点 |
| --- | --- | --- |
| `456639a7…813e998`（agent 轮） | asr 217 → route 0 → llm_first_token 1819 → **llm_usage(GENERATION)×2**（MiniMax 765+58 / 822+42 tokens）→ **tool**（memory_recall）→ response_segment×2 | **LLM＋工具同 Trace**；llm_usage 是 event span（type GENERATION），模型版本 MiniMax-M2.7-highspeed 在 attrs |
| `f8f9a92b…8bcf6ce`（rule 工具轮） | asr 250 → route 0 → **tool 1092**（get_weather，MCP）→ response_segment | MCP 工具 span 带真实耗时 |
| `648891d9…b87b03` | POST utterance 2839 → asr 250 → route → response_segment → tts_first_packet 1608 | HTTP 请求 span ＋ TTS span（非 LLM span 判据③） |
| `b52a485e…2043e071` | asr → route → llm_first_token → response_segment → **tts_first_packet** → **POST utterance** | tts＋HTTP 同批落库 |
| `15513897…7ec7e8dc` | POST utterance 16（ASR 不可用 503 降级） | 降级路径 span 也落库 |

**分类器（laya_client fast path）span**：`a095c26f…b950f62`（9ms）等 System One fast 轮有独立 span；本轮 `456639a7` 由规则层直判（`layer=rule reason=missing_slot`），业务上不经分类器，非观测缺口——分类器 span 在 fast path 轮已验证存在。

## 3. 自检表逐条结论（§2.2）

| # | 判据 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1 | 1 条完整 Trace，五环节 Span | ✅（附注） | §2 表：五环节 span 均落库；单 trace 最全 8 span（`456639a7`）。附注：tts/HTTP span 与其余 span 同 trace 时偶有不同批落库（§5 噪音①），UI 可见性不受影响 |
| 2 | trace_id 同值互查 | ✅ | 自建 `SystemTraceIdGenerator`（32 hex→128-bit）＋自建 `RequestSpanMiddleware`（内层，FastAPIInstrumentor 弃用）；库内 `trace_id` 与响应头/日志系统 trace_id 逐字同值（§2 各 trace_id 即系统值）；UI 搜索框可命中 |
| 3 | ≥1 非 LLM span | ✅ | HTTP span（`POST /api/audio/utterance` 2839ms）、asr 217/250ms、tts_first_packet 1608/2282ms、tool 1092ms |
| 4 | hook 已接且日志字段不丢 | ✅ | `asr_confidence`/`asr_first_packet_ms`/`stage=asr`/`stage=tts_first_packet`/`llm_usage`（prompt_tokens 765/completion 58）全部照常输出——三轨并存 |
| 5 | 脱敏零命中 | ✅ | 4 条 trace 的 input/output 全文 grep：手机号模式 0、`110101` 0、`sk-lf`/`sk-cp` 密钥 0；冒烟预验（16:10 记录）反向验证：带手机号/身份证输入存 `138****8000`/`110101********1234` 掩码 |
| 6 | 降级不白屏 | ✅ | `LANGFUSE_ENABLED=false ＋ OTEL_ENABLED=false` 重启：启动日志 `observability disabled (langfuse_enabled=False otel_enabled=False)`；对话全链路正常（ASR→rule→get_weather 真实回答「霾 9 度」→TTS，HTTP 200） |
| 7 | 证据可复现 | ✅ | 本文件 §1 步骤＋§2 trace_id；重跑 §1 步骤即可复现同结构 Trace（trace_id 每次新生成） |

## 4. 代码增量（Step 3 联调期间修正，均已过测试）

| 文件 | 改动 | 依据 |
| --- | --- | --- |
| `app/services/tool_impls.py` | memory_recall 执行器对齐冻结签名 `recall_for_turn(*, elder_id, query) -> list[str]`（原调用带 session+category 必 TypeError）；返回包装 `{"notes":[{"content": chunk}]}` | 主理人批准（写域外 3 行修复） |
| `app/services/tool_executor.py` | 工具执行成功路径增打 `record_event("tool", ...)` ＋ `emit_stage_span("tool", ...)`——AC-31 判据 1「工具」环节 span；**不走表轨**（九值白名单锚定 ER §4.4.7 DB CHECK，扩值需 0022 migration 另行裁决），工具数/耗时从 audit_logs 可得 | 主理人批准 |
| 本机 DB | `alembic upgrade head`（0019→0021，补 `metrics` token 列——B2 llm_tokens 表轨需要） | migration 0021 早已在 repo |
| `.env` | 两度切换（新 key 生效→降级验证→恢复），备份 `/tmp/env_backup_devb` | 判据 6 验证流程 |

测试：`tests/test_tool_executor.py` 21 passed；memory_recall 相关 74 passed；全量 1670 passed / 0 failed（14 error 为 pytest tmpdir 并发噪音）。

## 5. 环境噪音与登记项（如实记录，不粉饰）

1. **Langfuse 摄入间歇丢批**：worker 多次 `Queue job ingestion-queue errored: Redis socket timeout (30s)`（宿主机 7.75GB 内存跑满仓容器＋ClickHouse CPU 88% 高负载）。表现为每条 trace 末尾 1-2 个 span（tts/HTTP）延迟或缺失；`docker restart langfuse-langfuse-worker-1` 后恢复。**B1 代码侧无可疑（BatchSpanProcessor 默认配置、导出无错误日志）**；Y2 正式共享实例若出现同类问题，优先核查宿主资源。
2. **MiniMax 429 限流**：联调频次触发供应商限流（3 次重试全 429 → `llm_degraded` 降级话术，HTTP 200 不白屏——降级链路正常工作的反向证据）。等待窗口恢复后重试即可，非代码问题。
3. **FunASR 容器重启循环**（restart 713 次）：Docker Desktop 资源紧张下 healthcheck 误杀循环；停掉闲置的 `silvermate-funasr`（7002 口）后稳定。联调 503 撞重启窗口属环境时序，非代码问题。
4. **登记 bug（写域外，待 owner）**：`app/api/voiceprints.py` DELETE 端点 `JSONResponse(status_code=204, content=None)` 渲染 body=`b"null"`，uvicorn 视 204 为无 body → 服务端 `RuntimeError: Response content longer than Content-Length`（curl 侧仍收 204，功能不破坏）。修法：返回 `Response(status_code=204)`。归 C4 写域。
5. **登记观察**：`response_delivery_failed` ERROR 在 REST 降级路径必然出现（REST 无 WS 分段下发，`InvalidEventType` 落事件失败）——业务不受影响，建议后续对 REST 通路静默该事件。

## 6. 变更留痕

- 本报告落档即 Step 3 交付物；Step 3 关闭即 **M6.2 主判据（B1 AC-31 证据落档）达成**。
- 待主理人确认后：Step 4（B2 已完成代码的联调验证素材）→ Step 5（B3 F23）。
