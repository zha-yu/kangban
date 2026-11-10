> 状态：未开始　｜　版本：v1.0　｜　日期：2026-11-10
> 负责人：dev-C（接线实施）　｜　dev-A（接口契约与只读验收支持，**不改 A 线代码**）
> 关联：docs/plans/day07/handoff-A-to-C-用药到点接线.md（接线契约，唯一实现依据）｜docs/team_tasks/day06/dev-a-历史事件与工具事实.md（N10 已收官，不重开）｜docs/team_tasks/day07/contracts.md v1.0（触达消息与 played 回执口径）
> 上游依据：PRD line 332（FR-MED-02 四次触达 + 60 秒确认窗口）、line 335（FR-MED-05 双入口 + 重启恢复）、line 336（FR-MED-06 主动提醒与回执）、line 256（FR-UI-10 反向外呼）｜技术设计文档 line 175（`app/scheduler` 职责）、line 881（时间驱动 Workflow）、line 954（双入口共用一张图）｜AGENTS.md §4 禁区、§5 高风险文件、§7 硬性数值（用药触达 T0/+5/+10/+15，每次等 60s）、§8 幂等
> 变更记录见文末

# Task dev-C2 · 用药到点触发接线（APScheduler → 用药确认子图）

## 0. 一句话

把「到点 → 建事件 → 起图 → 触达 → 等回答 → 终态/工单」这条**调度侧驱动链**接上，让已收官的 N10 子图第一次有生产调用点。

**归属裁决（2026-11-10，主理人采纳 A 侧建议）**：

- **N10 不移交、不重开**。N10 已于 Day 6 收官（A 线 21.0/21.0h、18 个执行节点全交付、已合 `develop`），其卡内验收是「子图双入口 + `get_state` 可读 + 重启恢复 + 同键不重复建单 + 重试 ≤3 进终态」，**不含到点触发**。
- 另立本卡（归 Day 7），接线归 dev-C；A 线只出**接口契约**（§2 与交接件）与**只读验收支持**，不改 A 线代码、不重开 N10 卡状态。
- 原提议里那句「按 N10 验收标准（触发→落库→重试≤3→终态→重启恢复）实施」**口径不准**：其中「触发」不在 N10 卡内。本卡验收单列于 §4，**不复用 N10 的五条用例口径**。

## 1. 目标与边界

**做**

- `app/scheduler/` 下新增用药到点任务注册（APScheduler `AsyncIOScheduler`，绑 uvicorn 事件循环，时区 `Asia/Shanghai`）。
- 到点判定：`medication_plans.schedule_cron`（5 段 cron，如 `0 8 * * *`）命中当前时刻 → 建 `medication_events` 行 → 驱动子图。
- 驱动模型：四个触达时点 **T0 / +5 / +10 / +15 分钟**，每次 **60 秒确认窗口**（AGENTS §7 硬性数值）。
- 长者回答的**双入口**：`triggered_by=scheduler`（本卡）与 `triggered_by=dialog`（C 线 N13/N14 + `medication_confirm` 工具）走**同一张图**、同一 `thread_id`、同一 `attempt_no` 序列。

**不做（明确划出，避免重开 A 卡）**

- 不改 `app/graphs/**`、`app/services/medication_service.py`、`app/services/work_order_service.py`（A 线已收官文件）。
- 不改 `app/models/**`、不新建 Alembic 迁移（无表结构变更需求；`0013_touch_played_receipt` 已由 day07 落地）。
- 不实现反向外呼 WebRTC 建连（FR-MED-06 / FR-UI-10 / AC-41 属前端与信令侧）——本卡只保证「提醒文本/语音送达已绑设备 + 回执如实落库」。
- 不做 `notification_failed` 终态改造（见 §6 开放点 O1，需主理人裁决）。
- 不做 APScheduler 其余任务（19:00 采集 / 日报 / 记忆归并 / 漏采派单 / 72h 清理），按 `app/scheduler/__init__.py` 的 Day 7-9 口径另卡。

## 2. 前置与依赖

