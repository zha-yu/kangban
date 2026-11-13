# HARNESS_DESIGN · Agent Harness 十组件本仓落点

> 节点：Day 10 · C3（dev-C）
> 组件清单唯一来源：`docs/康伴3D数字人_大纲.md` §6.2「Agent Harness 组成」表（共 10 行，其中 State 与 Checkpoint 在大纲中为同一行，本文按大纲口径合并为一个组件，不拆成 11 项）。
> 实现口径唯一来源：`docs/康伴3D数字人_技术设计文档.md`（下称 TD）§4「康伴 Agent 闭环图」与 §6「主控 Agent Loop」。与大纲表述冲突处一律以 TD 为准，并在对应组件下以「⚠ 口径差异」注明。
> 事实纪律：本文不引用大纲正文中的表名/字段/接口/阈值作事实依据；全部事实来自 TD、PRD 编号引用（转引自 TD）与仓库实测（`app/core/config.py`、各文件头部 docstring、`ls`/`glob` 逐路径核验）。

---

## 1. Context（上下文注入与组装）

**组件行为**：服务端构造身份上下文 `runtime_context`（`elder_id` 等只由服务端注入，三道隔离闸门的第③道），每轮对话再由上下文组装器拼出 LLM 输入：固定前缀（人设/称呼/时间/医疗边界 few-shot，保持逐字节稳定以命中 prompt cache）+ 短期记忆（最近 5 轮）+ 结构化状态引用（TD §6.3 五层口径）。

**本仓落点**：
- `app/core/runtime_context.py` — 身份上下文构造（闸门③：`elder_id`/`thread_id` 只能由服务端注入，客户端与模型传入的值一律丢弃）
- `app/services/identity_service.py` — 身份级联编排（设备绑定 → 声纹 → 主动确认），产出 `runtime_context`，身份确定后事务内落 `conversation_threads`
- `app/core/context.py` — `build_dialogue_messages` / `build_turn_context` / `ContextBuilderConfig`（对话消息与轮次上下文组装）
- `app/services/dialogue_service.py` — `RedisTurnStore`：`<redis_key_prefix>:dialogue:<thread_id>:turns` 存取最近 5 轮（RPUSH + LTRIM 原子管线）
- `app/services/persona_service.py` — 人设/形象取值链（persona → Settings → 前端内置三层降级，降级点在后端）

**关键配置**（`app/core/config.py`）：`redis_url`、`redis_key_prefix`（短期轮次存储）；`default_avatar_url`、`default_fallback_avatar_url`（人设降级链）；`llm_temperature`（前缀稳定性相关）。

**⚠ 口径差异（以 TD 为准）**：大纲 §6.2 的 Context 行写「注入 `weather`」；TD §4 闭环图将天气定位为执行层的 MCP 工具（「T2 MCP 工具：天气（真实）」，唯一真实外部接口），不作为预注入上下文。本仓实现遵循 TD：天气经 Tool 组件调用，见 §3。

---

## 2. System One 快决策

**组件行为**：Laya 本地推理 sidecar 做分类器用（不做生成），一次前向输出业务视图 `SystemOneResult`（意图/置信度等信号），驱动三级路由的 L2 层。HTTP 客户端唯一承诺是**永不抛异常**：连不上、超时、5xx、schema 不合规一律返回 `degraded_result()`，下沉主控 Agent，对话照常。

**本仓落点**：
- `app/system_one/backend.py` — 推理后端：`RLAgent` 封装 + Mock；原始 JSON 解析只在本文件出现一次，其余一律只认 `SystemOneResult`
- `app/system_one/client.py` — HTTP 客户端：快路径 200ms / shadow 400ms / 熔断降级
- `app/system_one/questions.py` — `build_questions` / `build_fast_questions` / `build_shadow_questions`
- `app/system_one/schemas.py` — 信号结构定义
- `app/system_one/server.py` — sidecar 服务
- `app/router/decision.py` — L2/L3 路由决策（规则 → System One → Agent + G1 安全复核，顺序固定）

