# 交接件 · A3 → D3：`decision_logs` 查询口径与 `route_layer` 语义

> 发起：dev-A（A3 收口，2026-11-10 17:20）　｜　接收：dev-D（D3 真机联调/看板）
> 关联：`docs/team_tasks/day07/contracts-system-one.md` v1.1 §4、`app/models/decision.py`、`app/services/decision_log_service.py`
> 用途：D3 联调时**照抄 SQL 就能对账**，不用再猜字段语义。

---

## 0. 一句话

每轮对话**恰好 1 行** `decision_logs`，落在 `turn_service` 第 3.5 步（路由结论一出来就写，
与工具是否执行无关）；`route_layer` 回答"这轮谁做的决定"，`latency_ms` 回答"路由花了多久"。

---

## 1. 表结构（TD §3.4 的 14 列，无增减）

| 列 | 类型 | 写入方 | 语义 / 坑 |
| --- | --- | --- | --- |
| `log_id` | uuid PK | DB | `gen_random_uuid()` |
| `thread_id` | text FK→`conversation_threads` | turn_service | 会话串联键；**外键 ON DELETE CASCADE**（退房清数据时随会话删） |
| `trace_id` | text NOT NULL | 请求上下文 | 与 `X-Trace-Id` 同一个；无 trace 时写 `"-"` |
| `elder_id` | uuid NOT NULL | `runtime.elder_id` | **generic（身份未确认）轮次不写本表**，所以这里永远有值 |
| `input_text` | text NOT NULL | ASR final | **落库前已脱敏**：手机号 `138****5678`、身份证 `110101********1234`、房间号 `***房` |
| `intent` | text CHECK 七类 | RouteDecision | `chat/medication/opera/movie/weather/health_checkup/other` |
| `confidence` | numeric(4,3) | RouteDecision | 分类器的熵归一化值；**规则命中恒 `1.000`** |
| `is_sensitive` | bool | RouteDecision | G0 命中或 G1（shadow）判敏感；false 不代表"安全已确认"，只代表"没有信号" |
| `route_layer` | text CHECK 五值 | RouteDecision | 见 §3；`laya` 是历史只读值 |
| `escalated_to_s2` | bool | RouteDecision | 是否升级大模型（Agent）。clarify / tool / 规则直连 = false |
| `latency_ms` | int | RouteDecision | **含 shadow 等待**（v1.1 定口径），不是纯模型推理时间 |
| `asr_confidence` | numeric(4,3) 可空 | 调用方 | P2 埋点；当前 A 线传 `NULL`（turn_service 拿不到 ASR 置信度） |
| `model_version` | text NOT NULL | 装配层配置 | 取 `settings.system_one_model_version`：本地 `.env` 是 `laya:multilingual:dev`，目标是 `laya:multilingual:q1`（后缀 = 题面版本） |
| `created_at` | timestamptz | **服务层给值** | 不用 DB `now()`：同一事务内多行会拿到同一个时间戳，`ORDER BY created_at DESC LIMIT 1` 会取错行 |

索引（三条，别建第四条）：

```
ix_decision_logs_thread_id_created_at      (thread_id, created_at DESC)
ix_decision_logs_route_layer_created_at    (route_layer, created_at DESC)
ix_decision_logs_trace_id                  (trace_id)
```

---

## 2. 联调 SQL（照抄）

**① 这一轮走了哪一层、为什么慢**（按 trace，最常用）

```sql
SELECT created_at, thread_id, trace_id, intent, confidence, is_sensitive,
       route_layer, escalated_to_s2, latency_ms, model_version, input_text
FROM decision_logs
WHERE trace_id = :trace_id
ORDER BY created_at DESC;
```

**② 一个会话最近 10 轮的路由轨迹**（用 `ix_..._thread_id_created_at`，不要加 `WHERE created_at > ...` 破坏索引）

```sql
SELECT created_at, trace_id, route_layer, intent, confidence,
       escalated_to_s2, latency_ms, input_text
FROM decision_logs
WHERE thread_id = :thread_id
ORDER BY created_at DESC
LIMIT 10;
```

**③ 四层命中率 + P95（评测口径，与 `scripts/eval_router.py` 一致）**

```sql
SELECT route_layer,
       count(*)                                        AS rows,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct,
       round(avg(latency_ms))                          AS avg_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM decision_logs
WHERE created_at >= now() - interval '1 day'
GROUP BY route_layer
ORDER BY rows DESC;
```

> `fallback` **不计入** `system_one` 命中率（分类器不可用 ≠ 分类器命中）。看板若要算"快决策层闭环"，
> 分子只取 `rule + system_one`。

