> 状态：已冻结　｜　版本：v1.3　｜　日期：2026-11-09
> 关联：docs/plans/day06/day-06-长者侧Agent闭环.md（§3.1 节点表、§9.1 落地结构）｜docs/team_tasks/day06/dev-a~dev-d 四张任务卡
> 上游依据：PRD v2.9 §6.1 Day 6 行（验收）｜技术设计文档 v2.0 §3.4/§3.6/§8.3（实现）｜ER v0.5 §4.3.11–4.3.13（字段）
> 变更记录见文末

# Day 6 契约冻结件（N00 交付）

> 本文件是 Day 6 前后端与后端内部的**唯一口径**。字段只以本文为准，口头约定无效；任一方要加字段，**先改本文再改代码**（`AGENTS.md` §10.4）。
> 冻结时间：2026-11-09（G0 门禁通过）。冻结后新增字段走"版本号 +1 + 变更记录"，不静默追加。

---

## 1. 适用范围

| 覆盖 | 不覆盖（本日明确不做） |
| --- | --- |
| WS 消息：`tts_text` v2、`segment_ack` | 规则层 ≤10 条意图、Laya 四信号、`decision_logs`（随三级路由顺延） |
| 后端内部：`HistoryStore` / `StateSnapshot` / `ToolResult` / `MCPResult` | `app/system_one/**`、`app/router/rules.py` |
| REST：a0 `PUT /api/staff/{staff_id}/elders`（E20） | 天气以外的 MCP server（`mock-operations` 留 Day 8） |
| 数据层：0008/0009/0010 三迁移的落表结果 | 娱乐点播、健康采集、日报 |

**本日禁区**（违反即返工）：

1. 分类能力**不得**接入任何第三方商业 Jev API 或其他云端分类服务，**不新增相关依赖**；Laya 中文开源权重到位前，降级唯一路径是"全量走 L2 + `route_layer=fallback` 标记"。
2. 事件表与工具表**不留** `route_layer` / `intent` / `confidence` 字段——属路由日，提前建列会造成两套口径。
3. `app/router/safety.py` **只做一票否决**（急症/敏感短路），不做固定意图映射。

---

## 2. 五个标识（谁生成、谁消费）

| 标识 | 生成方 | 生命周期 | 用途 | 客户端可否指定 |
| --- | --- | --- | --- | --- |
| `thread_id` | 服务端 `identity_service`（`(elder_id, session_seq)` 或 `generic:<device_id>:<seq>`） | 一次会话 | 会话隔离、闸门① | **否**（传入一律丢弃） |
| `call_id` | 服务端建连时 | 一次 WebRTC 连接 | 断线重连后区分"同一会话的两次建连" | 否 |
| `turn_id` | 服务端 `begin_turn` | 一轮问答 | 幂等键（重复 FINAL 不重复处理） | 否 |
| `response_id` | 服务端 `response_stream.start` | 一次回复（可含多段） | 分段确认、打断淘汰 | 否 |
| `task_id` | 服务端建任务时（用药确认等） | 一个跨轮任务 | 待答绑定、checkpoint 关联 | 否 |

> 铁律：`elder_id` / `thread_id` 只从服务端 `runtime_context` 注入，客户端与模型传入的值一律丢弃（`AGENTS.md` §4-9）。

---

## 3. 事件枚举（12 种，冻结）

