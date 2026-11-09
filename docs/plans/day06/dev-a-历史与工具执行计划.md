> 状态：进行中（A02–A11、A13–A15 已交付；剩 A12 升级工单、A16 接 MCP、A17 收口）　｜　版本：v1.3　｜　日期：2026-11-09
> 关联：docs/team_tasks/day06/dev-a-历史事件与工具事实.md（任务卡）｜docs/plans/day06/day-06-长者侧Agent闭环.md（Day 6 主计划）｜docs/team_tasks/day06/contracts.md（冻结契约）
> 上游依据：PRD v2.9 §6.1 Day 6 行、§4.5 用药、FR-DLG-10、AC-08/17/30/35（验收）｜技术设计文档 v2.0 §3.4/§3.6/§6/§7/§8.3（实现）｜ER v0.5 §4.3.11–4.3.13、§5（字段与约束）
> 变更记录见文末

# dev-A 执行计划 · 历史事件与工具事实

> **角色**：dev-A（后端 · 历史与工具事实线）
> **范围**：主计划 25 节点中的 7 个（N00 共担、N01、N04、N10、N12、N26、N23），合计 **21.0 人时**，已完成 **17.0**，**剩 4.0**（A12 1.0 / A16 1.5 / A17 1.5）。
> **一句话职责**：让"发生过什么"可查可复算，并保证每次有副作用的调用只发生一次。

---

## 0. 边界与口径（先读，避免越界返工）

### 0.1 本计划只覆盖这 7 个节点

| 节点 | 内容 | 人时 | 状态 |
| --- | --- | --- | --- |
| N00 | 契约中的"表与事件枚举"章节（与主理人共担） | 1.5 | ✅ 已完成（`contracts.md` v1.0 已冻结） |
| N01 | 迁移 0008 + `models/conversation.py` 三个 Model | 3.5 | ✅ ORM 三类已建，`alembic check` 对三表零差异 |
| N04 | `HistoryStore`（事件追加 + 轮次投影 + 幂等） | 4.5 | ✅ 六方法 + 12 条测试全绿（含并发 20 写无重号） |
| N10 | `medication_confirmation_graph` + PG checkpointer | 5.0 | 🟡 A08–A11 已完成（checkpointer + 子图 + 四次触达 + 终态写一次，5 条测试全绿）；**A12 升级工单未做**，触达下发为占位（`TODO(E2)`） |
| N12 | `ToolRegistry` + `ToolExecutor` | 3.5 | ✅ 白名单 + 四类失败 + 幂等 + 审计，13 条测试全绿 |
| N26 | `ToolExecutor` 接 MCP + 缓存与降级话术 | 1.5 | ⬜ 未开始（等 E3 `app/mcp_client`） |
| N23 | 门禁收口 | 1.5 | ⬜ 未开始 |

### 0.2 明确不做（做了就是越界）

| 不做 | 归属 |
| --- | --- |
| `app/services/state_service.py`、`session_coordinator.py` | dev-B（N05） |
| `app/core/context.py` 任何改动 | dev-B（N06，L8 写锁） |
| `app/services/response_service.py`、`turn_service.py`、`companion.py` | dev-C（N07/N13/N14） |
| `app/services/memory_service.py`、`models/memory.py` | dev-B（N02/N08） |
| `app/router/**`、`app/system_one/**`、`mcp-servers/weather/**`、`app/mcp_client/**` | dev-C（N27/N24/N25） |
| `frontend/**` | dev-D（N03/N16/N18） |
| 修改 `alembic/versions/0001–0007` | 历史迁移一经合入不得改（`AGENTS.md` §4-6） |

### 0.3 两个待确认项（不替你决定，先按默认走）

| # | 事项 | 默认（本计划按此写） | 影响 |
| --- | --- | --- | --- |
| **Q1** | 上一轮提的调剂：把 **N11 知识服务（2.0h）从 dev-C 转给 dev-A**（A 在 W3 空转） | **不接**，本计划仍为 7 节点 21.0h | 若接，A 变 23.0h，且需在 `spikes/` 之外新增 `knowledge_service.py` 白名单 |
| **Q2** | 天气 `weather_api_key` 当前为空，N26 的"真实天气"无法端到端验收 | **按"无 key → 明确失败不编造"验收**，有 key 后再补一条成功用例 | 不影响 N26 交付，影响演示效果 |

