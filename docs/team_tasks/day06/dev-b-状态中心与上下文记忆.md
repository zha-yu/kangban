> 状态：未开始　｜　版本：v1.3　｜　日期：2026-11-09
> 负责人：dev-B（后端 · 状态中心与上下文记忆）
> 关联：docs/plans/day06/day-06-长者侧Agent闭环.md（主计划 §3.1 节点表为唯一事实源）｜docs/team_tasks/day06/dev-a/dev-c/dev-d
> 上游依据：技术设计文档 line 549（`memory_chunks`）、line 848-858（§6.3 上下文组装）、line 375-383（`elder_staff_assignments`）｜PRD line 268（FR-DLG-03）、line 420-429（FR-MEM-01~08）、line 178（管理员权限）、line 499（AC-18 越权）
> 变更记录见文末

# Task dev-B · 状态中心与上下文记忆（Day 6）

## 1. 目标与边界

**一句话**：让"现在做到哪"有唯一口径、让"这一轮模型该看见什么"可解释，并让"以后值得记住什么"有治理。

**负责节点**：N02、N05、N06、N08、N09、N19、N22，合计 20.0 人时。

> v2.2 起 **N09（用药/风险/工单六表迁移）由 dev-A 移交本卡**：B 掌握 0009 之后自然承接 0010，迁移链 `0008(A)→0009(B)→0010(B)` 只跨两人，head 冲突面更小。

> 计划 v2.0 起，N08 由「六步流水线」收为**骨架**（只做提取 + 召回），冲突检测与遗忘顺延 Day 7 并登记技术债。

**做**

- `memory_chunks` 迁移与 ORM（含讲义 §8.2 的结构化字段）。
- 用药/风险/工单六表迁移与 ORM（N09，自 dev-A 接手）。
- `StateCenter`（四组正交状态 + 版本 CAS）与 `SessionCoordinator`（每 `thread_id` 串行提交）。
- `ContextBuilder`（九层装配 + 预算裁剪 + 不可信数据边界）。
- `MemoryService`（**骨架**：候选提取 + 召回过滤，只做一类低风险偏好记忆）。
- a0 后端：`PUT /api/staff/{staff_id}/elders`（N19）。
- 讲义三件作业的文档部分（N22）。

**不做**

- 不做响应流与 epoch（dev-C 的 N07）；不做 Agent ReAct（dev-C 的 N13）。
- 不改 `app/services/dialogue_service.py`（dev-C 的 L7 写锁）。
- 不做知识检索（dev-C 的 N11，L 档可裁）。

**写作用域（白名单）**

```
alembic/versions/0009_day6_memory_chunks.py      新增
alembic/versions/0010_day6_medication.py          新增（v2.2 自 dev-A 接手）
app/models/medication.py                          新增（同上）
app/models/memory.py                             新增
app/models/__init__.py                           仅追加 import（在 dev-A 之后）
app/services/state_service.py                    新增
app/agents/session_coordinator.py                新增
app/core/context.py                              仅新增函数，不改既有签名
app/services/memory_service.py                   新增
app/services/staff_service.py                    新增 reassign_elders（不改既有方法）
app/api/staff.py                                 新增一个路由
docs/接口契约_身份与权限.md                       新增 E20
tests/test_state_service.py / test_context_builder.py / test_memory_service.py   新增
```

**禁触**：`alembic/versions/0008_*`、`0010_*`（A 的锁）、`app/services/history_service.py`、`frontend/**`。

## 2. 节点清单

| 节点 | 任务 | 人时 | 输入 | 输出 | 依赖 | 量化验收 |
| --- | --- | --- | --- | --- | --- | --- |
| N02 | 迁移 0009 + ORM | 1.5 | **主计划 §9.1 ④ 已裁决结构**（D-2） | `memory_chunks` 可建可降；`app/models/memory.py` | N01（head） | TD 原 10 字段 + 11 个增列齐全；`status` 五值 CHECK；`(elder_id, status)` 与 `(elder_id, subject, predicate)` 两个索引在 |
| N05 | StateCenter + Coordinator | 4.5 | N03 事件枚举 | `snapshot` / `apply` / `clear` | N03 | 版本冲突不覆盖；并发 20 写无 `seq` 重号；四组状态可恢复 |
| N06 | ContextBuilder | 3.5 | N04 读接口、N05 快照 | 九层装配函数 | N04、N05 | 超预算裁低优先级；四类必保内容不裁；只取 `delivered_text` |
| N08 | MemoryService 骨架 | 2.5 | N02、N06 | 提取 + 召回两个入口 | N02、N06 | 候选带 `source_turn_ids`；召回带 `elder_id` + 有效期；跨 elder 查不到 |
| N09 | 迁移 0010 用药/风险/工单六表 + ORM | 3.0 | TD line 422-534 | 六张表可建可降；`app/models/medication.py` | N02（head） | 字段与 TD 逐列对照表附 PR 描述；`UNIQUE(plan_id, scheduled_at)` 与 `UNIQUE(event_id, attempt_no)` 齐全 |
| N19 | a0 后端 | 2.5 | 契约 E20；`staff_service.py` | 端点 + 服务层 + 审计 | N00 | 403 / 409 / diff 三路径通过 |
| N22 | 作业三件套 | 2.5 | N06、N08 | 状态图 + 上下文样例 + 两条 `skip` 骨架测试 | N06、N08 | 状态图每节点标注"写历史/写状态/写业务表" |

