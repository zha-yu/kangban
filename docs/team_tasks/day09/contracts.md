> 状态：待冻结（N0 · D9 09:30 冻结评审；X6 已裁减作废）　｜　版本：v0.3　｜　日期：2026-11-12
> 关联：docs/team_tasks/day09/day09-任务总览与DAG.md v1.3 §5（对接点总表）
> 上游依据：PRD line 387（只有客户端回执算 delivered）、line 357–361（分级与校验）、line 320–325（用药终态）、line 436/441/443（重置/人设/退房）、line 440（三道闸门）｜技术设计文档 line 1166–1174（Day 9 落地）｜《接口契约_照护台业务数据.md》v1.3 §3.2（通知 5 值＋is_mock 冻结）/§6.1（稳定路径）/§6.4（种子规范）｜《接口契约_家属端数据.md》v1.2 S-01（用药统计键名）
> 变更记录见文末

---

# Day 9 跨成员接口契约（X1–X12）

## 0. 冻结与变更规则

- **N0（D9 09:00–09:30，dev-B 牵头）** 逐条评审 X1–X12，四方签认后把本文状态改「已冻结」。
- **冻结后不得私改**：任何字段/口径变更走 §7 变更记录留痕，并通知全部消费方。
- **身份铁律（适用全部契约）**：`elder_id`／`device_id`／`thread_id`／`staff_id`／`guardian_id` **一律由服务端 `runtime_context` 注入**；请求体或模型入参中出现的同名字段**一律丢弃**，不得作为授权依据（AGENTS §4.9）。下文各 schema 中标 `【服务端注入】` 的字段**不接受客户端传值**。
- **错误体**：REST 一律走既有 `ContractAPIRoute` 统一错误体；越权 **403＋`audit_logs` 1 行**；幂等冲突 **409**；参数错 **422**。

---

## X1 · 客户端回执帧（D → C）

**通道**：复用既有信令 WebSocket `/ws/signal`（**不新建通道**，AGENTS §1）。

**上行帧（pad → 服务端）**：

```json
{
  "type": "client_receipt",
  "receipt_kind": "card_rendered",
  "task_id": "3f2a…uuid",
  "notification_id": "9c1b…uuid",
  "ts": "2026-11-13T09:15:03.120Z"
}
```

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `type` | string | 是 | 恒 `"client_receipt"` |
| `receipt_kind` | enum | 是 | `card_rendered`（卡片渲染成功）／`tts_finished`（播报完成） |
| `task_id` | uuid | 是 | 来自服务端下发的 `task_command.taskId`，**不得自造** |
| `notification_id` | uuid \| null | 是 | 用药/健康触达回执必填；纯播报回执可为 null |
| `ts` | ISO8601 UTC | 是 | 客户端单调时钟；服务端以**接收时刻**为准写 `sent_at`，`ts` 仅入日志 |
| `elder_id`／`device_id` | — | — | **【服务端注入】** 客户端若传入一律丢弃 |

**下行确认帧（服务端 → pad）**：

```json
{"type":"receipt_ack","notification_id":"9c1b…","status":"delivered","duplicate":false}
```

**幂等**：键 `(notification_id, receipt_kind)`。重复回执 → 返回原结果、`duplicate=true`，**不重复写 `sent_at`**。

**服务端处理链**：`app/audio/signaling.py`（D 持锁）收帧 → 校验 `task_id` 归属当前设备绑定的长者 → 调 `notify_service.mark_notification_delivered(session, notification_id=…, receipt_kind=…)`（C 持锁）。

**C 侧函数签名（冻结）**：

```python
async def mark_notification_sent(session, *, notification_id: uuid.UUID) -> NotificationRef: ...
async def mark_notification_delivered(session, *, notification_id: uuid.UUID, receipt_kind: str) -> NotificationRef: ...
```

**状态机硬口径**（PRD line 387／契约 §3.2）：
- 出口发送成功 → `pending`（**不是 delivered**）＋`retry_count+1`
- 收到 `client_receipt` → `delivered`
- 人工确认 → `acknowledged`（写 `acknowledged_by`/`acknowledged_at`）
- `pending` 重试 **3 次**仍无回执 → `failed`（终态）
- **禁止**任何路径无条件写 `delivered`
- `is_mock=true` 的行不计入送达率分母

**联调判据（D10 12:00）**：pad 渲染卡片 → DB 该行 `status='delivered'`；不渲染 → 三次扫描后 `status='failed'`。

---

## X2 · 分级引擎签名与 rule_version（C → B）

**签名不变**（C1 只改阈值，不改签名，B 的子图 grade 节点零改动）：