| 项 | 状态 | 说明 |
| --- | --- | --- |
| N10 子图 + PG checkpointer | ✅ 已交付（A 线） | `app/graphs/medication_confirmation_graph.py`、`app/graphs/checkpointer.py`（独立 schema `langgraph` + 独立池） |
| E2 触达出口 `deliver()` | ✅ 已偿清 | 信令层 `app.audio.signaling.deliver` |
| TD-1 触达语音接线 | ✅ 已收口（day07 dev-C 卡，commit `6c32846`） | 触达消息带 `touch_id` / `touch_event_id` / `touch_attempt_no` / `audio`（16k s16le mono base64）；合成失败降级纯字幕 |
| TD-2 played 回执闭环 | ✅ 已收口（day07 dev-C 卡） | `touch_played` → `medication_service.mark_attempt_played` 幂等升级 `receipt` 为 `played`（迁移 0013） |
| 建单口 | ✅ 已交付（A 线 A12） | `work_order_service.open_missed_medication`，确定性单号幂等 |
| **子图工厂签名已变更** | ⚠️ **按合并后签名接线** | 现为 `build_medication_graph(*, session, checkpointer, deliver_fn=None, tts_fn=None)`——第四个参数是 `tts_fn(text) -> bytes \| None`（合成出口），**不是** `speak_fn`；缺省走真实信令层 `synthesize_touch_audio` |
| `medication_confirm` 工具执行器 | ⚠️ 未绑定 | 工具已在白名单注册但 `executor=None` → 当前调用返回 `not_found`。对话入口的绑定属 C 线 N13/N14，本卡只约定共用驱动模型 |
| 到点查询读服务 | ❌ 缺 | `knowledge_service.query_medication_plans` **强制按 `elder_id` 过滤**，调度器无法用它列全量到点计划 → 需新增只读服务（见 §3 白名单） |

## 3. 写作用域（白名单）

**新增**

```
app/scheduler/medication_jobs.py          新增：到点任务注册 + 驱动 + 停机
app/services/medication_due_service.py    新增：只读查询「当前时刻到点的 active 计划」
tests/test_scheduler_medication.py        新增：接线用例（≥8 条，见 §4）
```

**修改**

```
app/main.py                               lifespan 内启动/关闭 scheduler
```

**禁触**（他人文件 / 已收官 / 高风险）

```
app/graphs/**                             A 线已收官（§5 高风险文件：状态与断点恢复）
app/services/medication_service.py        A 线已收官
app/services/work_order_service.py        A 线已收官
app/models/**、alembic/versions/**        无表结构变更；禁新增迁移（head 唯一，0011/0013 已由 C 落地）
app/agents/**                             C 线 N13/N14 范围
frontend/**                               前端与信令侧（FR-MED-06 反向外呼）不在本卡
```

> 若主理人更希望由 A 线在 `medication_service.py` 增一个 `list_due_plans()`（属 A 卡重开），需**显式授权**；默认走 C 侧新文件，避免跨人改别人文件。

## 4. 验收判据（可执行、可量化）

**A1 · 静态与分层**

- `app/scheduler/` 存在任务注册模块；`app/main.py` lifespan 内 `scheduler.start()` 与关闭时 `scheduler.shutdown(wait=False)` 成对出现。
- `grep -rn "session.add\|session.commit\|session.execute" app/scheduler/` → **零命中**（`app/services/` 是唯一写库层；scheduler 只调 services/graphs）。
- `grep -rn "from app.models\|import app.models" app/scheduler/` → 零命中（依赖方向 `scheduler → services → models`）。

**A2 · 单测（`tests/test_scheduler_medication.py`，≥8 条）**

1. 到点建事件**恰好一次**：同一 `(plan_id, scheduled_at)` 触发两次 → `medication_events` 仍 1 行、`reminder_id` 相同（`create_event` 幂等键）。
2. 四次触达：`medication_attempts` 恰好 **4 行**，`attempt_no` = 1/2/3/4，`receipt` ∈ `voice_dispatched` / `device_offline` / `no_bound_device` / `played`（TD-2 已加 `played`）。
3. 四次无回答 → `terminal_state='missed_unconfirmed'`、`status='escalated'`、`failure_reason='no_answer'`、`retry_count=3`。
4. 建单**恰好一张**：`work_orders` 1 行且 `order_no` 确定性（重复触发不产生第二张）。
5. **重启恢复**：新 checkpointer 实例 `aget_state(graph_config(reminder_id))` 读得到等待节点；**不重复投递**（`attempts` 行数不增）。
6. **双驱动先到先得**：对话侧先 `resume("吃了")` → 终态 `taken_reported`；随后调度侧再 resume **不抛** `TerminalStateConflict`、`attempts` 不增行。
7. **窗口内回答**：60 秒窗口内回答 → 直接终态，后续三个触达时点**不再触发**。
8. **时区**：`schedule_cron` 按 `Asia/Shanghai` 解释，`medication_events.scheduled_at` 存 UTC。

