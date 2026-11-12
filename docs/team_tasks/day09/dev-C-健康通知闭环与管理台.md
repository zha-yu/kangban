> 状态：已完成（C1–C4/C6 交付＋S6 联调实测收口；X3 形态对表与 eval_gate q3 题面迭代归 Day 10）　｜　版本：v1.4　｜　日期：2026-11-12
> 负责人：**dev-C（唯一）**　｜　任务包：**C · 健康通知闭环与管理台**　｜　容量：**17.5h ＝ 2.19 人日**（人设裁减后）
> 关联：docs/team_tasks/day09/day09-任务总览与DAG.md v1.3（§4.3 分工、§5 对接点、§6 写域）｜docs/team_tasks/day09/contracts.md v0.3（X1/X2/X3/X5/X7/X10/X11/X12 消费与提供；**X6 已裁减**）
> 上游依据：PRD line 357–361（分级三档原文＋校验）、line 365–374（FR-HLT-01~10）、line 378–392（告警与通知状态机，**line 387 只有客户端回执算 delivered**、**line 392 工单关闭后事件才可关闭**）、line 394–400（FR-ALT-01~05）、line 405–412（FR-RPT-01~04）、line 441（FR-ADM-07 人设）、line 297（FR-CON-06 点播历史）、line 458（FR-TC-08 `/metrics`）、line 487–505（AC-09/10/13/21/22/23/25/38）｜技术设计文档 line 1017–1018（分级在代码＋HITL）、line 1169（api/memory、api/stats、console 四页）、line 1172（人设/记忆/统计/导出功能验收）
> 遗留承接：**F01（notify 侧）F04 F05（service 侧）F07（复核落库）F11 F12 F13 F21（media_service）F25 F26 F30（生成侧）**
> 变更记录见文末

---

# dev-C · Day 9 任务书 · 健康通知闭环收口 ＋ console 管理台

## 0. 你的边界（先读这条）

### 0.1 写域仅限（越界即返工）

```
app/services/health_service.py
app/services/notify_service.py
app/services/report_service.py
app/services/media_service.py
app/services/work_order_service.py
app/services/safety_service.py
app/api/health_records.py
app/api/stats.py                      （新增，FR-ALT-05）
app/api/memory.py                     （新增，消费 X5）
app/api/metrics.py                    （新增，FR-TC-08）
app/api/alerts.py                     （增量：admin 重扫端点）
app/api/__init__.py                   （仅末尾追加 3 行 ROUTERS）
frontend/src/console/**
对应 tests
```

> **人设裁减（2026-11-12）**：原写域中的 `persona_service.py`／`elder_service.py`（人设绑定）／`app/core/context.py`（注入点）／`app/api/personas.py` **全部移除**——人设功能不做，这些文件本日无人持有。

### 0.2 禁触

`app/models/**`、`alembic/**`（A）、`app/graphs/**`、`app/scheduler/**`、`app/agents/**`、`app/router/**`、`app/main.py`（**B 独占**）、`app/services/memory_service.py`（B）、`app/services/tool_impls.py`／`tool_executor.py`（B）、`app/core/redact.py`／`logging.py`／`runtime_context.py`（A）、`frontend/src/pad/**`／`family/**`（D）、`app/api/audio.py`／`reports.py`／`family.py`（D）、`tests/eval_gate.py`（D）。**人设相关任何文件（`persona_service`／`elder_service`／`context.py`／`api/personas`）一律不碰。**

**两处特别注意**：
- **`app/scheduler/` 归 B**：C3 的日报时点与重试节奏，**你只提供退避函数 `next_retry_at`（X11），由 B 挂 cron**。你不改调度文件。
- **`app/api/` 禁 import `graphs`**（AGENTS §2.2）：F07 复核**只写库**，resume 由 B 的 scheduler 扫描完成（X7）。

### 0.3 高风险文件（AGENTS §5：先出方案再落代码＋人工逐行读 diff）

`app/services/safety_service.py`（安全一票否决相邻）。**必须先出方案经确认后才落代码。**（原 `context.py` 因人设裁减不再涉及。）

### 0.4 角色口径

本包**不含**独立测试/产品/设计角色工作。单测与接口测试是开发内嵌交付物；console 三页按**既有页面风格与 PRD 既有要求**实现（原生 CSS＋`var()` 引 tokens.css，**不引组件库**，PRD line 88／v2.7 裁决），不产生新设计工作。

---

## 1. 节点 C1 · 血压分级回正 ＋ 计划件回改（2h）

**前置**：N0。**无其他前置，D9 上午第一个开工（B3 在等 X2）。**
**对接**：**提供 X2**（分级签名＋`rule_version='g2'`）给 B3。

### 1.1 现状实测（根因在计划件，不是笔误）

