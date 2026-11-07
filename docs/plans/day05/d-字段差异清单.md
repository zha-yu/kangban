# Day 5 · dev-D console 五 Mock 页字段差异清单

> 状态：部分完成（等待 A-4）　｜　版本：v1.3　｜　日期：2026-11-07
> 关联：docs/plans/day05/d-双端前端推进计划.md（W3）、docs/plans/day05/d-DAG.md（N9/N10/N11）
> 上游依据：技术设计文档 §3 表定义（行号见各表）｜PRD FR-HLT-06/AC-32（C4 冲突旧文）｜A-4 契约草案（⏳ 未交付）
> 变更记录见文末

---

## 0. 本清单怎么读

- **左半表**：前端 Mock 现状字段 → TD §3 出处（N9，已填；只盘点，不裁决）。
- **右半表（A-4 列）**：`待 A-4`；X1 草案未交付、X0 落点未裁定前一律 ⏳（N10 未启动）。
- 差异结论只用三值：`一致` / `前端多（登记）` / `前端缺（登记）`。
- 「前端多」不等于错误：展示冗余字段（姓名、房间号等）需 A-4 确认是否由服务端 JOIN 返回；未确认前**不改代码**。
- TD 行号均指 `docs/康伴3D数字人_技术设计文档.md`。

## 1. 工单看板 · `workorders-api.ts`

TD 出处：`work_orders`（TD 506-522）、`work_order_transitions`（TD 524-534）。

### 1.1 `WorkOrder`（17 个字段）

| # | Mock 字段 | 类型 | TD 字段与行号 | 左半表结论 | A-4 对照 | 确认 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `order_id` | string | `work_orders.order_id`（508） | 一致 | ⏳ | ⏳ |
| 2 | `order_no` | string | `order_no`（511） | 一致 | ⏳ | ⏳ |
| 3 | `elder_id` | string | `elder_id`（512） | 一致 | ⏳ | ⏳ |
| 4 | `elder_name` | string | **无**（TD 无展示名列） | 前端多（展示冗余，待 A-4 定 JOIN 返回） | ⏳ | ⏳ |
| 5 | `room_label` | string | **无** | 前端多（展示冗余） | ⏳ | ⏳ |
| 6 | `type` | 8 值枚举 | `type`（514） | 一致（8 值与 TD 逐字相同） | ⏳ | ⏳ |
| 7 | `severity` | `"red"｜"yellow"` | `severity`（515） | 一致（建单快照语义在 Mock 注释已注明） | ⏳ | ⏳ |
| 8 | `status` | 6 值枚举 | `status`（516） | 一致（含 `reopened`） | ⏳ | ⏳ |
| 9 | `assignee_id` | string \| null | `assignee_id`（517） | 一致 | ⏳ | ⏳ |
| 10 | `assignee_name` | string \| null | **无** | 前端多（展示冗余） | ⏳ | ⏳ |
| 11 | `false_alarm` | boolean | `false_alarm`（518） | 一致 | ⏳ | ⏳ |
| 12 | `false_alarm_reason` | string \| null | `false_alarm_reason`（519） | 一致 | ⏳ | ⏳ |
| 13 | `actual_handled_at` | string \| null | `actual_handled_at`（520） | 一致 | ⏳ | ⏳ |
| 14 | `note` | string \| null | `note`（521） | 一致 | ⏳ | ⏳ |
| 15 | `created_at` | string | `created_at`（522） | 一致 | ⏳ | ⏳ |
| 16 | `closed_at` | string \| null | `closed_at`（522） | 一致 | ⏳ | ⏳ |
| 17 | `transitions` | `WorkOrderTransition[]` | 独立表 `work_order_transitions`（524-534） | 前端多（内嵌数组；真实响应是否内嵌待 A-4） | ⏳ | ⏳ |
| — | **缺** `event_id` | — | `event_id`（513） | 前端缺（列表/看板展示非必需；待 A-4） | ⏳ | ⏳ |

### 1.2 `WorkOrderTransition`（8 个字段）