## 3. 逐节点执行步骤

### 3.1 N05 · 状态中心（先写契约，再写实现）

```python
@dataclass(frozen=True)
class StateSnapshot:
    thread_id: str
    version: int
    identity: dict    # elder_id / verified / privacy_scope
    input: dict       # status / active_turn_id / asr_partial
    output: dict      # status / response_id / generation_epoch / last_acked_segment
    media: dict       # status / media_id / position_ms
    task: dict        # active_task_id / kind / waiting_for

async def snapshot(thread_id: str) -> StateSnapshot
async def apply(*, thread_id, expected_version: int, events: list[StateEvent]) -> StateSnapshot  # 冲突抛 StateVersionConflict
async def clear(thread_id: str) -> None
```

1. 存储：Redis key `session:{thread_id}:state`，JSON 序列化，TTL 2h（**关键任务不依赖 TTL**，用药以 checkpointer + 业务表为准，讲义 §6.3）。
2. 版本：`apply` 用 Lua 脚本或 WATCH/MULTI 做 CAS；`expected_version` 不符即抛冲突，**不做"最后写入者覆盖"**。
3. 事件→状态映射表（12 种事件各改哪些字段）必须显式写出，禁止"整体重写 JSON"（讲义 §6.4 的当场检查）。
4. `SessionCoordinator`：每 `thread_id` 一个 `asyncio.Lock` + 提交队列；对外只暴露 `submit(thread_id, events)`，内部按"读快照→校验→apply→追加事件"顺序执行。
5. 降级：Redis 不可用时普通闲聊退化为单进程短会话并打 `state_degraded` 日志；用药任务**不得**静默退化，抛 `StateUnavailable` 交由上层提示人工。

### 3.2 N06 · 上下文组装

1. 九层顺序（讲义 §7.1）固化为列表，**顺序即代码**，不得调用方自定义：

```
1 safety_and_policy      2 identity_and_scope     3 runtime_info（时间/设备/房间）
4 active_task（waiting_for + 结构化槽位）          5 authoritative_facts（业务表查得）
6 relevant_memories（权限过滤后）                   7 older_summary
8 recent_delivered_turns                          9 current_user_turn
```

2. 预算：`CONTEXT_PRIORITY` 常量与 `CONTEXT_BUDGET_TOKENS`（走 `Settings`，不硬编码）；达到预算从**列表尾部**开始压缩；`1/2/4/9` 四层**永不裁**。
3. 只取 `delivered_text` 作为 assistant 历史；被打断的轮附 `interrupted=true` 标记（讲义 §7.4）。
4. 不可信数据加边界标签（讲义 §7.5）：`<retrieved_knowledge source=... version=...>`，工具结果同样包裹；**身份绝不从文本里读**。
5. **不改** `build_dialogue_messages` 的签名（L8 写锁）——新增 `build_turn_context(...)`，Day 3–5 的既有测试必须继续绿。
6. `waiting_for` 绑定：`task.waiting_for == "taken_answer"` 时把当前输入优先解释为对该任务的回答（讲义 §7.2 的"吃了"案例）。

### 3.3 N08 · 记忆骨架（本版只做两步）

> 讲义 §13.3 增量四的原文是"**先接一类低风险偏好记忆**"。本版照此收口，只做**提取 + 召回**，冲突检测与遗忘明确留 Day 7。

1. 提取：对话结束后**异步**跑（不阻塞 `handle_user_turn` 返回），只处理 `category=preference` 一类，输出结构化候选：