**代码侧**：
- `app/services/health_service.py:70-96` 常量：`RULE_VERSION="g1"`、`SYSTOLIC_RED_UPPER=180`、**`SYSTOLIC_RED_LOWER=90`**、`DIASTOLIC_RED_UPPER=120`、**`DIASTOLIC_RED_LOWER=60`**、`SYSTOLIC_YELLOW_LOWER=140`、`DIASTOLIC_YELLOW_LOWER=90`、`SLEEP_YELLOW_HOURS=5.0`、`MOOD_YELLOW=2`、`SYSTOLIC_GREEN_UPPER=139`、`DIASTOLIC_GREEN_UPPER=89`、**`SLEEP_GREEN_UPPER=12.0`**。
- `:150-159`：`if valid >= SYSTOLIC_RED_UPPER or valid <= SYSTOLIC_RED_LOWER: → RED`（舒张压同构于 `:162-171`）→ **90 判红**（PRD 属绿）、**<90 判红**（PRD 属黄）。
- `:183-186` 睡眠 >12h 判黄（**无 PRD 依据**）。
- `:214` 注释「complaint…当前版本仅参与日志，不单独触发升级」＋`:238-239` `if complaint: logger.debug(...)` → **非急症主诉不判黄**。

**计划件侧（同样写错，必须同提交回改）**：
- `docs/plans/day08/dev-a-数据分级规则层.md:170-171`「DIASTOLIC_RED: 120 # 舒张压 ≥120 **或 ≤90（低血压）**」
- `:179`「SYSTOLIC_LOW_NORMAL: 90 # 收缩压 ≤90 触发判红」
- `:214`「红：… OR 收缩压 ≤90 OR 舒张压 ≤60」
- `:217`「绿：… 睡眠 **5–12h**」

### 1.2 目标口径（PRD line 357–359 原文，唯一依据）

| 等级 | 规则 |
| --- | --- |
| **红** | 收缩压 **≥180** 或 舒张压 **≥120**；或命中急症症状规则。**低血压不判红** |
| **黄** | 收缩压 140–179；舒张压 90–119；**收缩压 <90**；**舒张压 <60**；睡眠 <5h；mood ≤2 且未命中急症；**非急症主诉**；任一采集项缺失 |
| **绿** | 收缩压 **90–139** 且 舒张压 **60–89**，其他无异常 |

PRD line 361：睡眠 **>24h** 才无效（**不是 >12h**）；无红分量但血压任一分量缺失判黄；每次分级保存 `rule_version`。

### 1.3 要做什么

1. 删除 `SYSTOLIC_RED_LOWER`／`DIASTOLIC_RED_LOWER` 两常量及其判红分支；`<90`／`<60` 改判黄。
2. 删除 `SLEEP_GREEN_UPPER=12.0` 与 >12h 判黄分支；睡眠上界只保留 **>24h 无效**校验。
3. `complaint` 非空且未命中急症 → **判黄**（删除「仅参与日志」注释与 `logger.debug` 占位）。
4. **`RULE_VERSION` 由 `g1` 升 `g2`**；既有 `g1` 记录**不回改**，保留历史可追溯。
5. **同提交回改 `docs/plans/day08/dev-a-数据分级规则层.md`**：修正 line 170-171/179/214/217，并在该件文末变更记录新增一条，写明「依据 PRD line 357-359 回改，原稿把低血压误写为判红、自加睡眠 12h 上界」。**不许静默改计划件**（AGENTS §10）。
6. 既有测试若断言了错误阈值（如期望 90→red），**改测试期望值而非改 PRD 口径**，提交正文说明依据。

### 1.4 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `health_service.py` 阈值与主诉分支修正 |
| 2 | `docs/plans/day08/dev-a-数据分级规则层.md` 回改＋变更记录（**同提交**） |
| 3 | 9 个边界值参数化测试 |
| 4 | 主诉/睡眠上界/缺失判黄测试 |

### 1.5 量化验收（9 个边界断言，逐条可执行）

| 输入（收缩压/舒张压） | 期望 | 依据 |
| --- | --- | --- |
| `90/60` | **绿** | PRD:359 |
| `89/60` | **黄** | PRD:358 收缩压 <90 |
| `90/59` | **黄** | PRD:358 舒张压 <60 |
| `139/89` | **绿** | PRD:359 |
| `140/89` | **黄** | PRD:358 |
| `90/90` | **黄** | PRD:358 舒张压 90–119 |
| `180/80` | **红** | PRD:357 |
| `120/120` | **红** | PRD:357 舒张压 ≥120 |
| `179/119` | **黄** | PRD:358 |

| 其他判据 | 期望 |
| --- | --- |
| 主诉「有点头晕」＋血压正常 | **黄** |
| 睡眠 13h＋其余正常 | **绿**（不再误判黄） |
| 睡眠 25h | **无效标缺失 → 黄** |
| `grep -n "RED_LOWER\|SLEEP_GREEN_UPPER" app/services/health_service.py` | **零命中** |
| 新记录 `rule_version` | **`g2`** |
| `SYSTOLIC_RED_UPPER=180`／`DIASTOLIC_RED_UPPER=120` | **不变**（PRD §7 硬性数值） |

```bash
uv run pytest tests/test_health_service*.py -q
uv run ruff check app tests && uv run ruff format --check app tests
```

**CP-C1**：报告 9 个边界实测表＋计划件回改 diff 摘要。**须 D9 12:00 前交付 X2**（B3 在等）。

---

## 2. 节点 C2 · 红档建单双通知 ＋ 升级对象 ＋ 事件关闭联动 ＋ 域清理钩子（5h）

**前置**：C1。**对接**：**提供 X3**（`notify_red_dual`）给 B3、**X12**（四个域 purge 钩子）给 A3；消费 X9（错误体/审计口径）。