| `event_type` | 写入时机 | `payload_json` 关键字段 |
| --- | --- | --- |
| `turn_started` | `begin_turn` 成功 | `user_text`、`identity_source` |
| `asr_final_committed` | 定稿文本落库 | `asr_confidence`、`audio_ms` |
| `response_started` | 首次下发前 | `response_id`、`model`、`degraded` |
| `response_segment_delivered` | 收到该段 `played` | `segment_id`、`index`、`text` |
| `response_interrupted` | 打断生效 | `response_id`、`last_played_segment` |
| `response_completed` | 末段 `played` | `total_segments` |
| `tool_started` | 工具调用前 | `tool_name`、`idempotency_key` |
| `tool_succeeded` | 工具返回成功 | `tool_name`、`latency_ms` |
| `tool_failed` | 工具失败/拒绝 | `tool_name`、`error_class` |
| `task_waiting` | 进入待答 | `task_id`、`waiting_for` |
| `task_resumed` | 收到对该任务的回答 | `task_id`、`answer_digest` |
| `turn_completed` | 本轮事实提交完毕 | `turn_id`、`status` |

**只追加规则**：更正/替代一律写新事件并带 `ref_event_id` 指向旧事件，**禁止 UPDATE 旧行**。枚举增删需新迁移改 CHECK。

---

## 4. WS 消息契约

### 4.1 S→C `tts_text` v2（向后兼容扩展）

```jsonc
{
  "type": "tts_text",
  "text": "出门记得带伞。",
  "degraded": false,
  "response_id": "r17",   // 可选：缺失时旧客户端按整段一播处理
  "epoch": 8,             // 可选：客户端**原样回传**，不得自增
  "segment_id": 2,        // 可选：0 基
  "index": 2,             // 可选：0 基，与 segment_id 同值（保留两字段便于未来乱序）
  "total": 3              // 可选：本回复总段数
}
```

- 老字段（`type`/`text`/`degraded`）**顺序与语义不变**；新字段全部可选。
- 分段由**服务端**切（`。！？；` 优先，单段 ≤40 字，长句按逗号补切），前端不再二次切分。

### 4.2 C→S `segment_ack`

```jsonc
{ "type": "segment_ack", "response_id": "r17", "epoch": 8,
  "segment_id": 2, "status": "playing", "at_ms": 1730 }
```

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `status` | `playing` / `played` / `canceled` | 打断时上报 `canceled`，并带**上一个成功播放段的 `segment_id`** |
| `at_ms` | 客户端单调时钟（ms） | 只用于折算播放时长，不参与判定 |

**服务端校验四步**（任一不过即丢弃并记 `response_ack_rejected`）：
① `response_id` 存在 → ② `epoch` 等于当前会话 epoch → ③ `response.status == playing` → ④ `segment_id > last_acked_segment`。

**v1.2 补充口径（J3 核对裁决，2026-11-09 主理人确认）**：

- **canceled 的 segment_id 取值**：= 最后一个已确认 `played` 段的 segment_id；**尚无一完整播放段时为 `-1`**（哨兵，0 基合法域 [0, total-1] 之外，不与合法段号混淆）。服务端 `last_acked_segment` 初始值同为 `-1`，两端同构；`response_interrupted.last_played_segment` 与 `delivered_text`（只拼 played 段）沿用同值。
- **canceled 类 ack 的校验范围**：只走 ①②③，**跳过规则④**（陈述性上报，不受单调性约束）；`playing`/`played` 走完整四步。
- **rejected_reason 枚举**（`response_ack_rejected` 日志携带，服务端内部口径）：`unknown_response / stale_epoch / wrong_status / not_monotonic`。
- **末段 played 触发**：以客户端 `played` ack 为准，`response_completed` 只由末段 played 触发；可靠性兜底不改契约——客户端「尽力而为 + 1 次重发」照旧，WebRTC 轨无可靠 ended 事件时 pad 端可用「该段合成音频时长 + at_ms 单调时钟计时」触发 played（复用现有信号，不新增消息类型）；服务端另以「下一代 epoch 出现 / `response_interrupted` / WS 45s 判死」作为 playing 的隐式关闭。

**迟到回调为什么两条都要校验**：只校验 `epoch` 时，同一 epoch 内被打断的回复仍可能被后续 ack 追认；`epoch` 负责"跨代淘汰"，`status` 负责"同代内打断后的追认拦截"。

#### 4.2.1 水位与截断（P1 裁决：方案 A，2026-11-09）