**④ 敏感两道的对账（G0 与 G1 各自命中多少）**

```sql
SELECT route_layer,
       count(*) FILTER (WHERE is_sensitive)                     AS sensitive_rows,
       count(*) FILTER (WHERE escalated_to_s2 AND is_sensitive)  AS escalated_sensitive
FROM decision_logs
WHERE created_at >= now() - interval '1 day'
GROUP BY route_layer;
```

---

## 3. `route_layer` 四值语义（+ 历史值）

| 值 | 谁做决定 | 典型 `action` | 说明 |
| --- | --- | --- | --- |
| `rule` | `app/router/rules.py` 10 条规则 或 G0 一票否决 | `tool` / `agent` / `escalate` | 0 次模型调用。**G0 命中时也记 `rule`**（契约 §6.1），`is_sensitive=true`、`action=escalate` |
| `system_one` | 分类器（≥阈值或动作型映射） | `tool` / `agent` / `clarify` / `escalate` | 任务口径的"快决策层"。`chat`/`other` 低置信也是这一层（只是 action=agent） |
| `agent` | Router 未经分类器直接下沉 | `agent` | **当前实现下正常为 0 行**：每条非规则句都会过一遍分类器，所以"直接下沉"不会发生；保留该值是为对齐契约四值，不是漏统计 |
| `fallback` | 分类器不可用（连不上/超时/熔断） | `agent` | **不计入** `system_one` 命中率 |
| `laya` | Day 6 及以前的历史值 | — | **只读兼容**，新代码不产出；看板统计时归到"历史"或直接排除 |

`action` 与 `route_layer` 是两件事：`route_layer=system_one` 且 `action=agent` 很常见（分类器认得，但能力未就绪/需要话术）。

---

## 4. 三条容易踩的口径

1. **没有 `action` 列**（TD 只给 14 列）。要判断"这轮是不是澄清轮"，用
   `confidence < 0.85 AND escalated_to_s2 = false`（`decision_log_service.was_clarified` 同一判据）。
   不要自己另发明一套。
2. **`latency_ms` 含 shadow 等待**，所以 p95 会落在 200–270ms；`decide_fast` 单独测是 ~170ms。
   看板不要把这两个数混在一个"路由耗时"里比。
3. **G1 的升级只在 `audit_logs`**：`action=system_one_sensitive_signal` 的动作名，`detail` 里有
   `thread_id` / `sensitive_probability` / `model_version`。**不建红色事件/工单**（缺「疑似急症」类别口径，
   待主理人裁决）。联调时请按 `audit_logs` 对账，别去 `risk_events` 找。

---

## 5. D3 联调检查清单（对着总表逐条打勾）

| 检查 | SQL / 动作 | 期望 |
| --- | --- | --- |
| 每轮 1 行 | `SELECT count(*) FROM decision_logs WHERE trace_id = :trace_id` | = 1（同一 trace 多行 = 重复写了） |
| generic 不写 | 用未确认身份的会话说一句，再按 thread 查 | 0 行 |
| trace 一致 | 对比响应头 `X-Trace-Id` 与库内 `trace_id` | 相同 |
| 无身份证明文 | `SELECT input_text FROM decision_logs WHERE input_text ~ '1[3-9][0-9]{9}'` | 0 行 |
| 版本形如 | `SELECT DISTINCT model_version FROM decision_logs` | `laya:multilingual:*` |
| 敏感对账 | 说一句「我胸口疼」，查 `is_sensitive` 与 `route_layer` | `is_sensitive=true`、`route_layer=rule`、`action` 不在库里（看日志 `router_g1_escalated` 或 `safety_veto_triggered`） |
| 写入失败不阻断 | 停库/断连接说一句 | 对话有回复；日志出现 `decision_log_failed`（统计丢一行，不报错给用户） |

---

## 6. 与 B1 的边界（别写重了）

- `TurnOutcome` 只加了三个字段（`route_layer` / `intent` / `confidence`，另加 `clarified` 供联调），
  **响应流契约不变**（B1 已确认）。
- `decision_logs` 的写口只有 `decision_log_service.record_decision()` 一个；
  `response_service` / `scheduler` 都不得直接写本表。
- 第 5 步的 `response_completed` 归 B1 终态结算写（P2-10），**与本表无关**，不要混在一张对账单里。

---

## 7. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版（A3 收口）：14 列语义、3 条查询 SQL、`route_layer` 五值（含历史 `laya`）、澄清判据推断、G1 只落 `audit_logs` 的限制、7 条联调检查清单 |