```python
def grade_health(
    *, systolic: int | None, diastolic: int | None,
    sleep_hours: float | None, mood_score: int | None, complaint: str | None,
) -> GradeResult: ...
```

`GradeResult` 字段：`severity ∈ {green,yellow,red}`、`components: list[ComponentGrade]`、`rule_version: str`。

**阈值口径（PRD line 357–359 原文，唯一依据）**：

| 等级 | 规则 |
| --- | --- |
| 红 | 收缩压 **≥180** 或 舒张压 **≥120**；或命中急症症状规则。**低血压不判红** |
| 黄 | 收缩压 140–179；舒张压 90–119；**收缩压 <90**；**舒张压 <60**；睡眠 <5h；mood ≤2 且未命中急症；**非急症主诉**；任一采集项缺失 |
| 绿 | 收缩压 **90–139** 且 舒张压 **60–89**，其他无异常 |

**校验（PRD line 361）**：非数字/负值/睡眠 **>24h** 无效；两分量均有时校验收缩压 > 舒张压；无效项重问一次，再失败标缺失继续；无红分量但血压任一分量缺失判黄。

**`rule_version`**：`g1` → **`g2`**。既有 `g1` 记录不回改。

**禁止**：自加睡眠上界（原 `SLEEP_GREEN_UPPER=12.0` 无 PRD 依据，删除）；把低血压判红。

---

## X3 · 红档双通知出口（C → B）

```python
async def notify_red_dual(
    session, *, elder_id: uuid.UUID, record_id: uuid.UUID, severity: str,
) -> NotifyRef: ...
```

| 项 | 口径 |
| --- | --- |
| 收件人 | **责任护工（primary_care）＋ 监护人（guardian）各 1 条**，同时发出（PRD line 378–392／FR-ALT-01） |
| 幂等键 | `dedupe_token = f"health_record:{record_id}:red"` |
| 返回 | `NotifyRef(notification_ids: list[uuid], risk_event_id: uuid, work_order_id: uuid \| None)` |
| 建单 | **由 C 的 `health_service` 统一建单**（`severity="red"`，assignee＝primary_care）；B 的子图**不得另建一单** |
| 去重 | 语音路径（子图）与 REST 路径共用同一 `dedupe_token`；同键重复 → 返回原结果；同键异内容 → **409** |

**B 侧注入点（两处，缺一不可）**：`app/scheduler/health_jobs.py:261-263` 与 `:378-380` 的子图构造，传 `notify_red_fn=notify_red_dual`、`create_red_order_fn=…`、`validate_fn=…`（X2 校验）。

**禁止**：保留「notify_service 尚未交付」注释（该服务早已存在）；`_default_notify_red` 只作注入缺失时的显式降级兜底，生产路径必须走注入。

---

## X4 · 生命周期清理钩子（B → A）

```python
async def purge_elder_memories(session, *, elder_id: uuid.UUID, reason: str) -> PurgeRef: ...
```

`PurgeRef(deleted_chunks: int, deleted_vectors: int, audit_id: uuid)`。

| 项 | 口径 |
| --- | --- |
| 语义 | 删除该长者全部 `memory_chunks` 行与向量索引；**删除后不得再被召回注入**（FR-MEM-05/08） |
| 调用方 | A 的 `lifecycle_service` 退房/重置流水线；**A 不直写 `memory_chunks`** |
| 审计 | 由 A 的流水线统一写（每步 1 条），B 不重复写 |
| `reason` | `checkout`（退房）／`teaching_reset`（教学周期重置） |

**A 侧其余清理职责（自有写域）**：声纹样本与向量（`voiceprint_service`）、全部 token 吊销（`auth_service`，吊销后旧凭证 **401**）、健康数据**脱敏归档**（用 A1 的 `redact.py`，不删原始业务行而是脱敏后归档）。

---

## X5 · 记忆管理函数（B → C）

```python
async def list_memories(session, *, elder_id: uuid.UUID, limit: int = 50, offset: int = 0) -> list[MemoryView]: ...
async def correct_memory(session, *, elder_id: uuid.UUID, chunk_id: uuid.UUID, new_text: str) -> MemoryRef: ...
async def delete_memory(session, *, elder_id: uuid.UUID, chunk_id: uuid.UUID) -> MemoryRef: ...
```

| 项 | 口径 |
| --- | --- |
| 隔离 | 三个函数**强制 `WHERE elder_id`**；`chunk_id` 不属于该长者 → **403＋审计**（闸门②，AC-39） |
| 纠正语义 | 保留版本关系：旧值标 `superseded`、新值生效（FR-MEM-03/06），**不物理删旧行** |
| 删除语义 | 立即不再被召回（FR-MEM-05/08）；向量残留由召回后二次过滤兜住 |
| 消费方 | C 的 `app/api/memory.py`（薄路由）＋ console 记忆管理页 |
| 禁止 | C 不重复实现隔离逻辑；B 不做前端 |

