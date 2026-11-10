# dev-A · 第 7 天剩余执行提示词（A2 → A3）

> 状态：草稿　｜　版本：v2.0　｜　日期：2026-11-10
> 关联：`docs/plans/day07/dev-a-三级路由与决策日志.md`（执行依据）｜`docs/team_tasks/day07/dev-a-day07.md`（任务卡）｜`docs/team_tasks/day07/contracts-system-one.md`（v1.0 已冻结）
> 上游依据：PRD FR-DLG-01~10（line 264–274）、§5.2（line 527–541）、§5.3（line 543–550）｜技术设计文档 §3.4 `decision_logs`（line 686–706）、§6
> 变更记录见文末

---

以下是可直接发送给 AI 的提示词正文（分隔线之间全文复制）。

---

# 任务：康伴 SilverMate · Day 7 · dev-A 剩余（A2 sidecar/客户端 → A3 规则与路由）

## 0. 角色与现状：A1 已完成，**不要重做**

你是本项目 **dev-A**。上一轮已完成 A1（配置 + 契约冻结），本提示词只做 **A2 与 A3**。

**A1 已完成（视为既成事实）**

```text
app/core/config.py           13 个 system_one_* 字段；6 个 laya_* 仅兼容映射（每变量一次告警）；生产禁 provider=mock
app/system_one/schemas.py    Intent 七类 / QuestionSpec 三型 / SystemOneRequest·Response·Result / 降级不变量
app/system_one/questions.py  题面唯一入口：build_questions / build_fast_questions / build_shadow_questions / build_state
app/system_one/__init__.py   已导出上述符号（不含 backend/server/client）
docs/team_tasks/day07/contracts-system-one.md   v1.0 已冻结（J1 用）
README.md / app/audio/asr.py 配置表、端口表、注释已改 system-one（端口仍 7001）
```

**S2.0 形态门禁已跑完，结论 = 形态 A（真跑通），不要再重跑、不要重新下载**

| 项 | 实测结论（16 逻辑核 / CPU / 8 线程） |
| --- | --- |
| 权重 | `convaiinnovations/laya-multilingual`，落 `D:\models\laya\multilingual`（615 MB，sha256 `9d628fd9…95aa8f204`） |
| 推理入口 | 模型仓库自带 `D:\models\laya\rl_agent_api.py` 的 `RLAgent(model_dir).system_one(state, questions)`，**禁止 import pip 的 `laya` 包** |
| 准确率 | 20 条七类 **17/20 = 85%**（门槛 15/20 过；PRD 50 例 90% 仍差 5 点） |
| 延迟 | 1 问 p95 **167 ms** ✅ / 2 问 285 ms ❌ / 4 问 668 ms ❌（加线程仅快 ~20%） |
| 裁决 | **快路径只问 intent；is_sensitive 走 shadow（独立 400ms 预算，不阻塞路由）**；不改 PRD 200ms，不上 GPU |
| 3 个错例 | 「换一段」「声音小一点」「今天几号」——全因 `state` 缺 `current_task`/`media_status`，**不是模型随机错** |

**已知坑（写代码前必须记住）**：`confidence` 是**熵**不是 max prob；`noul` 题**不返回置信度**；
`rl_agent.act_probability` 恒为 1 无用；模型对**候选顺序敏感**；`head_max_len=256`。

**已合入他人代码，装配必须用真实签名**（见 §4.4）。

## 1. 目标与范围

只做两件事：

- **A2**：本地 sidecar（`/health` + `/v1/decide`）+ HTTP 客户端（快路径 200 ms / shadow 400 ms / 熔断降级）。
- **A3**：规则层 + 路由决策 + `decision_logs`，并把路由接进统一出口。

**明确不做**：重做 A1、改 `app/graphs/**`（AC-20 已由 A 线实现）、实现 scheduler、改前端、动 `docker-compose.yml`、改 0013 及更早迁移、改 PRD/技术文档正文。

## 2. 技术栈与硬约束

