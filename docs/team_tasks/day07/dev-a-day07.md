# Task dev-A · Day 7 · Chinese-Jev 三级路由与决策日志

> 状态：未开始　｜　版本：v1.0　｜　日期：2026-11-10
> 负责人：dev-A　｜　总人日：3.0（A1 0.5 + A2 1.5 + A3 1.0）
> 关联：`docs/team_tasks/day07/README.md`（总表）｜`docs/team_tasks/day07/contracts-system-one.md`（A1 产出）｜`docs/plans/adhoc-20261110-day6全面审查.md`（收口依据）
> 上游依据：PRD FR-DLG-02/03/06/07/09｜技术设计文档 §1.4、§3.4、§3.6、§8.3
> 本卡只含前后端开发；验收由开发自证，不设测试/产品/设计角色。

---

## 0. 一句话

把 Laya 配置与空壳 `app/system_one/` 替换为 **Chinese-Jev 本地 sidecar + HTTP 客户端**，补上规则层、路由决策与 `decision_logs`，让一轮对话能明确回答“走了哪一层、为什么、耗时多少”。

## 1. 写作用域（独占）

```text
app/core/config.py
app/system_one/**
app/router/rules.py
app/router/decision.py
app/router/__init__.py
app/models/decision.py
app/services/decision_log_service.py
alembic/versions/0014_decision_logs.py
app/services/turn_service.py
app/main.py                         # 唯一装配点，C2 只提供 install_scheduler
docs/team_tasks/day07/contracts-system-one.md
spikes/chinese_jev_hello/**         # A2 首日验证
README.md                           # 仅配置/架构/端口段落
```

**禁触**：`app/services/response_service.py`（B1）、`app/core/context.py`/`state_service.py`（B2）、`app/agents/companion.py`/`tool_executor.py`（C1/C3）、`app/scheduler/**`（C2）、`frontend/**`（D1/D2）、`0013` 及以前迁移。

> `turn_service.py` 与 `main.py` 是高风险共享文件：本卡是唯一写者。B/C/D 需要接线时只提接口/patch，不直接改。

---

## 2. A1 · 配置替换与契约冻结（0.5 人日）

### 2.1 目标

把 6 个 `laya_*` 配置替换为 `system_one_*`，冻结 Chinese-Jev 的请求/响应、七类意图、四信号、`RouteDecision`、`decision_logs.route_layer`，让 B/C/D 可以不见面并行。

### 2.2 实施步骤

1. 读 `app/core/config.py` L90-96，新增 `system_one` 配置组：
   - `system_one_enabled`、`system_one_provider`、`system_one_base_url`、`system_one_api_key`、`system_one_timeout_ms`、`system_one_confidence_threshold`、`system_one_fallback_to_agent`
   - `system_one_model_id`、`system_one_model_path`、`system_one_model_version`、`system_one_device`、`system_one_max_concurrency`、`system_one_warmup`
2. 增加 `model_validator(mode="before")`：把旧 `LAYA_*` 环境变量映射到新字段，打一次 `deprecation_warning`；新代码不得再读 `settings.laya_*`。
3. 新增 `app/system_one/schemas.py`：
   - `Intent = Literal["chat","medication","opera","movie","weather","health_checkup","other"]`
   - `PrivacyScope = Literal["personal","shared","none"]`
   - `SystemOneRequest`、`SystemOneResponse`、`SystemOneResult`（含 `degraded`、`latency_ms`、`model_version`、可选 `multi_intent`、`sub_intents`）
4. 新增 `app/system_one/questions.py`：`build_questions()` 唯一构造入口；七类 intent 候选、敏感命题、置信度 score、隐私 choice、域外 noul。
5. 新建 `docs/team_tasks/day07/contracts-system-one.md` v1.0，内容至少：
   - `/v1/decide` 请求/响应字段表；
   - `RouteDecision` 字段表（含 `multi_intent/sub_intents`）；
   - C→S `interrupt` 消息；
   - `decision_logs.route_layer` 新写值 `rule/system_one/agent/fallback`，历史 `laya` 只读；
   - 低置信“只澄清一次”的判定方式（按最近一条 `decision_logs`）；
   - **三层护栏口径**：G0 入口安全 → G1 分类器信号触发的安全复核 → G2 `ToolExecutor` 权限校验；Router 与 Agent 共用唯一工具入口。
6. 更新 `app/router/__init__.py` docstring：`safety.py` → `rules.py` → System One → Agent；更新 `app/system_one/__init__.py` docstring。
7. 更新 `README.md` 配置表/架构图/端口表，把 Laya 改为 Chinese-Jev；`app/audio/asr.py` 仅改注释，端口 7001 不动。