### 2.1 现状实测

- `app/services/health_service.py:396-422`：建 `RiskEvent(type="health_alert", severity=grade.severity, source_type="health_record", source_id=record.record_id, status="open")` 后，`:410` **只有 `if grade.severity == SEVERITY_YELLOW:` 分支建单**；注释写「红档走 safety_service（dev-C），本函数不重复建单」。**该模块从未 import `notify_service`** → REST 录入的红档**不建单、不通知**，风险事件永久 open。
- `app/services/notify_service.py:718-770 escalate_red_event`：`:739` 正确写 `event.status="escalated"`（不建第二事件/工单 ✓），但 `:759 send_staff_notifications(session, elder_id=event.elder_id, ntype="escalated_red_event", …)` → 走 **primary_care 护工**，**不是 admin**。
- `:692-715 scan_escalations`：docstring 声称「无 acknowledged_at」，实际 WHERE 只有 `status=="open"`／`severity=="red"`／`created_at < cutoff`。
- `risk_events.acknowledged_at`／`closed_at` 在 `app/services/safety_service.py:82-83` 与 `:107` 声明，**从不写入**；`closed_at` 全仓唯一写入点是 `app/services/work_order_service.py:387`（写的是**工单**）。
- `safety_service.escalate_safety_event` **已合规接线**（`main.py:94/:412`、`dialogue_service.py:439`），语音急症 AC-04 通路成立——**不要动它**。

### 2.2 要做什么

1. **F05 红档建单＋双通知**：`health_service.py` 补红档分支——`open_work_order(severity="red", dedupe_token=f"health_record:{record_id}:red", assignee=resolve_primary_care_staff(...))` ＋ 新增 **`notify_red_dual`**（X3 签名）同时发**责任护工＋监护人各 1 条**。
2. **去重**：语音路径（B 的子图）与 REST 路径共用同一 `dedupe_token`；同键重复 → 返回原结果；同键异内容 → **409**。**建单统一由你这边做，B 的子图不另建一单**。
3. **F12 升级对象改 admin**：`escalate_red_event` 的 `:759` 改为按 **admin 角色**查收件人，不用 `resolve_primary_care_staff`；保持「不建第二事件/第二工单」。
4. **F12 扫描条件**：`scan_escalations` WHERE 补 `acknowledged_at IS NULL`，与 docstring 一致。
5. **F13 事件确认/关闭联动**：告警确认路径写 `risk_events.acknowledged_at`；**工单关闭时**联动关闭关联 `risk_events`（写 `closed_at`），**顺序不可反**；未关工单时拒绝直接关事件（**409**）。
6. **X12 四个域清理钩子（1h）**：`health_service`／`notify_service`／`report_service`／`work_order_service` 各增 `purge_elder_data(session, *, elder_id, reason) -> PurgeRef`。**`reason=checkout` 时健康数据脱敏归档（调 A 的 `redact.py`，不物理删）；`reason=teaching_reset` 时清除**。**审计由 A 统一写，你不重复写**；保留账号/角色/配置；同键幂等。**A 不直写你的表，你也不直写 A/B 的表。**

### 2.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `health_service.py` 红档建单分支＋`notify_red_dual` |
| 2 | `notify_service.py` 升级对象改 admin＋扫描补确认过滤 |
| 3 | `safety_service.py`／`work_order_service.py` 事件确认与关闭联动 |
| 3b | 四个域 `purge_elder_data` 钩子（X12）＋单测 |
| 4 | 两路径（REST／语音）幂等去重集成测试 |
| 5 | X3 签名冻结与联调证据（与 B3） |

### 2.4 量化验收

| 场景 | 期望 | 依据 |
| --- | --- | --- |
| REST 录入 `185/95` | `risk_events` **1** 条 red open ＋ `work_orders` **1** 条 red ＋ `notifications` **2** 条（护工＋监护人各 1） | PRD:357／FR-ALT-01／AC-38 |
| 语音路径同输入 | 同样 **2** 条通知，**不产生第二事件/第二工单** | AC-22／AC-38 |
| 同 `record_id` 重复提交 | 幂等返回原结果；同键异内容 **409** | AGENTS §8 |
| 红档 2 分钟未确认 | 原事件 `escalated` ＋ **admin 收到通知**、护工不重复收；`risk_events` **行数不变** | PRD:385 |
| 已确认事件 | **不被升级扫描命中** | F12 |
| 工单 open 时关事件 | **409** | PRD:392 |
| 工单 closed | 事件 `closed_at` **非空** | PRD:392 |
| 确认告警 | `acknowledged_at` **非空** | F13 |
| 通知状态 | 遵循 X1：**无回执时 `pending`，不得直接 `delivered`** | PRD:387 |

```bash
uv run pytest tests/ -q -k "health_service or notify or work_order or safety"
```

**SQL 核对模板**（联调留证用）：
```sql
SELECT severity, status FROM risk_events WHERE source_id = :record_id;      -- 期望 1 行 red/open
SELECT severity, status FROM work_orders WHERE event_id = :event_id;        -- 期望 1 行 red
SELECT target_type, status FROM notifications WHERE elder_id = :elder_id
  AND created_at > :t0;                                                     -- 期望 2 行：staff + guardian
```