---

## 1. 项目认知（只保留与 dev-A 相关的部分）

### 1.1 分层与写库纪律

```
api | audio | scheduler | graphs  →  services  →  models
```

- **`app/services/` 是唯一写库层**。`app/graphs/**` 里**不得**直接 `session.add()`，必须调 service（`AGENTS.md` §2 硬约束 1）。这条对 N10 尤其关键：子图只做状态流转，落库走 `medication_service`（本计划新建在 services 层）。
- 模块间禁止横向 import：`graphs` 不得 import `agents`，`agents` 不得 import `graphs`。N10（graphs）与 N12（agents/services）之间只通过 `services` 与 `contracts.md` 里的数据结构交互。

### 1.2 现状基线（实测，不要按文档想象）

| 项 | 现状 | 证据 |
| --- | --- | --- |
| `models/conversation.py` | **只有** `ConversationThread` 一个类 | `grep "^class"` 实测 |
| `models/medication.py` | 不存在（0010 只建了表，ORM 归 dev-B） | 目录实测 |
| `services/history_service.py` | 不存在 | 目录实测 |
| `services/tool_executor.py` | 不存在 | 目录实测 |
| `agents/guardrails.py` | 已有 `Guardrails.check()`，含同 Tool 熔断与重复调用短路 | 源码 |
| `agents/tool_registry.py` | 不存在 | 目录实测 |
| `graphs/` | 只有 `__init__.py` | 目录实测 |
| 迁移 0008/0009/0010 | **已 apply**，`alembic current` = `0010_day6_medication (head)` | 实测 |
| 业务表 | 24 张 + `alembic_version` | `information_schema` 实测 |
| 测试夹具 | `tests/conftest.py` 提供 `db_engine` / `db_conn`；DB 不可达时 `pytest.skip` | `test_staff_service.py` 的 `session` fixture 是同款写法 |

### 1.3 硬性数值（写死，不得调整）

`MAX_TOOL_CALLS_PER_TURN=8`｜同 Tool 1 分钟内第 3 次熔断｜用药触达 T0/+5/+10/+15，每次等 60s｜重试 ≤3 次后进终态｜原始音频 72h｜`seq` 与会话绑定且唯一。

### 1.4 dev-A 的四条铁律

1. **`seq` 必须行锁分配**：`SELECT … FOR UPDATE` 锁 `conversation_threads` 行后 +1，禁止"先查 max 再 +1"。
2. **事件只追加**：更正走新事件 + `ref_event_id`，不 UPDATE 旧行。
3. **身份只从 `runtime_context` 注入**：`args` 里的 `elder_id` 一律丢弃并写审计。
4. **子图不直写库**：`graphs` 只调 service。

---

## 2. 第六天任务清单（dev-A 视角）

> Day 6 全量是 16 项任务（T1–T16，见主计划 §2）。下表只列**与 A 有关**的，并标出 A 是主责还是配合。

| # | 任务 | A 的角色 | 前置条件 | 依赖 | 量化验收 |
| --- | --- | --- | --- | --- | --- |
| T1 | 口径变更登记与契约冻结 | **主责之一**（表与事件枚举章节） | 主理人在场 | — | ✅ 已完成：`contracts.md` 含 12 类事件与三表字段 |
| T2 | 事件三表迁移与 ORM | **主责** | T1 | T1 | `alembic heads` 单 head；三表可升可降；ORM 与 ER 逐列一致 |
| T5 | HistoryStore | **主责** | T2 | T2 | 两轮按 `seq` 读回；partial 不入库；同 `turn_id` 幂等 |
| T10 | 用药 6 表 + 子图 | **主责（子图部分）** | T3（B 的表） | T3、T8 | 双入口；`graph.get_state` 可见；重启不重发已完成触达 |
| T11 | ToolRegistry + ToolExecutor | **主责** | T2、T4 | T2、T4 | 身份越权拒绝 + 审计；四类失败各有分支；幂等键唯一 |
| T16 | 天气 MCP（N26 部分） | **配合**（消费 C 的 client） | C 的 N25 | C 的 N25 | 「今天天气」走通；无 key 明确失败不编造 |
| T15 | 作业与门禁收口 | **主责（门禁）** | T5/T10/T11 | 全部 | `pytest` 全绿；cov ≥70%；看板与索引无过期 |

