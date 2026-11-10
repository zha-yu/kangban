# 交接件 · A→C：Day 7 调度接线测试修红

> 状态：已完成（8 条 fixture ERROR 与 10 个 lint 错已随 8c980aa/7565b6a 清零）　｜　版本：v1.0　｜　日期：2026-11-10
> 关联：`docs/team_tasks/day07/dev-c2-用药到点触发接线.md`（C2 卡 §4 验收判据）｜`docs/plans/day07/handoff-A-to-C-用药到点接线.md`（A 线接口契约，不重开）
> 触发：develop `4cefef6` 合入后，A 线跑全量门禁发现该测试文件全红 + lint 不干净

---

以下是可直接发送给 dev-C（或其 AI 执行端）的提示词正文。

---

# 任务：修红 Day 7 调度接线测试（C2 收口）

## 0. 你的角色与背景

你是 **dev-C**，负责 Day 7 的 C2「用药到点触发接线」。你的代码已随 develop 合入（`4cefef6`）：

- `app/scheduler/medication_jobs.py`（`install_scheduler` + 驱动与状态守卫）
- `app/services/medication_due_service.py`（到点只读查询）
- `tests/test_scheduler_medication.py`（8 条用例）

A 线（dev-A）在拉取 develop 后跑全量门禁，发现两件事：

1. `tests/test_scheduler_medication.py` 的 **8 条用例全部 ERROR**（fixture 构造处就炸，一条都没真正跑起来）；
2. 同一文件有 **6 个 ruff 错误**，pre-push 会被拦。

这两条让 C2 的「8 条用例通过」验收**实际上还没成立**，也挡住了其他人的推送门禁。

## 1. 唯一目标

让这 8 条用例**真的跑起来并通过**，并让该文件 lint 干净。

**不许**：放宽断言、`skip`、`xfail`、删用例、`try/except` 吞错来"变绿"。

## 2. 先复现（这一步不要改代码）

```bash
uv run pytest tests/test_scheduler_medication.py -q --tb=short
uv run ruff check tests/test_scheduler_medication.py --output-format=concise
```

预期看到：

```text
TypeError: 'room_number' is an invalid keyword argument for Room
  tests/test_scheduler_medication.py:101
ruff: 6 errors —— 17:1 I001、23:27 F401、32:5 F401、39:5 F401、46:61 F401、55:5 I001
```

## 3. 根因（已核实，不必再猜）

| 事实 | 证据 |
| --- | --- |
| `Room` 只有 `room_id` / `room_no` / `status` —— **既没有 `room_number`，也没有 `room_type`** | `app/models/organization.py:58` 起 |
| 全仓既有正确写法是 `Room(room_no="A-301")` | `tests/test_audio_security.py:71`、`tests/test_audio_utterance.py:83` |
| fixture 里这个 `room` **构造后从未被使用**（`seeded` 只返回 `elder_id` / `device_id` / `plan_id`） | `tests/test_scheduler_medication.py:101` 与 return 块 |

## 4. 修法（二选一，推荐 A）

- **A（推荐）**：直接删掉 `tests/test_scheduler_medication.py:101` 这行未被使用的 `Room(...)`，fixture 不再建房间。最小改动、不引入唯一键风险。
- **B**：确实需要房间时写 `Room(room_no=f"A-{uuid.uuid4().hex[:6]}")`。注意 `rooms.room_no` 有**全局唯一约束**，8 条用例若都插 `"101"` 会在同一库上撞唯一键。

改完**再跑**，遇到下一个字段不匹配（`MedicationPlan` / `Device` / `Elder` 的必填项等）时，**一律回 `app/models/` 读实际字段再改**，不要凭记忆、也不要让模型猜字段名。

## 5. 硬约束

**写作用域**

```text
tests/test_scheduler_medication.py        ← 你只改这里
```

**禁触**

```text
app/models/**                             表结构是既成事实，测试向模型对齐，不是反过来
app/services/medication_service.py        A 线已收官（N10 不重开）
app/services/work_order_service.py        A 线已收官
app/graphs/**                             A 线已收官（高风险：状态与断点恢复）
app/router/**  app/system_one/**          A 线 Day 7 在写
app/core/config.py                        A 线 Day 7 在写（已加 SYSTEM_ONE_* 兼容层）
alembic/**                                head 必须仍是 0013_touch_played_receipt
frontend/**                               dev-D 写锁
```

**不许改验收判据**（C2 卡 §4 的硬口径，修测试不等于改判据）：

- 一次跑通后 `medication_events` 1 行、`medication_attempts` **恰好 4 行**、`work_orders` **1 张**；
- 四次无回答 → `terminal_state='missed_unconfirmed'`、`status='escalated'`、`failure_reason='no_answer'`、`retry_count=3`；
- 重启恢复不重复投递（`attempts` 行数不增）；
- 双驱动先到先得：对话侧先 `resume("吃了")` → `taken_reported`，调度侧再 resume 不抛 `TerminalStateConflict`。