**CP-C2**：报告两路径三表行数对照＋幂等重放结果＋升级收件人角色。**须 D10 15:00 前冻结 X3 并联调**（B3 在等）。

---

## 3. 节点 C3 · 送达真链路 ＋ 日报时点与重试节奏（4h）

**前置**：N0（X1 帧形状已冻结）。**软依赖**：D1 的回执生产者（**并行开发，不等待**）。
**对接**：**消费 X1**（回执帧 → `mark_notification_delivered`）；**提供 X11**（`next_retry_at` 退避函数）给 B 挂 cron。

### 3.1 现状实测

- `app/services/notify_service.py:785-803`：
  ```python
  pending = await scan_pending_notifications(session, now=now)
  for notification in pending:
      notification.retry_count += 1
      notification.status = _STATUS_DELIVERED      # ← 无条件伪造送达
      notification.sent_at = now
  ```
  **无任何回执校验**，`failed` 分支永不可达。
- 真实入口 `:551 mark_notification_sent`／`:566 mark_notification_delivered` **只有定义与 `__all__`（`:850-851`），生产调用零次**。
- `:45 RETRY_INTERVAL_S = 30` **未被使用**；实际由 `health_jobs.py:198-211` 的 `CronTrigger.from_crontab("* * * * *")`（60s）驱动。
- `app/scheduler/health_jobs.py:59 _DAILY_REPORT_FIRE = "21:30"`（标注自裁）；`:438` 是 `generate_daily_report` 唯一调用点。**上游 day-08 计划 H4/H8 与 dev-B/C 任务卡均明写 20:00** → 21:30 属偏离已冻结计划，非有效自裁。
- `app/api/reports.py` 只有 3 个 GET；`grep resend|重发|manual_retry` **零命中**。

### 3.2 要做什么

1. **F01 送达真链路**：扫描逻辑改为「发送 → 置 `pending` ＋ `retry_count+1`」，**删除无条件 `delivered` 赋值**；`retry_count >= 3` 置 `failed`。`delivered` **只由 `mark_notification_delivered` 写入**（由 D1 的信令收帧调用，X1）。`is_mock=true` 不计入送达率分母（`:8` 既有口径保持）。
2. **F11 日报时点**：提供 20:00 口径给 B（`_DAILY_REPORT_FIRE` 由 B 改，**你不动调度文件**）；采集完成即生成首版的触发保持。
3. **F11 两条退避序列（X11）**：实现 `next_retry_at(*, ntype, retry_count, now) -> datetime | None`：
   - 告警通知（`health_alert`／`escalated_red_event`／`medication_*`）→ **30s / 60s / 90s**，3 次后 `failed`（契约 §3.2）
   - 日报推送（`report_ready`／`report_updated`）→ **1min / 5min / 15min**（PRD §4.8 FR-RPT-03）
   - 返回 `None` 表示次数用尽 → 调用方置 `failed`
4. **管理员手动重发**：在 **`app/api/alerts.py`**（你持锁）增 admin-only 端点，**仅触发一次既有 `scan_pending_notifications`**，**不含独立重试逻辑**（遵 Q-D3 裁决，总览 §5.4 Q3）。

### 3.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `notify_service.py` 扫描逻辑修正＋`mark_notification_*` 接线 |
| 2 | `next_retry_at` 退避函数（X11）＋单测 |
| 3 | admin-only 重扫端点（落 `app/api/` 你持锁的文件，或提需求给 A/D） |
| 4 | 与 D1 的 X1 联调证据 |
| 5 | 与 B 的 X11 挂载联调证据 |

### 3.4 量化验收

| 判据 | 期望 | 依据 |
| --- | --- | --- |
| 无回执时扫描 3 次 | `status='failed'`，**永不出现 delivered** | PRD:387／契约 §3.2 |
| 有回执 | `delivered` ＋ `sent_at` 取**回执时间** | AC-23 |
| `grep -n "_STATUS_DELIVERED" app/services/notify_service.py` | **只在 `mark_notification_delivered` 内出现** | F01 |
| 状态序列 | `generated → pending → delivered → acknowledged` 依次可见；**送达≠确认，确认≠工单完成** | AC-23 |
| 客户端先离线后上线回执 | 离线期 `pending`，上线回执后 `delivered`，再人工确认 `acknowledged` | AC-23 原文 |
| 日报首版时点 | **20:00**；`grep -n "21:30" app/scheduler/health_jobs.py` **零命中**（B 改，你核对） | PRD §4.8 |
| 告警重试时间点 | **30s / 60s / 90s** 可断言 | 契约 §3.2 |
| 日报重试时间点 | **1min / 5min / 15min** 可断言 | PRD §4.8 |
| `is_mock=true` | **不计入送达率分母** | `notify_service.py:8` |
| 补录新版本 | 旧版保留、每版只发一次更新通知 | AC-25 |
| 日报内容 | **不读 `memory_chunks`**（`grep` 零命中）；含固定声明「教学模拟信息，不构成医疗诊断」 | 任务卡 C2 明禁／PRD §4.8 |

```bash
uv run pytest tests/ -q -k "notify or report"
```

**CP-C3**：报告两条退避序列实测时间点＋无回执落 failed 的证据＋X1/X11 联调记录。

---

