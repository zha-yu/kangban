# Day 8 契约增量 · 健康采集任务下发（§C-1）、pad 答案回传（§P-1）与撤销/说明（§P-2）

> 状态：已冻结　｜　版本：v1.1　｜　日期：2026-11-11
> 关联：docs/team_tasks/day08/day08-任务总览与DAG.md（B3 调度 / dev-C 健康域）、docs/接口契约_照护台业务数据.md（§3.3 H-3 随本契约同日冻结，见其 §7 v1.4）
> 上游依据：PRD §4.6（采集窗口 19:00–20:00）、FR-HLT-01、FR-UI-10（任务下发）｜技术设计文档 §3 health_records.source 枚举、§6 health_checkup_graph、Day 7 §6.2 task_command 帧先例
> 变更记录见文末

## 1. §C-1 `task_command` 下行帧扩 kind=health（S→C，信令 WS）

载体沿用 Day 7 L6#9 已冻结的 `task_command` 帧类型（`frontend/src/pad/protocol.ts`），
**不新增消息 type**，只扩 `kind: "health"` 分支：

```jsonc
{
  "type": "task_command",
  "task_id": "health:<elder_id>:<YYYYMMDD>",   // 按日固定幂等键；同日重发同键，pad 端不重启面板（FR-MED-04 同款口径）
  "kind": "health",
  "source": "scheduler" | "staff",             // 触发方：19:00 定时 / 护工补启动（H-3）
  "hint": "今天的健康采集开始了，请回答右边的问题",
  "payload": {
    "kind": "health",
    "startedAt": "19:05",                       // 触达时刻 HH:MM（上海时区），仅展示
    "source": "voice_scheduled" | "staff_scheduled"  // 与 health_records.source 前缀口径对齐，仅展示
  },
  "issued_at": 1762848000000
}
```

- **投递通道**：信令 WS 下行（`app/audio/signaling.deliver`，进程内注册表）。
  **不做 REST 轮询**（AGENTS 禁区 8：一个能力一种形态）；离线兜底见 §1.1。
- **离线兜底（帧与起图解耦）**：调度 tick 在采集窗口内（`health_checkup_start`
  –`end`，上海时间）每分钟对「当日无记录」的 active 长者重推本帧；设备离线时
  `deliver` 丢弃，设备上线后下一分钟 tick 自动触达。起图本身是 graph/checkpointer
  事实（幂等跳过），不受帧投递成败影响。
- **客户端纪律**：同一 `task_id` 重复收到 → 幂等不重启面板；`source`/`startedAt`
  仅作展示标签，**不参与分级、不写入任何业务事实**。
- **单 worker 约束**：`deliver` 注册表在进程内，uvicorn 须单 worker 运行
  （用药触达同款约束，部署侧已有先例）。

## 2. §P-1 `POST /api/pad/health-records`（C→S，pad 答案回传）

- 权限：**只认设备凭证**（口径同 C7 avatar-config）；设备未绑定长者 →
  403 `device_not_bound`。
- 请求体（五项任一非空，否则 422；`extra=forbid`）：

| 字段 | 类型/可空 | 出处 |
| --- | --- | --- |
| systolic / diastolic | int 可空（0–300） | TD health_records（值域照 H-2 口径） |
| sleep_hours | float 可空（0–24） | TD health_records.sleep_hours |
| mood_score | int 可空（1–5） | TD health_records.mood_score |
| complaint | text 可空（≤500，第 5 问主诉；「没有」前端解析为 null） | TD health_records.complaint |
| ~~elder_id~~ | **不收**——服务端从设备 Identity 解出（AGENTS §4.9） | — |
| ~~source~~ | **不收**——服务端判定（见下） | — |

- **source 服务端判定（自裁口径，主理人知情，调整须改本契约留痕）**：
  采集窗口内提交 → `voice_scheduled`；窗口外提交 → `voice_active`。
  只影响来源标签与展示，不参与分级。
- 响应 `200`：照 H-2 冻结五字段 `record_id / severity / rule_version / recorded_at /
  follow_up`（severity 为服务端规则引擎写入，前端不传）。
- 分级、risk_events / work_order 联动全部复用 `create_health_record`
  （voice_* 时 operator_id=None 合法），**不建第二条写库路径**。
- 前端失败兜底（主理人裁决）：**不自动重试**，完成页给「重新提交」按钮；
  重试语义按 H-2「追加新行不覆盖」（AC-22）如实登记——模糊失败重试可能产生重复行，
  由复核纠错（R-4）兜底。见 §4 开放问题。

