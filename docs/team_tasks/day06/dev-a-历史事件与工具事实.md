> 状态：未开始　｜　版本：v1.3　｜　日期：2026-11-09
> 负责人：dev-A（后端 · 历史事件与工具事实）
> 关联：docs/plans/day06/day-06-长者侧Agent闭环.md（主计划 §3.1 节点表为唯一事实源）｜docs/team_tasks/day06/dev-b/dev-c/dev-d
> 上游依据：技术设计文档 line 536（conversation_threads）、line 619（decision_logs 不建）、line 690-701（§3.6 迁移批次）、line 836-869（§6.2 护栏 / §6.4 四类失败）｜PRD line 275（FR-DLG-10 预算与熔断）、line 511（AC-30 重启恢复）
> 变更记录见文末

# Task dev-A · 历史事件与工具事实（Day 6）

## 1. 目标与边界

**一句话**：把"发生过什么"落成可复盘的事件账本，并保证每次有副作用的调用只发生一次。

**负责节点**：N00（与主理人共担）、N01、N04、N10、N12、N26、N23，合计 21.0 人时。

> v2.2 起 **N09（用药六表迁移）移交给 dev-B**，本卡改为承接 **N26（ToolExecutor 接 MCP）**——A 的职责从「三条迁移」收敛为「历史 + 用药子图 + 工具执行 + 门禁」。

> 计划 v2.0 起，原 N21（50 例评测 + 基线报告）已取消，本卡不再承担评测交付。

**做**

- 三张事件表的迁移与 ORM（`conversation_events` / `conversation_turns` / `tool_executions`）。
- 用药子图与 checkpointer（六表迁移已移交 dev-B，本卡只消费其 ORM）。
- `ToolExecutor` 接入 MCP 工具（N26）：mcp 分支、缓存与降级话术。
- `HistoryStore`：事件追加 + `seq` 原子分配 + 轮次投影 + `turn_id` 幂等。
- `ToolExecutor` 与 `ToolRegistry`：白名单、参数 schema、身份强制注入、幂等、审计、四类失败。
- `medication_confirmation_graph` + PG checkpointer（独立 schema `langgraph`）。
- 门禁收口（`pytest` 全绿、cov ≥70%、README 回填、索引登记）。

**不做**（越界即返工）

- 不写 `app/router/**`、`app/system_one/**`、`app/mcp_client/**`、`mcp-servers/**`（C-1/C-3 已顺延）。
- 不做上下文组装（dev-B 的 N06）、不做响应流（dev-C 的 N07）、不做 Agent ReAct（dev-C 的 N13）。
- 不改 `app/api/**` 的业务逻辑；`app/api/staff.py` 的 a0 端点归 dev-B（N19）。

**写作用域（白名单）**

```
alembic/versions/0008_day6_conversation_events.py     新增
alembic/versions/0010_day6_medication.py              ❌ 不归本卡（dev-B）
app/models/conversation.py                            扩展（保留既有类与字段）
app/models/__init__.py                                仅追加 import
app/services/history_service.py                       新增
app/services/tool_executor.py                         新增
app/graphs/medication_confirmation_graph.py           新增
app/graphs/checkpointer.py                            新增
app/agents/tool_registry.py                           新增
tests/test_history_store.py / test_tool_executor.py / test_medication_graph.py   新增
```

**禁触**：`app/services/dialogue_service.py`、`app/core/context.py`、`app/audio/**`、`frontend/**`、`alembic/versions/0001~0007`（历史迁移一经合入不得改）。

## 2. 节点清单