**关键配置**（`app/core/config.py`）：`system_one_enabled`、`system_one_provider`（`laya|mock`，生产禁 mock——`_reject_mock_in_production` 校验器）、`system_one_base_url`（默认 `http://127.0.0.1:7001`）、`system_one_timeout_ms=200`、`system_one_confidence_threshold=0.85`、`system_one_fallback_to_agent=True`、`system_one_fast_question="intent"`、`system_one_shadow_question="is_sensitive"`、`system_one_shadow_timeout_ms=400`、`system_one_max_concurrency=1`、`system_one_model_id/path/version`、`system_one_device`（CPU 推理）。

**⚠ 口径差异（以 TD/实仓为准）**：大纲 §6.2 写「一次返回意图分类、敏感标记、升级建议、隐私收敛信号」；实仓口径（2026-11-10 主理人裁决，`config.py` 注释与 `app/system_one/client.py` docstring）：快路径只问 intent（实测 p95 达标 200ms 预算），`is_sensitive` 走 shadow 且不阻塞路由，急症仍由 G0 规则硬兜底。旧 `LAYA_*` 变量仅做兼容映射，新代码禁止再读。

---

## 3. Tool（工具）

**组件行为**：工具白名单注册表（只描述工具是什么：名字、说明、参数 schema、副作用、幂等性）+ 唯一执行层（白名单 → 参数消毒 → 护栏预算/熔断 → 参数校验 → 幂等查重 → 落 running 行 → 执行 → 收口）；对外副作用收敛到 MCP（天气为唯一真实外部接口，工单/家属通知走 Mock Operations，带越权审计）。

**本仓落点**：
- `app/agents/tool_registry.py` — 工具白名单（ToolSpec 描述层）
- `app/agents/bind_tools.py` — 工具装配 hook（装配层一次性接线，不碰 `main.py`）
- `app/services/tool_executor.py` — 工具执行器：唯一写库层（白名单、四类失败、幂等、审计）
- `app/services/tool_impls.py` — 具体工具实现
- `app/mcp_client/client.py` — MCP 客户端：工具发现/调用/超时/越权审计（闸门③）
- `mcp-servers/weather/`（`server.py`、`weather_core.py`）— 天气 MCP Server
- `mcp-servers/mock-operations/` — 工单/家属通知 Mock Operations Server

**关键配置**（`app/core/config.py`）：`mcp_weather_server_url`（默认 `http://localhost:7101`）、`mcp_operations_server_url`（默认 `http://localhost:7102`）、`mcp_call_timeout_seconds=5`；`weather_api_provider/base_url/api_key`、`weather_cache_ttl_seconds=600`、`weather_default_city`；`tool_repeat_window_seconds=60`、`tool_repeat_break_threshold=3`。

---

## 4. State / Checkpoint（状态与检查点，大纲 §6.2 同一行）

**组件行为**：LangGraph Checkpointer 按 `thread_id` 持久化会话状态到 PostgreSQL（断线恢复、子图跨分钟等待）；会话内结构化状态由 StateCenter 存 Redis（四组正交状态 + 版本 CAS + 12 种事件显式映射表，Redis 不可用时按对话类型分级降级）。

**本仓落点**：
- `app/graphs/checkpointer.py` — LangGraph checkpointer：独立 schema + 独立连接池（不复用 `app.db` 主池，checkpoint 表不进 Alembic 业务迁移）
- `app/services/state_service.py` — StateCenter：`session:{thread_id}:state`（TTL 2h），Lua 脚本原子 CAS，`StateVersionConflict` / `StateUnavailable` 语义
- `app/agents/session_coordinator.py` — 每 `thread_id` 一把 `asyncio.Lock` 串行提交，版本冲突自动重试（最多 3 次）
- `app/graphs/medication_confirmation_graph.py` — 业务子图①：用药确认（定时/对话双入口同一状态机，TD §7.1）
- `app/graphs/health_checkup_graph.py` — 业务子图②：健康采集与分级（定时/主动主诉双入口，TD §7.2）

**关键配置**（`app/core/config.py`）：`langgraph_checkpoint_database_url`、`langgraph_checkpoint_schema`（默认 `langgraph`）、`langgraph_checkpoint_pool_min_size=1`、`langgraph_checkpoint_pool_max_size=5`；`redis_url`（StateCenter）。

