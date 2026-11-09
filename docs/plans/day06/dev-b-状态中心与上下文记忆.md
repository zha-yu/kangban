> 状态：草稿　｜　版本：v1.1　｜　日期：2026-11-09
> 关联：docs/team_tasks/day06/dev-b-状态中心与上下文记忆.md（任务卡，唯一验收源）｜docs/plans/day06/day-06-长者侧Agent闭环.md（主计划 v3.0，唯一任务范围源）｜docs/plans/day06/d-DAG.md（DAG 执行路径）｜docs/team_tasks/day06/contracts.md（冻结契约 v1.0，唯一接口契约源）
> 上游依据：PRD FR-DLG-03（上下文）、FR-MEM-01~08（记忆）、AC-18（越权）｜技术设计文档 line 549（memory_chunks）、line 422-534（用药/风险/工单六表）、§6.3（上下文组装）、§6.4（状态版本 CAS）
> 变更记录见文末

# Day 6 dev-B · 状态中心与上下文记忆 执行计划

> 本计划是 dev-b 个人单元的**落地执行件**，逐步骤拆解主计划 v3.0 分配给 dev-B 的 7 个节点（N02/N05/N06/N08/N09/N19/N22，合计 20.0 人时）。内容来源依次为：任务卡验收口径 → DAG 依赖拓扑 → 冻结契约接口 → 现状代码核查。**只回答「怎么按步骤做到」，不新增任何任务范围**。

---

## 1. 现状基线核查（开工前必须确认）

### 1.1 已完成（直接受益）

| 项 | 状态 | 证据 | 对 dev-b 的意义 |
| --- | --- | --- | --- |
| N00 契约冻结 | ✅ 已冻结 v1.0 | `contracts.md` 标题 + 冻结时间 | StateSnapshot / HistoryStore / ToolResult / MCPResult / E20 五个契约直接引用，无需协商 |
| 迁移 0008 apply | ✅ 已执行 | `alembic upgrade head` 实测；down_revision 0007 | dev-A ORM 尚未写但迁移已到位，dev-b 迁移链的前置存在 |
| 迁移 0009 apply | ✅ 已执行 | `alembic upgrade head` 实测；down_revision 0008 | `memory_chunks` 表 23 列 + CHECK + 索引全部在位，dev-b 只写 ORM |
| 迁移 0010 apply | ✅ 已执行 | `alembic upgrade head` 实测；down_revision 0009 | 6 张业务表全部在位，dev-b 只写 ORM |

**核查发现**（计划 v1.0 写的是主计划 §11 声称的"已 apply"，v1.1 开工前实测补证）：

- 写计划时从主计划 §11 抄"三条迁移已 apply"，但**本台机器实测 `alembic current` → 0006_day5_metrics**（14 张表，Day 6 相关表一张都没有）；
- 根因：数据库有一条 **6.5 小时的 idle transaction（pid 1168）** 持有共享锁，阻塞了 0007 的 `ALTER TABLE beds DROP CONSTRAINT`、0008 的 `CREATE TABLE conversation_events`、乃至后续所有 psql 查询全部挂起；
- 修复：`SELECT pg_terminate_backend(1168)` kill 后，`uv run alembic upgrade head` 一次性跑通 0007→0008→0009→0010 全部四条迁移；
- 现 `alembic current` 为 **0010_day6_medication (head)**，共 **25 张表**，迁移链闭合，ORM 可安全编码。

### 1.2 待 dev-b 新建（白名单）

```
app/models/memory.py                   新增（memory_chunks ORM）
app/models/medication.py               新增（6 张业务表 ORM）
app/models/__init__.py                 仅追加 import（dev-A conversation ORM 合入后）
app/services/state_service.py          新增（StateCenter + SessionCoordinator）
app/agents/session_coordinator.py      新增（每 thread_id 串行提交锁）
app/core/context.py                    仅新增 build_turn_context + 常量，不改 build_dialogue_messages 签名
app/services/memory_service.py         新增（骨架：提取 + 召回）
app/services/staff_service.py          新增 reassign_elders 函数
app/api/staff.py                       新增 PUT /api/staff/{staff_id}/elders
tests/test_state_service.py            新增
tests/test_context_builder.py          新增
tests/test_memory_service.py           新增
```

### 1.3 已有代码盘点（dev-b 要在它上面建）