| # | Mock 字段 | 类型 | TD 字段与行号 | 左半表结论 | A-4 对照 | 确认 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `transition_id` | string | `transition_id`（528） | 一致 | ⏳ | ⏳ |
| 2 | `order_id` | string | `order_id`（529） | 一致 | ⏳ | ⏳ |
| 3 | `from_status` | `WorkOrderStatus` | `from_status`（530） | 一致 | ⏳ | ⏳ |
| 4 | `to_status` | `WorkOrderStatus` | `to_status`（531） | 一致 | ⏳ | ⏳ |
| 5 | `actor_id` | string \| null | `actor_id`（532） | 一致（系统自动流转可空同 TD） | ⏳ | ⏳ |
| 6 | `actor_name` | string \| null | **无** | 前端多（展示冗余） | ⏳ | ⏳ |
| 7 | `at` | string | `at`（533） | 一致 | ⏳ | ⏳ |
| 8 | `note` | string \| null | `note`（534） | 一致 | ⏳ | ⏳ |

## 2. 告警通知中心 · `alerts-api.ts`

TD 出处：`notifications`（TD 600-617）。

| # | Mock 字段 | 类型 | TD 字段与行号 | 左半表结论 | A-4 对照 | 确认 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `notification_id` | string | `notification_id`（604） | 一致 | ⏳ | ⏳ |
| 2 | `target_type` | `"guardian"｜"staff"` | `target_type`（605） | 一致 | ⏳ | ⏳ |
| 3 | `target_id` | string | `target_id`（605） | 一致 | ⏳ | ⏳ |
| 4 | `type` | 5 值枚举 | `type`（606，开放枚举） | 一致（前端只列常用 5 值，未知值渲染回退） | ⏳ | ⏳ |
| 5 | `payload`（外层） | `NotificationPayload` | `payload`（607） | 一致（jsonb） | ⏳ | ⏳ |
| 5a | `payload.title` | string | **无**（TD 只写「脱敏 jsonb」） | 前端多（内部键待 A-4） | ⏳ | ⏳ |
| 5b | `payload.summary` | string | **无** | 前端多（内部键待 A-4） | ⏳ | ⏳ |
| 5c | `payload.order_no?` | string | **无** | 前端多（内部键待 A-4） | ⏳ | ⏳ |
| 5d | `payload.room_label?` | string | **无** | 前端多（内部键待 A-4） | ⏳ | ⏳ |
| 6 | `channel` | `"inapp"｜"webpush"` | `channel`（608） | 一致 | ⏳ | ⏳ |
| 7 | `status` | 5 值枚举 | `status`（609） | 一致 | ⏳ | ⏳ |
| 8 | `is_mock` | boolean | `is_mock`（610） | 一致 | ⏳ | ⏳ |
| 9 | `retry_count` | number | `retry_count`（611） | 一致 | ⏳ | ⏳ |
| 10 | `dedupe_key` | string \| null | `dedupe_key`（612） | 一致 | ⏳ | ⏳ |
| 11 | `created_at` | string | `created_at`（613） | 一致 | ⏳ | ⏳ |
| 12 | `sent_at` | string \| null | `sent_at`（614） | 一致 | ⏳ | ⏳ |
| 13 | `acknowledged_by` | string \| null | `acknowledged_by`（615） | 一致 | ⏳ | ⏳ |
| 14 | `acknowledged_by_name` | string \| null | **无** | 前端多（展示冗余） | ⏳ | ⏳ |
| 15 | `acknowledged_at` | string \| null | `acknowledged_at`（616） | 一致 | ⏳ | ⏳ |
| 16 | `read_at` | string \| null | `read_at`（617） | 一致 | ⏳ | ⏳ |

> 补充（只登记）：通知 payload 内部键在 TD 无出处；家属侧已有契约草案 S-02 三键（`elder_name` / `event_brief` / `report_id`），员工侧尚无对应条文，属 A-4 必须回答项。

## 3. 健康代填 · `health-api.ts`

TD 出处：`health_records`（TD 470-490）。

### 3.1 `HealthRecordInput`（8 个字段 + 1 缺）

| # | Mock 字段 | 类型 | TD 字段与行号 | 左半表结论 | A-4 对照 | 确认 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `elder_id` | string | `elder_id`（475） | 一致 | ⏳ | ⏳ |
| 2 | `source` | `staff_scheduled｜staff_active` | `source`（477，四值枚举） | 一致（本页只承担护工两值；**窄化登记见 §6 C4**） | ⏳ | ⏳ |
| 3 | `systolic` | number \| null | `systolic`（479） | 一致 | ⏳ | ⏳ |
| 4 | `diastolic` | number \| null | `diastolic`（479） | 一致 | ⏳ | ⏳ |
| 5 | `sleep_hours` | number \| null | `sleep_hours`（480） | 一致 | ⏳ | ⏳ |
| 6 | `mood_score` | number \| null | `mood_score`（481） | 一致 | ⏳ | ⏳ |
| 7 | `mood_note` | string \| null | `mood_note`（482） | 一致 | ⏳ | ⏳ |
| 8 | `complaint` | string \| null | `complaint`（483） | 一致 | ⏳ | ⏳ |
| — | **缺** `operator_id` | — | `operator_id`（478） | 前端缺（写入侧应由服务端从 runtime_context 注入，待 A-4 定） | ⏳ | ⏳ |