| 节点 | 任务 | 人时 | 输入 | 输出 | 依赖 | 量化验收 |
| --- | --- | --- | --- | --- | --- | --- |
| N00 | 口径变更登记 + 契约冻结 | 1.5 | 讲义 §4/§14；PRD 573；TD 1046 | PRD/TD/ER 三处修订记录；`day06/contracts.md` 的"表与事件枚举"章节 | — | 三份文档修订记录同提交；事件枚举 12 种写死 |
| N01 | 迁移 0008 + ORM | 3.5 | N00 表语义 | 三张表可建可降；`app/models/conversation.py` 三个 Model | N00 | `upgrade→downgrade→upgrade` 三次成功；`UNIQUE(thread_id, seq)` 在 |
| N04 | HistoryStore | 4.5 | N01 | 六个方法（见 §3.1） | N01 | 两轮按 `seq` 读回；partial 不入库；同 `turn_id` 幂等；跨 thread/elder 读不到 |
| N09 | ~~迁移 0010 + ORM~~ → **移交 dev-B**（v2.2） | 0 | — | 本卡只消费 `medication_*` ORM | — | 不适用 |
| N10 | 用药子图 + checkpointer | 5.0 | N07 触达出口、N09 | 子图双入口 + `graph.get_state` 可读 | N09、N07 | 重启恢复用例通过；同键重试不重复建单；重试 ≤3 后进终态 |
| N12 | ToolRegistry + ToolExecutor | 3.5 | N01 `tool_executions`、N03 契约 | 白名单 + 执行器 | N01、N03 | 模型传他人 `elder_id` → 拒绝 + 审计行；四类失败各一条用例 |
| N26 | ToolExecutor 接 MCP + 缓存与降级 | 1.5 | N12、N25（dev-C） | `tool_executor.py` 的 mcp 分支 + weather 降级话术 | N12、N25 | 「今天天气」走通；无 key 时返回「查不到」不编造；MCP 失败映射到四类失败 |
| N23 | 门禁收口 | 1.5 | 全部 | `pytest` 绿、cov ≥70%、README 回填 | 全部 | V11/V14 通过 |

## 3. 逐节点执行步骤

### 3.1 N01 + N04 · 历史事件账本

1. 读 `app/models/conversation.py` 既有 `ConversationThread`，确定关系与外键策略（`elder_id` 可空，随 thread 的 CHECK 口径）。
2. 迁移 0008 建三表。**逐字段定义以主计划 §9.1 ①②③ 为准（D-1 已于 2026-11-09 裁决，第 28–30 张表）**，本卡不重复列全，只强调四个易错点：

   | 易错点 | 正确做法 |
   | --- | --- |
   | `conversation_events.ref_event_id` | 更正/替代类事件**指向旧事件**而不是覆盖旧行；无此列就无法复盘"改口" |
   | `conversation_events.seq` | `bigint` + `UNIQUE(thread_id, seq)`，靠行锁分配（见第 4 条），**不是** `id()` 或时间戳 |
   | `conversation_turns.elder_id` | 可空且 `FK→elders`，generic 模式写 NULL；NOT NULL 会让通用模式建不出轮次 |
   | `tool_executions` | 必须有 `error_class`（四类失败）与 `finished_at`，否则失败分析只能看 `status` 粗粒度 |

3. `HistoryStore` 六个方法（签名即契约，dev-B/dev-C 直接调）：

```python
async def begin_turn(*, thread_id, elder_id, device_id, call_id, turn_id,
                     user_text, trace_id) -> TurnRef          # 不写 assistant 字段
async def append_event(*, thread_id, event_type, payload, turn_id=None,
                       response_id=None, task_id=None, event_id=None) -> int  # 返回 seq
async def commit_response(*, turn_id, response_id, generated_text,
                          delivered_text, status) -> None     # 幂等：同 turn 同 response 只生效一次
async def finish_turn(*, turn_id, status) -> None
async def read_turns(*, thread_id, elder_id, limit=8) -> list[TurnView]
async def read_events(*, thread_id, after_seq=0) -> list[EventView]
```

4. **`seq` 原子分配**：同事务内 `SELECT ... FOR UPDATE` 锁 `conversation_threads` 行 + 递增 `last_event_seq` + 插入事件（讲义 §4.6）。**禁止**"先查 max 再 +1"。`conversation_threads` 需 `ADD COLUMN last_event_seq int NOT NULL DEFAULT 0`——该列在 0008 里加，属允许的 schema 扩展（非改历史迁移）。
5. 幂等：`turn_id` 已存在时 `begin_turn` 返回原 `TurnRef` 并打 `history_turn_duplicate` 日志，不新增行。
6. generic 模式（`thread_id` 形如 `generic:` 或 `elder_id is None`）：**只写事件不写 `conversation_turns`**，且 `read_turns` 对 generic 返回空。

### 3.2 N09 + N10 · 用药子图