| 文件 | 现状 | dev-b 的动作 |
| --- | --- | --- |
| `app/core/context.py` | `build_dialogue_messages()`（固定前缀 + 身份 + 最近 5 轮 + 本轮 user）；**无**九层装配、**无**预算裁剪、**无**不可信数据边界标签 | **只新增** `build_turn_context()` 与相关常量（`CONTEXT_BUDGET_TOKENS`、`CONTEXT_PRIORITY`、`CONTEXT_SECTIONS`）；**不改** `build_dialogue_messages` 签名（L8 写锁） |
| `app/services/staff_service.py` | 已建号、员工/家属 CRUD、绑定家属、角色互转联动；**无** `reassign_elders()` | 新增纯函数 `reassign_elders()`：事务内 diff + 409 判定 + audit_logs 写入；**不改**既有函数签名 |
| `app/services/__init__.py` | 只有 docstring | 不写 exports（AGENTS §2：services 层不做 re-export） |
| `app/models/__init__.py` | 导出 14 个既有模型 | dev-A conversation ORM 合入后再追加 memory + medication 模型 import |
| `app/services/dialogue_service.py` | RedisTurnStore 存 {user, assistant} 二元组，5 轮 TTL；是 DialogueService 的核心 LLM 入口 | **禁触**（L7 写锁属于 dev-C N14），只参考它的调用模式 |

---

## 2. 任务拆解与波次编排（按 DAG 依赖拓扑）

> DAG 波次（主计划 §3.4 / d-DAG §2）：W1 → W2 → W3 → W4 → W6。**同波次内全并行，跨波次必须等边表里的硬依赖**。dev-b 各节点的实际可开工时点、阻塞点、前置条件逐条标注。

### 波次 W1 · 立即开工（无任何硬阻塞，G0 已过）

> 波次 W1 可并行：dev-A N01 + dev-D N03 + dev-B N19 + dev-C N20。dev-b 的 N19 完全独立，今日第一优先级。

#### 子任务 B-1 · N19 a0 改绑接口（2.5h）

**一句话**：admin 可以把一位护工/医生负责的长者列表全量替换式修改，老 token 的 scope 随之实时变化。

**时间**：W1 全段可做（~2.5h）。

**分步执行**：

| 步 | 做什么 | 产出 | 预估 | 验据 |
| --- | --- | --- | --- | --- |
| B-1.1 | 写 `staff_service.reassign_elders(session, staff_id, elder_ids, operator_id)` | staff_service.py 末尾追加函数（只 append，不改既有） | 0.5h | 伪代码审查通过（事务内 `SELECT … FOR UPDATE` 锁 staff 行 + `LEFT JOIN elder_staff_assignments` 求 diff） |
| B-1.2 | 写 diff 逻辑（新增写、缺失删） + 409 判定（移除后某长者 `primary_care` 计数为 0 → 回滚抛 `PrimaryCareGapError`） | 同上函数体中部 | 0.8h | SQL 草图写在 PR 描述里；单测过 409 用例 |
| B-1.3 | 写 audit_logs（操作者、trace_id、added/removed 差集、staff_id 原 role） | 同上函数体末尾 + `audit_log_service` 调用 | 0.4h | 审计行 trace_id 可在 audit_logs 表查询到 |
| B-1.4 | 写 `app/api/staff.py` 路由 `PUT /api/staff/{staff_id}/elders` | staff.py 追加路由（admin only → 403；非 admin 403） | 0.3h | 路由文件末尾追加，不改既有行 |
| B-1.5 | 补 `docs/接口契约_身份与权限.md` E20 节（含 diff 三路径 200/403/409 响应体） | 契约文档追加 E20 小节 | 0.2h | 同 PR 提交 |
| B-1.6 | 写测试 `tests/test_staff_service.py` 或追加既有测试 | 测试文件 | 0.3h | 覆盖 403/409/diff 三路径 + audit 断言 |

**验收标准**（契约 E20 + 任务卡 §3.4）：
- 200 `added`/`removed` 差集正确；
- 403 非 admin；
- 409 移除后某长者失去全部 `primary_care`（回滚）；
- `audit_logs` 有记录（操作者 ID、trace_id、差集 JSON）。

**依赖**：N00 契约冻结（✅ 已完成）。**无** dev-A/dev-D/dev-C 阻塞。

**风险**：`elder_staff_assignments` 表的 `relation` 值 CHECK 是 `primary_care`/`doctor`，与 `staff.role` 的映射在 `_ROLE_TO_RELATION` 字典里（staff_service.py L37-40），**不能新增第三个 role**（如 admin 不参与绑定）。admin 角色的 staff_id 传入时必须 403，不执行任何 diff。

---

### 波次 W2 · 底座铺设（N01 ORM 合入 + N03 事件枚举已冻结 → dev-b 两节点全并行）

> W2 可并行度 6 人。dev-b 在 W2 有三个子任务可并行：B-2（N02 ORM）+ B-3（N09 ORM）+ B-4（N05 StateCenter）。**B-2 和 B-3 完全独立，可以同时写**。

#### 子任务 B-2 · N02 memory_chunks ORM（1.5h）

**一句话**：把 `memory_chunks` 23 列（TD 原 10 + D-2 增 13）全部映射成 SQLAlchemy 2.x async ORM。

**前置**：迁移 0009 已 apply（✅）。**无 dev-A 依赖**（迁移链已闭合）。

**分步执行**：

