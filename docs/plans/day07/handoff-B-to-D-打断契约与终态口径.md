> 状态：已完成　｜　版本：v1.0　｜　日期：2026-11-10
> 关联：docs/team_tasks/day07/dev-d-day07.md（D1 §2.2-2 / §2.4）｜docs/team_tasks/day07/README.md §5.4（冻结口径）｜docs/plans/day07/dev-b-day07-工作推进计划.md §4（交接矩阵）
> 上游依据：PRD FR-VPR-02（打断）｜技术设计文档 §5.4｜adhoc-20261110-day6全面审查.md P2-7/8、P3-27/28/30
> 交付方向：dev-B（B1 回合终态与打断闭环，已收口）→ dev-D（D1 pad 播放/打断接线）

# 交接件 · C→S `interrupt` 契约与终态口径（B1 → D1，J3 节点）

> 这份是 B 给 D 的**正式交接件**（J3）。字段以 `day07/README.md` §5.4 冻结口径为准；
> B1 服务端已于 Day 7 Step 5a/5b 全部实现并回归绿（1043 passed），D1 可直接按本文联调。
> 与之前给您的"预通知"相比，本文是**验收基线**：数字与字段均以实现为准，不再口头对齐。

## 0. 一句话结论

**D1 按 §5.4 契约接线即可，服务端已就绪。** 前端发 `interrupt`，服务端 200ms 内置
`interrupted` 终态并落库；⚠️ `canceled` 语义 Day 7 已变（不再是"陈述性上报"），见 §3。

## 1. interrupt 消息契约（C→S，§5.4 冻结）

```json
{ "type": "interrupt", "response_id": "<32位hex>", "reason": "barge_in" }
```

- **发送时机**：本地 `stopPlayback()` 之后**立即发送**，不等待回执（D1 卡 §2.2-2 原文）；
- **形状校验**：三字段任一缺失/类型不对 → 服务端 warn + 回 `error` 帧，**不关连接**；
- **归属校验**：`response_id` 不属于当前设备的回复 → no-op（不落库、不 bump epoch、无副作用）；
- **未知 response_id**：no-op + `response_interrupt_ignored` 日志，无 error 帧；
- **服务端幂等**：回复已结束（delivered/interrupted）再发 interrupt → no-op，不重复写事件；
- **旧 epoch 回调丢弃**：打断响应只对当前 epoch 生效，旧代回调前端自行丢弃（A1/B1 契约）。

## 2. 服务端五步顺序（已实现，前端只读不依赖）

①收到 → ②停 TTS 队列（该 response 待播下行全清）→ ③epoch+1 → ④取消下发任务 →
⑤写 `response_interrupted` 事件 + `delivery_status="interrupted"` + 终态结算落库。

## 3. ⚠️ canceled 终态口径（Day 7 行为变更，P2-7/8）

Day 6 及以前：canceled ack 是"陈述性上报，不改任何状态"。**Day 7 起改为终态结算**：

| 行为 | Day 6 | Day 7（现行为） |
| --- | --- | --- |
| canceled ack 被接受后 | 状态不变 | 置 `interrupted` 终态 + 落库 |
| `delivered_text` | 不变 | 按**水位**（最后连续 played 段）截断 |
| `response_interrupted` 事件 | 无 | 写 1 条，payload 带 `reason:"canceled"` |
| 终态后再发同代 played/canceled | 接受 | **拒绝**（`response_ack_rejected`） |
| 客户端段号与水位不一致 | 忽略 | 告警 + **以水位为准**结算 |
| 无一完整播放段（哨兵 `-1`） | 打 0.0ms 伪打点 | 不打点 |

**前端含义**：不要对被拒的 ack 重试；打断/取消后状态条直接按 `interrupted` 收口。

## 4. 事件与状态取值

- `response_interrupted` 事件 payload：`{ "response_id": ..., "last_played_segment": <int>, "reason?": "canceled" }`
  （用户主动打断不带 `reason` 字段；canceled 路径带）；
- `delivery_status` 取值：`unknown`（初始）→ `delivered`（末段 played）| `interrupted`（打断/canceled）；
- `response_completed` 事件**每轮恰好 1 条**（B1 保证；重复/迟到 ack 不产生第二条）。

## 5. 联调判据（D1 §2.4 验收 + B1 侧 SQL 抽查）

- **时延判据**：前端发 interrupt → 200ms 内服务端状态 `interrupted`；
- **前缀判据**：`delivered_text` 只含已 `played` 的连续前缀（`[0..last_played]`）；
- SQL 抽查（真实一轮后，D3 联调时执行）：

```sql
SELECT status, assistant_delivered_text IS NOT NULL AS has_delivered
FROM conversation_turns ORDER BY started_at DESC LIMIT 1;
-- 打断轮期望：interrupted | t（且文本为已播前缀）；完整轮期望：completed | t

SELECT count(*) FROM conversation_events
WHERE turn_id = :turn_id AND event_type = 'response_completed';  -- 期望 1

SELECT event_type, payload_json FROM conversation_events
WHERE turn_id = :turn_id AND event_type = 'response_interrupted';  -- 打断轮期望 1 条
```

## 6. 服务端问题回滚路径

联调发现服务端终态/打断异常：回 **B1**，附 `trace_id` + 请求/响应原文 +
`conversation_events` 中该 `turn_id` 的事件序列；**不在前端兜底**（不伪造 played、
不本地伪装 interrupted）。

## 7. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | B1 收口（Step 5d）正式交接：interrupt 契约、canceled 终态变更、事件/状态取值、联调判据与 SQL |