### 2.3 交付物

- `app/core/config.py` 新配置组 + 兼容映射。
- `app/system_one/schemas.py`、`questions.py`。
- `contracts-system-one.md` v1.0（含变更记录）。
- `README.md` 与 `asr.py` 注释更新。

### 2.4 验收（命令级）

```bash
uv run python -c "from app.core.config import Settings; s=Settings(system_one_provider='chinese_jev'); print(s.system_one_timeout_ms, s.system_one_confidence_threshold)"
# 期望：200 0.85
uv run python -c "from app.system_one.questions import build_questions; q=build_questions(); print(list(q)); print(q['intent']['choices'])"
# 期望字段齐全，intent 恰 7 类
uv run ruff check app/core/config.py app/system_one
```

- 旧 `LAYA_BASE_URL=http://localhost:7001` 启动时能映射到 `system_one_base_url`，日志有且仅有一次弃用告警。
- 全仓 `grep -rn "settings.laya_" app/` 零命中（测试/迁移说明除外）。
- B/C/D 在 J1 会上确认字段表，无口头待定项。

### 2.5 依赖与交接

- 无前置；第 1 天上午完成，17:00 主持 J1。
- 交接：B1 拿 `interrupt` 字段；D1 拿 `interrupt`/epoch 规则；C1 拿 `RouteDecision` 的工具分流字段；B2 拿 `route_layer` 枚举。

---

## 3. A2 · Chinese-Jev 本地 sidecar 与客户端（1.5 人日）

### 3.1 目标

在不接商业 API 的前提下，提供本地 `/v1/decide` 服务与 200ms 客户端；上游权重/API 不具备时，必须显式降级并如实报告，不得伪造“已上线”。

### 3.2 现状与硬约束

- 现有 `app/system_one/` 只有一行 docstring；`spikes/laya_hello/` 只验证过 HTTP mock，不是 Chinese-Jev。
- Chinese-Jev 公开页提供 CLI/训练/评测，**未提供稳定 HTTP server / Python API**；只有 `CJ-Medical` 权重明确开放，General 权重状态需首日核实。
- 禁止：商业 Jev API、云端分类 SaaS、把 Medical 模型冒充通用意图模型、为补分类能力新增云端依赖。
- 允许：本地 sidecar、上游 CLI/子进程适配、`MockBackend` 仅用于无权重开发与契约测试。

### 3.3 实施步骤

1. 新建 `spikes/chinese_jev_hello/`：
   - `README.md` 记录上游 commit、权重来源/许可、可用的 inference 入口；
   - `bench.py` 跑 20 条固定中文样例，记录 p50/p95/max；
   - 若 General 权重不可用，记录阻塞并用 `CJ-Medical` 仅验证字段/延迟，不产出准确率结论。
2. 新建 `app/system_one/backend.py`：
   - `DecisionBackend` Protocol：`async score(text, questions) -> dict`；
   - `ChineseJevBackend`：按上游实际入口加载模型/调用 inference；模型只加载一次；CPU/GPU 由配置；
   - `MockBackend`：关键词/固定映射，仅 `system_one_provider="mock"` 或测试使用。
3. 新建 `app/system_one/server.py`（FastAPI sidecar）：
   - `GET /health`：`{status, model_id, model_version, model_loaded, device, provider}`；模型缺失返回 503 + `model_missing`。
   - `POST /v1/decide`：Pydantic 校验；`asyncio.Semaphore(system_one_max_concurrency)`；内部推理预算 150ms；返回 `SystemOneResponse`。
   - lifespan：`system_one_warmup=true` 时预热 1 条样例；启动失败只影响 health，不让业务进程崩。
   - 不连业务库、不写 `decision_logs`、不读 `runtime_context`。
4. 新建 `app/system_one/client.py`：
   - `async decide(text, trace_id) -> SystemOneResult`；
   - `httpx.AsyncClient(timeout=200ms)`；`Authorization` 仅当 api_key 非空；
   - schema 校验失败/连接失败/5xx/超时 → `degraded=True`、`intent="other"`、`confidence=0.0`、`is_out_of_domain=True`，不抛异常；
   - 3 次连续失败/30s 熔断，半开探测 1 次；打 `system_one_degraded` 指标/日志；
   - 响应头/日志带 `trace_id`，不记录完整原文（只记长度/哈希）。
5. 更新 `app/system_one/__init__.py` 导出 `SystemOneClient`、`SystemOneResult`。
6. 不在 `docker-compose.yml` 增加服务（TD 留 Day 10）；本地启动命令写入 spike README 与本卡。

### 3.4 交付物