## 4. 节点 C4 · 复核落库 ＋ 语音条 ＋ media 接线 ＋ /metrics ＋ stats ＋ memory API（4h）

**前置**：C2。**软依赖**：B2 的 X5、D5 的 X10（**骨架先行**）。
**对接**：**提供 X7**（`review_status` 写入）给 B3；**消费 X5/X10**；提供 `audio_url` 给 D4。

### 4.1 现状实测

- `app/api/health_records.py:505` 与 `:544` 直调 `health_service.review_health_record`，**无 graph resume**（正确——api 禁 import graphs；缺的是 B 侧扫描）。
- `app/models/report.py:88 audio_url` 声明；`app/api/reports.py:109/:218` 只读；`app/services/report_service.py` **无任何写入** → 恒 null。
- `app/services/media_service.py:123 list_media`／`:148 search_media`／`:181 get_playback_url`／`:202 start_playback`／`:229 finish_playback`／`:264 list_media_history` —— `app/` 内**全部零调用**。`app/services/tool_impls.py:326`「record_playback（未交付），过渡期退化为**随机换一首**」、`:333`「**不写 media_history**」、`:380-393 order_by(func.random()).limit(1)`。
- `grep metrics app/api/*.py app/main.py` **零命中**；`app/services/metrics_service.py:191 get_metrics_recorder` 只有 recorder。
- `app/api/stats.py` **不存在**。

### 4.2 要做什么

1. **F07 复核落库（X7）**：复核端点写 `review_status ∈ {confirmed, corrected}`＋`reviewed_by`＋`review_note`／`superseded_reason`（**全部既有列，无 `reviewed_at`，不新增 schema**）。**不 import graphs、不自行调 resume**。
2. **F30 日报语音条**：`report_service.py` 接 TTS 生成并写 `audio_url`（FR-RPT-04，P1）。
3. **F21 media 接线**：把 `media_service` 六个零调用函数接上真实消费方——**`tool_impls.py` 归 B**，故你**提供函数＋签名**，由 B 改调用侧（X10）。**Tool 层不写 `media_history`，history 归你的 `media_service`**（契约 §6 冻结口径）。「换一段」改**顺序续播**而非随机。
4. **F26 `/metrics`**：新增 `app/api/metrics.py`，输出分层命中率／S1 闭环率／token 消耗（TD:1182 口径）；鉴权按既有口径。
5. **FR-ALT-05 统计**：新增 `app/api/stats.py`——本周红色事件数、平均确认时长、各楼栋漏采次数。
6. **记忆管理 API**：新增 `app/api/memory.py`，薄路由消费 B 的 X5 三函数；**越权由 B 的 service 层拒绝＋审计，你不重复实现隔离逻辑**。
7. **ROUTERS 追加 3 行**（stats／memory／metrics），只增不改。（原 personas 行随人设裁减移除。）

### 4.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `api/health_records.py` 复核写 `review_status`（X7） |
| 2 | `report_service.py` TTS 语音条＋`audio_url` 写入 |
| 3 | `media_service.py` 六函数接线＋顺序续播＋history 写入 |
| 4 | `app/api/metrics.py`、`app/api/stats.py`、`app/api/memory.py`（三新文件） |
| 5 | `app/api/__init__.py` 追加 3 行 |
| 6 | 接口测试（权限 × 统计一致性 × 记忆越权） |

### 4.4 量化验收

| 判据 | 期望 | 依据 |
| --- | --- | --- |
| `grep -n "from app.graphs" app/api/` | **零命中** | AGENTS §2.2 |
| 复核后 | `review_status` 落 `confirmed`/`corrected`，B 侧 ≤30s resume | X7 |
| 日报 `audio_url` | **非空且可播** | FR-RPT-04 |
| `media_history` | 播放后**有行** | FR-CON-06 |
| 「换一段」 | **顺序续播**（下一首），非随机 | F21 |
| `curl /metrics` | 返回分层命中率／S1 闭环率／token 消耗；未授权 **401** | FR-TC-08／TD:1182 |
| 统计页三项指标 | 与 SQL 复算**一致**（本周红色事件数／平均确认时长／各楼栋漏采次数） | FR-ALT-05／TD:1172 |
| 记忆 API 越权 | 跨长者 `chunk_id` → **403＋审计** | AC-39② |
| ROUTERS | 增 **4** 行；`main.py` **未被修改** | 装配归 B |

```bash
uv run pytest tests/ -q -k "reports or media or metrics or stats or memory_api"
```

**CP-C4**：报告 9 条判据实测＋X5/X7/X10 联调记录＋统计 SQL 复算对照。

---

## 5. 节点 C5 · 人设切换 —— **已裁减，不做**

> **2026-11-12 主理人决定：人设功能（FR-ADM-07）移出 Day 9。** 本节点取消，不交付 `get_active_persona`／`switch_persona`／`api/personas.py`／`context.py` 注入／TTS 音色切换；X6 契约作废；C6 不再做人设配置页。裁减依据与留痕见总览 §8.1（PRD §6.2 人设属 P1 可裁减＋§7 裁减有记录）。
>
> **现状留档（仅供将来恢复时参考，本次不动）**：`personas` 表已建（`models/organization.py:39-54`）、`seed_personas.py` 已入库、`elder_service.py:244` 校验 persona_id、`context.py:189-194` 有注入模板但**全仓 `PersonaContext(` 零构造点（死代码）**、`greeting` 零消费、TTS 用全局 `tts_voice_id`、console 人设页 `enabled: false`。**这些既有代码与表一律保留，console 人设入口保持置灰。**
>
> 释放的 3h 计入 C 包缓冲（C 包 21h→17.5h），可用于支援全员联调。