### 3.2 `HealthRecordResult`（7 个字段 + 5 缺）

| # | Mock 字段 | 类型 | TD 字段与行号 | 左半表结论 | A-4 对照 | 确认 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `record_id` | string | `record_id`（474） | 一致 | ⏳ | ⏳ |
| 2 | `elder_id` | string | `elder_id`（475） | 一致 | ⏳ | ⏳ |
| 3 | `source` | `StaffRecordSource` | `source`（477） | 一致 | ⏳ | ⏳ |
| 4 | `severity` | `green｜yellow｜red` | `severity`（484） | 一致 | ⏳ | ⏳ |
| 5 | `rule_version` | string | `rule_version`（485） | 一致 | ⏳ | ⏳ |
| 6 | `follow_up` | string | **无** | 前端多（页面提示文案，非表字段） | ⏳ | ⏳ |
| 7 | `recorded_at` | string | `recorded_at`（476） | 一致 | ⏳ | ⏳ |
| — | **缺** `review_status` / `reviewed_by` / `version` / `superseded_by` / `graph_thread_id` | — | 486 / 487 / 488 / 489 / 490 | 前端缺（提交响应裁剪；待 A-4 定响应字段，不在本页消费） | ⏳ | ⏳ |

## 4. 药单 · `medication-api.ts`

TD 出处：`medication_plans`（TD 422-434）。

| # | Mock 字段 | 类型 | TD 字段与行号 | 左半表结论 | A-4 对照 | 确认 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `plan_id` | string | `plan_id`（426） | 一致 | ⏳ | ⏳ |
| 2 | `elder_id` | string | `elder_id`（427） | 一致 | ⏳ | ⏳ |
| 3 | `drug_name` | string | `drug_name`（428） | 一致 | ⏳ | ⏳ |
| 4 | `dose` | string | `dose`（429） | 一致 | ⏳ | ⏳ |
| 5 | `schedule_cron` | string | `schedule_cron`（430） | 一致 | ⏳ | ⏳ |
| 6 | `start_date` | string | `start_date`（431） | 一致 | ⏳ | ⏳ |
| 7 | `end_date` | string \| null | `end_date`（431） | 一致 | ⏳ | ⏳ |
| 8 | `version` | number | `version`（432） | 一致 | ⏳ | ⏳ |
| 9 | `created_by_name` | string | `created_by`（433，uuid FK） | 前端多（以姓名替代 UUID）＋前端缺 `created_by` | ⏳ | ⏳ |
| 10 | `status` | `"active"｜"stopped"` | `status`（434） | 一致 | ⏳ | ⏳ |
| — | **缺** `created_by` | — | `created_by`（433） | 前端缺（展示层用姓名；待 A-4 定是否 JOIN 返回） | ⏳ | ⏳ |

## 5. 复核队列 · `review-api.ts`

TD 出处：`health_records`（TD 470-490）。