| 步 | 做什么 | 产出 | 预估 | 验据 |
| --- | --- | --- | --- | --- |
| B-2.1 | 新建 `app/models/memory.py`，逐字段写 ORM | 完整文件 | 1.0h | 0009 迁移 DDL 逐行对照；23 字段 + 4 CHECK + 2 索引一个不漏 |
| B-2.2 | dev-A conversation ORM 合入后，追加 import 到 `app/models/__init__.py` | models/__init__.py | 0.2h | 只追加 `from app.models.memory import MemoryChunk` + `__all__` 追加，不删不排既有 |
| B-2.3 | 跑 `alembic upgrade head && downgrade -1 && upgrade head` 三次验证 | 终端输出 | 0.2h | 三次全绿 |
| B-2.4 | 伪代码单测 ORM 可实例化（`MemoryChunk(elder_id=..., content=..., subject=..., predicate=..., object_value=..., ...)`） | pytest 片段（单独文件或临时） | 0.1h | 实例化不抛；CHECK 约束触发时抛 IntegrityError |

**逐字段清单**（直接照 0009 迁移 + TD + 任务卡 §3.3.1 核对）：

```
chunk_id (UUID PK gen_random_uuid)
elder_id (UUID FK→elders NOT NULL CASCADE)
content (Text NOT NULL)                  ← 自然语言事实，给人读
embedding (Vector1536 可空)              ← pgvector 召回用，可空
category (Text server_default 'private')
importance (Numeric(3,2) server_default 1.0)
status (Text server_default 'active')    ← CHECK: active/superseded/deleted/disputed/pending_review
source_thread_id (Text 可空)
created_at (DateTime(tz) server_default now())
last_accessed (DateTime(tz) 可空)
subject (Text NOT NULL)                  ← D-2 增列
predicate (Text NOT NULL)                ← D-2 增列
object_value (Text NOT NULL)             ← D-2 增列
polarity (Text server_default 'positive') ← D-2 增列 + CHECK
confidence (Numeric(3,2) NOT NULL)       ← D-2 增列
sensitivity (Text server_default 'private') ← D-2 增列 + CHECK
source_type (Text NOT NULL)              ← D-2 增列 + CHECK
source_turn_ids (JSONB NOT NULL)         ← D-2 增列
valid_from (DateTime(tz) NOT NULL)        ← D-2 增列
valid_to (DateTime(tz) 可空)              ← D-2 增列
supersedes_id (UUID 可空, 故意不建 FK)    ← D-2 增列
conflicts_with_id (UUID 可空, 故意不建 FK) ← D-2 增列
updated_at (DateTime(tz) server_default now()) ← D-2 增列

CHECK: status (5值), polarity (3值), sensitivity (3值), source_type (3值), category (=private)
FK: elder_id → elders.elder_id CASCADE
INDEX: ix_memory_chunks_elder_id_status, ix_memory_chunks_elder_id_subject_predicate
```

#### 子任务 B-3 · N09 medication 六表 ORM（3.0h）

**一句话**：把 0010 迁移的 6 张业务表（medication_plans/events/attempts + risk_events + work_orders/transitions）全部映射成 ORM。

**前置**：迁移 0010 已 apply（✅）。**N09 down_revision = N02 head**，迁移链已闭合。**无 dev-A 阻塞**。

**分步执行**：

| 步 | 做什么 | 产出 | 预估 | 验据 |
| --- | --- | --- | --- | --- |
| B-3.1 | 新建 `app/models/medication.py`，逐表写 ORM（6 张表） | 完整文件 | 2.0h | 0010 DDL 逐行对照；所有 CHECK/UNIQUE 齐全 |
| B-3.2 | dev-A conversation ORM 合入后，追加 import 到 `app/models/__init__.py` | models/__init__.py | 0.2h | 只追加 6 个 import，不排既有 |
| B-3.3 | 伪代码单测 ORM 可实例化 + 幂等键 CHECK 触发 | pytest 片段 | 0.5h | `UNIQUE(plan_id, scheduled_at)` 和 `UNIQUE(event_id, attempt_no)` 冲突时抛 IntegrityError |
| B-3.4 | 跑 `alembic upgrade head && downgrade -1 && upgrade head` 三次验证 | 终端输出 | 0.3h | 三次全绿 |

**逐表要点**（0010 迁移 DDL + 任务卡验收标准）：

| 表 | 核心幂等键 | CHECK | 说明 |
| --- | --- | --- | --- |
| `medication_plans` | PK plan_id | status IN (active/stopped) | `created_by` FK→staff_accounts RESTRICT；version server_default 1 |
| `medication_events` | **UNIQUE(plan_id, scheduled_at)**（0010 行 270-274） | triggered_by(2), status(4), terminal_state(4), failure_reason(4), confirm_channel(2) | 幂等判定的唯一键；`payload_hash` 是同键异内容 409 的依据 |
| `medication_attempts` | **UNIQUE(event_id, attempt_no)**（0010 行 351-355） | receipt(3), tts_result(2), response_channel(3) | 触达幂等键 |
| `risk_events` | PK event_id | type(5), severity(2), status(3) | 红黄分级；2 分钟无人确认自动 escalated |
| `work_orders` | **UNIQUE(order_no)** | type(8), severity(2), status(6) | `severity` 是**建单快照**不随后同步 |
| `work_order_transitions` | PK transition_id | from_status(6), to_status(6) | 工单完整流转历史；actor_id 可空（系统自动流转） |