**⚠ 口径差异（以 TD 为准）**：大纲 §6.2 只写 Checkpointer 持久化；TD §6.3 明确结构化状态（当前任务/槽位/进行中子图引用）放 Redis、由 `thread_id` 索引，与 Checkpointer 是两套存储——本仓两者都有，职责不混。

---

## 5. Memory（记忆）

**组件行为**：短期记忆 = 最近 5 轮对话原文（Redis）；长期记忆 = pgvector 1536 维，全部查询强制 `WHERE elder_id = :runtime.elder_id`（闸门②），写入经唯一写库出口 MemoryService（向量抽取失败降级为无向量行、召回本轮不加载记忆）；治理含五步（萃取 → 存储 → 召回过滤 → 冲突检测 → 衰减归并），召回后经 LLM 二次过滤再注入（TD §6.3）。

**本仓落点**：
- `app/services/memory_service.py` — 长期记忆唯一写库出口；embedding 走 OpenAI 兼容 `/embeddings`，维度必须恰好 1536（不一致抛 `EmbeddingDimensionError`，不截断不补零）；`decay_stale` / `consolidate_similar` 治理接口
- `app/models/memory.py` — 长期记忆 ORM（`Vector1536` UserDefinedType，闸门②强制过滤；`supersedes_id`/`conflicts_with_id` 自引用不建 FK）
- `app/scheduler/memory_jobs.py` — 衰减 + 归并两类周维护 job（零写库铁律：不 import `app.models`，事务由 service 层自管）
- `app/api/memory.py` — 记忆管理 HTTP 接口
- 短期记忆落点同 §1：`app/services/dialogue_service.py`（`RedisTurnStore`）

**关键配置**（`app/core/config.py`）：`embedding_base_url`、`embedding_api_key`、`embedding_model`（任一为空 → 视为未配置，降级为无向量行）、`embedding_dim=1536`、`embedding_timeout_seconds=5.0`、`embedding_batch_size=16`（代码侧另有硬上限 16）。

---

## 6. Loop（主控循环）

**组件行为**：主控 CompanionAgent ReAct 循环（思考 → 选工具 → 执行 → 观察 → 再思考，直到生成回复或触顶）；单轮对话最多 8 次工具调用，超限强制收尾并礼貌提示；统一出口 `handle_user_turn` 七步顺序提交（事实先落 → 状态快照 → 上下文组装 → 三级路由 → …）；规则层 / System One 命中时短路，不进入大模型 Loop。

**本仓落点**：
- `app/agents/companion.py` — 受约束 CompanionAgent · ReAct Loop（LLM 经 `ChatClient` 协议注入；工具调用先过 `Guardrails.check` 再进 `ToolExecutor.execute`）
- `app/services/turn_service.py` — 统一出口 `handle_user_turn`（七步顺序即契约）+ `ContextBuilderLike` / `MinimalContextBuilder`
- `app/services/dialogue_service.py` — 通用对话回合（加载 5 轮 → 组装 → LLM → 写回）
- `app/router/decision.py` — 三级路由：G0 一票否决 → L1 规则层 → L2 System One → L3 Agent（低置信澄清一次后仍模糊才进 L3）
- `app/services/llm_service.py` — LLM 适配器（httpx 直连，非流式，瞬态错误退避重试）

**关键配置**（`app/core/config.py`）：`max_tool_calls_per_turn=8`；`llm_provider`、`llm_base_url`、`llm_api_key`、`llm_model`、`llm_timeout_seconds=20`、`llm_max_retries=2`、`llm_temperature=0.6`。

**⚠ 口径差异（以 TD 为准）**：大纲 §6.2 的 Loop 行写「每个 Agent 独立运行循环，Supervisor 负责分发和汇总」；TD §5.2 / §7 的口径是**三级路由（G0 → L1 规则 → L2 System One → L3 主控 Agent）+ 全系统仅 2 个业务子图**，无 Supervisor 多 Agent 分发层，主控 Agent Loop 即 `CompanionAgent`。本仓实现遵循 TD。