```json
{"category":"preference","subject":"<elder_id>","predicate":"preferred_opera_genre",
 "value":"越剧","polarity":"positive","fact":"喜欢越剧",
 "source_turn_ids":["t21"],"source_type":"elder_explicit",
 "confidence":0.96,"sensitivity":"private","valid_from":"2026-11-09"}
```

2. 入库前三条硬校验：无 `source_turn_ids` 不入库；generic（身份未确认）不写个人记忆；`sensitivity=health/family` 不写。
3. 同 `(elder_id, subject, predicate)` 已有 `active` 条目时，本版**不判冲突关系**：仅当 value 完全相同才跳过，否则新插一条并打 `memory_candidate_pending` 日志，把判定留给 Day 7。**不得**在本版静默覆盖旧条目。
4. 召回：`WHERE elder_id=:runtime.elder_id AND status='active' AND (valid_to IS NULL OR valid_to > now())` 之后才做向量相似度；只注入 `active` 条目。
5. 明确不做：冲突五关系判定、`superseded`/`disputed` 状态迁移、90 天衰减归并、删除流水。以上全部记入下表技术债，随 Day 7 补。

### 3.3.1 N02 · `memory_chunks` 建表要点（D-2 已裁决）

逐字段以主计划 §9.1 ④ 为准，本卡只列**四个不能错的点**：

| 点 | 正确做法 | 错了会怎样 |
| --- | --- | --- |
| `status` 取值 | `active` / `superseded` / `deleted` / **`disputed`** / **`pending_review`**（五值，CHECK） | 只建三值 → 冲突中间态无处落，Day 7 必须 ALTER 改 CHECK（历史迁移不得改，只能再加迁移） |
| `embedding` | `vector(1536)`，可空 | 维度写错 → 后续所有召回要重建向量 |
| 冲突检索键 | 新增 `(elder_id, subject, predicate)` 复合索引 | 只靠 `(elder_id, status)` → 每次冲突检测全表扫 |
| 可空性 | `subject` / `predicate` / `object_value` / `confidence` / `sensitivity` / `source_type` / `source_turn_ids` / `valid_from` / `updated_at` 均 **NOT NULL** | 放宽成可空 → 骨架期写入脏数据，Day 7 清洗成本更高 |

> `content` 保留为自然语言事实（人读），`subject+predicate+object_value` 是机读三元组。两者**同时写**，不是二选一——前者给人看，后者给冲突检测用。

### 3.4 N19 · a0 改绑接口

1. 契约（写进 `docs/接口契约_身份与权限.md` E20）：

```
PUT /api/staff/{staff_id}/elders        admin only
body   { "elder_ids": ["uuid", ...] }   全量覆盖式替换
200    { "staff_id": "...", "added": [...], "removed": [...] }
403    非 admin
409    移除后某长者失去全部 primary_care
```

2. `staff_service.reassign_elders()`：事务内 diff（新增写入 / 缺失删除）；relation 按当前 role 映射（`care_worker → primary_care` / `doctor → doctor`）；写 `audit_logs`（操作者、`trace_id`、前后差集）。
3. 409 判定在**删除路径**上：某长者移除后 `primary_care` 计数为 0 → 回滚并 409（裁决 C4）。
4. 生效口径：**逐请求查表**（`scope_elder_ids` 每次查 `elder_staff_assignments`），不靠 token 快照；该口径必须写进契约，否则改绑后 A 仍能看该长者（主计划 R8）。

### 3.5 N22 · 讲义作业（文档）

1. 健康采集状态图（mermaid）：提问 → 等待回答 → ASR final → 结构化校验 → 追问 → 完成 → 人工复核；**每个节点标注"写历史 / 写状态 / 写业务表"**，并写明重启从哪恢复。
2. 上下文样例：给 12 轮历史 + 3 条记忆 + 2 条知识 + 1 个待答任务，写出真实送给模型的上下文，并逐条说明删了什么、为什么。
3. 两条一致性测试（"今天天气" / "停止播放"）各写主控 Agent 与快捷路径的对照——本日无快捷路径，写成**待路由日执行的 skeleton 测试**并在文件里标注 `skip` 原因。

## 4. 交付物规格

