> 状态：已完成　｜　版本：v1.0　｜　日期：2026-11-10
> 关联：docs/team_tasks/day07/dev-c2-用药到点触发接线.md（新卡）｜docs/team_tasks/day06/dev-a-历史事件与工具事实.md（N10 已收官，不重开）｜docs/team_tasks/day07/contracts.md v1.0（触达消息与 played 回执）
> 上游依据：PRD line 332（FR-MED-02）、line 335（FR-MED-05）、line 336（FR-MED-06）｜技术设计文档 line 175、line 881、line 954｜AGENTS.md §7 硬性数值、§8 幂等
> 交付方向：dev-A（历史与工具层，已收官）→ dev-C（调度接线，新卡 dev-C2）

# 交接件 · `APScheduler 到点 → medication_confirmation_graph` 接线契约

> 这份是 A 给 C 的**接线说明**，不是新契约。字段以 `day07/contracts.md` v1.0 与
> `day06/contracts.md` v1.7 为准；本文只讲「什么时候调、传什么、谁写哪条事实」——
> 这三件事弄错，用药提醒会重复播、重复建单。

## 0. 一句话结论

**A 线只交接口，不交代码。** N10 子图本体已收官且**不重开**；C 侧新卡 `dev-c2` 按本文
§2–§4 接线即可，§5 是幂等红线，§6 是四个开放点（开工前要主理人回话）。

## 1. 现状基线（2026-11-10 实测，接线前不必复测）

| 事实 | 证据 |
| --- | --- |
| `app/scheduler/` 只有占位 | `app/scheduler/__init__.py` 仅 3 行 docstring，自标 Day 7-9；**无** `jobs.py`、无 `add_job`、无 `AsyncIOScheduler` |
| 子图**无生产调用点** | 全仓 `build_medication_graph` 引用只有：定义处、`__all__`、`tests/test_medication_graph.py` |
| 触达出口已就绪 | `app/audio/signaling.py: deliver(device_id, message) -> bool`；子图 `_default_deliver` 延迟 import 它 |
| 触达语音已接线（TD-1） | 子图 `_default_tts` → `app.audio.signaling.synthesize_touch_audio`；触达消息带 `touch_id` / `touch_event_id` / `touch_attempt_no` / `audio` |
| played 回执已闭环（TD-2） | `app/audio/signaling.py` 的 `touch_played` 分支 → `medication_service.mark_attempt_played` 幂等升级 `receipt` 为 `played`（迁移 `0013_touch_played_receipt`） |
| 子图可编译可恢复 | `app/graphs/medication_confirmation_graph.py` + `app/graphs/checkpointer.py`（独立 schema `langgraph`、独立连接池、`ensure_schema` 幂等） |
| 建单口唯一 | `app/services/work_order_service.py: open_missed_medication`（确定性 `order_no` 幂等） |
| 幂等键在库上 | `medication_events` UNIQUE`(plan_id, scheduled_at)`；`medication_attempts` UNIQUE`(event_id, attempt_no)` |
| 依赖已声明 | `pyproject.toml` 已含 `apscheduler>=3.10`（本地实测 3.11.3），**无需新增依赖** |

### 1.1 ⚠️ 子图工厂签名已变更（最容易踩空的一处）

```python
def build_medication_graph(
    *, session: Any, checkpointer: Any,
    deliver_fn: Any | None = None,     # 信令层下行投递
    tts_fn: Any | None = None,         # ★ 现为 tts_fn(text) -> bytes | None（合成出口）
) -> Any
```

第四个参数是 **`tts_fn`**（合成 PCM 字节），**不是**早先的 `speak_fn`（写 WebRTC 轨）。
两者都缺省用真实实现，**接线方一个都不用传**——照抄旧签名会直接 `TypeError`。

## 2. 接线契约：调用面（照抄即用）

### 2.1 A 线提供的公开入口（签名以合并后代码为准）