---

## 6. 节点 C6 · console 管理台三页 ＋ 空 catch 修复（2.5h：骨架 1.5h＋联调 1h）

**前置**：C4。**软依赖**：A4（生命周期/审计端点）、B2 的 X5（**骨架先行，按冻结契约先建页**）。
**对接**：消费 A4 端点、X5；**M5.4 的 C 侧判据**。**人设配置页已裁减，不做。**

### 6.1 现状实测

- console 现有 11 页（`AdminDevicesPage`／`AdminRoomsPage`／`AdminUsersPage`／`AlertsPage`／`EldersPage`／`HealthFormPage`／`LoginPage`／`MedicationEditPage`／`MedicationPage`／`ReviewQueuePage`／`WorkOrdersPage`）。
- **「教学数据重置 / 人设配置 / 审计日志」三入口置灰**（`App.tsx:34`、`layout/AppLayout.tsx:8,39`）；**无记忆管理页、无统计页**。本次只解除**重置与审计**两处；**人设入口保持置灰**。
- `frontend/src/console/pages/WorkOrdersPage.tsx:271-274`：
  ```javascript
  } catch {
    // Mock 层不会失败；真实接口接入后此处改为行内错误提示
  }
  ```
  吞掉 403/422（同文件 `:256` 另一路径已用 `MockApiError` 处理）。
- 医生趋势图**已真实落地**（`TrendChart.tsx:20` 真实 fetch＋Chart.js 三序列、`ReviewQueuePage.tsx:222-229` 按需展开）——**不要重做**。

### 6.2 要做什么

1. **三个新页**（原生 CSS＋`var()` 引 tokens.css，**不引组件库**）：
   - **教学重置与退房页**：两条流水线入口，**二次确认弹窗**后才发请求（消费 A4）；away 暂离开关
   - **记忆管理页**：列表／纠正／删除（消费 `api/memory.py`），删除后列表即时移除
   - **统计与审计页**：三项统计指标（消费 `api/stats.py`）＋审计日志表＋**导出 CSV 按钮**（消费 A4）
2. **解除置灰**：`AppLayout.tsx` 的**重置与审计**两处 `enabled: false` → `true`；**人设配置保持 `false`**。
3. **F25 空 catch 修复**：`WorkOrdersPage.tsx:271` 改行内错误提示，403/422 文案区分；删过期注释。

### 6.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | 三个新页组件＋路由＋侧栏重置/审计入口解除置灰 |
| 2 | 对应 `*-api.ts`（按冻结契约，不临时改字段） |
| 3 | `WorkOrdersPage.tsx` 行内错误提示 |
| 4 | 走查截图（三页 × 关键操作）＋`tsc`/`build` 输出 |

### 6.4 量化验收

| 判据 | 期望 | 依据 |
| --- | --- | --- |
| 三页可达 | 侧栏重置/审计/记忆/统计 `enabled: true`，路由可进；**人设仍置灰** | TD:1169（人设裁减） |
| 重置/退房 | **二次确认弹窗**后才发请求；取消则零请求 | FR-ADM-02／AC-19 |
| 退房后 console 状态 | 该长者在列表/详情显示 **archived**（或已退房态），相关操作入口不可再点；与后端 `elders.status` 一致 | FR-ADM-09／E8 状态语义 |
| **退房→pad/family 端到端（联调）** | C 在 console 触发退房后，**联合 D 确认**：pad 持旧 token → 401 回登录/未授权态；原绑定家属查日报 → 403「无权查看该长者的数据」/空态。**C 只负责触发与 console 侧断言，pad/family 表现由 D 确认，C 不改 pad/family** | FR-ADM-09／AC-24／FR-RPT-02 |
| 记忆删除 | 列表即时移除；再查不再召回 | TD:1172 |
| 导出按钮 | 下载 CSV，内容与审计表一致 | FR-ADM-05／TD:1172 |
| 统计页 | 三项指标可见且与后端一致 | FR-ALT-05／TD:1172 |
| 越权操作 | 显示 **403 文案，不静默** | F25 |
| 构建 | `tsc` 与 `vite build` **全绿** | 门禁 |
| 包隔离 | `grep -r "three-vrm" dist/console dist/family` **零命中** | AGENTS §2.4 |
| 样式 | 全部 `var()` 引 tokens.css，**无第三方组件库 import** | PRD:88／v2.7 裁决 |

```bash
pnpm --dir frontend exec tsc --noEmit
pnpm --dir frontend build
grep -r "three-vrm" frontend/dist/console frontend/dist/family   # 期望零命中
```

**CP-C6**：报告三页走查截图＋二次确认取消零请求证据＋构建输出。**C6 完成即 M5.4 的 C 侧判据达成。**

---

## 7. 执行记录与对接登记（2026-11-12 已回填）