| 交付物 | 格式 | 质量红线 |
| --- | --- | --- |
| 0009 迁移 | Python | 可 downgrade；pgvector 列维度与 TD 一致；枚举走 CHECK；**字段一次建全**（冲突治理逻辑后补，列不后补） |
| 0010 迁移 | Python | 六表字段与 TD line 422-534 逐列对齐；两个幂等 UNIQUE 齐全；`severity` 为建单快照不复同步 |
| `state_service.py` | Python | CAS 不用"整包覆盖"；Redis 异常有降级路径 |
| `context.py` 扩展 | Python | 旧函数签名不变；预算常量集中；边界标签固定 |
| `memory_service.py` | Python | 强制 `elder_id`；召回带 `status='active'` 与有效期；无 `source_turn_ids` 不入库 |
| E20 端点 | Python + Markdown | 403/409 语义与前端一致；审计必写 |
| 三件作业 | Markdown + mermaid | 状态图节点三标注齐全；上下文样例可对照预算表 |

## 5. 自检

```bash
uv run pytest tests/test_state_service.py tests/test_context_builder.py tests/test_memory_service.py tests/test_staff_service.py -q
uv run pytest tests/test_dialogue_service.py tests/test_dialogue_integration.py -q   # 旧测试必须仍绿
uv run pytest -q
uv run python scripts/update_progress.py --check
```

判据见主计划 §6 的 V4/V5/V7/V11/V14。

## 6. 对接与联调

| 时点 | 对象 | 我给出 | 我要到 | 判据 |
| --- | --- | --- | --- | --- |
| J1 W1 末 | dev-A | 0009 的 `down_revision` 指向 0008 | 0008 已合入 | 单 head |
| J2 W2 末 | dev-A、dev-C | `snapshot` / `apply` 签名与 `StateSnapshot` 结构 | `read_turns` / `read_events` 签名 | 双方签名进 `contracts.md` |
| J4 W4 末 | dev-C | 记忆召回函数（入参 `elder_id` + 查询文本） | Agent 调用点 | 跨 elder 返回空 |
| J6 W6 末 | dev-D | E20 端点与 403/409 响应体 | console UI 调用 | admin 改绑端到端通过 |

## 7. 风险与回退

| 风险 | 处置 |
| --- | --- |
| `context.py` 改动打破 Day 3–5 测试 | L8 写锁：只新增函数；改完立刻跑 `test_dialogue_*` 两个文件 |
| 记忆冲突检测写成"相似度 >0.9 即冲突" | 明令禁止：相似度只用来扩候选集，关系判定走 `subject+predicate` 精确查 + 枚举规则（本版不判关系，见下） |
| 记忆骨架被当成完整治理交付 | 拒绝：本版只做提取 + 召回，冲突/遗忘进下表技术债，汇报时如实写"骨架"
| Redis 不可用导致状态丢失 | 普通闲聊降级；用药任务抛 `StateUnavailable`，不静默 |
| 迁移与 dev-A 抢 head | 严格等 0008 合入后再生成 0009，再生成 0010；**本卡独占 0009/0010 两把锁（L2/L3）**，不与 A 并行写 |

## 8. 技术债（本版主动留下，随 Day 7 补）

| # | 债 | 影响 | 偿还时点 |
| --- | --- | --- | --- |
| M-1 | 记忆冲突五关系判定（unrelated/duplicate/supports/supersedes/contradicts） | 同一偏好被改口时会出现两条 active 条目，需人工核对 | Day 7 |
| M-2 | `superseded` / `disputed` 状态迁移与 `valid_to` 回填 | 旧事实不会自动失效 | Day 7 |
| M-3 | 90 天衰减归并（FR-MEM-07） | `importance` 只写不衰减 | Day 7 |
| M-4 | 删除流水（原文 + embedding + 派生摘要）与 `memory_delete` 审计 | 长者暂不能自助删除记忆 | Day 7 |

## 9. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 首版：按主计划 v1.0 拆 6 个节点；含状态 CAS 与事件映射、九层上下文与预算、记忆五关系治理、a0 E20 契约、三件作业规格 |
| v1.2 | 2026-11-09 | 随主计划 v2.1：**D-2 已裁决**（`memory_chunks` 按讲义结构化字段一次建全，表尚未创建故不拆 ALTER）；N02 输入改为指向主计划 §9.1 ④，验收补 `status` 五值与第二个索引 |
| v1.3 | 2026-11-09 | 随主计划 v2.2：**接手 N09**（六表迁移，3.0h），节点 6→7、人时 17.0→20.0；写作用域加 0010 与 `models/medication.py`，并独占 L2/L3 两把迁移锁 |
| v1.1 | 2026-11-09 | 随主计划 v2.0 收紧：N08 由六步收为**骨架**（只提取 + 召回，人时 4.5→2.5，冲突/遗忘登记技术债）；N22 依赖去掉 N21、人时 3.0→2.5、一致性测试改 `skip` 骨架；本卡合计 19.5→17.0 |