- `spikes/chinese_jev_hello/`（README + bench 脚本 + 结果）。
- `app/system_one/backend.py`、`server.py`、`client.py`。
- `app/system_one/__init__.py` 导出。
- A2 收口报告：权重是否可用、真实 p50/p95、20 条样例通过数、降级路径证据。

### 3.5 验收（命令级）

```bash
# 启动 sidecar（本地）
uv run uvicorn app.system_one.server:app --port 7001
curl -s localhost:7001/health
# 期望：status=ok 或 503 + model_missing（必须显式，不得假 ok）

uv run python spikes/chinese_jev_hello/bench.py
# 期望：20 条请求全部有合法 JSON；记录 p50/p95/max；p95 ≤200ms 或如实标红

uv run pytest -q tests/test_system_one_client.py
# 覆盖：正常 / 超时 / 5xx / schema 非法 / 熔断 / 恢复
```

- 20 条固定样例：响应字段名、类型、枚举 20/20 合法。
- sidecar 停掉后，业务客户端 250ms 内返回 `degraded=True`，不抛异常。
- `grep -rn "api.jev\|jev.com\|openai" app/system_one` 零商业 API 命中。

### 3.6 依赖与交接

- 依赖 A1 配置/schema；可与 B/C/D 并行。
- 交接 A3：`SystemOneResult` 与降级语义；交接 D3：`degraded` 可用于前端提示，但本日不新增 UI 协议。

---

## 4. A3 · 规则层 + 路由决策 + decision_logs（1.0 人日）

### 4.1 目标

在 G0 安全一票否决之后、Agent 之前插入规则层与 System One 路由；每次决策写一行 `decision_logs`；低置信只澄清一次；分类器输出 `is_sensitive` 信号后，由安全模块复用同一升级口裁决，不进 Agent；路由直连与 Agent 共用唯一 `ToolExecutor`。

### 4.2 实施步骤

1. 新建 `app/router/rules.py`：
   - `RuleHit` dataclass；`match_rule(text) -> RuleHit | None`；
   - 规则 ≤10 条，清单在 A1 契约中冻结（按 PRD FR-DLG-02 的固定意图，不直接抄大纲正文）；
   - 纯函数、无 IO、无 DB；规则版本号常量 `RULESET_VERSION`；
   - 命中返回 `intent/action/tool_name/tool_args/confidence=1.0`。
2. 新建 `app/router/decision.py`：
   - `RouteDecision`（字段见 README §5.3，含 `multi_intent/sub_intents`）；
   - `async decide(text, *, thread_id, runtime) -> RouteDecision`；
   - 顺序：规则 → System One → Agent；
   - **G1 分类器信号触发的安全复核**：System One 返回 `is_sensitive=true` 时，调用装配层注入的同一个安全升级口（既有 `_escalate_safety` / `safety_service`），进入安抚/人工路径；**不得**返回 `agent` 继续对话，也不得绕过安全层直接执行工具；`decision_logs` 照写；
   - 低置信/域外：查最近一条 `decision_logs`（同 `thread_id`）是否已 `action=clarify`；未澄清 → `clarify`，已澄清 → `agent`；
   - 意图到动作映射：`weather → get_weather`；`medication → 用药查询/确认能力`（C1 提供）；`health_checkup → health 入口`（未就绪则 `agent`）；`opera/movie` 未就绪 → `agent` 并标 `reason=capability_not_ready`；`chat/other → agent`；
   - **多意图**：识别 `multi_intent/sub_intents`；简单顺序多意图在 Router 内展开为有序动作，动态组合直接 `agent`；任一子意图敏感则整轮升级；
   - **统一工具入口**：所有直连工具只调用 `ToolExecutor.execute()`；Router 不直连 HTTP/MCP/DB，不复制权限判断；
   - 直接工具执行失败 → 不重试第二套逻辑，返回 `ToolResult` 话术或 Agent 兜底。
3. 新建 `app/models/decision.py` + `alembic/versions/0014_decision_logs.py`：
   - 字段按 TD §3.4：`log_id/thread_id/trace_id/elder_id/input_text/intent/confidence/is_sensitive/route_layer/escalated_to_s2/latency_ms/asr_confidence/model_version/created_at`；
   - 索引：`(thread_id, created_at DESC)`、`(route_layer, created_at DESC)`、`(trace_id)`；
   - `route_layer` CHECK：`rule/system_one/agent/fallback/laya`（`laya` 只读兼容）；`intent` CHECK 七类 + `other`；
   - `down_revision="0013"`，可 downgrade。
4. 新建 `app/services/decision_log_service.py`：
   - `async record_decision(session, *, runtime, text, decision, asr_confidence) -> None`；
   - `input_text` 复用现有脱敏工具；只 INSERT，不 UPDATE；
   - 失败：不阻断对话，打 `decision_log_failed` + 指标；但 G5 验收必须证明正常路径每轮 1 行。