| 日期 | 节点 | 提交 | 验收结果 | 等待项 |
| --- | --- | --- | --- | --- |
| 2026-11-12 | C1 | `fix/health C1` 等 2 笔（详见计划件 §9-S1/S2） | 49 passed（9 边界参数化＋否定归一化）；`RED_LOWER`/`SLEEP_GREEN_UPPER` 零命中；`rule_version='g2'`；day08 计划件回正 v1.5；X2 v0.4 已发 dev-B | — |
| 2026-11-12 | C2 | 2 笔（S2） | 全量 1428 passed；`notify_red_dual` 三段幂等键＋升级改 admin＋确认/关闭联动＋F13 admin 关闭端点＋四域 `purge_elder_data`；REST 全链路实测三表行数达标（1 事件/1 工单/2 通知） | — |
| 2026-11-12 | C3 | `252e9b3` | 全量 1434 passed；`delivered` 写点收敛唯一（X1 回执驱动）；X11 `next_retry_at`（30/60/90s＋1/5/15min）；admin-only rescan 端点 | X11 挂 cron 后 live PASS（见 S6） |
| 2026-11-12 | C4 | `5d539c4` | 全量 1445 passed；F07/X7 验证在位（Day 8 落地）；F30 语音条（data URL 承载）；F21 `get_next_media` 顺序续播；F26 `api/metrics.py`；FR-ALT-05 `api/stats.py`；`api/memory.py` 骨架＋ROUTERS 追加 3 行 | F26 token_usage 后经 `fb83909a` live 闭环 |
| — | ~~C5~~ | — | **已裁减，不执行** | — |
| 2026-11-12 | C6 | `466ccd4` | console 三页（重置退房/记忆/统计审计）＋F25 空 catch 修复；tsc/build 全绿；three-vrm 包隔离零命中；A4 teaching-reset elder_id 错位修复 `7c87f713` 闭环 | — |
| 2026-11-12 | S6 联调 | 合并 `fb9c529`＋Laya/F26 `fb83909a`（C 代码） | 全量 1660 passed；X1/X11/X5/A4 live 全链路 PASS；语音红档阶段 0＋1 真机全链路 PASS；执行细节见计划件 `docs/plans/day09/dev-c-day09-工作推进计划.md` §9 | X3 语音/REST 形态对表 D10 12:00；eval_gate q3 题面迭代归 Day 10 |

**对接登记**：

| 对接点 | 对方 | 我提供/消费 | 冻结时点 | 联调时点 | 状态 |
| --- | --- | --- | --- | --- | --- |
| X2 分级签名 g2 | B3 | **提供** | N0 | D9 12:00 | 已交付（v0.4 含主诉归一化口径） |
| X3 红档双通知出口 | B3 | **提供** | N0 | D10 15:00 | 已交付（v0.6）；语音/REST 通知形态对表 D10 12:00 |
| X1 回执帧 | D1 | 消费 | N0 | D10 12:00 | 联调 PASS（live：回执→delivered→幂等→acknowledged） |
| X12 域清理钩子（四个） | A3 | **提供** | D9 下午 | D10 16:00 | 已提供（lifecycle 接线 stub 待 A 侧收口） |
| X11 退避函数 | B（cron 持锁） | **提供函数** | N0 | D10 15:00 | 已交付＋cron 自动重试 live PASS |
| X7 review_status | B3 | **提供** | N0 | D11 14:00 | 已验证在位（签名证据可交 B3） |
| X5 记忆管理函数 | B2 | 消费 | D10 上午 | D11 10:00 | 联调 PASS（live GET/PATCH/DELETE＋越权 403） |
| ~~X6 PersonaView~~ | ~~B3~~ | **已裁减，不提供** | — | — | 作废 |
| ~~X10 素材规范~~ | ~~D5~~ | 消费（seed 函数） | N0 | D11 10:00 | 作废（contracts v0.5，media 改现有媒体库） |
| `audio_url` | D4 | **提供** | — | D11 下午 | 已交付（data URL 承载，真 TTS provider 由 B 注入） |
| 生命周期/审计端点 | A4 | 消费 | N0 | D11 15:00 | 联调 PASS（elder_id 错位已修 `7c87f713`） |
| media 调用侧改动 | B（tool_impls 持锁） | 提需求 | — | D11 上午 | 需求已提（`get_next_media` 待 B 迁接） |

**高风险文件方案登记**（`safety_service.py`，落代码前必填）：**已登记 2026-11-12（S0），待主理人确认后才落代码**——

1. **确认联动（写 `acknowledged_at`）**：`notify_service.acknowledge_notification` 确认员工通知成功后，**同事务**检查 `notification.payload.risk_event_id`，存在且对应 `risk_events.acknowledged_at IS NULL` → 写入；`notify_red_dual`（X3）建通知时在 payload 写 `risk_event_id`（`notifications.payload` 为既有 JSON 列，**不新增 schema**）；无 linkage 的既有通知行只更新通知本身，事件字段留空（如实，不编造）。`safety_service.escalate_safety_event` 既有接线**不动**。
2. **关闭联动（写 `closed_at`）**：`work_order_service.transition_work_order` 在 `to_status=="closed"` 且 `order.event_id` 非空时，**同事务**将关联 `risk_events` 置 `status="closed"`＋写 `closed_at`（幂等：已 closed 跳过）——顺序天然不反（事件只能随工单关闭或经下条入口关闭）。
3. **直接关事件入口（409 守门）**：`app/api/alerts.py` 增 admin-only `POST /risk-events/{event_id}/close`：关联工单存在且任一非 closed → **409 ORDER_NOT_CLOSED**；工单已 closed → 写事件 `closed_at`。
4. **扫描与收件人**：`scan_escalations` WHERE 补 `acknowledged_at IS NULL`；`escalate_red_event` 收件人改按 admin 角色查，不建第二事件/第二工单。
5. **分层**：全改动落在 C 写域三文件（`notify_service`／`work_order_service`／`safety_service` 表定义只读引用），`api` 不 import `graphs`，审计仍由既有 `_forbidden_response` 路径写。