### 2.1 任务间的先后关系（A 内部）

```
T1 ──► T2 ──┬──► T5 ──────────────┐
            ├──► T11 ──► T16      │
            └──►(B 的表)──► T10 ───┴──► T15
```

- T2 是 A 的**唯一硬前置**，其余都可并行推进（但 A 只有一双手，实际按 §4 的 A-DAG 顺序）。
- T10 的启动不必等 T11；它只等 **B 的 0010 ORM** 与 **C 的 N07 触达出口**。

---

## 3. 逐节点执行步骤

### N00（已完成，仅留痕）

`contracts.md` 中 A 负责的两节已冻结：**§3 事件枚举 12 种**、**§7 数据层落地**。后续任何字段变化，先改 `contracts.md` 并升版本号，再改代码。

### N01 · 迁移 0008 的 ORM 补齐（剩 1.0h）

1. 打开 `app/models/conversation.py`，**保留** `ConversationThread` 不动，新增三个类：`ConversationEvent` / `ConversationTurn` / `ToolExecution`。
2. 字段严格照 ER §4.3.11–4.3.13；`payload_json` 用 `JSONB`，`seq` 用 `BigInteger`。
3. `ConversationThread` **加一列** `last_event_seq`（`Integer`/`BigInteger`，`server_default="0"`），与迁移 0008 的 `ADD COLUMN` 对齐。
4. 在 `app/models/__init__.py` **只追加**三个 import（L5 写锁：A→B→A 顺序，别顺手排序整个文件）。
5. 不写 `relationship()` 的双向级联——本日没有跨表 ORM 导航需求，加了反而会带来 lazy load 在 async 下的坑。

**验收**：`uv run python -c "from app.models import ConversationEvent"` 可导入；`alembic check`（若版本支持）或 `alembic revision --autogenerate` 的 diff 为空。

### N04 · HistoryStore（4.5h）

按此顺序实现，每步都能单独跑通：

1. **`begin_turn`**：插入 `conversation_turns`（`status='open'`）→ `append_event(turn_started)` → 返回 `TurnRef(turn_id, seq)`。`turn_id` 已存在时返回原值并打 `history_turn_duplicate`，不新增行。
2. **`append_event`**（核心）：同一事务内
   ```
   SELECT last_event_seq FROM conversation_threads WHERE thread_id=:tid FOR UPDATE
   → next = last + 1 → UPDATE last_event_seq=next → INSERT conversation_events(seq=next)
   ```
   返回 `next`。**禁止**用 `max(seq)+1`。
3. **`commit_response`**：写 `assistant_delivered_text` + `status`；同 `(turn_id, response_id)` 重复调用只生效一次（查已存在则直接返回）。
4. **`finish_turn`**：置 `ended_at` 与终态 `status`。
5. **`read_turns` / `read_events`**：`read_turns` 必须带 `elder_id` 过滤，且 **generic 模式（`elder_id is None`）返回空列表**；`read_events` 按 `after_seq` 增量读。
6. **单测**：`tests/test_history_store.py`，用 conftest 的 `db_engine` + savepoint 写法（照 `test_staff_service.py` 的 `session` fixture），覆盖：两轮顺序、partial 不入库、同 turn 幂等、跨 thread 不可见、generic 返回空、**并发 20 次 `append_event` 无重号**。

**验收**：见 §5 的 V3。

### N10 · 用药子图 + checkpointer（5.0h）

> **前置**：B 的 0010 ORM（`models/medication.py`）已合入；C 的 N07 提供"下发触达 + 收 ack"的出口。两者未就绪时，先做 `checkpointer.py` 与图的骨架，用假 `deliver` 桩推进。

1. **`app/graphs/checkpointer.py`**：独立 schema `langgraph` + 独立连接池；启动时 schema 必须已存在（由迁移或 `CREATE SCHEMA IF NOT EXISTS` 保证），**不在请求路径建表**。
2. **`app/services/medication_service.py`**（新）：子图的唯一写库出口——`create_event`（幂等键 `(plan_id, scheduled_at)`，同键异内容抛冲突）、`record_attempt`（键 `(event_id, attempt_no)`）、`set_terminal`（四值互斥）、`escalate_to_work_order`。
3. **子图状态**：`MedicationState`（`reminder_id / plan_id / elder_id / attempt_no / deadline / answer / terminal`）。
4. **节点**：`remind` → `wait_confirm`（`interrupt`）→ 命中 `record_taken`；超时 `retry`（≤3）→ `finalize` 终态分流 → 需要时 `escalate`。
5. **双入口**：`triggered_by=scheduler|dialog` 只是入参差异，同一张图。
6. **事实修订**：`taken_reported` 后收到"我说错了" → 新增版本 + `late_taken` 标记，**不覆盖原终态**、不自动关工单（AC-08）。
7. **恢复测试**：进程重启后 `graph.get_state(config)` 能读到等待节点；**不重发已完成触达**。