| 层 | 口径 |
| --- | --- |
| 主服务 | Python 3.12 + FastAPI + SQLAlchemy 2.x + Alembic；`uv run ...`；**项目 `.venv` 里没有、也不许装 torch** |
| sidecar 运行环境 | 独立 conda env **`jev`**：`D:\Users\Alienware\anaconda3\envs\jev\python.exe`（torch 2.14.0+cpu / transformers 5.17.0 / safetensors 0.8.0 / numpy 2.5.4） |
| 启动 sidecar | `PYTHONPATH=<repo> D:\Users\Alienware\anaconda3\envs\jev\python.exe -m uvicorn app.system_one.server:app --port 7001`（**不能用 `uv run`**，否则 import torch 失败） |
| 模型目录 | 取 `settings.system_one_model_path`（空则启动即 `/health` 503 `model_missing`）；代码目录 = 模型目录的**父目录**（`D:\models\laya`），`sys.path.insert` 后 `from rl_agent_api import RLAgent` |
| 重依赖隔离 | `torch/transformers/safetensors/rl_agent_api` **只能在函数内延迟导入**，类型标注用 `Any`；`app/system_one/__init__.py` **不得**导出 backend/server，保证 `uv run pytest` 与主服务 import 不触发 torch |
| 硬性数值 | 客户端超时 200 ms；sidecar 内部预算 **180 ms**（= 200 − 20 序列化/网络缓冲；实测单问 p95 167 ms，故不再用原计划的 150 ms）；置信阈值 0.85；并发 1；熔断连续 3 次失败/30 s + 半开 1 次；shadow 超时 400 ms；规则 ≤10 条；`decision_logs` 每轮 1 行 |
| 分层 | `app/services/` 唯一写库层；`app/router/` 不得 import DB/httpx/mcp/ToolExecutor 实现，依赖全部注入 |
| 禁止 | 商业 Jev API、云端分类 SaaS、为"跑通"调大 `system_one_timeout_ms`、复制第二套权限判断/计数器/触达消息 |

## 3. A2 · sidecar 与客户端

### 3.1 `app/system_one/backend.py`

```python
class DecisionBackend(Protocol):
    async def score(self, *, state: str | dict | list, questions: dict[str, dict]) -> dict: ...
```

- `ChineseJevBackend`：`_ensure_loaded()` 里延迟导入 `torch` 与 `rl_agent_api.RLAgent`；**只加载一次**（`asyncio.Lock` 防并发重复加载）；设备取 `settings.system_one_device`；`score()` 返回 `RLAgent.system_one()` 的原始 `answers`。
- `MockBackend`：固定映射，**仅** `settings.system_one_provider == "mock"` 或测试使用。
- 结果映射（只此一处，别处不许再解析原始 JSON）：
  - `intent = intent_of_key(answers["intent"]["choice"])`
  - `confidence = answers["intent"]["confidence"]`（**熵**，别自己用 max prob 重算）
  - `is_sensitive = (answers["is_sensitive"]["noul"] >= SENSITIVE_THRESHOLD)`（阈值默认 **0.80**，常量放本文件并注明「实测偏自信，0.80 起步，50 例后再标定」）
  - `sensitive_probability = answers["is_sensitive"]["noul"]`
  - `privacy_scope = answers["privacy_scope"]["choice"]`
  - `is_out_of_domain = (answers["is_out_of_domain"]["noul"] >= 0.5)`
  - 缺题（shadow 未问）时对应字段留默认，**不得**抛异常

### 3.2 `app/system_one/server.py`

- `GET /health` → `{status, model_id, model_version, model_loaded, device, provider}`；模型缺失/加载失败 → **HTTP 503 + `{"status":"model_missing","model_loaded":false}`**（禁止 200 + false）。
- `POST /v1/decide`：按 `SystemOneRequest` 校验 → `Semaphore(settings.system_one_max_concurrency)` → `asyncio.wait_for(backend.score(...), timeout=0.180)` → 按 §3.1 映射 → 返回 `SystemOneResponse`。
  - 超时 → 503 + `{"status":"timeout"}`；backend 异常 → 503 + `{"status":"inference_failed"}`。**不要**返回半成品。