#### 子任务 B-4 · N05 StateCenter + SessionCoordinator（4.5h）

**一句话**：让"现在这个 thread 做到哪了"有唯一可读的快照；版本冲突不覆盖；每 thread_id 串行提交，杜绝并发写竞争。

**前置**：N03 事件枚举（✅ contracts.md §3 已冻结 12 种事件）。**不需要等 dev-D 写代码**——events → StateSnapshot 的映射从契约里就能读。

**时间**：W2 全段可做（~4.5h）。

**分步执行**：

| 步 | 做什么 | 产出 | 预估 | 验据 |
| --- | --- | --- | --- | --- |
| B-4.1 | 定义 `StateEvent`（12 种事件 → StateSnapshot 字段映射表，**显式写在代码里**，禁止整体重写 JSON） | `state_service.py` 开头的映射表 dataclass 或 dict | 0.8h | 任务卡 §3.1 第 3 条的 12 事件 × 字段变更矩阵全覆盖 |
| B-4.2 | 实现 `StateSnapshot` dataclass（frozen，四组状态 + version + thread_id） | 同上文件 dataclass | 0.3h | 字段与 contracts.md §5.2 完全一致 |
| B-4.3 | 实现 Redis 存储：key `session:{thread_id}:state`，JSON 序列化，TTL 2h；**CAS 用 Lua 脚本或 WATCH/MULTI**，expected_version 不符抛 `StateVersionConflict` | `async def snapshot / apply / clear` | 2.0h | pytest 用例：并发 20 写无 version 重号（`apply` 必须串行被 SessionCoordinator 包） |
| B-4.4 | 实现 `SessionCoordinator`：每 `thread_id` 一个 `asyncio.Lock` + 提交队列；对外只暴露 `async def submit(thread_id, events)` | `app/agents/session_coordinator.py` | 0.8h | pytest 用例：同一个 thread 并发 100 次 submit 最终 version 单调递增无重号 |
| B-4.5 | 降级路径：Redis 不可用时普通闲聊退化为单进程短会话（打 `state_degraded` 日志）；**用药任务不得静默退化**，抛 `StateUnavailable` 交由上层 | state_service.py 内的 try/except 块 | 0.4h | mock 掉 redis 后跑 pytest，验证 degraded 分支生效 |
| B-4.6 | 写测试 `tests/test_state_service.py` | pytest 文件 | 0.2h | 覆盖：版本冲突、并发 20 写无重号、四组状态可恢复、Redis 降级路径 |

**状态数据结构**（直接抄 contracts.md §5.2 + 任务卡 §3.1）：

```python
@dataclass(frozen=True)
class StateSnapshot:
    thread_id: str
    version: int
    identity: dict   # elder_id / verified / privacy_scope
    input: dict      # status / active_turn_id / asr_partial
    output: dict     # status / response_id / generation_epoch / last_acked_segment
    media: dict      # status / media_id / position_ms
    task: dict       # active_task_id / kind / waiting_for
```

**12 事件 → 字段映射**（禁止整体覆盖 JSON）：

| event_type | 改动字段 |
| --- | --- |
| turn_started | identity（从 runtime_context）→ input.status=active, input.active_turn_id |
| asr_final_committed | input.asr_partial → None |
| response_started | output.status=playing, output.response_id, output.generation_epoch++ |
| response_segment_delivered | output.last_acked_segment = segment_id |
| response_interrupted | output.status=interrupted |
| response_completed | output.status=completed |
| tool_started / tool_succeeded / tool_failed | 不改状态（只进历史表） |
| task_waiting | task.active_task_id, task.kind, task.waiting_for |
| task_resumed | task.waiting_for → None |
| turn_completed | input.status=completed |

---

### 波次 W3 · 三件套（N05 完成 + N02 ORM 完成 → dev-b 可开两节点）

> W3 dev-b 可并行：B-5（N06 ContextBuilder，依赖 N05 快照 + N04 HistoryStore 读接口）+ B-3 收尾（N09 ORM 伪代码单测 + 三次 upgrade）。

#### 子任务 B-5 · N06 ContextBuilder 九层装配（3.5h）

**一句话**：把「这一轮模型该看见什么」按固定九层顺序组装出来，超预算从尾部裁，四类必保内容永不裁，不可信数据加边界标签。