**验收**：见 §5 的 V8。

### N12 · ToolRegistry + ToolExecutor（3.5h）

1. **`app/agents/tool_registry.py`**：条目含 `name / description / parameters_schema / side_effect / idempotent / executor`。本日注册：`memory_recall`（B 提供）、`get_weather`（C 的 MCP，N26 才接）、`medication_confirm`（子图入口）。
2. **`app/services/tool_executor.py`**：
   - `execute(*, tool_name, args, runtime, turn_id, idempotency_key) -> ToolResult`（签名见 `contracts.md` §5.3）；
   - 身份：`runtime.elder_id` 是唯一来源，`args` 里的 `elder_id/thread_id/device_id` 丢弃 + `audit_logs.action='tool_identity_override_attempt'`；
   - 白名单：未注册直接 `denied` + 审计；
   - 幂等：`side_effect=True` 必须落 `tool_executions`（`UNIQUE(idempotency_key)`），同键已成功返回原结果并置 `idempotent_replay=True`；
   - 四类失败：`bad_args` 回传模型（计预算）｜`not_found` 如实告知｜`transient` 退避 ≤2｜`forbidden` 拒绝 + 审计；
   - **预算与熔断复用 `Guardrails.check()`**，不新建第二套（`AGENTS.md` §4-8）。
3. **单测**：`tests/test_tool_executor.py`，四条失败各一例 + 越权一例 + 幂等两例（重复调 / 同键异参）。

**验收**：见 §5 的 V9。

### N26 · 接 MCP（1.5h，依赖 dev-C 的 N25）

1. 在 `ToolExecutor` 内加 `mcp` 分支：`get_weather` 走 `app/mcp_client`，把 `MCPResult` 映射到 `ToolResult`（`timeout→transient`、`unauthorized→forbidden`、`bad_response→transient`、`upstream_error→transient`）。
2. 降级话术：无 key 或上游失败 → "这个我暂时查不到，帮您记下来"，`status=failed, error_class=transient`；**禁止**用模型常识生成天气。
3. 缓存：TTL 取既有 `weather_cache_ttl_seconds`（600s），**不新增配置项**；命中缓存时话术带"（数据时间 xx:xx）"。
4. 不改 `execute()` 签名（L10 写锁）。

**验收**：见 §5 的 V10。

### N23 · 门禁收口（1.5h）

1. `uv run pytest -q` 全绿；`--cov=app` 核心包 ≥70%。
2. `uv run python scripts/update_progress.py --check` 与 `--check-plans` 均 0。
3. README 的"常用命令/表数量"若有偏差，同批回填（不新开文档）。
4. 汇总一份**交接清单**：本日 A 交付的 6 个文件 + 3 条迁移 + 未完成项（如有）。

---

## 4. 交付物规格

| # | 交付物 | 格式 | 内容范围 | 质量红线 |
| --- | --- | --- | --- | --- |
| DA-1 | `app/models/conversation.py`（扩展） | Python | 3 个新 Model + `last_event_seq` | 与 ER 逐列一致；不加无用的 `relationship` |
| DA-2 | `app/services/history_service.py` | Python | 6 个方法 | 全 async；`seq` 行锁；**无 UPDATE 旧事件路径** |
| DA-3 | `app/services/medication_service.py` | Python | 4 个写库方法 | 两个幂等键；终态四值互斥；越权审计 |
| DA-4 | `app/graphs/medication_confirmation_graph.py` | Python | 双入口 + 重试 + 终态 + 升级 | 只调 service，不直接 `session.add` |
| DA-5 | `app/graphs/checkpointer.py` | Python | 独立 schema + 连接池 | schema `langgraph`；不在请求路径建表 |
| DA-6 | `app/agents/tool_registry.py` + `app/services/tool_executor.py` | Python | 白名单 + 四类失败 + 幂等 + mcp 分支 | 身份只从 runtime；复用 Guardrails |
| DA-7 | 三个测试文件 | Python | 各覆盖 §5 判据 | DB 不可达时 `pytest.skip`，不静默通过 |
| DA-8 | 门禁清单 | Markdown（贴 PR 描述即可） | 命令 + 输出摘要 | 没有运行输出只能写"已代码审查" |