---

## X6 · 人设视图与注入（C → B）——**已裁减作废**

> **2026-11-12 主理人决定：人设功能（FR-ADM-07）移出 Day 9，本契约作废。** 不冻结、不实现 `get_active_persona`／`switch_persona`／`PersonaView`；C 不再持有 `persona_service.py`／`elder_service.py`／`app/core/context.py`／`app/api/personas.py`；B3 不再消费 X6。编号 **X6 保留不复用**，避免与历史引用混淆。裁减依据与留痕见总览 §8.1（PRD §6.2 人设属 P1 可裁减＋§7 裁减有记录）。**现有 `personas` 表／`elders.persona_id` 外键／`seed_personas.py`／`context.py:189-194` 注入模板保留不动**；console 人设入口保持置灰。

---

## X7 · 复核 → resume（C → B）

**落点裁决（本编排决定，依据 AGENTS §2.2）**：`api` 禁 import `graphs`，故 **C 只写库、B 在 scheduler 侧 resume**（`scheduler → graphs` 是合法方向）。

**C 侧写入字段**（`app/api/health_records.py` 复核端点，**全部为既有列，不新增 schema**）：

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `review_status` | `pending` → `confirmed`／`corrected` | 既有枚举（`app/models/health.py:44` `_REVIEW_STATUSES = ("pending","confirmed","corrected")`），不新造 |
| `reviewed_by` | uuid \| null | 既有列，复核操作者 |
| `review_note` | text \| null | 既有列（契约 Q-1 已补） |
| `superseded_reason` | text \| null | 纠错时写，服务端固定枚举 |

> **注**：`health_records` **无 `reviewed_at` 列**（实测 19 列，2026-11-12）。扫描水位**不依赖时间戳**，改用「`review_status` 已终态 ＋ 对应线程仍处 interrupt」双条件判定，幂等由 checkpoint 状态保证——**不为此新增列**。

**B 侧扫描 job**（`app/scheduler/health_jobs.py`）：

- 触发：**30s tick**
- 条件：`review_status ∈ {confirmed, corrected}` AND 对应 `thread_id` 的 checkpoint **仍处 interrupt**（已 resume 的线程不再命中，天然幂等）
- 动作：调 `resume_health_checkup(thread_id, payload)`；`thread_id` **只从服务端 checkpoint 记录取**，不接受客户端传入
- 幂等：同一 `record_id` 只 resume 一次（按 checkpoint 是否仍 interrupt 判定，**不靠时间戳水位**）
- **scheduler 零写库**：`grep "session.add(\|from app.models" app/scheduler/` 须零命中

**联调判据（D11 14:00）**：子图停在 interrupt → console 复核 → **≤30s** 子图恢复并落终态；重启后仍可恢复。

---

## X8 · 迁移 0020（A → B/D）

**文件**：`alembic/versions/0020_day9_medication_enums.py`（**只新增，不改历史**）

| 表 | 列 | 变更 |
| --- | --- | --- |
| `medication_events` | `terminal_state` | CHECK 由 4 值扩为 **5 值**：`taken_reported`／`refused`／`missed_unconfirmed`／`notification_failed`／**`late_taken_reported`** |
| `medication_events` | `failure_reason` | CHECK 由 4 值扩为 **5 值**：`no_answer`／`denied`／`tts_failed`／`offline`／**`rtc_failed`** |

**依据**：PRD line 320–325（明确要求追加 `late_taken_reported`）、line 336（FR-MED-06 要求 `rtc_failed`）。

**约束**：upgrade/downgrade 均可逆；不改既有行数据；**TD line 778「Day 9 迁移 0 张」须同提交在 TD 文末修订记录新增一条**（AGENTS §4.7，登记为总览 Q3）。

**消费方**：B4（`late_taken_reported` 落库）、D2（`rtc_failed` 落库）。

---

## X9 · 门禁硬线与归因（D → 全员）

**闸门 E（新增，与既有 B/D 并列且独立）**：

| 判据 | 阈值 | 依据 |
| --- | --- | --- |
| 普通集准确率 | **≥45/50（90%）** | PRD line 531–536 |
| 敏感集召回 | ≥19/20（95%） | PRD line 531–536（既有闸门 A 已有，保留） |
| 路由耗时 | **p95 ≤200ms** | PRD line 547 |
| 注入拦截 | **100%（≥10 条）** | AC-36（Day 9 新增） |