```python
# app/graphs/medication_confirmation_graph.py
def build_medication_graph(
    *, session: Any, checkpointer: Any,
    deliver_fn: Any | None = None, tts_fn: Any | None = None,
) -> Any

async def start_medication_task(
    graph: Any, *, reminder_id: uuid.UUID, plan_id: uuid.UUID,
    elder_id: uuid.UUID, deadline: str, triggered_by: str = "scheduler",
) -> dict[str, Any]      # 首轮跑到 wait_confirm 即停，返回里带 "__interrupt__"

async def resume_medication_task(
    graph: Any, *, reminder_id: uuid.UUID, answer: str,
) -> dict[str, Any]      # Command(resume={"answer": answer})

def graph_config(reminder_id: uuid.UUID | str) -> dict[str, Any]
# → {"configurable": {"thread_id": f"medication:{reminder_id}"}}   ← thread_id 服务端生成

MAX_ATTEMPTS = 4        # T0 / +5 / +10 / +15（AGENTS §7 硬性数值）

# app/graphs/checkpointer.py
@asynccontextmanager
async def medication_checkpointer(*, dsn: str | None = None, schema: str | None = None)
# → AsyncPostgresSaver；schema 默认 settings.langgraph_checkpoint_schema（"langgraph"）

# app/services/medication_service.py
async def create_event(session, *, plan_id, elder_id, scheduled_at,
                        drug_snapshot: dict, triggered_by: str) -> uuid.UUID
# 幂等键 (plan_id, scheduled_at)；同键异内容 → MedicationConflict；triggered_by ∈ {scheduler, dialog}
# drug_snapshot 形状照 {"drug_name": ..., "dose": ...}（话术取 drug_name）

async def record_attempt(session, *, event_id, attempt_no, receipt: str | None = None,
                          tts_result: str | None = None,
                          response: str | None = None, response_channel: str | None = None) -> None
# (event_id, attempt_no) 重复写是幂等无操作

async def get_attempt(session, *, event_id, attempt_no) -> dict | None   # 先查后发用
async def mark_attempt_played(session, *, event_id, attempt_no) -> bool  # TD-2 played 幂等升级
async def set_terminal(session, *, event_id, terminal_state, failure_reason=None,
                        retry_count=None, confirm_channel=None, late_taken=False) -> None
async def list_bound_device_ids(session, *, elder_id) -> list[uuid.UUID]

# app/services/work_order_service.py
async def open_missed_medication(session, *, elder_id, reminder_id, assignee_id) -> WorkOrderRef
```

> **写库唯一出口是 `app/services/`**。`app/scheduler/` 里不得出现 `session.add` / `session.commit` /
> `session.execute`，也不得 `import app.models`——依赖方向 `scheduler → services → models`（AGENTS §2）。

### 2.2 驱动链（一次触达的完整顺序）

```
APScheduler 到点（cron 命中，Asia/Shanghai）
  └─ ① 取到点计划：active + schedule_cron 命中 + start_date/end_date 覆盖今天
        └─ ② create_event(plan_id, elder_id, scheduled_at, drug_snapshot, triggered_by="scheduler")
              │    ↳ 同键已存在 → 返回原 reminder_id（不新建行）
              └─ ③ build_medication_graph(session=…, checkpointer=…)
                    └─ ④ start_medication_task(...) → 停在 wait_confirm（interrupt）
                          │   ↳ remind 节点：先查 attempt → 无记录才投递 → record_attempt(receipt)
                          │     投递消息由子图组装（含 touch_id/touch_attempt_no/audio），**scheduler 不拼**
                          └─ ⑤ 等 60 秒窗口内的回答
                                ├─ 有回答 → resume_medication_task(answer) → 终态分流
                                └─ 无回答 → 本轮 attempt 作废，按 T0/+5/+10/+15 进入下一轮
```

> **scheduler 只「驱动」，不「发消息」**。触达消息（`tts_text` + `touch_id` + `audio`）由子图
> `remind` 节点组装并投递；scheduler 若自己拼一条 `tts_text`，就出现第二套触达消息路径
> （违反 AGENTS §4-8「不新增第二种同类能力实现」）。

### 2.3 四个触达时点与 60 秒窗口（AGENTS §7 硬性数值）

| 轮次 | 时刻 | `attempt_no` | 无回答时的下一跳 |
| --- | --- | --- | --- |
| 1 | T0 | 1 | → 轮次 2 |
| 2 | T0 + 5 分钟 | 2 | → 轮次 3 |
| 3 | T0 + 10 分钟 | 3 | → 轮次 4 |
| 4 | T0 + 15 分钟 | 4 | → 终态 `missed_unconfirmed`（`retry_count=3`）+ 升级工单 |

- 窗口长度 **60 秒**；窗口内长者回答 → 该轮即命中，走 `taken_reported` / `refused`。
- 回答归一由 A 线 `_normalize` 负责：命中 `吃了/吃过了/taken…` → `taken_reported`；`没吃/不吃/refused…` → `refused`；其余（含空答）→ 按未确认重试。**C 侧不要自己再写一套词表**。
- `MAX_ATTEMPTS = 4`（A 线常量）。C 侧调度器**不要**再叠一层自己的重试上限，否则会出现「9 次触达」这类越界。

