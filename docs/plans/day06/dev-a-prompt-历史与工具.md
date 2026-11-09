> 状态：草稿　｜　版本：v1.0　｜　日期：2026-11-09
> 关联：docs/plans/day06/dev-a-历史与工具执行计划.md｜docs/plans/day06/dev-a-DAG.md｜docs/team_tasks/day06/contracts.md
> 上游依据：技术设计文档 v2.0 §3.4/§3.6/§6/§7｜ER v0.5 §4.3.11–4.3.13｜PRD v2.9 FR-DLG-10、AC-08/17/30/35
> 变更记录见文末

# dev-A 交接提示词（可直接粘给 AI 编码助手）

> 用法：整段复制给 AI；它读完后应先回"我读到了什么、边界在哪、第一步做什么"，**不要**立刻开写。

---

## 1. 任务目标

你要在仓库 `kangban`（Python 3.12 + FastAPI + SQLAlchemy 2.x async）里，完成 **Day 6 · dev-A 的剩余工作**：把"会话事件账本 + 用药确认子图 + 工具执行器"三件事落地，使长者侧 Agent 具备可复盘的历史、幂等的副作用与可恢复的长流程。

**已完成的不要重做**：迁移 `0008/0009/0010` 已 apply（`alembic current` = `0010_day6_medication head`），业务表 24 张。

**剩余 17.0 人时，按 A02 → A17 顺序执行**（见 §5）。

---

## 2. 必读清单（按顺序读，别跳）

| 序 | 文件 | 读什么 |
| --- | --- | --- |
| 1 | `AGENTS.md` §2 / §4 / §5 / §7 | 分层硬约束、禁区、高风险文件、硬性数值 |
| 2 | `docs/team_tasks/day06/contracts.md` | §5.1 `HistoryStore` 六方法、§5.3 `ToolResult`、§5.4 `MCPResult`、§3 事件枚举 |
| 3 | `docs/康伴3D数字人_技术设计文档.md` §3.4（三表 + memory）、§3.6（索引/CHECK/批次）、§6.2–6.4（护栏与四类失败）、§7.1（用药子图） | 字段与行为口径 |
| 4 | `docs/ER_数据模型设计.md` §4.3.11–4.3.13 | 三张事件表的逐列定义 |
| 5 | `docs/plans/day06/dev-a-历史与工具执行计划.md` §3 | 每个节点的执行步骤（本提示词的上位文档） |
| 6 | `app/models/conversation.py`、`app/services/dialogue_service.py`、`app/agents/guardrails.py`、`tests/conftest.py`、`tests/test_staff_service.py` | 现有代码风格与测试夹具写法 |

**读完后必须先回答三个问题**（不写代码）：
1. 本任务哪些文件允许你新建／修改？
2. `seq` 为什么不能用 `max(seq)+1`？
3. 子图落库为什么要经过 service，而不是直接 `session.add`？

---

## 3. 代码范围

### 3.1 允许新建

```
app/services/history_service.py
app/services/medication_service.py
app/services/tool_executor.py
app/agents/tool_registry.py
app/graphs/checkpointer.py
app/graphs/medication_confirmation_graph.py
tests/test_history_store.py
tests/test_medication_graph.py
tests/test_tool_executor.py
```

### 3.2 允许修改（只追加，不改既有语义）

```
app/models/conversation.py      新增 3 个 Model；ConversationThread 只加 last_event_seq 一列
app/models/__init__.py          仅追加 3 个 import（不要重排整个文件）
```

### 3.3 严禁触碰

```
app/core/context.py             dev-B 的 L8 写锁
app/services/state_service.py   dev-B
app/services/response_service.py / turn_service.py / companion.py   dev-C
app/services/memory_service.py / models/memory.py                   dev-B
app/router/**  app/system_one/**  mcp-servers/**  app/mcp_client/** dev-C
frontend/**
alembic/versions/0001~0007      历史迁移不得改
app/models/medication.py        由 dev-B 交付；你只消费，不创建
```

> 碰到"必须改禁触文件才能继续"的情况：**停下来报告**，不要改。

---

## 4. 硬约束（违反即返工）