**权限边界（明确，避免争议）**：闸门 E 校验的是 **PRD 明文硬线**，**不高于 PRD**，因此**不属** `eval_gate.py:577-601` 注释所禁止的「私自加严」（该注释反对的是「要求全对」）。既有闸门 B（不回退）/D（失败集合不扩大）**语义与权限保持不变**。

**失败处置**：E 失败 → 打印未达标项与差值、退出码非 0；写锁文件须显式 `--allow-below-prd` ＋留痕理由。**禁止**调阈值、删用例、放宽 `SENSITIVE_FLOOR_PCT`。

**归因分工**：路由/分类器/规则层失败 → **dev-B**；分级/通知失败 → **dev-C**；前端回执/素材失败 → **dev-D**；闸门/审计失败 → **dev-A**。D **只读阈值，不改 `rules.py`**。

**七层归因（F31）**：ASR／分类器／规则层／召回／LLM／Tool／编排 **七层均须真实值或带原因的空值**，禁止硬编码「无数据」。ASR 置信度**已埋点**（`app/audio/_observe.py:69`、`app/models/decision.py:119`、`app/services/decision_log_service.py:87/:114`）；funasr 源 confidence 恒 None 属**数据源限制**，须如实标注原因。

**管理员手动重发（遵 Q-D3）**：admin-only 端点**仅触发一次既有 `scan_pending_notifications`**，**不含独立重试逻辑**、不新造端点语义。

**夹具 gold 修正（F22）**：`eval/fixtures/day7_intents.jsonl` 的 20 条 boundary（negation/history/other_person/hedge 各 5）gold 由 `other`/`is_sensitive=False` 改为**判黄复核**口径，依据 PRD line 272 FR-DLG-07。**属向 PRD 对齐，非改验收**。

---

## X10 · 戏曲素材规范（D → C）

**唯一依据**：《接口契约_照护台业务数据.md》v1.3 **§6.4**（已冻结，本文不重定义）。

| 项 | 冻结值 |
| --- | --- |
| 条数 | **20–30 条** |
| `title` | **中文剧名**（如「梁山伯与祝英台·楼台会」） |
| `genre` | **≥4 剧种且必含越剧、京剧** |
| `duration` | **15–60 秒** |
| 稳定路径 | §6.1：`media_url` 落库值决定扩展名（wav/mp3） |
| 写库口 | **`media_service.seed_media_library`（C 持锁）**；D 的脚本只调该函数，不直写 ORM |
| history | **Tool 层不写 `media_history`，history 归 C 的 `media_service`**（契约 §6 冻结口径） |

**现状纠正**：`scripts/media_manifest.json` 现为流行/摇滚/说唱/民谣（cruel summer／the cure／仙儿／冷战／安河桥 等）且全部行 `type=opera`，**违反 §6.4**。改用已合规的 `scripts/generate_opera_seed.py`（程序生成、越剧/京剧/黄梅戏/豫剧/评剧、1950s–1990s），并按其 `:16` 注释改调 `seed_media_library`。

**素材不入库**：`static/media/` 保持 `.gitignore:78`（AGENTS §4.11 不提交媒体大文件）；**重建脚本须在新克隆机器可跑通**。

---

## X11 · 调度节奏挂载（C → B）

**写锁**：`app/scheduler/` 归 **dev-B**；C **不改调度文件**，只提供退避函数。

```python
def next_retry_at(*, ntype: str, retry_count: int, now: datetime) -> datetime | None: ...
```

| `ntype` 类别 | 退避序列 | 上限 | 依据 |
| --- | --- | --- | --- |
| 告警通知（`health_alert`／`escalated_red_event`／`medication_*`） | **30s → 60s → 90s** | 3 次后 `failed`（终态） | 契约 §3.2「pending 每 30s 重试 3 次失败置 failed」 |
| 日报推送（`report_ready`／`report_updated`） | **1min → 5min → 15min** | 3 次后 `failed` | PRD §4.8 FR-RPT-03 |

**B 侧挂载**：`health_jobs` 的 tick 由 `* * * * *`（60s）改 **30s**（否则无法表达 30s 重试）；`_DAILY_REPORT_FIRE` 由 `21:30` 改 **20:00**（PRD §4.8；上游 day-08 计划 H4/H8 与 dev-B/C 任务卡均明写 20:00，21:30 属偏离）。

**返回 None** 表示重试次数用尽 → 调用方置 `failed`。

**禁止**：B 改退避数值（PRD/契约硬口径）；C 改 cron 表达式。

---