### 2.4 终态四值与失败归因

| 终态 | 触发条件 | `status` | `failure_reason` |
| --- | --- | --- | --- |
| `taken_reported` | 窗口内答「吃了」 | `confirmed` | —（`confirm_channel='voice'`） |
| `refused` | 窗口内答「没吃」 | `escalated` | — |
| `missed_unconfirmed` | 四次窗口皆无回答 | `escalated` | `no_answer` |
| `notification_failed` | **当前子图不产出**（见 §6 O1） | — | — |

- 触达回执白名单（迁移 0013 后七值）：`card_rendered` / `tts_finished` / `none` / `voice_dispatched` / `device_offline` / `no_bound_device` / **`played`**。
- `played` 语义（day07 contracts §3）：只允许从 `voice_dispatched` 升级；`device_offline` / `no_bound_device` **不可**被回执「洗白」；**played 不参与重试与终态判定**。
- 触达语音走 WS 帧 base64，**不进** WebRTC 音频轨、不进回复流播放队列（day07 contracts §1）。

### 2.5 双入口的同图约定

`triggered_by=scheduler`（本卡）与 `triggered_by=dialog`（C 线 N13/N14 + `medication_confirm` 工具）
走**同一张图**、**同一个 `thread_id`**、**同一 `attempt_no` 序列**（PRD FR-MED-05 / AC-35）。

> ⚠️ **`medication_confirm` 工具目前只注册未绑定**（`tool_registry.py` 有 spec，`executor=None`，
> 调用返回 `not_found`）。绑定属 C 线 N13/N14，本卡不实现。但**必须与调度侧共用同一驱动模型**：
> resume 前先 `aget_state` 看有没有终态、`attempt_no` 是否与预期一致（§3）。

## 3. 并发与重启的两条硬规矩

### 3.1 双驱动先到先得（resume 前必查状态）

调度侧与对话侧可能同时 resume 同一 `thread_id`。**规矩**：

```
resume 之前：
  1) state = await graph.aget_state(graph_config(reminder_id))
  2) 若 state.values 已有 terminal → 跳过本轮，不 resume、不投递
  3) 若 state.values["attempt_no"] 与本轮预期不符 → 说明已被另一侧推进 → 跳过
  4) 否则才 resume
```

即使漏了第 3 步，A 线还有一道兜底：`remind` 节点**先查后发**（`get_attempt` 已有记录则跳过投递），
所以不会重复播；但 `attempt_no` 会错位，所以第 3 步仍要做。

### 3.2 重启恢复

- 图状态在 **checkpointer（独立 schema `langgraph`、独立连接池）**，业务事实在**业务表**（`medication_events` / `medication_attempts` / `work_orders`）。
- 进程重启后：用同一 `thread_id`（`medication:{reminder_id}`）`aget_state` → 读得到等待节点 → 从原状态继续，**不重发已完成触达**（先查后发）、**不重复建单**（确定性 `order_no`）。
- **不要**在启动时对所有 active 药单无脑重跑 start：`create_event` 的幂等键会挡住重复建事件，但 `attempt_no` 会错位。恢复路径应以 checkpointer 的状态为准。

## 4. 接线方要新写的三块

| # | 内容 | 落点 | 判据 |
| --- | --- | --- | --- |
| ① | **到点计划只读查询**：列 `status='active'`、`schedule_cron` 命中当前时刻、`start_date ≤ 今天 ≤ end_date` 的计划 | 新增 `app/services/medication_due_service.py`（**不新建迁移**；不塞进 A 线 `medication_service.py`，除非主理人授权） | 能被单测直接调用；不写库 |
| ② | **APScheduler 任务注册**：`AsyncIOScheduler` + `CronTrigger.from_crontab(expr, timezone=settings.scheduler_timezone)`；在 `app/main.py` lifespan 内 `start()`，关闭时 `shutdown(wait=False)`；**绑 uvicorn 事件循环**（`app/scheduler/__init__.py` 已写明这条） | `app/scheduler/medication_jobs.py` + `app/main.py` | 启停成对；scheduler 层无写库语句 |
| ③ | **驱动与状态守卫**：按 §2.2 顺序调 A 线入口，按 §3.1 resume 前查状态 | `app/scheduler/medication_jobs.py` | `attempts` 恰好 4 行；重复触发不增行 |