1. **分层**：`api | audio | scheduler | graphs → services → models`；`graphs` 里不得出现 `session.add/commit`，一律调 `medication_service`。
2. **身份**：`elder_id` 只从 `runtime_context` 读；`args` 里出现 `elder_id/thread_id/device_id` 一律丢弃并写 `audit_logs.action='tool_identity_override_attempt'`。
3. **`seq` 分配**：同事务内 `SELECT last_event_seq … FOR UPDATE` → +1 → 回写 → 插事件；禁止 `max+1`，禁止应用层加锁。
4. **事件只追加**：更正走新事件 + `ref_event_id`，不 UPDATE 旧行。
5. **幂等**：`tool_executions.idempotency_key` 唯一；用药事件 `(plan_id, scheduled_at)`；触达 `(event_id, attempt_no)`。同键同内容返回原结果，同键异内容报冲突。
6. **预算与熔断**：复用 `app/agents/guardrails.py` 的 `Guardrails.check()`，**不得**新写一套。
7. **不新增依赖**：不加 `pgvector`/`langgraph` 之外的库；`langgraph` 已可用则直接用，未安装则**停下来报告**。
8. **不写死密钥**：天气 key、DB 口令一律经 `Settings`。
9. **测试不能静默通过**：DB 不可达时 `pytest.skip(原因)`，与 `test_staff_service.py` 一致。
10. **提交粒度**：一次提交只做一件事，正文用中文写"为什么"。

---

## 5. 逐节点指令

> 每条都按「改哪些文件 → 做什么 → 验收命令」写。做完一条**跑一次验收**再进下一条。

### A02 · 三个 Model（1.0h）
- 改：`app/models/conversation.py`、`app/models/__init__.py`
- 做：`ConversationEvent` / `ConversationTurn` / `ToolExecution`，字段严格照 ER §4.3.11–4.3.13；`ConversationThread` 加 `last_event_seq`（默认 0，非空）。**不要**加 `relationship`。
- 验收：`uv run python -c "from app.models import ConversationEvent, ConversationTurn, ToolExecution"`

### A03 · 测试骨架（0.5h）
- 改：`tests/test_history_store.py`
- 做：照 `test_staff_service.py` 的 `session` fixture（`db_engine.connect()` + savepoint + 不可达 skip）；先写空测试占位。
- 验收：`uv run pytest tests/test_history_store.py -q`（允许 skip，不允许收集错误）

### A04 · `begin_turn` + `append_event`（2.0h）
- 改：`app/services/history_service.py`
- 做：`begin_turn` 插 `conversation_turns(status='open')` + 追加 `turn_started`；`append_event` 用行锁分配 `seq` 并插入事件，返回 `seq`。`turn_id` 重复时返回原值 + 打 `history_turn_duplicate`。
- 验收：`uv run pytest tests/test_history_store.py -q -k "begin or append"`

### A05 · `commit_response` + `finish_turn`（1.0h）
- 改：`app/services/history_service.py`
- 做：写 `assistant_delivered_text` 与 `status`；同 `(turn_id, response_id)` 幂等；`finish_turn` 置 `ended_at`。
- 验收：同文件 `-k "commit or finish"`

### A06 · `read_turns` / `read_events`（1.0h）
- 改：`app/services/history_service.py`
- 做：`read_turns` 强制 `elder_id` 过滤，`elder_id is None`（generic）返回空；`read_events` 按 `after_seq` 增量。
- 验收：`-k "read"`；外加一条并发用例：20 次 `append_event` 后 `seq` 无重复。

### A07 · 消费用药 ORM（0.5h，阻塞点 E1）
- 改：无（只读 `app/models/medication.py`）
- 做：确认六张表 ORM 可用；若 B 未交付，用 `sa.table()` 临时映射并在代码注释标 `TODO(E1)`。
- 验收：`uv run python -c "from app.models.medication import MedicationEvent"`（或临时映射可导入）

### A08 · checkpointer（1.0h）
- 改：`app/graphs/checkpointer.py`
- 做：独立 schema `langgraph` + 独立连接池；连接串由 `Settings` 派生，**不新建配置项**；schema 缺失时报可读错误，不在请求路径建表。
- 验收：`uv run python -c "from app.graphs.checkpointer import get_checkpointer"`

### A09 · 子图骨架（1.0h）
- 改：`app/graphs/medication_confirmation_graph.py`
- 做：`MedicationState` + `remind / wait_confirm / retry / finalize / escalate` 五个节点签名；先只连边，不写业务。
- 验收：`uv run python -c "from app.graphs.medication_confirmation_graph import build_graph; build_graph()"`

### A10 · `wait_confirm` + interrupt（1.0h，阻塞点 E2）
- 做：`interrupt` 等长者回答；恢复用 `Command(resume=...)`；触达出口先注入假 `deliver`（`TODO(E2)`）。
- 验收：`uv run pytest tests/test_medication_graph.py -q -k "interrupt or resume"`

### A11 · 重试与终态（0.5h）
- 做：重试 ≤3（T+5/+10/+15），终态四值互斥写 `terminal_state`；`late_taken` 追加不覆盖。
- 验收：`-k "retry or terminal"`