**A3 · 手工联调（一次跑通）**

- 造一条 `schedule_cron='* * * * *'`（每分钟）、`status='active'` 的药单 → 到点后查 `medication_events` **1 行**、`medication_attempts` **4 行**、`work_orders` **1 行**、`medication_events.terminal_state='missed_unconfirmed'`。
- 重启服务 → 不产生新的 `medication_attempts` 行。

**A4 · 门禁**

```
uv run pytest -m "not slow" -q                 # 全绿，零新增失败
uv run ruff check app/scheduler app/services/medication_due_service.py tests/test_scheduler_medication.py
uv run mypy app/scheduler app/services/medication_due_service.py    # 新增文件 0 错误
uv run python scripts/update_progress.py --check                    # 0
```

## 5. 幂等与并发约束（照抄即用）

| 键 | 约束 | 落点 |
| --- | --- | --- |
| 提醒幂等键 | `(plan_id, scheduled_at)` 唯一；同键异内容 → `MedicationConflict`（409 语义） | `medication_events` 唯一约束 + `create_event` |
| 触达幂等键 | `(event_id, attempt_no)` 唯一 | `medication_attempts` 唯一约束 |
| 投递防重放 | 图恢复会重放 `remind` → **先查后发**（已有 attempt 记录则跳过投递） | `medication_service.get_attempt` |
| 终态互斥 | 已定终态改写其他值 → `TerminalStateConflict`（不覆盖） | `medication_service.set_terminal` |
| 建单幂等 | 确定性 `order_no`（A12 走 B 方案，`work_orders` 无 `source_*` 列） | `work_order_service.open_missed_medication` |
| played 回执 | `voice_dispatched` → `played` 幂等升级；`device_offline` / `no_bound_device` **不可**洗白；played **不参与**重试与终态判定 | `medication_service.mark_attempt_played`（day07 contracts §3） |
| 图状态键 | `thread_id = medication:{reminder_id}`，**服务端生成** | `graph_config()` |

## 6. 开放点（开工前需主理人/相关线回话）

| # | 问题 | 建议裁决 | 不裁决的后果 |
| --- | --- | --- | --- |
| **O1** | PRD FR-MED-02 / AC-20 要求「A 有回执 → `missed_unconfirmed`；B 无回执 → `notification_failed`」，但当前子图 `finalize` 不区分收件回执，四次全 `device_offline`/`no_bound_device` 也记 `missed_unconfirmed`；`notification_failed` 只存在于表 CHECK 与 service 枚举里 | 本卡**不改子图**：统一记 `missed_unconfirmed`，把「无回执归 `notification_failed`」登记为技术债；若主理人要求本卡就改，需授权 A 线小改 `app/graphs/**`（§5 高风险文件） | 验收判据按「统一 `missed_unconfirmed`」写；AC-20 字面偏差留账，不阻塞本卡 |
| **O2** | 「到点计划」查询放哪：`knowledge_service` 强制 `elder_id` 过滤，调度器用不了 | 默认 C 侧新增 `app/services/medication_due_service.py`（只读、只列到点计划）；若主理人要求放 `medication_service.py`（A 线文件），需显式授权 A 线重开 | 白名单按默认走；A 线文件零改动 |
| **O3** | APScheduler 任务条数口径不一致：TD line 175 列 **6 项**（用药到点/19:00 采集/日报/记忆归并/漏采派单/72h 清理），`app/scheduler/__init__.py` docstring 列 **5 项**（缺「记忆归并」） | 本卡**只做「用药到点」一个 job**；条数口径请主理人在 Day 7-9 开工前统一（登记待澄清，不阻塞） | 本卡范围清晰，不受影响 |
| **O4** | 对话入口的 `medication_confirm` 工具执行器尚未绑定（本卡不实现） | 由 C 线 N13/N14 绑定，并与调度侧**共用**同一驱动模型与 `thread_id`（交接件 §3） | 联调时 scheduler 与 dialog 可能各自推进 `attempt_no` → 重复投递；A2-6 用例正是为拦这个 |