| # | Mock 字段 | 类型 | TD 字段与行号 | 左半表结论 | A-4 对照 | 确认 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `record_id` | string | `record_id`（474） | 一致 | ⏳ | ⏳ |
| 2 | `elder_id` | string | `elder_id`（475） | 一致 | ⏳ | ⏳ |
| 3 | `elder_name` | string | **无** | 前端多（JOIN 展示冗余） | ⏳ | ⏳ |
| 4 | `room_no` | string \| null | **无** | 前端多（展示冗余） | ⏳ | ⏳ |
| 5 | `bed_no` | string \| null | **无** | 前端多（展示冗余） | ⏳ | ⏳ |
| 6 | `source` | `StaffRecordSource` | `source`（477） | 一致（窄化登记见 §6 C4） | ⏳ | ⏳ |
| 7 | `operator_name` | string | `operator_id`（478，uuid FK） | 前端多（名称替代 UUID）＋前端缺 `operator_id` | ⏳ | ⏳ |
| 8 | `recorded_at` | string | `recorded_at`（476） | 一致 | ⏳ | ⏳ |
| 9 | `systolic` | number \| null | `systolic`（479） | 一致 | ⏳ | ⏳ |
| 10 | `diastolic` | number \| null | `diastolic`（479） | 一致 | ⏳ | ⏳ |
| 11 | `sleep_hours` | number \| null | `sleep_hours`（480） | 一致 | ⏳ | ⏳ |
| 12 | `mood_score` | number \| null | `mood_score`（481） | 一致 | ⏳ | ⏳ |
| 13 | `mood_note` | string \| null | `mood_note`（482） | 一致 | ⏳ | ⏳ |
| 14 | `complaint` | string \| null | `complaint`（483） | 一致 | ⏳ | ⏳ |
| 15 | `severity` | `green｜yellow｜red` | `severity`（484） | 一致 | ⏳ | ⏳ |
| 16 | `rule_version` | string | `rule_version`（485） | 一致 | ⏳ | ⏳ |
| 17 | `version` | number | `version`（488） | 一致 | ⏳ | ⏳ |
| 18 | `review_status` | `pending｜confirmed｜corrected` | `review_status`（486） | 一致 | ⏳ | ⏳ |
| 19 | `reviewed_by_name` | string \| null | `reviewed_by`（487，uuid FK） | 前端多（名称替代 UUID）＋前端缺 `reviewed_by` | ⏳ | ⏳ |
| 20 | `reviewed_at` | string \| null | **无** | 前端多（TD `health_records` 无复核时间列） | ⏳ | ⏳ |
| 21 | `review_note` | string \| null | **无** | 前端多（已登记 Mock 自裁，f7 D4） | ⏳ | ⏳ |
| 22 | `superseded_by` | string \| null | `superseded_by`（489） | 一致 | ⏳ | ⏳ |
| 23 | `superseded_reason` | `review_correction｜backfill` \| null | **无** | 前端多（TD 只有 `superseded_by` 指针） | ⏳ | ⏳ |
| — | **缺** `operator_id` / `reviewed_by` / `graph_thread_id` | — | 478 / 487 / 490 | 前端缺（展示用名称替代 ID；待 A-4 定） | ⏳ | ⏳ |

### 5.1 版本链判定差异（只登记，不改逻辑）

`review-api.ts:getRecordChain` 按「同 `elder_id` + 同 `recorded_at`」聚合同批次版本；TD 只有 `superseded_by` 指针（489）。属**判定口径差异**，不是字段差异；A-4 未回答前保持现状（业务判定逻辑零改动）。

## 6. 既有差异与缺陷登记（只登记，不裁决、不改）

| # | 事项 | 现状 | 依据/出处 | 处置 |
| --- | --- | --- | --- | --- |
| C4 | `source` 枚举冲突 | TD 四值 `voice_scheduled/voice_active/staff_scheduled/staff_active`（TD 477、684）；PRD FR-HLT-06 / AC-32 旧文写 `staff_report`/`resident`；前端只用 `staff_scheduled/staff_active` | 计划 §1.6 C4；AGENTS 前置铁律 4 | 以 TD §3 四值为准（N3 口径）；差异登记回传 dev-A，不自行选一个、不改 PRD |
| H1 | 硬编码演示身份 | `MedicationEditPage.tsx:24` `const DOCTOR = "演示·钱医生"`；`ReviewQueuePage.tsx:105` `const REVIEWER = "演示·钱医生"` | 计划 §1.7 | 只登记；真实接口后由登录态注入，本轮不改 |
| H2 | `createDevice` 抛数字 status | `elders-api.ts:131-132` 非 2xx 抛 `res.status` 而非 `ApiError` | 计划 R7 | 已在 `AdminDevicesPage` 用 `typeof err === "number"` 守卫兜底；不改 `elders-api.ts` |
| H3 | `console/api.ts` 无 `forbidden` kind | `ApiErrorKind = "invalid" \| "network"` | 计划 K2 | 采 K2-B：`admin-api.ts` 内解析 403；`console/api.ts` 零改动 |
| H4 | 既有未定义 token 变量 | `console/index.css` 引用 `--color-border` / `--radius-lg` / `--radius-md` / `--color-surface-soft` / `--color-danger` / `--color-primary`（tokens.css 未定义） | 计划 §1.7 | 只登记不修；新增 admin 样式已自检 0 引用 |
| H5 | 既有未定义 CSS 类 | `MedicationEditPage` / `ReviewQueuePage` 使用 `.rv-mask` / `.rv-dialog` / `.btn-ghost` | 计划 §1.7 | 只登记不修；新增 admin 样式不沿用 |