- lifespan：`system_one_warmup=true` 时预热 1 条；**启动失败只影响 /health，不崩进程**。
- 纪律：不连业务库、不写 `decision_logs`、不读 `runtime_context`；日志只记 `trace_id` 与文本长度/哈希。

### 3.3 `app/system_one/client.py`

```python
async def decide_fast(self, text: str, *, trace_id: str) -> SystemOneResult   # 只问 intent，200ms
async def decide_shadow(self, text: str, *, trace_id: str) -> SystemOneResult # 只问 is_sensitive，400ms
```

- 请求体用 `build_fast_questions()` / `build_shadow_questions()`，`state` 由调用方给（`build_state()` 已支持上下文）。
- `httpx.AsyncClient(timeout=0.200 / 0.400)`；`api_key` 非空才带 Bearer。
- 任何失败（连接/超时/5xx/schema 非法）→ `degraded_result(...)`，**不抛异常**；shadow 失败直接返回降级结果，调用方据此"当没问过"。
- 熔断：连续 3 次失败 / 30 s 打开，半开探测 1 次；打开期间立刻返回降级结果（不等超时）。
- 日志：`trace_id` + `text_len` + hash 前 8 位，**不记原文**。

### 3.4 `tests/test_system_one_client.py`（新建，≥6 条）

正常 / 超时 / 5xx / schema 非法 / 熔断打开后不再发请求 / 半开恢复；另加：
`decide_shadow` 失败返回 degraded 且 `is_sensitive=False`、`degraded_result()` 的三个不变量成立。
用 `httpx.MockTransport`，**不打真网、不加载模型**。

### 3.5 A2 验收

```bash
# 1) sidecar（另开终端；注意用 jev 的 python）
PYTHONPATH=. D:/Users/Alienware/anaconda3/envs/jev/python.exe -m uvicorn app.system_one.server:app --port 7001
curl -s localhost:7001/health        # 期望 200 + model_loaded=true；缺模型必须 503+model_missing
curl -s -X POST localhost:7001/v1/decide -H "content-type: application/json" \
  -d '{"request_id":"r1","state":{"asr_final":"今天天气怎么样"},"questions":{"intent":{"type":"choice","instructions":"判断意图","criteria":{"天气查询":"问天气","闲聊陪伴":"聊天"}}}}'
# 期望 200，result.intent=weather 且 latency_ms 有值

# 2) 客户端
uv run pytest -q tests/test_system_one_client.py     # 期望全绿
# 3) 停掉 sidecar 后再跑一次客户端用例，期望快速返回 degraded=True（≤250ms）
# 4) 无商业 API
grep -rn "api.jev\|jev.com\|openai" app/system_one | wc -l    # 期望 0
```

## 4. A3 · 规则层 + 路由 + `decision_logs`

### 4.1 `app/router/rules.py`

- `RuleHit`（`rule_id / intent / action / tool_name / tool_args / confidence=1.0`）+ `match_rule(text) -> RuleHit | None`。
- **纯函数、无 IO、无 DB、不读 settings**；`RULESET_VERSION` 常量。
- **≤10 条**，来源是 PRD（戏曲播放控制 8 条 + 天气 1 条 + 用药查询 1 条），**不许抄大纲课时正文的十句清单**。
- 每条至少 1 正例 / 1 负例；「不用停」这类否定句必须证明不误命中。

### 4.2 `app/router/decision.py`

```python
@dataclass(frozen=True, slots=True)
class RouteDecision:
    route_layer: Literal["rule", "system_one", "agent", "fallback"]
    action: Literal["tool", "agent", "clarify", "escalate"]
    intent: Intent; confidence: float; is_sensitive: bool; escalated_to_s2: bool
    reason: str | None; tool_name: str | None; tool_args: dict | None
    clarify_text: str | None; multi_intent: bool; sub_intents: tuple[Intent, ...]; latency_ms: int
```