1. 迁移 0010 六表字段**逐列照 TD line 422-534**，不自创字段。三处易错点：
   - `medication_events` 唯一约束 `UNIQUE(plan_id, scheduled_at)`（幂等键），`payload_hash` 用于"同键不同内容 → 409"。
   - `medication_attempts` 唯一约束 `UNIQUE(event_id, attempt_no)`，`event_id` 是外键列名（表名 `medication_events`，主键 `reminder_id`——**PRD 与 TD 命名不一致，以 TD 为准**）。
   - `work_orders.severity` 是建单快照，不随后同步（TD line 515）。
2. `checkpointer.py`：`langgraph` 独立 schema + 独立连接池，与业务库同实例不同 schema。启动时确保 `CREATE SCHEMA IF NOT EXISTS langgraph` 由迁移完成，不在运行期建。
3. 子图节点（讲义 §10 + PRD FR-MED-05）：`remind → wait_confirm`（`interrupt` 等 `taken_answer`）→ 命中则 `record_taken`；超时进 `retry`（≤3）→ 终态分流（`taken_reported` / `refused` / `missed_unconfirmed` / `notification_failed`）→ 升级工单。
4. 双入口：`triggered_by=scheduler|dialog`，同一张图，只记来源（AC-35）。
5. 事实修订：`confirmation v1=taken` 后 `v2=not_taken` 时**新增版本不改旧行**，`late_taken` 与工单不自动关闭（PRD AC-08 / 讲义 §10.3）。
6. 重启恢复：进程重启后 `graph.get_state(config)` 能读到等待节点；**不重发已完成触达**。

### 3.3 N12 · 工具执行器

```python
@dataclass(frozen=True)
class ToolResult:
    status: Literal["succeeded", "failed", "denied"]
    data: dict | None
    error_class: Literal["bad_args", "not_found", "transient", "forbidden"] | None
    message: str | None          # 可直接播报的降级话术
    idempotent_replay: bool = False

async def execute(*, tool_name, args, runtime, turn_id, idempotency_key) -> ToolResult
```

1. `runtime.elder_id` 是**唯一**身份来源；`args` 若含 `elder_id`/`thread_id`/`device_id` 一律丢弃，并写 `audit_logs.action='tool_identity_override_attempt'`。
2. 白名单：未注册工具直接 `denied` + 审计；注册表条目含 `name` / `description` / `parameters_schema` / `side_effect: bool` / `idempotent: bool`。
3. 幂等：`side_effect=True` 必须以 `idempotency_key` 写入 `tool_executions`；同键已成功 → 返回原结果且 `idempotent_replay=True`；同键 running → 返回"处理中"话术。
4. 四类失败分支与话术（TD §6.4）：`bad_args` 回传模型重试（计入预算）；`not_found` 如实告知；`transient` 退避 ≤2 次后降级话术；`forbidden` 拒绝 + 审计 + `trace_id`。
5. 预算与熔断：复用 `app/agents/guardrails.py` 的既有常量与实现，**不自建第二套**（`AGENTS.md` §4-8）。

### 3.4 N00 契约片段（本卡负责部分）

事件枚举 12 种（写进 `contracts.md`，全队对齐）：

```
turn_started / asr_final_committed / response_started /
response_segment_delivered / response_interrupted / response_completed /
tool_started / tool_succeeded / tool_failed /
task_waiting / task_resumed / turn_completed
```

> 更正类事件统一 `*_corrected` / `*_superseded` 后缀并带 `ref_event_id`，**不覆盖旧事件**（讲义 §4.4）。

**N00 需一并冻结的禁区声明**（主理人 2026-11-09 裁决，写进 `contracts.md` 首页）：

1. 本日**不建** `decision_logs`、`app/router/**`、`app/system_one/**`、`app/mcp_client/**`、`mcp-servers/**`；
2. 分类能力**不得**接入任何第三方商业 Jev API 或其他云端分类服务，**不新增相关依赖**；Laya 中文开源权重到位前，降级唯一路径是"全量走 L2 + `route_layer=fallback` 标记"；
3. 事件与工具表中**不留** `route_layer` / `intent` / `confidence` 字段——这些属路由日，提前建列会造成两套口径。

## 4. 交付物规格