| 项 | 口径 |
| --- | --- |
| `last_acked_segment` 语义 | **最后一段「整段播完」的段 id**；初始 `-1`，表示一段都没播完 |
| 谁推进水位 | **只有 `status=played`**。`playing` 只走 ①②③ 并打 `response_segment` 指标（含首段开播延迟），**不推进**；`canceled` 也**不推进** |
| `canceled.segment_id` | = 最后一个已确认 `played` 段的 id；**一段都没播完时取 `-1`**（回显水位，与水位同构，不引入第二个概念） |
| `delivered_text` | `None` 当 `last_acked_segment < 0`；否则按 `index` 升序拼接 `[0 .. last_acked_segment]` |
| 依赖的不变量 | ④ 的**严格单调**（`>`）保证 `[0..wm]` 全部已 `played`。**若日后把 ④ 放宽为 `>=` 或允许跳号，截断计算会静默出错**，必须同步改本节 |

**为什么 `playing` 不能推进水位**（两条都曾在讨论中出现过，记录在此以免重犯）：

1. 同段随后到达的 `played` 会被 ④（`segment_id > last_acked_segment`）判为迟到而丢弃 → 没有任何段被标记「播完」，`delivered_text` 恒为 `None`，正常播完的一轮也会被记成「一个字都没说」。
2. 把 ④ 改成 `>=` 能绕过 1，但水位会同时承担「最高见过」与「最后播完」两个语义；打断时两者必然分叉（第 2 段播到一半插话：最高见过 2、实际听完 1），用前者算截断等于把只播了一半的段算成「听过」——这正是 FR-UI-04 要防的事。

---

## 5. 后端内部契约

### 5.1 `HistoryStore`（`app/services/history_service.py`）

```python
async def begin_turn(*, thread_id, elder_id, device_id, call_id, turn_id,
                     user_text, trace_id, identity_source=None) -> TurnRef
async def append_event(*, thread_id, event_type, payload, turn_id=None,
                       response_id=None, task_id=None, event_id=None,
                       ref_event_id=None, call_id=None, trace_id=None) -> int   # 返回 seq
async def commit_response(*, turn_id, response_id, generated_text,
                          delivered_text, status) -> None  # 同 (turn_id, response_id) 幂等
async def finish_turn(*, turn_id, status) -> None          # status: completed/interrupted/failed
async def read_turns(*, thread_id, elder_id, limit=8) -> list[TurnView]
async def read_events(*, thread_id, after_seq=0) -> list[EventView]
```

- `seq` 分配：同事务内 `SELECT last_event_seq FROM conversation_threads WHERE thread_id=:t FOR UPDATE` → +1 → 写回 → 插事件。**禁止"先查 max 再 +1"**。
- `generic` 模式（`elder_id is None`）：只写事件，不写 `conversation_turns`；`read_turns` 返回空。
- `TurnView`：`turn_id / user_text / assistant_delivered_text / status / started_at / ended_at`。
- `TurnRef`：`turn_id / seq / duplicate`；重复 `turn_id` 时 `seq=None`、`duplicate=True`（没有新事件，就不该给新序号）。
- `EventView`：`seq / event_type / occurred_at / payload / turn_id / response_id / task_id`。
- **单写口**：`conversation_turns.status` 终态只由 `finish_turn` 写；`commit_response` 的 `status` 只进事件 payload。否则迟到的 `completed` 会覆盖已发生的 `interrupted`。
- `read_turns` 返回**对话先后正序**（最早在前），排序依据是各轮 `turn_started` 的 `seq`，**不是** `started_at`——`now()` 取事务开始时间，同事务两轮会撞同一时间戳，按它排等于随机；会话归属与 `elder_id` 不符时返回空而非抛错（读路径不该让整轮对话失败）。
- **`session` 是可选参数**（v1.3 明确）：签名里不带 `session` 时自开一条只读会话。上下文层（`app/core/context.py`）就是 `read_turns(thread_id=..., elder_id=..., limit=...)` 这样调的；需要读到同一事务内尚未提交的历史时，才显式传入 `session`。`read_events` 同口径。
- **`commit_response` 与响应层的关系**（v1.3 明确）：`response_completed` 事件按 §3 的时机由**响应层**（N07）在末段 `played` / 打断时写入；`commit_response` 发现该事件已存在时**只补投影、不重复写事件**。若事件由 `commit_response` 兜底写入（响应层未接线），它同时落 `generated_text` / `delivered_text` / `response_status`。