---

## 5. 自检校验标准（命令级）

| # | 项 | 命令 / 判据 | 通过标准 |
| --- | --- | --- | --- |
| V1 | 迁移可逆 | `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` | 三次全成功（已于 11:45 实测通过，改动 schema 后需重跑） |
| V2 | 表约束 | `docker exec silvermate-postgres psql -U silvermate -d silvermate -c "\d conversation_events"` | 见 `UNIQUE(thread_id, seq)` 与 `ck_conversation_events_event_type` |
| V3 | 历史语义 | `uv run pytest tests/test_history_store.py -q` | 6 条用例全绿；并发 20 写无重号 |
| V8 | 用药恢复 | `uv run pytest tests/test_medication_graph.py -q` | 重启恢复 + 幂等不重复建单 + 重试 ≤3 后进终态 |
| V9 | 工具越权 | `uv run pytest tests/test_tool_executor.py -q` | 越权拒绝并留审计；四类失败各一例 |
| V10 | MCP 分支 | `uv run pytest tests/test_tool_executor.py -k mcp -q` | `MCPResult` 四类映射正确；无 key 不编造 |
| V11 | 全量门禁 | `uv run pytest -q && uv run pytest --cov=app --cov-report=term-missing` | 全绿；核心包 ≥70% |
| V14 | 文档一致 | `uv run python scripts/update_progress.py --check` / `--check-plans` | 退出码 0 |

---

## 6. 与其他任务的边界与对接

### 6.1 我给别人什么

| 时点 | 对象 | 交付物 | 判据 |
| --- | --- | --- | --- |
| J1（W1 末） | dev-B | 0008 的 `down_revision`（`0007_admin_org_audit`） | `alembic heads` 单 head |
| J2（W2 末） | dev-B / dev-C | `read_turns / read_events / begin_turn / commit_response` 签名与 `TurnView` 结构 | 签名与 `contracts.md` §5.1 一致 |
| J4（W4 末） | dev-C | `ToolResult` 与 `execute()` | 四类失败各跑一条 |
| J7（W4 末） | dev-C | `get_weather` 消费契约（入参、错误映射） | 双方字段名逐一对照 |

### 6.2 我需要别人什么

| 需要 | 谁 | 没有它时怎么办 |
| --- | --- | --- |
| `models/medication.py`（0010 ORM） | dev-B | 用 `sa.table()` 临时映射，不写 ORM 类（避免与 B 撞 L5 锁） |
| 触达出口 `deliver_reminder()` + ack 回调 | dev-C（N07） | 用本地假实现推进，联调期替换 |
| `app/mcp_client` 的 `call_tool()` | dev-C（N25） | N26 阻塞，先做 §3 前五步 |

### 6.3 冲突面（写锁）

L1（0008，已释放）、L4（`models/conversation.py`）、L5（`models/__init__.py`，A→B→A）、L10（`tool_executor.py`）。**L5 特别注意**：B 要在同文件加 `models/memory.py` 与 `models/medication.py` 的 import，两人不能同时改。

---

### 6.4 联调提醒（做到这一步就必须喊人）

> 规则：**到了触发点就停下联调，不许自己造桩把跨人契约"先糊过去"**。造桩只能用于推进本地逻辑，且必须带 `TODO(Ex)` 标记。