| 交付物 | 格式 | 质量红线 |
| --- | --- | --- |
| 0008 迁移 | Python | 可 downgrade；不写死连接；不引 `op.execute` 拼接用户输入 |
| 两个 Model 文件（`conversation.py` / `medication.py`） | SQLAlchemy 2.x async | 全量类型注解；关系与索引显式声明；枚举走 CHECK 不用 PG ENUM |
| `history_service.py` | Python | 全 async；**无 UPDATE 旧事件路径**；`seq` 分配用行锁 |
| `tool_executor.py` | Python | 身份只从 `runtime`；四类失败齐全；审计必写 |
| 用药子图 | Python | 双入口；重试 ≤3；终态四值互斥；checkpointer 独立 schema |

## 5. 自检

```bash
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
uv run alembic heads                     # 必须只有一个 head
uv run pytest tests/test_history_store.py tests/test_tool_executor.py tests/test_medication_graph.py -q
uv run pytest -q && uv run pytest --cov=app --cov-report=term-missing
uv run python scripts/update_progress.py --check
```

判据见主计划 §6 的 V1/V3/V8/V9/V11/V14。**没有运行输出的条目只能写"已完成代码审查"。**

## 6. 对接与联调

| 时点 | 对象 | 我给出 | 我要到 | 判据 |
| --- | --- | --- | --- | --- |
| J1 W1 末 | dev-B | 0008 的 `down_revision` 值 | 0009 的 `down_revision` 指向 0008 | `alembic heads` 单一 |
| J2 W2 末 | dev-B、dev-C | `read_turns` / `read_events` 的签名与 `TurnView` / `EventView` 结构 | — | 签名进 `contracts.md` |
| J4 W4 末 | dev-C | `execute()` 与 `ToolResult` | Agent 侧调用点 | 四类失败各跑一条 |
| J5 W5 末 | dev-C、dev-D | 真实一轮的事件流水 | `trace_id` 一致 | 事件按 `seq` 完整 |

**联调纪律**：`contracts.md` 是唯一口径；要加字段先改它；跨端问题带 `trace_id` 与请求/响应原文。

## 7. 风险与回退

| 风险 | 处置 |
| --- | --- |
| `conversation_threads` 加 `last_event_seq` 与 A1 的身份服务冲突 | 只加列不改语义；A1 未使用该列即无影响；迁移里给 `DEFAULT 0 NOT NULL` |
| 三个迁移串行导致等待 | 严格按 L1→L2→L3；自己那份写完立即 push，不攒批量 |
| 用药子图当日做不完 | 降级为"工具 + 业务表"并**报主理人签字**，登记技术债，不静默降级 |
| 21.0h 仍偏重（迁移 + 历史 + 子图 + 工具 + 门禁） | G2 落后 >2h → 把 N26 交 dev-C（C 已有 mcp_client，接自己写的客户端更快） |
| ER 与迁移口径漂移（新增三表后风险上升） | 先改 ER §4.4/§5/§6 再写迁移；PR 描述附"字段 ↔ ER 行号"对照表 |

## 8. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 首版：按主计划 v1.0 拆 8 个节点，含事件三表与用药六表的字段红线、`seq` 行锁分配、`ToolResult` 契约、评测口径与自检命令 |
| v1.1 | 2026-11-09 | 随主计划 v2.0 收紧：**删除 N21**（50 例评测 + 基线报告）及其写作用域与交付物；节点 8→7，人时 26.5→22.5；调剂条款改为「N09 交 dev-B」 |
| v1.2 | 2026-11-09 | 随主计划 v2.1：**D-1 已裁决**（三表为第 28–30 张，总表数 27→30），§3.1 的建表细节改为指向主计划 §9.1，本卡只保留四个易错点 |
| v1.3 | 2026-11-09 | 随主计划 v2.2：**N09 迁移移交 dev-B**，本卡新增 **N26**（ToolExecutor 接 MCP）；节点 8→7、人时 22.5→21.0；写作用域去掉 0010 与 `models/medication.py`；风险调剂改为「N26 交 dev-C」 |
| v1.1 | 2026-11-09 | 随主计划 v2.0 收紧：**删除 N21**（50 例评测 + 基线报告）及其写作用域与交付物；节点 8→7，人时 26.5→22.5；调剂条款改为「N09 交 dev-B」 |