### 5.2 `StateSnapshot`（`app/services/state_service.py`）

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

async def snapshot(thread_id: str) -> StateSnapshot
async def apply(*, thread_id, expected_version: int, events: list[StateEvent]) -> StateSnapshot
async def clear(thread_id: str) -> None
```

- `apply` 版本不符抛 `StateVersionConflict`，**不覆盖**。
- 存储：Redis `session:{thread_id}:state`，TTL 2h；关键任务以 checkpointer + 业务表为准，不依赖 TTL。

### 5.3 `ToolResult`（`app/services/tool_executor.py`）

```python
@dataclass(frozen=True)
class ToolResult:
    status: Literal["succeeded", "failed", "denied"]
    data: dict | None
    error_class: Literal["bad_args", "not_found", "transient", "forbidden"] | None
    message: str | None            # 可直接播报的降级话术
    idempotent_replay: bool = False

async def execute(*, tool_name, args, runtime, turn_id, idempotency_key) -> ToolResult
```

- 身份只从 `runtime.elder_id` 读；`args` 含 `elder_id`/`thread_id`/`device_id` 一律丢弃并写 `audit_logs.action='tool_identity_override_attempt'`。
- 四类失败与处置：`bad_args` 回传模型重试（计预算）｜`not_found` 如实告知｜`transient` 退避 ≤2 后降级话术｜`forbidden` 拒绝 + 审计。
- 预算与熔断复用 `app/agents/guardrails.py`（`MAX_TOOL_CALLS_PER_TURN=8`、同 Tool 1 分钟第 3 次熔断），**不自建第二套**。

### 5.4 `MCPResult`（`app/mcp_client/`）

```python
@dataclass(frozen=True)
class MCPResult:
    ok: bool
    data: dict | None
    error: Literal["timeout", "upstream_error", "bad_response", "unauthorized"] | None