## 7. 与既有留白的关系

| 留白 | 本卡是否处理 | 说明 |
| --- | --- | --- |
| TD-1（WS 侧 TTS 为 `NullTtsQueue`） | ❌ **已由 day07 收口** | 触达语音已走 day07 contracts §1 的 `audio` 字段（16k s16le base64）；WS **回复流** TTS 仍为 `NullTtsQueue`（与本卡无关） |
| TD-2（主动触达无 played 闭环） | ❌ **已由 day07 收口** | `touch_played` → `mark_attempt_played` 已落地，迁移 0013；本卡只负责把 `attempt_no` 正确推进到 1/2/3/4，使 played 回执能对上行 |
| WD-1 / WD-2（两套建单口、`work_orders` 缺通用幂等列） | ❌ | 沿用 A 线唯一建单口 `open_missed_medication`，不新增第二套 |
| N10 卡状态 | 不动 | 已收官，保持 ✅；本卡不重开 A 卡 |

## 8. 风险与回退

| # | 风险 | 缓解 | 回退 |
| --- | --- | --- | --- |
| R1 | 子图工厂签名已变为 `tts_fn`（非 `speak_fn`），按旧签名接线必然踩空 | 接线前读合并后代码；`tts_fn` 缺省走真实信令层，**接线方不传它** | 撤掉本卡新增文件即可，无副作用 |
| R2 | 调度侧与对话侧同时 resume 同一 `thread_id` → `attempt_no` 双重推进、重复投递 | resume 前必查 `aget_state`：已有终态直接跳过；`attempt_no` 与预期不符则放弃本次推进；投递侧另有「先查后发」兜底 | 关闭 scheduler（`lifespan` 停机），退化为仅对话入口 |
| R3 | 60 秒窗口与 T0/+5/+10/+15 的时序写错 → 少触达或多触达 | 驱动模型写死在交接件 §2.3，用例 A2-2 断言 `attempts` 恰好 4 行 | — |
| R4 | 进程重启后重复建单/重复投递 | 图状态在 checkpointer（独立 schema），事件在业务表；重启后按 `thread_id` 恢复，先查后发 | 以 `medication_events` 为准人工核对，`order_no` 确定性可定位重复单 |
| R5 | cron 匹配写错（时区/跨天/多计划） | 只允许一种实现：APScheduler `CronTrigger.from_crontab(expr, timezone=settings.scheduler_timezone)`；用例 A2-8 | — |
| R6 | `notification_failed` 口径分歧在联调期反复 | 记 O1，先按统一 `missed_unconfirmed` 走，不在本卡改子图 | — |
| R7 | 触达消息已带 `touch_id` / `touch_attempt_no`（day07 contracts §1），但 **scheduler 不发消息**——消息由子图 `remind` 节点组装 | 接线方只负责**驱动**（起图 / resume），**不要**自己拼 `tts_text`；否则会出现第二套触达消息 | — |

## 9. 验收判据索引（与 §4 一致）

V-S1 静态分层 → V-S2 单测八条 → V-S3 手工一次跑通 → V-S4 门禁四命令。逐条可查、可复算。

## 10. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版（归 Day 7）。主理人采纳 A 侧建议「不移交 N10、另立新卡」后成卡：§0 归属裁决与「N10 验收口径不含触发」更正；§1 做/不做边界；§2 前置（E2 已偿清、**TD-1/TD-2 已由 day07 收口**、子图签名已改为 `tts_fn`、`medication_confirm` 未绑定、到点查询读服务缺）；§3 写作用域白名单与禁触清单；§4 五组验收判据（静态/单测 8 条/手工/门禁）；§5 幂等七键（含 played 回执）；§6 四个开放点 O1–O4；§7 与 TD-1/TD-2/WD-1/WD-2 的边界；§8 七条风险与回退（含 R7「scheduler 不自己拼触达消息」） |