---

## 7. Policy（策略与护栏）

**组件行为**：护栏全部落在代码、不靠 prompt 自觉（TD §6.2）：身份强制注入（忽略模型/客户端传入的任何 `elder_id` 参数）、调用预算 `MAX_TOOL_CALLS=8`、同工具 1 分钟内第 3 次调用熔断、相同工具+相同参数重复调用短路返回上次结果、高危写操作（改药单/关工单/删数据）一律拒绝并生成护工跟进任务、敏感一票否决在 Loop 之前、医疗边界固定拒答、提示注入检测。

**本仓落点**：
- `app/agents/guardrails.py` — 会话级 Tool 调用护栏（预算 + 同工具熔断，进程内内存态）
- `app/services/tool_executor.py` — 参数消毒（丢弃模型自报身份 + 审计）、幂等查重、预算熔断的执行侧落点
- `app/router/safety.py` — 安全一票否决（急症红色判定，纯检测无 I/O，命中即短路）
- `app/router/injection.py` — 提示注入检测（纯函数；单一入口仍是 `safety.py` 的 `safety_check`，排在急症判定之后）
- `app/router/medical_refusal.py` — 医疗拒答固定话术与拦截（早于 Agent，与 `safety_check` 同级）
- `app/services/safety_service.py` — 安全升级唯一写库路径（红色事件 + 工单 + 护工通知三行同事务落库）
- `app/agents/followup.py` — 高危工具被拒后的真实出口（`followup_sink` 落库，拆除「假承诺」）
- `app/core/runtime_context.py` — 身份强制注入的闸门③落点
- `app/router/rules.py` — L1 规则层（高频固定指令零成本闭环，System One/Agent 前置）

**关键配置**（`app/core/config.py`）：`max_tool_calls_per_turn=8`、`tool_repeat_window_seconds=60`、`tool_repeat_break_threshold=3`、`system_one_confidence_threshold=0.85`（低于此值只澄清一次）、`voiceprint_similarity_threshold=0.62`（身份级联阈值，运行时经 Settings 可调）。

---

## 8. Recovery（恢复与降级）

**组件行为**：工具临时错误退避重试（最多 2 次）→ 仍失败用降级话术，不阻塞对话；工具失败四分类分别处置（参数错回传模型修正、目标不存在换目标或如实告知、临时错误退避重试、越权拒绝 + 审计 + trace_id）；LLM 失败返回固定长者可读模板（`degraded=True`，不回显内部异常）；System One 任何失败返回 `degraded_result()` 下沉 Agent；Redis 不可用时普通闲聊退化为单进程内存字典（用药任务抛 `StateUnavailable`）；LangGraph Checkpoint 恢复断线会话与跨分钟等待。

**本仓落点**：
- `app/services/tool_executor.py` — 四类失败分类处置 + transient 退避 ≤2 的执行侧落点
- `app/services/llm_service.py` — LLM 重试（至多 2 次，仅瞬态错误）+ 固定降级模板
- `app/system_one/client.py` — System One 熔断降级（永不抛异常承诺）
- `app/services/state_service.py` — StateCenter Redis 降级逻辑
- `app/graphs/checkpointer.py` — 断点恢复存储层
- `app/audio/webrtc.py`、`app/main.py` — `degrade_*_slow_ms` 三档慢响应并行 watchdog（provider 还在跑但超时即先发 slow 提示；两套超时不混：provider 层断网兜底走异常路径）

**关键配置**（`app/core/config.py`）：`llm_max_retries=2`、`llm_timeout_seconds=20`；`degrade_asr_slow_ms=2000`、`degrade_llm_slow_ms=3000`、`degrade_tts_slow_ms=1000`；`system_one_fallback_to_agent=True`、`system_one_timeout_ms=200`、`system_one_shadow_timeout_ms=400`；`mcp_call_timeout_seconds=5`；`embedding_timeout_seconds=5.0`。

---

## 9. Observability（可观测性）