```

- 超时取 `mcp_call_timeout_seconds`（默认 5s）；天气缓存 TTL 取 `weather_cache_ttl_seconds`（默认 600s），**不新增配置项**。
- 天气上游失败或无 `weather_api_key`：`ok=False`，工具层话术"这个我暂时查不到"，**禁止**用模型常识生成天气。
- 每次调用写 `audit_logs`（工具名、耗时、结果分类），不回显上游内部细节。

---

## 6. REST 契约（a0，E20）

```
PUT /api/staff/{staff_id}/elders          # admin only
body   { "elder_ids": ["uuid", ...] }     # 全量覆盖式替换
200    { "staff_id": "...", "added": ["uuid"], "removed": ["uuid"] }
403    非 admin
409    移除后某长者失去全部 primary_care
```

- `relation` 按当前 role 映射（`care_worker → primary_care`、`doctor → doctor`）；`admin` 一律 403。
- 写 `audit_logs`：操作者、`trace_id`、`added`/`removed` 差集。
- **生效口径**：`scope_elder_ids` **逐请求查表**（不靠 token 快照），改绑后旧 token 的范围随之变化。

---

## 7. 数据层落地（已执行）

| 迁移 | 内容 | 结果 |
| --- | --- | --- |
| `0008_day6_conversation_events` | `conversation_events`(28) / `conversation_turns`(29) / `tool_executions`(30) + `conversation_threads.last_event_seq` | 已 apply |
| `0009_day6_memory_chunks` | `memory_chunks` 23 列（TD 原 10 + 增 13）、`status` 五值、`vector(1536)` | 已 apply |
| `0010_day6_medication` | `medication_plans` / `medication_events` / `medication_attempts` / `risk_events` / `work_orders` / `work_order_transitions` | 已 apply |

表数：14（既有）+ 10（本日）= **24 张业务表** + `alembic_version`；全量口径 **30 张**（含 Day 7/8 待建 5 张与随路由顺延的 `decision_logs`）。

---

## 8. 冻结范围外但需知晓

- `decision_logs` 表**不建**：它随三级路由顺延，提前建会让 `route_layer` 统计口径提前固化。
- `app/system_one/` **不建**：Laya 权重未就绪，建空壳会诱导"先接线再补权重"。

---

## 9. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 首版并冻结：五个标识、12 类事件、`tts_text` v2 与 `segment_ack`、`HistoryStore`/`StateSnapshot`/`ToolResult`/`MCPResult` 四组内部契约、a0 E20、三迁移落地结果、两条禁区（无第三方分类 API、不提前建路由表） |
| v1.1 | 2026-11-09 | 主理人裁决（C-0-5/C-0-6）：N27 通知环节提前——新增 0011 迁移建 `notifications` 表 + `app/models/notifications.py` ORM（仅此一笔迁移/一张表解禁）；N27 命中写护工通知（`status=generated`，`dedupe_key` 部分唯一防重，落库即视为已通知）；mock-operations 与家属通知仍留 Day 8，范围不变 |
| v1.2 | 2026-11-09 | J3 核对裁决（主理人确认，dev-D 提清单）：`canceled.segment_id` 无一完整播放段时取 `-1` 哨兵（服务端 `last_acked_segment` 初始同值）；canceled 类 ack 跳过校验④；`rejected_reason` 四枚举（unknown_response/stale_epoch/wrong_status/not_monotonic）；末段 played 触发与三层兜底口径。字段本身零增删，§4.2 增补充口径块；protocol.ts 注释同步由 dev-D 落 |
| v1.3 | 2026-11-09 | **与 B/C 代码合流后的三处口径补正**（均属既有实现的接口对齐，非业务变更）：① `read_turns` / `read_events` 的 `session` 明确为**可选**——契约 §5.1 签名本来就没有它，`app/core/context.py` 按此调用，实现方不得强制要求；② 明确 `response_completed` 按 §3 由响应层先写，`commit_response` 在其已存在时**只补投影**、不重复写事件（否则 delivered 永远为空）；③ 明确投影写入只接受**不更短**的文本，同长可覆盖。 |
| v1.2 | 2026-11-09 | **P1 裁决落地（方案 A）**：§4.2 新增 4.2.1「水位与截断」——`last_acked_segment` 语义定为「最后整段播完的段」、初始 `-1`；**只有 `played` 推进水位**，`playing` 只走 ①②③ 并打指标，`canceled` 不推进且其 `segment_id` 回显水位（无则 `-1`）；补 `delivered_text` 计算式与「④ 严格单调」这一依赖不变量。与 dev-C 在 S2 实现前确认的口径一致（C 侧称 v1.2），**零字段变更、不改四步校验** |
| v1.1 | 2026-11-09 | 实现 `HistoryStore` 时发现两处**漏参**，按冻结规则升版（**仅加可选参数，向后兼容**）：① `append_event` 增 `call_id` / `trace_id`（0008 已建这两列，原签名无法写入 → 会成为死列，链路排查断线）；② `begin_turn` 增 `identity_source`（缺省取会话行上的值）。同时补齐 `TurnRef` / `EventView` 字段定义与两条单写口约束（`status` 只由 `finish_turn` 写；`read_turns` 时间正序、归属不符返回空）。**现有调用方零改动**：新参数均有默认值。 |