**不许改 `install_scheduler` 签名**：

```python
install_scheduler(app, *, session_factory, checkpointer_dsn=None,
                  checkpointer_schema=None, timezone_name="Asia/Shanghai") -> None
```

A3 已按这个签名在 `app/main.py` lifespan 里装配。要改先回报，不要单方面改。

**纪律**：`app/scheduler/` 里不得出现 `session.add` / `session.commit` / `session.execute`，不得 `import app.models`（依赖方向 `scheduler → services → models`）。

## 6. 技术栈与环境

- Python 3.12、pytest（`pytest-asyncio`）、PostgreSQL 15；
- 测试库由 `tests/conftest.py` 从 `DATABASE_URL` 推导 `<库名>_test`，**不得回退开发库**；PG 不可达时 fixture 会 skip —— 先确认 PG 起了（`docker compose up -d postgres redis`）；
- 包管理 `uv run ...`；ruff/mypy 按仓库配置。

## 7. 验收（逐条贴输出，缺一条不算完）

```bash
uv run ruff check tests/test_scheduler_medication.py     # 期望 0 errors
uv run pytest tests/test_scheduler_medication.py -q      # 期望 8 passed（0 skipped / 0 error）
uv run pytest -q -m "not slow"                           # 期望 1000 passed（当前基线 992 passed + 8 errors）
uv run alembic heads                                     # 期望单 head：0013_touch_played_receipt
uv run python scripts/update_progress.py --check         # 期望 0
```

## 7.5 口径更新（A 线 2026-11-10 已实现，你的断言要跟上）

1. **AC-20 `notification_failed` 已实现**（A 线小改 `finalize`，未重开 N10 卡）：
   写终态前查 `list_attempts` 的回执集合，若四次全为 `device_offline` / `no_bound_device`
   → 终态改判 `notification_failed`、`failure_reason='offline'`（白名单内，**无迁移**）；
   两种终态**都建工单**。
   → 因此你文件里「设备离线四次 → `missed_unconfirmed`」的断言**必须改成 `notification_failed`**；
   只有「绑了设备、下发成功、但四次都没人回答」才是 `missed_unconfirmed`。
2. **「60 秒窗口」是下限**：语义为「至少给满 60 秒才算本轮无回答」，由调度 tick 推动，
   **不要求**秒级对齐 T0+60.000s，也不要在 60 秒前判无回答。断言别写死"恰好 60s"。
3. **C1 验收补 resume 守卫**：resume 前 `aget_state` 先看 `terminal` 与 `attempt_no`（双驱动先到先得）；
   这条属 C1/C2 卡内修订，随本次一并写上。
4. **`session.commit()` 口径**：`app/scheduler/` 里若出现 `commit`，要么内聚进 service（推荐），
   要么在计划里写明"为何此处必须自己收口"。本日不阻塞，但要在报告里说明现状。

## 8. 报告格式（回给 A 线 / 主理人）

```text
任务：C2 调度测试修红
结论：已修红 / 仍阻塞
改动：<文件:行 + 一句话说明，例如 tests/test_scheduler_medication.py:101 删除未使用的 Room 构造>
归因：<测试错 / 实现错（附证据）/ 环境问题>
证据：
- ruff：<原始输出末行>
- 该文件 pytest：<passed/error 数>
- 全量 pytest：<passed/error 数>
- alembic heads：<输出>
未完成：<无 / 具体项 + 原因>
```

## 9. 联调提醒（A 线接口边界，别越界重写）

- 子图工厂签名：`build_medication_graph(*, session, checkpointer, deliver_fn=None, tts_fn=None)` —— 第四参是 `tts_fn(text)->bytes|None`，**不是 `speak_fn`**；
- `thread_id = medication:{reminder_id}`（`graph_config()` 服务端生成），对话侧与调度侧**共用同一 thread_id 与 attempt_no 序列**；
- **scheduler 只驱动、不发消息**：触达消息由子图 `remind` 节点组装（含 `touch_id`/`touch_attempt_no`/`audio`），scheduler 自己拼一条就是第二套触达路径；
- `resume` 前先 `aget_state` 看终态与 `attempt_no`（双驱动先到先得），投递侧另有「先查后发」兜底。

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版：A→C 交接（develop `4cefef6` 后 8 条 fixture error + 6 个 ruff 错误的根因、两种修法、禁触清单、五条验收命令与报告模板、A 线接口边界提醒） |
| v1.1 | 2026-11-10 | 增 §7.5 口径更新：AC-20 `notification_failed` 已由 A 线实现（offline 用例断言须改）、60 秒窗口=下限、C1 验收补 resume 守卫、`session.commit()` 口径 |