**前置**：N05 `snapshot` 函数（✅ W2 内完成）。N04 `read_turns`（dev-A，contracts.md §5.1 已冻结 → 可按契约桩写，不需等 dev-A 实际实现）。**L8 写锁**：**只新增** `build_turn_context()`，**不改** `build_dialogue_messages` 签名。

**时间**：W3 全段可做（~3.5h）。

**分步执行**：

| 步 | 做什么 | 产出 | 预估 | 验据 |
| --- | --- | --- | --- | --- |
| B-5.1 | 定义九层顺序常量（固化为列表，顺序即代码） | `context.py` 末尾追加 `CONTEXT_SECTIONS` 列表 | 0.3h | 任务卡 §3.2 第 1 条的九层顺序一字不差 |
| B-5.2 | 定义预算常量（`CONTEXT_BUDGET_TOKENS` 走 Settings，不硬编码）+ `CONTEXT_PRIORITY` 字典（给每层打优先级） | `context.py` 追加常量块 | 0.3h | Settings 里能查到 `CONTEXT_BUDGET_TOKENS` 默认值 |
| B-5.3 | 实现 `build_turn_context(...)` 函数（签名要包含：StateSnapshot → 九层各 section → 预算裁剪 → 最终 messages） | `context.py` 新增函数 | 2.0h | 九层装配逻辑；预算裁剪从尾部开始；1/2/4/9 四层永不裁 |
| B-5.4 | 实现每层 section 的独立组装函数（安全、身份、运行时、活跃任务、权威事实、相关记忆、旧摘要、最近 delivered 轮、当前 user 输入） | 同上文件内的 9 个 helper | 0.5h | 每层独立可测；不可信数据（记忆、工具结果）加 `<retrieved_knowledge source=...>` 边界标签 |
| B-5.5 | 写边界标签逻辑与身份隔离：身份**绝不从文本里读**；`waiting_for` 绑定（task.waiting_for == "taken_answer" 时把当前输入优先解释为对该任务的回答） | 同上文件内的安全分支 | 0.3h | 任务卡 §3.2 第 4/6 条实现 |
| B-5.6 | 写测试 `tests/test_context_builder.py` | pytest 文件 | 0.1h | 覆盖：预算裁剪、四类必保不裁、waiting_for 优先解释、generic 模式隔离 |

**九层顺序**（固化为列表，**顺序即代码**，禁止调用方自定义）：

```
1 safety_and_policy          ← 永不裁
2 identity_and_scope         ← 永不裁
3 runtime_info（时间/设备/房间）
4 active_task（waiting_for + 结构化槽位） ← 永不裁
5 authoritative_facts（业务表查得）
6 relevant_memories（权限过滤后）
7 older_summary
8 recent_delivered_turns     ← 只取 delivered_text，被打断的轮附 interrupted=true
9 current_user_turn          ← 永不裁
```

**预算裁剪规则**：
- 从列表**尾部**开始压缩（先裁 8 → 7 → 6 → 5 → 3）；
- `safety_and_policy` / `identity_and_scope` / `active_task` / `current_user_turn` 四层**永不裁**；
- 裁剪前对每层按 token 数降序取前 N 条。

---

### 波次 W4 · 记忆与工作流（N02 + N06 完成 → dev-b 可开 N08）

> W4 dev-b 唯一节点：B-6（N08 MemoryService 骨架）。

#### 子任务 B-6 · N08 MemoryService 骨架（2.5h）

**一句话**：让对话结束后能异步提取一类低风险偏好记忆入库，让下次会话能按 elder_id + 有效期召回这些记忆进上下文。本版只做**提取 + 召回**，冲突检测与遗忘明确留 Day 7。

**前置**：N02 memory_chunks ORM（✅ W2 完成）+ N06 ContextBuilder（✅ W3 完成）。

**时间**：W4 全段可做（~2.5h）。

**分步执行**：

| 步 | 做什么 | 产出 | 预估 | 验据 |
| --- | --- | --- | --- | --- |
| B-6.1 | 实现提取函数 `async def extract_candidates(thread_id, elder_id, turns)` | `memory_service.py` | 0.8h | 只处理 `category=preference`；输出候选带 `source_turn_ids`；三条硬校验（无 source_turn_ids 不入库、generic 不写、sensitivity=health/family 不写） |
| B-6.2 | 实现入库函数 `async def insert_candidate(session, candidate)`（同 `(elder_id, subject, predicate)` 已有 active 时：value 相同则跳过，否则新插一条并打 `memory_candidate_pending` 日志） | 同上文件 | 0.7h | 任务卡 §3.3 第 3 条 |
| B-6.3 | 实现召回函数 `async def recall(elder_id, query_text, limit=5)`（强制 WHERE elder_id=:runtime.elder_id AND status='active' AND (valid_to IS NULL OR valid_to > now()) 之后才做向量相似度） | 同上文件 | 0.5h | 跨 elder 查不到（服务层硬校验 elder_id 必须匹配） |
| B-6.4 | 写提取后调用入口：对话结束后**异步**跑（不阻塞 `handle_user_turn` 返回），用 `asyncio.create_task()` 或类似 | dialogue_service.py 调用点（**只追加，不改既有逻辑**）或在 N14 turn_service.py 留桩 | 0.3h | 提取在后台跑，主链延迟不增 |
| B-6.5 | 写测试 `tests/test_memory_service.py` | pytest 文件 | 0.2h | 覆盖：候选带 source_turn_ids、召回带 elder_id + 有效期、跨 elder 查不到；**冲突/遗忘不验**（顺延 Day 7） |