| # | 触发点（你做到哪） | 找谁 | 你要交出去 | 你要拿回来 | 没到位的处置 |
| --- | --- | --- | --- | --- | --- |
| **J1** | A02 完成后（三 Model 可导入） | **dev-B** | 0008 的 `down_revision=0007_admin_org_audit`；三表 ORM 字段清单 | B 确认 0009 的 `down_revision=0008`，链不断 | 若 B 已建 0009，一起跑 `alembic heads` 确认单 head |
| **J2** | A06 完成后（六方法可跑） | **dev-B、dev-C** | `begin_turn / append_event / commit_response / finish_turn / read_turns / read_events` 六个签名 + `TurnView`/`EventView` 字段 | 双方确认"C 能直接调用、不改签名" | 未确认就冻结签名；后续要加参数走 `contracts.md` 版本 +1 |
| **E1** | A07 开始前 | **dev-B** | 你需要的 6 个 Model 名与用到的字段 | `models/medication.py` 可导入 | 用 `sa.table()` 临时映射 + `TODO(E1)`，**不**自建 ORM 类（L5 锁） |
| **E2** | A10 开始前 | **dev-C** | 你期望的 `deliver(reminder)` 入参与 ack 回调形状（字段名 + 语义） | N07 的触达出口与 ack 消息定义 | 注入假 `deliver` + `TODO(E2)`，A12 联调时替换 |
| **E3** | A16 开始前 | **dev-C** | `get_weather` 入参、返回结构、错误枚举 | `app/mcp_client.call_tool()` 与 `MCPResult` | A16 标阻塞，先做 A13–A15；A17 门禁注明"缺 MCP 分支" |
| **J4** | A15 完成后 | **dev-C** | `ToolResult` 四类失败 → 话术对照表（每条给一句中文） | C 确认 Agent 侧按此渲染、不再二次加工 | 未确认不进 A17 |
| **J7** | A16 完成后 | **dev-C** | 两条用例输出：①有 key 成功（含数据时间）②无 key 明确失败 | C 提供 7101 端口的 server，两人同 `trace_id` 对一次 | 无 key 时只跑失败用例，成功用例标 `skip` |
| **J5** | A17 门禁前 | **dev-C + dev-D + dev-B** | 真实一轮的 `trace_id`、`conversation_events` 流水、`delivered_text` | pad 端 `segment_ack` 上报日志与截图 | 不齐就不能签 G-A4 |

### 6.5 交接怎么说（照这个格式发消息）

```
【交接】J2 HistoryStore 签名确认
我在：A06 已完成（tests/test_history_store.py 6 项通过）
给你：begin_turn/append_event/commit_response/finish_turn/read_turns/read_events
      TurnView = turn_id, user_text, assistant_delivered_text, status, started_at, ended_at
要你：确认 C 侧按此签名调用，不需要额外参数
卡点：无 / 若你要加 xxx 字段，请在 contracts.md §5.1 升到 v1.1
```

三行就够：**我在哪、给你什么、要你什么**。别写"帮忙看下"。

## 7. 风险与回退

| 风险 | 触发信号 | 处置 |
| --- | --- | --- |
| 子图写库越过 service 层 | code review 看到 `graphs` 里 `session.add` | 打回重写；这是 `AGENTS.md` §2 的硬约束 |
| `seq` 并发重号 | 并发用例报 `IntegrityError` | 确认用的是行锁而非 `max+1`；必要时改 `INSERT ... SELECT last_event_seq+1` |
| B 的 ORM 迟迟不到，N10 空转 | W3 末仍无 `models/medication.py` | 降级为"工具 + 业务表"实现，**报主理人签字**，登记技术债 |
| 天气 key 为空 | N26 联调只说"查不到" | 按 Q2 默认验收；补一条"有 key 才跑"的 skip 用例 |
| 21h 超载 | W2 末剩余 >14h | 把 N12 拆给 dev-C（他写 mcp_client，接自己的客户端更快） |

---

## 8. 执行记录（2026-11-09 下午）

| 节点 | 产物 | 验证 |
| --- | --- | --- |
| A02 / N01 | `app/models/conversation.py` 三个 Model + `last_event_seq`；`models/__init__.py` 追加导出 | `alembic check` 对三表与 `last_event_seq` **零差异**（其余报错为 B/C 未同步的既有漂移） |
| A03–A06 / N04 | `app/services/history_service.py`（六方法 + 三个视图对象 + 四个域异常）；`tests/test_history_store.py` 12 条 | `pytest tests/test_history_store.py -q` → **12 passed**，含并发 20 写 `seq` 恰为 1..20 |
| A13–A15 / N12 | `app/agents/tool_registry.py`、`app/services/tool_executor.py`；`tests/test_tool_executor.py` 13 条 | `pytest tests/test_tool_executor.py -q` → **13 passed** |
| 全量回归 | — | `pytest -q` → **720 passed / 4 skipped / 2 failed**；两条失败为既有问题（见下） |