### A12 · 升级工单（1.0h）
- 做：`escalate` 节点经 `medication_service.escalate_to_work_order()` 写 `work_orders`（`type='missed_medication'`，`severity` 用快照）。
- 验收：`-k "escalate"`；并验证"同一 `reminder_id` 重复 escalate 不建第二张单"。

### A13 · registry（1.0h）
- 改：`app/agents/tool_registry.py`
- 做：条目含 `name/description/parameters_schema/side_effect/idempotent/executor`；本日注册 `memory_recall`、`get_weather`（占位）、`medication_confirm`。
- 验收：`uv run python -c "from app.agents.tool_registry import TOOL_REGISTRY; print(sorted(TOOL_REGISTRY))"`

### A14 · 身份 + 四类失败（1.5h）
- 改：`app/services/tool_executor.py`
- 做：`execute()` 签名照 `contracts.md` §5.3；身份只读 runtime；未注册 → `denied` + 审计；四类失败分支与话术。
- 验收：`uv run pytest tests/test_tool_executor.py -q -k "identity or forbidden or bad_args"`

### A15 · 幂等（1.0h）
- 做：`side_effect=True` 落 `tool_executions`；同键已成功返回原结果 + `idempotent_replay=True`。
- 验收：`-k "idempotent"`（重复调一次 + 同键异参一次）

### A16 · mcp 分支（1.5h，阻塞点 E3）
- 做：`MCPResult` 映射到 `ToolResult`；无 key/上游失败 → "查不到"话术；缓存 TTL 取 `weather_cache_ttl_seconds`。
- 验收：`-k mcp`；无 key 用例断言"不编造天气"。

### A17 · 门禁（1.5h）
- 验收：`uv run pytest -q && uv run pytest --cov=app --cov-report=term-missing`；`uv run python scripts/update_progress.py --check`；`--check-plans`。

---

## 6. 联调提醒与停止条件

### 6.1 到达这些节点必须停下请求联调（不得自行造桩替代跨人契约）

| 做完哪个节点 | 找谁 | 交接什么 |
| --- | --- | --- |
| A02 | dev-B | 0008 的 `down_revision`；三表字段清单 → 换 B 确认 0009 链接不断 |
| A06 | dev-B、dev-C | 六个方法签名 + `TurnView`/`EventView` 字段 → 换「可直接调用」的确认 |
| A07 前 | dev-B | 索取 `models/medication.py`（拿不到就用 `sa.table()` + `TODO(E1)`） |
| A10 前 | dev-C | 对齐 `deliver(reminder)` 入参与 ack 形状（拿不到就假实现 + `TODO(E2)`） |
| A15 | dev-C | `ToolResult` 四类失败 → 中文话术对照表 |
| A16 前 | dev-C | 索取 `app/mcp_client.call_tool()`；无 key 时只验失败用例 |
| A17 前 | dev-C、dev-D、dev-B | 真实一轮 `trace_id` + 事件流水 + pad 的 `segment_ack` 日志 |

**请求格式**（三行，别写「帮忙看下」）：

```
【交接】A06 → HistoryStore 签名
给你：begin_turn/append_event/commit_response/finish_turn/read_turns/read_events（附 TurnView 字段）
要你：确认 C 侧可直接调用；要加字段请先改 contracts.md §5.1 并升版本
```

### 6.2 停止条件（遇到就停，不要猜）

1. 需要改 §3.3 的禁触文件；
2. 需要新增依赖（如 `langgraph` 未安装）；
3. `contracts.md` 里的字段与 ER/DB 实际列不一致；
4. 子图实现必须绕开 service 层才能跑通；
5. 同一测试连续两次因为环境（DB/端口）失败。

停下时输出：**卡在哪一步 / 已尝试什么 / 需要谁提供什么**，不要自行降级实现。

---

## 7. 输出与报告格式

每完成一个节点，按此模板回报（贴进 PR 描述或日报）：

```
节点：A04  begin_turn + append_event
改动：app/services/history_service.py（+138 行）
验证：uv run pytest tests/test_history_store.py -q  → 5 passed
证据：并发 20 写，seq 为 1..20 无重复（日志片段 …）
遗留：无 / 依赖 E1 未到，已用 sa.table() 临时映射，TODO(E1)
```

全部完成后追加一段**交接摘要**：

```
交付文件：6 个（列出路径）
迁移：0008/0009/0010 已 apply，未新增
未完成：无 / 列出
他人依赖：等 dev-C 的 N25 替换健康检查桩
```

---

## 8. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 首版：目标、必读清单、白名单/禁触、10 条硬约束、A02–A17 逐节点指令、5 条停止条件、报告模板 |
| v1.1 | 2026-11-09 | 按要求新增 **§6.1 联调提醒表**（7 个触发点：做完哪个节点、找谁、交接什么）与三行交接格式；原「停止条件」降为 §6.2 |