**组件行为**：每次路由写 `decision_logs`（唯一写口、只 INSERT、落库前脱敏，`model_version` 来自配置）；全链路分段耗时打点（唤醒→建连→ASR→路由→LLM→TTS→前端播放）经唯一写库入口异步批量入 `metrics` 表（观测旁路，失败丢弃不拖垮主链路）；`trace_id` 中间件贯穿调用链；脱敏工具集中一处（字段级 + 文本级）；Langfuse / OTel 双通道按配置开关。

**本仓落点**：
- `app/services/decision_log_service.py` — `decision_logs` 唯一写口（只 INSERT、写口处脱敏）
- `app/models/decision.py` — decision_logs ORM
- `app/services/metrics_service.py` — `metrics` 表唯一写库入口（异步批量缓冲、非法值不入库）
- `app/models/metrics.py` — metrics ORM
- `app/core/metrics.py` — 全链路打点位单点收口（日志级 + 双轨升级）
- `app/core/logging.py` — 日志配置
- `app/core/trace.py` — trace_id 中间件（纯 ASGI，进出请求各打一行）
- `app/core/redact.py` — 集中脱敏（`mask_phone` / `mask_name` / `mask_secret` 等，唯一实现）
- `app/api/metrics.py` — `/metrics` 观测接口

**关键配置**（`app/core/config.py`）：`langfuse_enabled=False`、`langfuse_host`、`langfuse_public_key`、`langfuse_secret_key`、`langfuse_sample_rate=1.0`；`otel_enabled=True`、`otel_exporter_otlp_endpoint`、`otel_service_name="silvermate-backend"`；`metrics_enabled=True`；`log_level="INFO"`。

---

## 10. Evaluation（评测）

**组件行为**：评测门禁脚本五道门判定，任一不过即 `exit 1`（bad case 回流 fixtures 防回退）；固化评测集纪律为「不换样本、不改分母、不删列」，改集必须新建版本文件并登记哈希；pytest 回归测试与开发库物理隔离（专用测试库）。

**本仓落点**：
- `tests/eval_gate.py` — 评测门禁（五道门，FR-TC-09 / FR-TC-07 对应）
- `eval/fixtures/day7_intents.jsonl` — 路由固化集（110 条：普通 50 / 敏感 20 / 边界 20 / 模糊 20，sha256 登记于 `eval/fixtures/README.md`）
- `eval/fixtures/boundary_v1.jsonl` — 六类边界集（30 条）
- `eval/fixtures/injection.jsonl` — 提示注入评测集
- `eval/fixtures/badcase_backlog.jsonl` — bad case 回流待办
- `eval/fixtures/README.md` — 固化集登记与纪律
- `eval/reports/`、`eval/artifacts/` — 评测报告与证据工件归档
- `tests/conftest.py`、`app/core/config.py` 的 `database_url_test` — 测试库隔离（pytest 从 `database_url` 推导 `<库名>_test`，绝不回退开发库）

**关键配置**（`app/core/config.py`）：`database_url_test`；评测集本身为仓库文件（非环境变量），阈值类判据以 `tests/eval_gate.py` 内定义为准。

**⚠ 口径差异（以实仓为准）**：大纲 §6.2 写「固定 20 个长者场景测试集」；本仓实际固化评测集为 `eval/fixtures/` 下的 jsonl 文件（110 条 + 30 条 + 注入集等），并有 sha256 登记与「不换样本」纪律。以实仓文件为准。

---

## 路径核验清单