**执行中发现并修掉的两处设计缺陷（不在原计划里）**

1. **`read_turns` 按 `started_at` 排序会错序**：`now()` 取的是**事务开始时间**，同一事务里建的两轮拿到完全相同的时间戳，排序结果由数据库随机决定 —— 上下文一旦错序，「刚才那个」就指到别处。改为按各轮 `turn_started` 事件的 `seq` 排序（`seq` 本来就是业务顺序）。
2. **`finish_turn` 对 generic 轮次什么都不写**：generic 不建投影行，原实现查不到 turn 就直接 return，导致通用模式的每一轮都停在 `turn_started`，重连补帧时「永远没结束」。改为用 `turn_id`（有索引）回查所属 thread 并补一条 `turn_completed`。

**契约按冻结规则升到 v1.1**（只加可选参数，向后兼容）：`append_event` 增 `call_id` / `trace_id` —— 0008 建了这两列却没有任何写法能填，留着就是死列；`begin_turn` 增 `identity_source`（缺省取会话行上的值）。

**两条失败用例与本次改动无关**（已用 `--ignore` 去掉两个新文件复现同样结果）：`test_edge_live_synthesis_over_network`（依赖外网 Edge TTS）、`test_voiceprint_service::test_delete_all_removes_rows_and_reports_count`（全量跑时的用例间数据耦合，单独跑通过）。

**仍未开始**：A07–A12（缺 E1 `models/medication.py` 与 E2 触达出口）、A16（缺 E3 `app/mcp_client`）、A17（收口）。

| A07–A11 / N10 | `app/graphs/checkpointer.py`、`app/services/medication_service.py`、`app/graphs/medication_confirmation_graph.py`；`tests/test_medication_graph.py` 5 条 | `pytest tests/test_medication_graph.py -q` → **5 passed**：正常确认 / 重启恢复 / 四次触达后 `missed_unconfirmed` / 事件幂等 / 终态不覆盖 |

**A07 的过渡方案（主理人已于 2026-11-09 签字）**：`medication_service.py` 用 `sa.table()` 临时映射六张用药表，标注 `TODO(E1)`；B 的 `models/medication.py` 到位后**只改这一个文件的表定义**，子图与调用方一行不动。放弃自建 ORM 的理由：会出现两份 `medication_events` 定义，且 `Base.metadata` 多一张同名表，`alembic check` 立刻报重复表。

**A12 明确未做**：`finalize` 只在状态里置 `needs_escalation=True`，建工单留给 A12。原因是 N27（dev-C 的安全层）同样要建工单，两个写口并存会让「谁负责建单、`order_no` 谁生成」没有唯一答案——这部分要等 C 的 `safety.py` 落地后对齐同一个服务函数。

**新增一条 Windows 本地约束**：psycopg 3 的异步连接不支持 ProactorEventLoop，`checkpointer` 走 psycopg 异步池 → Windows 上跑用药用例必须换 Selector 策略。已写进 `tests/conftest.py` 顶部（生产是 Linux 容器，不受影响）。

## 9. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 首版：dev-A 7 节点 / 21.0h 逐节点执行步骤、8 项交付物规格、7 条自检判据、三方对接清单、5 条风险；Q1（是否接 N11）、Q2（天气 key）列为待确认，按默认口径编写 |
| v1.1 | 2026-11-09 | 按主理人要求补 **§6.4 联调提醒触发表**（7 个触发点：找谁 / 交出什么 / 拿回什么 / 未到位怎么办）与 **§6.5 交接话术模板**（我在哪、给你什么、要你什么） |
| v1.3 | 2026-11-09 | 用药线落地：A07（`sa.table()` + `TODO(E1)`，主理人签字）、A08–A11（checkpointer 独立 schema/独立池、`interrupt` 恢复、四次触达、终态写一次），5 条测试全绿；累计 17.0h、剩 4.0h；新增 A12 未做的理由与 Windows 事件循环约束 |
| v1.2 | 2026-11-09 | 开工执行：Q1 裁决「不接 N11」（本计划范围不变）；A02–A06 / A13–A15 完成，累计 13.0h、剩 8.0h；新增 **§7.5 执行记录**（产物、测试数字、两处设计缺陷修正、全量回归结论）；契约升 v1.1 |