**明确不做**（登记技术债 M-1 ~ M-4，随 Day 7 补）：
- 冲突五关系判定（unrelated/duplicate/supports/supersedes/contradicts）
- `superseded` / `disputed` 状态迁移与 `valid_to` 回填
- 90 天衰减归并（FR-MEM-07）
- 删除流水（原文 + embedding + 派生摘要）与 `memory_delete` 审计

---

### 波次 W6 · 收口（N06 + N08 完成 → dev-b 可开 N22）

> W6 dev-b 唯一节点：B-7（N22 讲义作业三件套）。

#### 子任务 B-7 · N22 讲义作业三件套（2.5h）

**一句话**：把今天做出来的状态中心、上下文组装、记忆骨架分别以文档形式呈现——状态图（mermaid）+ 上下文样例 + 两条 skip 骨架测试。

**前置**：N06 ContextBuilder（✅ W3）+ N08 MemoryService 骨架（✅ W4）。

**时间**：W6 全段可做（~2.5h）。

**分步执行**：

| 步 | 做什么 | 产出 | 预估 | 验据 |
| --- | --- | --- | --- | --- |
| B-7.1 | 健康采集状态图（mermaid） | Markdown 文件 | 0.8h | 任务卡 §3.5 第 1 条：每个节点标注「写历史 / 写状态 / 写业务表」+ 重启恢复点 |
| B-7.2 | 上下文样例（给 12 轮历史 + 3 条记忆 + 2 条知识 + 1 个待答任务） | 同上文件 | 1.0h | 逐条说明送进模型的内容、被裁掉的内容、为什么被裁 |
| B-7.3 | 两条一致性测试（"今天天气"/"停止播放"） | `tests/test_skip_consistency.py` | 0.5h | `@pytest.mark.skip(reason="待路由日执行")`；主控 Agent 与快捷路径的对照 |
| B-7.4 | 三件套合入 `docs/plans/day06/` 目录，文件命名 `day06/dev-b-作业三件套.md` | 完整文件 | 0.2h | 同 dev-a/dev-c 的作业一起归档 |

---

## 3. 时间分配总表

| 子任务 | 节点 | 波次 | 人时 | 阻塞 | 最早开工 | 最晚完 |
| --- | --- | --- | --- | --- | --- | --- |
| B-1 N19 改绑接口 | N19 | W1 | 2.5h | 无 | Day6 09:00 | Day6 10:00 |
| B-2 N02 memory ORM | N02 | W2 | 1.5h | 迁移 0009 apply ✅ | Day6 10:00 | Day6 11:00 |
| B-3 N09 medication ORM | N09 | W2/W3 | 3.0h | 迁移 0010 apply ✅ | Day6 10:00 | Day6 12:00 |
| B-4 N05 StateCenter | N05 | W2 | 4.5h | N03 事件枚举 ✅ 契约冻结 | Day6 10:00 | Day6 12:30 |
| B-5 N06 ContextBuilder | N06 | W3 | 3.5h | N05 + N04 契约冻结 | Day6 12:30 | Day6 14:30 |
| B-6 N08 MemoryService | N08 | W4 | 2.5h | N02 + N06 | Day6 14:30 | Day6 15:30 |
| B-7 N22 讲义作业 | N22 | W6 | 2.5h | N06 + N08 | Day6 15:30 | Day6 16:30 |
| **合计** | | | **20.0h** | | | |

> 按 8h/人 排班：W1（2.5h）+ W2（1.5+3+4.5=9h 并行 → 实际 4.5h）+ W3（3.5h）+ W4（2.5h）+ W6（2.5h）= **15.5h 实际工时**（含波次等待的空闲不计入净工作时间），在 10h/日容量内。

---

## 4. 依赖矩阵

| dev-b 节点 | dev-A 依赖 | dev-C 依赖 | dev-D 依赖 |
| --- | --- | --- | --- |
| N02 memory ORM | 无（迁移已 apply） | 无 | 无 |
| N05 StateCenter | N04 HistoryStore 契约（✅ 冻结，不需等实现） | 无 | N03 事件枚举（✅ 契约冻结） |
| N06 ContextBuilder | N04 `read_turns` 契约（✅ 冻结） | 无 | 无 |
| N08 MemoryService | 无 | 无 | 无 |
| N09 medication ORM | 无（迁移已 apply） | 无 | 无 |
| N19 改绑接口 | 无 | 无 | 无 |
| N22 讲义作业 | 无 | 无 | 无 |