> 口径提醒：`app/scheduler/__init__.py` docstring 列 **5** 个任务，TD line 175 列 **6** 个（多「记忆归并」）。
> 本卡只做「用药到点」一个 job，其余按 Day 7-9 另卡；条数口径请主理人统一（§6 O3）。

## 5. 幂等红线（照抄即用）

| 键 | 值 | 落点 |
| --- | --- | --- |
| 提醒 | `(plan_id, scheduled_at)` 唯一；同键异内容 → `MedicationConflict` | `medication_events` 唯一约束 + `create_event` |
| 触达 | `(event_id, attempt_no)` 唯一 | `medication_attempts` 唯一约束 |
| 投递 | 图恢复会重放 `remind` → **先查后发** | `medication_service.get_attempt` |
| 终态 | 已定终态改写其他值 → `TerminalStateConflict`（**不覆盖**） | `medication_service.set_terminal` |
| 建单 | 确定性 `order_no`（`work_orders` 无 `source_*` 列） | `work_order_service.open_missed_medication` |
| played | `voice_dispatched` → `played` 幂等升级；离线态不可洗白；不参与重试判定 | `medication_service.mark_attempt_played` |
| 图状态 | `thread_id = medication:{reminder_id}`（服务端生成） | `graph_config()` |
| 幂等键总口径 | 用药提醒键 `(resident_id, plan_id, scheduled_at)`、触达键 `(reminder_id, attempt_no)` | AGENTS §8 |

## 6. 开放点（开工前要回话）

| # | 问题 | 建议 | 影响 |
| --- | --- | --- | --- |
| **O1** | PRD FR-MED-02 / AC-20 要求「无回执 → `notification_failed`」，子图 `finalize` 不区分收件回执 | 本卡不改子图：统一 `missed_unconfirmed`，登记技术债；若要本卡就改 → 需授权 A 线改 `app/graphs/**`（§5 高风险文件） | 验收按统一 `missed_unconfirmed` 写 |
| **O2** | 到点查询放哪 | 默认 C 侧新文件 `app/services/medication_due_service.py` | A 线文件零改动 |
| **O3** | APScheduler 任务条数 5 vs 6 | 本卡只做用药到点；主理人统一口径 | 不阻塞 |
| **O4** | `medication_confirm` 未绑定；与调度侧共用驱动模型 | C 线 N13/N14 绑定，与调度侧共用 `thread_id` + `attempt_no` 序列 | 不共用 → 重复投递 |

## 7. 自检清单（C 可直接抄）

```bash
# 静态：scheduler 层不写库、不碰 models
grep -rn "session.add\|session.commit\|session.execute" app/scheduler/        # 期望 0
grep -rn "app.models" app/scheduler/                                          # 期望 0

# 依赖与循环
grep -n "apscheduler" pyproject.toml                                          # 期望已存在
grep -rn "AsyncIOScheduler\|CronTrigger" app/scheduler/                        # 期望命中

# 单测与门禁
uv run pytest tests/test_scheduler_medication.py -q
uv run pytest -m "not slow" -q
uv run ruff check app/scheduler app/services/medication_due_service.py
uv run mypy app/scheduler app/services/medication_due_service.py
```

数据库核对（手工一次）：

```sql
-- 一次跑通后应看到：events 1 行 / attempts 4 行 / work_orders 1 行
SELECT terminal_state, status, failure_reason, retry_count FROM medication_events ORDER BY created_at DESC LIMIT 1;
SELECT attempt_no, receipt, tts_result FROM medication_attempts ORDER BY attempt_no;
SELECT order_no, status FROM work_orders ORDER BY created_at DESC LIMIT 1;
```

## 8. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版（归 Day 7）。主理人采纳「不移交 N10、另立新卡」后成文：§1 现状基线（子图零生产调用点实测 + **TD-1/TD-2 已由 day07 收口** + **§1.1 子图工厂签名已改为 `tts_fn`**）；§2 调用面（A 线公开入口逐个签名 + 驱动链 + 「scheduler 不拼触达消息」+ 四时点/60 秒窗口 + 终态四值 + played 白名单七值 + 双入口同图）；§3 双驱动先到先得与重启恢复两条硬规矩；§4 C 侧要新写的三块；§5 幂等红线八条；§6 开放点 O1–O4；§7 自检清单与 SQL 核对 |