| 路径 | 验证方式 | 结果 |
| --- | --- | --- |
| `app/core/config.py` | Read 全文 | 存在 |
| `app/core/runtime_context.py` | head 头部 docstring | 存在 |
| `app/core/context.py` | head + grep（`build_dialogue_messages`/`build_turn_context`） | 存在 |
| `app/core/logging.py` | `find app -type f -name "*.py"` 文件清单 | 存在 |
| `app/core/trace.py` | head 头部 docstring | 存在 |
| `app/core/metrics.py` | head 头部 docstring | 存在 |
| `app/core/redact.py` | head 头部 docstring | 存在 |
| `app/services/identity_service.py` | head 头部 docstring | 存在 |
| `app/services/dialogue_service.py` | head 头部 docstring | 存在 |
| `app/services/persona_service.py` | head 头部 docstring | 存在 |
| `app/system_one/backend.py` | head 头部 docstring | 存在 |
| `app/system_one/client.py` | head 头部 docstring | 存在 |
| `app/system_one/questions.py` | grep（`build_fast_questions` 等） | 存在 |
| `app/system_one/schemas.py` | `find` 文件清单 | 存在 |
| `app/system_one/server.py` | `find` 文件清单 | 存在 |
| `app/router/decision.py` | head 头部 docstring | 存在 |
| `app/router/rules.py` | head 头部 docstring | 存在 |
| `app/router/safety.py` | head 头部 docstring | 存在 |
| `app/router/injection.py` | head 头部 docstring | 存在 |
| `app/router/medical_refusal.py` | head 头部 docstring | 存在 |
| `app/agents/tool_registry.py` | head 头部 docstring | 存在 |
| `app/agents/bind_tools.py` | head 头部 docstring | 存在 |
| `app/agents/companion.py` | head 头部 docstring | 存在 |
| `app/agents/guardrails.py` | head 头部 docstring | 存在 |
| `app/agents/followup.py` | head 头部 docstring | 存在 |
| `app/agents/session_coordinator.py` | head 头部 docstring | 存在 |
| `app/services/tool_executor.py` | head 头部 docstring | 存在 |
| `app/services/tool_impls.py` | `find` 文件清单 | 存在 |
| `app/services/turn_service.py` | head + grep（`handle_user_turn`、`ContextBuilderLike`） | 存在 |
| `app/services/llm_service.py` | head 头部 docstring | 存在 |
| `app/services/state_service.py` | head 头部 docstring | 存在 |
| `app/services/memory_service.py` | head 头部 docstring | 存在 |
| `app/services/metrics_service.py` | head 头部 docstring | 存在 |
| `app/services/decision_log_service.py` | head 头部 docstring | 存在 |
| `app/services/safety_service.py` | head 头部 docstring | 存在 |
| `app/mcp_client/client.py` | head 头部 docstring | 存在 |
| `mcp-servers/weather/server.py` | `ls -1 mcp-servers/weather` | 存在 |
| `mcp-servers/mock-operations/` | `ls -1 mcp-servers` | 存在（目录） |
| `app/graphs/checkpointer.py` | head 头部 docstring | 存在 |
| `app/graphs/medication_confirmation_graph.py` | head 头部 docstring | 存在 |
| `app/graphs/health_checkup_graph.py` | head 头部 docstring | 存在 |
| `app/models/memory.py` | head 头部 docstring | 存在 |
| `app/models/decision.py` | `find` 文件清单 | 存在 |
| `app/models/metrics.py` | `find` 文件清单 | 存在 |
| `app/scheduler/memory_jobs.py` | head 头部 docstring | 存在 |
| `app/api/memory.py` | `find` 文件清单 | 存在 |
| `app/api/metrics.py` | `find` 文件清单 | 存在 |
| `app/audio/webrtc.py` | grep（`degrade_asr_slow_ms` 使用处） | 存在 |
| `app/main.py` | grep（`context_builder=`、`degrade_llm_slow_ms`） | 存在 |
| `tests/eval_gate.py` | head 头部 docstring | 存在 |
| `tests/conftest.py` | `ls -1 tests` | 存在 |
| `eval/fixtures/day7_intents.jsonl` | `find eval -type f` | 存在 |
| `eval/fixtures/boundary_v1.jsonl` | `find eval -type f` | 存在 |
| `eval/fixtures/injection.jsonl` | `find eval -type f` | 存在 |
| `eval/fixtures/badcase_backlog.jsonl` | `find eval -type f` | 存在 |
| `eval/fixtures/README.md` | head 全文 | 存在 |
| `eval/reports/` | `find eval -type f` | 存在（目录） |
| `eval/artifacts/` | `find eval -type f` | 存在（目录） |

（上表全部路径均在本节点执行时以 `find`/`ls`/`head`/`grep`/`Read` 实测核验，零虚构。）