（原 `core/context.py` 随人设裁减不再涉及。）

---

## 8. 风险与回退

| # | 风险 | 处置 |
| --- | --- | --- |
| R-C1 | C1 阈值回改导致既有测试大面积红 | 改测试期望值对齐 PRD line 357-359，**不改 PRD 口径**；提交正文写明依据 |
| R-C2 | 拆除假 `delivered` 后，依赖「恒 delivered」的测试与 console 展示连锁红 | 按契约 §3.2 五值＋`is_mock` 修展示；**`failed` 分支必须可展示**（契约 v1.2 明要求） |
| R-C3 | 双路径重复建单/重复通知 | 统一 `dedupe_token`；上线前跑重复提交用例；**建单只在你这边做** |
| R-C4 | 本包 5 节点，C6 依赖 A4/B2 易成尾段瓶颈 | C6 **骨架先行**（按冻结契约先建页＋Mock 态），D11 下午集中联调；若 A4 延迟，C6 先交统计页与记忆页（自有依赖），重置/审计页留联调段 |
| R-C5 | 人设裁减后 C 包释放 3.5h，可能被误当作可加新任务 | 释放工时**只作缓冲与全员联调支援**，不擅自加项；如需恢复人设须主理人另行裁决并回补 X6 |
| R-C6 | 日历口径压缩（总览 Q1） | 先裁顺位：**F30 日报语音条（P1，O2 溢出池）→ F26 `/metrics`（P1）→ 统计页（FR-ALT-05）**；**C1 分级、C2 红档闭环、C3 送达真链路属 P0，不容顺延** |

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.4 | 2026-11-12 | 状态改「已完成」（C1–C4/C6 交付＋S6 联调实测收口）；§7 执行记录与对接登记全量回填（证据细节见计划件 `docs/plans/day09/dev-c-day09-工作推进计划.md` §9 至 v1.13）。遗留：X3 语音/REST 形态对表（D10 12:00）、eval_gate q3 题面迭代（Day 10）。 |
| v1.3 | 2026-11-12 | C6 验收补：退房后 console 该长者显示 **archived** 且入口不可再操作；**退房→pad/family 端到端联调**（C 触发＋console 侧断言，pad 401 回登录／family 403 空态由 D 确认，C 不改 pad/family）。依据：`elders.status` 已含 archived（E8），两端 401/403 兜底已存在。 |
| v1.2 | 2026-11-12 | **人设裁减（主理人决定）**：删除 C5 节点（改为裁减声明＋现状留档）、C6 人设配置页（四页→三页）、X6 提供义务；写域移除 `persona_service`／`elder_service`／`context.py`／`api/personas.py`；ROUTERS 追加 4 行→3 行；高风险清单去掉 `context.py`；C 包 21h→**17.5h＝2.19 人日**，释放 3.5h 作缓冲/联调支援；console 人设入口保持置灰、既有表与代码不动。 |
| v1.1 | 2026-11-12 | 全量校验后修正：① C2 增 **X12 四个域清理钩子**（health/notify/report/work_order 各一个 `purge_elder_data`，供 A3 编排，A 不直写 C 的表），C2 由 4h 调为 **5h**，包容量 20h→**21h＝2.625 人日**；② 写域补 `app/services/elder_service.py`（人设绑定）与 `app/api/alerts.py`（admin 重扫端点落点明确，消除原「或提需求给 A/D」的模糊表述）；③ 排程校验后队列确定为 **C1→C2→C3→C5→C4→C6**，X1 联调时点随之定为 **D10 12:00**（C3 骨架 10.5h 完成）。程序校验：硬前置 11 边、软依赖 18 边**零违例**，五里程碑全达标。 |
| v1.0 | 2026-11-12 | 首版。承接 day08 遗留 10 项（F01 notify 侧/F04/F05 service 侧/F07 复核落库/F11/F12/F13/F21 media_service/F25/F26/F30 生成侧）＋ Day 9 原生人设切换、记忆管理 API、统计页、审计导出前端；6 节点 20h＝2.5 人日。**明确两处不越界**：`app/scheduler/` 归 B（C3 只提供 X11 退避函数）、`api` 禁 import graphs（F07 只写库，resume 由 B 扫描）。实测校准：`persona_service.py` 只做 avatar config（人设切换未实现）、`personas` 表与 seed 已就绪、console 三入口置灰、医生趋势图已真实落地不重做。X7 字段按实测修正为 `review_status`＋`reviewed_by`（**无 `reviewed_at` 列，不新增 schema**）。 |