## 3. 调度装配（实现留痕）

`install_health_scheduler` 此前全仓无调用方；本批在 `app/main.py` lifespan 照用药侧
同款装配（手动 start/stop）。窗口内补推 tick 为第五类 job（`health_panel_push`）。

## 4. 开放问题（登记待主理人复核）

| # | 问题 | 当前口径 |
| --- | --- | --- |
| O-1 | pad 面板提交与语音子图问询并发时的重复采集 | 追加新行 + 复核纠错兜底；如需互斥另立裁决 |
| O-2 | 模糊失败（网络超时）重试产生重复行 | AC-22「追加不覆盖」为冻结口径，前端仅在明确失败时允许手动重试；不引入客户端去重键 |

## 5. §P-2 pad 撤销 / 补充说明端点（D3 · Q-D7，FR-UI-08 / AC-26）

权限：均只认设备凭证（同 §P-1 / C7）。未绑定长者 → 403 `device_not_bound`；
事件不属于本设备长者一律 **404 `NOT_FOUND`**（防枚举）；`elder_id` 只从设备绑定表解出。

### 5.1 普通「已服自报」撤销（10 秒窗口）

`POST /api/pad/medication-events/{reminder_id}/revoke`

| 项 | 口径 |
| --- | --- |
| 200 | `{"reminder_id":"uuid","status":"waiting_confirm","terminal_state":null}` |
| 409 `UNDO_WINDOW_EXPIRED` | `now - confirmed_at > 10s` |
| 409 `CONFLICT` | 非 `terminal_state='taken_reported'`（红色事件不走本端点） |
| 404 `NOT_FOUND` | 事件不存在或不属于本设备长者 |
| 副作用 | 恢复 `waiting_confirm`；清空 `terminal_state/confirmed_at/confirm_channel`、`late_taken=false`；写审计 `medication_taken_report_revoked`；**不删触达历史**（medication_attempts 只增不改） |

### 5.2 红色事件补充说明（不可撤销）

`POST /api/pad/sos/{event_id}/correction`

| 项 | 口径 |
| --- | --- |
| 200 | `{"event_id":"uuid","status":"open|escalated","note":"刚才说错了"}`，**status 保持不变** |
| 409 `CONFLICT` | 事件已 `closed`，不再接受说明 |
| 404 `NOT_FOUND` | 事件不存在或不属于本设备长者 |
| 副作用 | 仅追加审计 `sos_correction_appended`（append-only），**不关闭/不撤销事件**，由工作人员核实 |

> 冻结说明：两条件端点为 Day 8 D3 自裁口径（Q-D7 撤销契约未冻结前），随本契约一并冻结；
> 变更须回 dev-B 走修订记录。红色事件「不可一键撤销」依据 PRD FR-UI-08 / AC-26。

## 6. 引用登记：`tts_text.route_degraded`（D6）

字段名、类型、恒下发、与 `degraded` 正交等口径以
`docs/plans/day08/adhoc-20261111-route_degraded契约冻结.md` v1.0 为准，本契约不重复定义。
消费方 dev-D（`protocol.ts`/`realtime.ts` 已实现）；产出方 dev-B（服务端置位待实现）。

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-11 | 首版冻结：§C-1 task_command kind=health 帧扩展（含离线补推、幂等键、单 worker 约束）；§P-1 pad 答案回传端点（source 服务端判定自裁口径）；§3 调度装配留痕；§4 开放问题 O-1/O-2。配套实现：app/services/task_dispatch_service.py、app/api/pad.py、app/api/health_records.py H-3、health_jobs 第五 tick、pad 前端 protocol/health-client/HealthCheckup 接入 |
| v1.1 | 2026-11-11 | **新增 §5 §P-2 pad 撤销/补充说明端点**（D3 · Q-D7）：`POST /api/pad/medication-events/{id}/revoke`（仅已服自报、10 秒窗口、404 防枚举、审计）与 `POST /api/pad/sos/{id}/correction`（红事件只追加「说错了」、status 不变、审计）；**新增 §6 引用登记** `tts_text.route_degraded`（口径以 adhoc 冻结文 v1.0 为准，D6 消费/B 产出）。依据 PRD FR-UI-08 / AC-26；实现见 app/services/{medication_service,sos_service}.py、app/api/{pad_undo,sos}.py、pad SosResult 与 24 例测试 |