## X12 · 域清理钩子集（B/C → A，退房与教学重置专用）

**为什么需要**：`lifecycle_service`（A）要清空的业务表分属 B/C 的写域。**A 不直写他人表**（AGENTS §2.1 唯一写库层），改为**各域 service 提供 purge 钩子、A 只编排与审计**。

| 提供方 | 钩子签名 | 清理范围 |
| --- | --- | --- |
| **B** | `memory_service.purge_elder_memories(session, *, elder_id, reason) -> PurgeRef` | 即 **X4**，`memory_chunks` 行＋向量索引 |
| **B** | `medication_service.purge_elder_data(session, *, elder_id, reason) -> PurgeRef` | `medication_plans`／`medication_events`／`medication_attempts` |
| **C** | `health_service.purge_elder_data(session, *, elder_id, reason) -> PurgeRef` | `health_records`：**退房＝脱敏归档**（用 A 的 `redact.py`，不物理删）；**教学重置＝清除** |
| **C** | `notify_service.purge_elder_data(session, *, elder_id, reason) -> PurgeRef` | `notifications` |
| **C** | `report_service.purge_elder_data(session, *, elder_id, reason) -> PurgeRef` | `daily_reports` |
| **C** | `work_order_service.purge_elder_data(session, *, elder_id, reason) -> PurgeRef` | `work_orders`／`work_order_transitions`／`risk_events` |
| **A 自有** | `auth_service.revoke_all_tokens`／`voiceprint_service.purge_elder_samples`／`device_service.unbind_all` | token 吊销（旧凭证 **401**）、声纹样本与向量、设备解绑 |

**统一口径**：
- `reason ∈ {checkout, teaching_reset}`；`PurgeRef(affected_rows: int, archived_rows: int, domain: str)`。
- **审计由 A 的流水线统一写，每步 1 条**；各域钩子**不重复写审计**。
- **保留项**（FR-ADM-02）：账号／角色／配置／重置审计**不得清除**。
- **幂等**：同一 `(elder_id, reason)` 重复调用返回原结果，不重复归档。
- **顺序**（退房）：吊销 token → 删声纹 → 清记忆 → 健康脱敏归档 → 设备解绑；**每步独立可重入**，中途失败可单独重跑，**不回滚已吊销 token**。

**联调时点**：D10 16:00（与 X4 同场）。**骨架先行**：A3 先用桩，B/C 的钩子签名 D9 下午冻结。

---

## 7. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v0.3 | 2026-11-12 | **X6 人设契约裁减作废**（主理人决定，人设移出 Day 9）：删除 `get_active_persona`／`switch_persona`／`PersonaView` 全部字段级定义，改为作废声明；编号 X6 保留不复用。有效契约变为 X1–X5、X7–X12 共 11 份＋总览 §5.2 两条消费既有契约的边。裁减依据与现有代码保留口径见总览 §8.1。 |
| v0.2 | 2026-11-12 | 全量校验后补 **X12 域清理钩子集**：退房/教学重置需清空的业务表分属 B/C 写域，A 的 `lifecycle_service` **不直写他人表**，改为各域 service 提供 `purge_elder_data` 钩子、A 只编排与审计（AGENTS §2.1）。明确 `reason` 枚举、`PurgeRef` 结构、审计归属（A 统一写、各域不重复）、保留项、幂等键与退房五步顺序及可重入语义。另修正 **X7**：`health_records` 实测 19 列**无 `reviewed_at`**，改用既有 `review_status`＋`reviewed_by`，扫描水位改「已终态＋线程仍 interrupt」双条件，不新增 schema。 |
| v0.1 | 2026-11-12 | 首版草稿，供 N0（D9 09:30）冻结评审。定义 X1–X11 共 11 个跨成员对接点的字段级口径：X1 回执帧（复用 `/ws/signal`，幂等键 `(notification_id, receipt_kind)`，禁止无条件 delivered）、X2 分级阈值照 PRD:357-359 原文＋`rule_version` g2、X3 红档双通知出口与统一建单去重、X4 记忆清理钩子、X5 记忆管理三函数（强制 `WHERE elder_id`）、X6 人设视图与 prompt 注入点归 C、X7 复核 resume 落点（C 写库＋B scheduler 30s 扫描，依据 AGENTS §2.2）、X8 迁移 0020 扩两枚举、X9 闸门 E 校验 PRD 明文硬线（不属私自加严）＋七层归因＋夹具 gold 向 PRD:272 对齐、X10 素材照契约 §6.4 不重定义、X11 退避函数与 cron 写锁分离。全部契约含「服务端注入身份、客户端传值丢弃」铁律。 |