**dev-b 对其他 dev 的输出**：

| dev-b 交付 | 接收方 | 内容 |
| --- | --- | --- |
| N05 `snapshot/apply` 签名 | dev-C N07/N14 | 双方签名进 contracts.md |
| N06 `build_turn_context` | dev-C N13/N14 | Agent 上下文组装入口 |
| N08 `recall` 函数 | dev-C N13 | 记忆召回（入参 elder_id + 查询文本） |
| N19 E20 端点 | dev-D N18 | admin 改绑 UI 的后端 |
| N02+N09 ORM | dev-A N10 | 用药子图 ORM 依赖 |

---

## 5. 验收标准（逐条自检）

> 每条都是命令级或判据级，可被第二人复跑。**没有运行证据只能写「已完成代码审查」，不能写「已验证」**。

### 5.1 单元级（每子任务完成即跑）

```bash
# B-1 N19 改绑接口
uv run pytest tests/test_staff_service.py -q                      # 覆盖 403/409/diff 三路径
# 或追加到既有文件

# B-2 N02 + B-3 N09 ORM
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head  # 三次连续成功

# B-4 N05 StateCenter
uv run pytest tests/test_state_service.py -q                       # V4：版本冲突 + 并发 20 无重号

# B-5 N06 ContextBuilder
uv run pytest tests/test_context_builder.py -q                     # V5：预算裁剪 + 四类必保 + waiting_for

# B-6 N08 MemoryService
uv run pytest tests/test_memory_service.py -q                      # V7：候选带 source_turn_ids + 召回过滤 + 跨 elder 隔离
```

### 5.2 回归级（每子任务完成后必须跑旧测试）

```bash
# L8 写锁回归：旧 dialogue 测试必须仍绿
uv run pytest tests/test_dialogue_service.py tests/test_dialogue_integration.py -q
```

### 5.3 全量门禁（W5 合流后）

```bash
# dev-b 自检命令（任务卡 §5）
uv run pytest tests/test_state_service.py tests/test_context_builder.py tests/test_memory_service.py tests/test_staff_service.py -q
uv run pytest tests/test_dialogue_service.py tests/test_dialogue_integration.py -q
uv run pytest -q
uv run python scripts/update_progress.py --check
```

### 5.4 契约一致性

| 项 | 判据 |
| --- | --- |
| StateSnapshot | 字段与 contracts.md §5.2 完全一致 |
| HistoryStore 调用 | N06 按 contracts.md §5.1 的 `read_turns` 签名调用（桩或 dev-A 实现在所不论） |
| E20 端点 | 契约文档与代码 TypedDict 字段名零差异 |
| 迁移链 | `alembic heads` 单一 head；0008→0009→0010 链式 down_revision |

---

## 6. 风险预案

| # | 风险 | 触发信号 | 概率 | 影响 | 预案 |
| --- | --- | --- | --- | --- | --- |
| R-1 | `context.py` 改动打破 Day 3-5 测试 | `test_dialogue_service.py` 或 `test_dialogue_integration.py` 红 | 中 | 5h 返工 | **L8 写锁**严格执行：只新增函数，**绝不改** `build_dialogue_messages` 签名和现有行；改完立刻跑旧测试 |
| R-2 | dev-A conversation ORM 未合入，`app/models/__init__.py` 无法追加 import | dev-A N01 没完成就先被 L5 锁卡住 | 低 | 1h 等待 | 等 dev-A 先合入 conversation ORM 再追加，**不越锁**；在此之前 dev-b 的 memory/medication 两个 ORM 文件可独立写好先提交（暂不追加 import） |
| R-3 | Redis 不可用导致 StateCenter 测试全红 | 本地 dev 没起 redis | 低 | 2h 卡壳 | 测试用 mock redis（`fakeredis` 或 pytest fixture 的 `monkeypatch`），**不依赖本地 redis 容器**；生产态 Redis 不可用的降级路径用 mock 验证 |
| R-4 | `context.py` 九层装配顺序错了或漏了 | G2 预算用例失败 | 低 | 3h 返工 | 九层顺序**固化为列表常量**，第一行就写死；用 `enumerate(CONTEXT_SECTIONS)` 确保不丢 |
| R-5 | 记忆骨架被当成完整治理交付 | 汇报时被问"冲突检测在哪" | 中 | 汇报尴尬 | 明确写"骨架"，技术债 M-1~M-4 全量登记在任务卡；Day 7 偿还 |
| R-6 | 改绑接口 409 判定在删除路径上位置错了 | 先删后判定 → 某长者暂失去 primary_care | 中 | 数据不一致 | diff 算完 → 先判定 409 → 通过了再执行删除；事务在 409 判定失败时回滚 |
| R-7 | 迁移与既有 0007/0008 冲突 | `alembic heads` 出现两个 head | 极低 | 阻塞全队 | 按 L1→L2→L3 顺序，后写者 rebase 到前一个 down_revision；**本迁移已 apply**，不会再生成新迁移，ORM 不受影响 |
| R-8 | 数据库 idle transaction 死锁（本次实测发现） | `alembic upgrade head` 长时间无响应；任何 `psql SELECT` 全超时；pg_stat_activity 有 `state=idle in transaction` + 长 duration | 中 | 所有 dev 全阻塞 | **排查**：`SELECT pid, state, now()-query_start AS d, wait_event, left(query,80) FROM pg_stat_activity WHERE datname='silvermate'` 看谁持锁；**修复**：`SELECT pg_terminate_backend(pid)` kill 掉长 idle 连接后立刻重试；**预防**：Ctrl+C 中断 Alembic/psql 前先确认没有长事务在跑 |