## 7. 覆盖统计（左半表）

| 页面 | Mock 类型 | 字段数 | 一致 | 前端多（登记） | 前端缺（登记） | A-4 覆盖 |
| --- | --- | --- | --- | --- | --- | --- |
| 工单 | `WorkOrder` + `WorkOrderTransition` | 17 + 8 | 20 | 4（`elder_name`/`room_label`/`assignee_name`/`actor_name`）+1 结构（`transitions`） | 1（`event_id`） | ⏳ |
| 告警通知 | `NotificationItem` | 16 + 4 内部键 | 15 | 1（`acknowledged_by_name`）+4 内部键 | 0 | ⏳ |
| 健康代填 | `HealthRecordInput` + `HealthRecordResult` | 8 + 7 | 13 | 1（`follow_up`） | 1 + 5 | ⏳ |
| 药单 | `MedicationPlan` | 10 | 9 | 1（`created_by_name`） | 1（`created_by`） | ⏳ |
| 复核 | `ReviewRecord` | 23 | 17 | 6（`elder_name`/`room_no`/`bed_no`/`operator_name`/`reviewed_by_name`/`reviewed_at`/`review_note`/`superseded_reason` 中展示类） | 3（`operator_id`/`reviewed_by`/`graph_thread_id`） | ⏳ |

> 说明：上表「前端多」与「前端缺」均为**左半表结论**，只说明 Mock 与 TD 表字段的对应关系；是否保留展示冗余、响应要不要裁剪，必须等 A-4 草案并双向确认后才能动代码（N10/N11）。

## 8. 渲染基线（N9 附，2026-11-07）

> 目的：在 A-4 未到、N11 未启动前记录五个 Mock 模块的当前渲染状态，作为字段修正后的回归对照。
> 只记录、不改字段、不改业务判定；账号为本地教学数据。

| # | 页面 | 账号 | 走查项 | 结果 | 截图（外部目录 `...\Temp\trae\screenshots\`） |
| --- | --- | --- | --- | --- | --- |
| 1 | 工单看板 | care_worker | 列表 / 筛选 / 详情 / 状态历史 / 误报弹窗 | 通过：共 7 单（红 3 / 黄 4），未处理 4；详情含建单快照与完整状态历史；误报弹窗可开可取消 | `care_worker_dashboard.png` / `care_worker_ticket_detail.png` / `care_worker_misreport_cancel.png` |
| 2 | 告警通知中心 | care_worker | 列表 / 筛选 / 详情时间线 / 快照 | 通过：共 8 条；详情展开 generated→pending→delivered→acknowledged 时间线与通知快照 | `care_worker_alert_center.png` / `care_worker_alert_detail.png` |
| 3 | 健康代填 | care_worker | E7 长者下拉 / 字段 / 提交结果 | 通过：张爷爷默认选中；135/85/7.5/3 提交后显示绿档 + 规则 v1.0 结果卡 | `health_fill_result.png` |
| 4 | 药单只读 | care_worker | E7 长者下拉 / 计划列表 | 通过（空态）：张爷爷下显示「该长者当前没有用药计划」 | `medication_readonly.png` |
| 5 | 复核队列 | doctor | 队列 / 红档置顶 / 版本链 / 纠错弹窗 | 通过渲染：待复核 2 条、红档 1 条置顶；版本链展开 1 项；纠错弹窗可开，但无 modal 样式（H5） | `doctor-review-queue.png` / `doctor-review-chain.png` / `doctor-review-correct-dialog.png` |
| 6 | 药单维护 | doctor | E7 长者下拉 / 计划列表 / 新增弹窗 | 通过（空态）：分配长者后下拉 1 项张爷爷、0 条计划；新增弹窗可开，但同样无 modal 样式（H5） | `doctor-medication-edit.png` / `doctor-medication-create-dialog.png` |

### 8.1 基线发现（只登记，不改）