5. 改 `app/services/turn_service.py`：
   - 第 3 步上下文之后、第 4 步 Agent 之前调用 `router.decide()`；
   - `action="clarify"`：构造 `AgentTurnResult(text=clarify_text, degraded=False)`，不调 Agent；
   - `action="tool"`：经 `ToolExecutor` 执行，成功/失败都转成 `AgentTurnResult` 可播报文本；
   - `action="agent"`：保持现有 `agent.run()`；
   - `TurnOutcome` 增 `route_layer/intent/confidence`；
   - 删除/私有化 `TurnService.commit_response` 第二写口（P3-31），只留响应层写口。
6. 改 `app/main.py`：
   - 构造 `SystemOneClient` + `Router` 注入 `TurnService`；
   - 调用 C2 提供的 `install_scheduler(app)`（C2 不直接改本文件）；
   - 不改变现有 safety/orchestrator 顺序。
7. 更新 `contracts-system-one.md` 到 v1.1，记录 `RouteDecision` 最终字段与 `route_layer` 取值。

### 4.3 交付物

- `app/router/rules.py`、`decision.py`。
- `app/models/decision.py`、`0014_decision_logs.py`、`decision_log_service.py`。
- `turn_service.py`/`main.py` 接线。
- 50 例路由报告（可放 `docs/plans/day07/a3-路由基线.md`，仅开发证据，不新增计划件状态）。

### 4.4 验收（命令级）

```bash
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
uv run pytest -q tests/test_router_rules.py tests/test_router_decision.py tests/test_decision_logs.py
uv run python scripts/eval_router.py --fixtures eval/fixtures/day7_intents.jsonl --report /tmp/day7-router.md
```

- 规则层：≤10 条；每条至少 1 正例/1 负例；规则命中不调用 System One/Agent（用 fake 断言 0 调用）。
- System One：置信 ≥0.85 走 `system_one`；<0.85 首次 `clarify`、第二次 `agent`；域外走 `clarify`/`agent`；sidecar 失败走 `fallback`。
- **G1 二次安全**：分类器 `is_sensitive=true` → 走既有安全升级口，不调用 Agent、不执行工具；重复触发按 `turn_id/thread_id` 幂等，不重复建红色事件。
- **统一工具入口**：路由直连与 Agent 工具调用都经 `ToolExecutor.execute()`；`grep` Router 内无第二套权限判断/HTTP/MCP 直连。
- **MCP 直连用例**：`weather → get_weather` 的调用链必须是 `Router → ToolExecutor → MCPClient`；Router 内不 import `mcp_client`、不直连 HTTP/MCP；MCP 超时/上游失败走统一 `transient`/降级话术并留痕。
- **多意图**：简单顺序多意图按序执行且每个子动作过 G1/G2；动态组合进 Agent；含敏感子句时整轮升级。
- **边界六类**：否定表达、多意图、工具超时、打断、重复调用、权限越权各有用例（打断/重复可与 B1/C3 用例复用，但断言必须存在）。
- `decision_logs`：每轮恰好 1 行；`trace_id` 与请求一致；`input_text` 无手机号/身份证明文；`model_version` 为 `chinese-jev:*`；不改表、不加列，G1/G2 摘要走现有 `audit_logs`/metrics/结构化日志。
- 50 例报告含：意图准确率、敏感召回率、分层命中率（rule/system_one/agent/fallback）、p50/p95；不把 fallback 计入 system_one 命中。
- P3-31：全仓 `TurnService.commit_response` 零调用点，方法删除或改私有。

### 4.5 依赖与交接

- 硬依赖 A2；软依赖 C1 工具执行器、C2 `install_scheduler`。
- 交接 D3：`decision_logs` 查询 SQL 与 `route_layer` 含义；交接 B1：`TurnOutcome` 新字段不改变响应流契约。

---

## 5. 完成记录模板

```text
任务：A1/A2/A3
状态：已完成 / 阻塞
证据：
- 代码：<commit>
- 命令：<命令 + 关键输出>
- 数据：<decision_logs 样例 / trace_id>
- 未完成：<无 / 具体项 + 原因 + 下一步>
```

## 6. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版：A1 配置与契约、A2 Chinese-Jev sidecar/client、A3 规则/路由/decision_logs；共 3.0 人日 |
| v1.1 | 2026-11-10 | 补充设计要求：G1 分类器信号触发的安全复核复用同一安全升级口；Router 与 Agent 统一走 `ToolExecutor`；契约与验收补 `multi_intent/sub_intents`、否定表达、多意图、工具超时、重复调用、权限越权 |