---

## 7. 交付物总表

| # | 交付物 | 格式 | 关联节点 | 质量红线 |
| --- | --- | --- | --- | --- |
| D-B1 | `app/models/memory.py`（memory_chunks ORM） | Python | N02 | 23 字段 + 4 CHECK + 2 索引与 0009 迁移逐行对照；Vector1536 列维度 1536 |
| D-B2 | `app/models/medication.py`（6 张业务表 ORM） | Python | N09 | 逐表与 0010 迁移 DDL 对齐；`UNIQUE(plan_id, scheduled_at)` 与 `UNIQUE(event_id, attempt_no)` 齐全 |
| D-B3 | `app/services/state_service.py` + `app/agents/session_coordinator.py` | Python | N05 | CAS 不用整包覆盖；版本冲突不覆盖；SessionCoordinator 每 thread_id 一个 asyncio.Lock；Redis 失败有降级路径 |
| D-B4 | `app/core/context.py`（只新增函数） | Python | N06 | 九层顺序固化；预算常量集中；不可信数据加边界标签；**旧函数签名不变**；身份绝不从文本里读 |
| D-B5 | `app/services/memory_service.py`（骨架） | Python | N08 | 强制 elder_id；召回带 status='active' + 有效期；无 source_turn_ids 不入库；**冲突/遗忘明确留 Day 7** |
| D-B6 | `staff_service.reassign_elders()` + `PUT /api/staff/{staff_id}/elders` | Python | N19 | 403/409/diff 三路径通过；audit_logs 必写；生效口径逐请求查表 |
| D-B7 | `docs/接口契约_身份与权限.md` E20 节 | Markdown | N19 | 与 contracts.md §6 字段零差异 |
| D-B8 | 测试集（3~4 个测试文件） | Python | N05/N06/N08/N19 | pytest -q 全绿；覆盖任务卡量化验收条目 |
| D-B9 | 讲义作业三件套（状态图 + 上下文样例 + skip 骨架测试） | Markdown + mermaid + Python | N22 | 状态图每节点标注"写历史/写状态/写业务表"；上下文样例可对照预算表；测试文件带 skip reason |

---

## 8. 技术债（本版主动留下，随 Day 7 补）

| # | 债 | 影响 | 偿还时点 |
| --- | --- | --- | --- |
| M-1 | 记忆冲突五关系判定（unrelated/duplicate/supports/supersedes/contradicts） | 同一偏好被改口时会出现两条 active 条目，需人工核对 | Day 7 |
| M-2 | `superseded` / `disputed` 状态迁移与 `valid_to` 回填 | 旧事实不会自动失效 | Day 7 |
| M-3 | 90 天衰减归并（FR-MEM-07） | `importance` 只写不衰减 | Day 7 |
| M-4 | 删除流水（原文 + embedding + 派生摘要）与 `memory_delete` 审计 | 长者暂不能自助删除记忆 | Day 7 |
| C-1 | `app/models/__init__.py` 追加 memory + medication import 等 dev-A conversation ORM 合入 | 暂时 models 包不完整，依赖 dev-A 合入 | Day 6 结束前 dev-A 合入后 |

---

## 9. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 首版：dev-b 执行计划，7 个节点 / 7 个波次子任务 / 20.0 人时；含现状基线核查、逐节点分步执行、时间分配总表、依赖矩阵、验收标准、风险预案、交付物清单、技术债登记 |
| v1.1 | 2026-11-09 | 开工前实测基线核查补证：① 本台机器数据库实际停在 0006_day5_metrics（非计划里假设的 0010 head），根因是一条 6.5h idle transaction（pid 1168）占锁，已 kill + 重新 `alembic upgrade head` 跑通 0007→0010 全部四条迁移，现 25 张表闭合；② §1.1 增补"核查发现"小节描述问题与修复过程；③ §6 新增风险 R-8（数据库 idle transaction 死锁，含排查 SQL + 修复命令 + 预防手段） |