| # | 发现 | 证据 | 处置 |
| --- | --- | --- | --- |
| B1 | **药单 Mock 与真实长者 ID 不匹配**：`medication-api.ts` 的 Mock 计划使用 `elder-301/303/305`，E7 返回真实 UUID；两个药单页按选中真实 `elder_id` 过滤 → 恒为空列表 | 药单只读 / 药单维护均显示空态（截图 #4/#6） | 只登记；属 Mock 数据与真实接口的接线差异，A-4 确认前不改 Mock 数据、不改业务逻辑 |
| B2 | **弹窗无样式（H5 实测）**：`.rv-mask` / `.rv-dialog` / `.btn-ghost` 未在 `console/index.css` 定义，渲染为 `position: static`、背景透明、`padding: 0px`，弹窗内联插入页面、无遮罩 | `doctor-review-correct-dialog.png` / `doctor-medication-create-dialog.png` 可见表单直接铺在页面上 | 只登记；不属本任务，不修既有缺陷 |
| B3 | **复核版本链基线仅 1 项**：待复核记录（rv-mock-001）没有历史版本；含两版本的纠错记录（rv-mock-003/004）不在 pending 队列，UI 无法从队列打开两版本链 | `doctor-review-chain.png` 显示 1 项 | 只登记；演示若要展示两版本链，需先对一条 pending 记录执行纠错，或后续调整队列/数据（不在本任务） |
| B4 | **医生药单维护初始无长者范围**：测试医生由 E12 建档时未带 `elder_ids`，E7 返回空范围，药单维护下拉为空；本轮仅在本地测试库补 `elder_staff_assignments`（relation=`doctor`，张爷爷）后完成走查 | `doctor-medication-edit.png` 显示张爷爷 1 项 | 测试数据补齐，非代码/迁移改动；正式 seed 应覆盖医生负责关系 |
| B5 | ~~健康代填容器样式受 H4 影响~~ **merge 后已解决**：远端 `console/index.css` 已把未定义 token 全部替换（`--color-border`→`--color-line`、`--radius-lg`→`--radius-card`、`--color-surface-soft`→`--color-surface-sunken` 等），合并后 token 扫描 **UNDEFINED=0** | 原 `health_fill_result.png` 采于 merge 前 | 已由远端修复；N11 回归以 merge 后为准 |

> 基线结论：五个 Mock 模块均能正常渲染与交互，无崩溃/白屏；发现 1 处 Mock/真实 ID 接线差异、1 组既有弹窗样式缺陷、1 处演示数据缺口。N11 修正字段后可用本基线逐页对照，避免渲染回归。
>
> **merge 说明（2026-11-07）**：本基线截图采于 merge `origin/develop` 之前（console 控件 48px）；merge 后 console 全局控件统一为 40px（远端口径），仅影响控件高度，不影响本表的字段/列表/弹窗结构。B5 的 H4 未定义 token 已被远端修复（token 扫描 0）；B2 的 `.rv-mask`/`.rv-dialog`/`.btn-ghost` 仍未定义，缺陷仍在。N11 回归以 merge 后版本为准。

## 9. 回传与双向确认

| 项 | 状态 |
| --- | --- |
| A-4 草案交付（X1） | ⏳ 未交付（`dev-a` 卡 A-4「未开始」） |
| 落点裁定（X0） | ⏳ 未闭环（主计划 §6 #6） |
| 差异清单回传 dev-A（N3） | ⏳ 未回传（无跨人通道；本文件即回传载体） |
| dev-A 双向确认 | ⏳ 未确认 |
| N11 Mock 修正 | **未启动**（DAG F2：无双向确认不得改五 Mock 页字段） |

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | G0 建表占位 |
| v1.1 | 2026-11-07 | N9 完成：导出五页全部类型与字段，逐字段标注 TD §3 表名+行号并给出三列结论；登记 C4/H1–H5 既有差异；A-4 对照列与 N10/N11 全部 ⏳。**未改任何 Mock 代码** |
| v1.2 | 2026-11-07 | 补 §8 渲染基线（N9 附）：六页面视图逐项走查并截图；登记 B1 药单 Mock/真实 ID 不匹配、B2 弹窗无样式实测、B3 版本链仅 1 项、B4 医生药单范围缺口、B5 健康容器样式缺失。**Mock 代码零改动** |
| v1.3 | 2026-11-07 | merge origin/develop 后更新：B5（H4 未定义 token）已由远端修复，token 扫描 UNDEFINED=0；B2（rv-mask/rv-dialog/btn-ghost）仍存在；补 merge 后 40px 对基线的影响说明。Mock 代码仍零改动 |