- 顺序固定：**规则 → System One（快路径单问）→ Agent**；`decide(text, *, thread_id, runtime)`。
- 依赖**全部注入**：`system_one`（客户端）、`escalate`（装配层传既有 `_escalate_safety`）、`was_clarified`、`tool_executor_factory`。**不得 import DB / httpx / mcp / ToolExecutor 实现**。
- **G1**：`is_sensitive=True` → 调 `escalate`，`action="escalate"`、`tool_name=None`，**不进 Agent、不执行工具**；`escalate` 未装配时**保守按敏感处理**。
- 低置信（<0.85）或域外：`was_clarified(thread_id)` False → `clarify`；True → `agent`（`escalated_to_s2=True`）。同一 thread 只澄清一次。
- 意图→动作：`weather → get_weather`；`medication →` C1 工具（未到位则 `agent`）；`health_checkup/opera/movie` 未就绪 → `agent` + `reason="capability_not_ready"`；`chat/other → agent`。
- 多意图：简单顺序展开为**有序动作**并逐个过 G1/G2；动态组合直接 `agent`；**任一子句敏感 → 整轮 `escalate`**。
- 直连工具只走 `ToolExecutor.execute()`；失败不写第二套重试。

### 4.3 数据层

- `app/models/decision.py`：`DecisionLog` 严格按技术设计文档 §3.4 的 14 列。
- `alembic/versions/0014_decision_logs.py`：`down_revision="0013"`；索引 `(thread_id, created_at desc)`、`(route_layer, created_at desc)`、`(trace_id)`；CHECK `route_layer IN ('rule','system_one','agent','fallback','laya')`、`intent` 七类；**必须可 downgrade**，autogenerate 后人工读 diff。
- `app/services/decision_log_service.py`：`record_decision()` **只 INSERT**，`input_text` 落库前脱敏；失败不阻断对话（打 `decision_log_failed`）；另出只读 `last_action(thread_id)` 供 `was_clarified`。

### 4.4 接线

- `turn_service.py`：在**第 3 步 `context_builder.build` 与第 4 步 `agent.run` 之间**插入路由；`clarify` 直接构造 `AgentTurnResult` 不调 Agent；`tool` 经 `ToolExecutor` 后转可播报文本；`TurnOutcome` **只加** `route_layer/intent/confidence`；**删除或私有化 `TurnService.commit_response`（P3-31）**。
- `app/main.py`（唯一装配点，按真实签名照抄，别凭记忆）：

```python
# D1 调度（C2 已交付；比原计划多 4 个参数）
from app.scheduler.medication_jobs import install_scheduler
install_scheduler(app, session_factory=session_factory,
                  checkpointer_dsn=settings.langgraph_checkpoint_database_url,
                  checkpointer_schema=settings.langgraph_checkpoint_schema,
                  timezone_name=settings.scheduler_timezone)

# D2 工具绑定（C3 已交付；第三参是 checkpointer 实例，不是 dsn）
from app.agents.bind_tools import bind_dev_c_tools
bind_dev_c_tools(default_registry(), session_factory=session_factory, checkpointer=<同一个实例>)
```

  D3：`bind_dev_c_tools` 的实例与 `SchedulerRuntime` 必须指向**同一 DSN + 同一 schema**（`langgraph`）。
  D4：C1 已按 `ToolSpec.name/parameters_schema/side_effect` 写死，**不得再改 spec**。
  另：`SystemOneClient` + `Router` 注入 `TurnService`；预热失败只打日志；**不改** safety/orchestrator 现有顺序。

### 4.5 评测与报告

- 新建 `eval/fixtures/day7_intents.jsonl`（普通 50 + 敏感 20 + 边界 20 + 模糊 20）与 `scripts/eval_router.py`。
- `docs/plans/day07/a3-路由基线.md`：意图准确率、敏感召回、**四层命中率**（rule/system_one/agent/fallback）、p50/p95、bad case 逐条归因。
- **fallback 不计入 `system_one` 命中**；快决策层 ≥60% 未达标要如实标红，不改分母。
- 测准确率时 `state` **必须带 `current_task`/`media_status`**；若仍 <90%，归因到题面工程或能力边界并如实写，**不许换样本凑数**。

### 4.6 A3 验收

```bash
uv run alembic heads                                  # 单 head 0014
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
uv run pytest -q tests/test_router_rules.py tests/test_router_decision.py tests/test_decision_logs.py
uv run python scripts/eval_router.py --fixtures eval/fixtures/day7_intents.jsonl --report docs/plans/day07/a3-路由基线.md
grep -nE "httpx|session\.|app\.models|tool_executor" app/router/rules.py app/router/decision.py   # 期望 0
uv run pytest -q -m "not slow"                        # 零新增失败（当前基线 992 passed + 8 errors 属他人文件）
```

## 5. 验收总表

| 项 | 门槛 |
| --- | --- |
| A2 健康 | `/health` 200 + `model_loaded=true`；缺模型 503 + `model_missing` |
| A2 延迟 | 快路径 p95 ≤200 ms；shadow 超时不影响路由 |
| A2 降级 | 停 sidecar ≤250 ms 返回 `degraded=True`，不抛异常；三个不变量成立 |
| A2 隔离 | 主 `.venv` 无 torch；sidecar 只跑在 `jev`；无商业 API 域名 |
| A3 规则 | ≤10 条；每条 1 正 1 负；命中时 System One/Agent 调用 = 0 |
| A3 路由 | ≥0.85 走 `system_one`；<0.85 首次 `clarify`、二次 `agent`；失败 `fallback` |
| A3 安全 | G1 命中 → Agent 调用 0、工具执行 0；G2 直连与 Agent 同执行器 |
| A3 数据 | 每轮 1 行；`trace_id` 一致；`input_text` 无身份证明文；`model_version` 形如 `laya:multilingual:*` |
| A3 评测 | 普通 ≥45/50（90%）；敏感 ≥19/20；四层命中率如实报 |
| 全局 | ruff 全过；pytest 零新增失败；`update_progress.py --check` = 0 |

## 6. 必须停下来汇报的 6 种情况

1. 需要改禁触文件（`app/graphs/**`、`app/models/**`、`app/services/medication_service.py`、`work_order_service.py`、`app/scheduler/**`、`frontend/**`、0013 及更早迁移）。
2. 迁移不可逆或要删表。
3. 想调大 `system_one_timeout_ms` 才能"跑通"。
4. `install_scheduler` / `bind_dev_c_tools` 签名与 §4.4 不一致（先回报，不要单方面改）。
5. 50 例 <90% 且归因是标签/题面设计（回报，不要靠加规则硬凑，会破 ≤10 条）。
6. 发现 PRD 与技术设计文档冲突且拆不开（PRD 定验收、TD 定实现仍冲突）。

## 7. 汇报格式（A2 / A3 各一次）

```text
确认点：② A2 / ③ A3
状态：已完成 / 阻塞
交付物：<文件清单 + 要点>
证据：
- 命令：<原命令>
- 输出：<关键 5–10 行>
- 数据：</health 输出 / p95 / decision_logs 样例 / 50 例指标>
验收对照：§5 逐条 通过 / 不通过（不通过写原因）
未完成：<无 / 具体项 + 原因 + 下一步>
需要你决定：<无 / 具体问题 + 建议>
```

## 8. 联调边界（A3 收口时要主动打招呼）

- **C1**：`medication_confirm` 工具名/参数形状 → 直连与 Agent 必须同一 `ToolExecutor`。
- **C2**：`install_scheduler` 五参数 + checkpointer 同 DSN 同 schema。
- **D3**：`decision_logs` 查询 SQL + `route_layer` 四值语义。
- **B1**：`TurnOutcome` 只加三个字段，响应流契约不变。

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版：A1–A3 全量执行提示词 |
| v2.0 | 2026-11-10 | 改为**剩余 A2/A3** 版：A1 与 S2.0 门禁标为已完成事实（含权重/入口/85%/167ms 实测与快路径+shadow 裁决）；补 sidecar 用 `jev` python 启动、重依赖延迟导入、内部预算 150→180ms、C2/C3 真实装配签名（D1–D4）、AC-20 已由 A 线实现不再动 `graphs`、50 例须带上下文的纪律 |
