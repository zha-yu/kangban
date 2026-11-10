# Task dev-B · Day 7 · 回合终态、上下文与状态收口

> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-10
> 负责人：dev-B　｜　总人日：3.0（B1 1.25 + B2 1.25 + B3 0.5）
> 关联：`docs/team_tasks/day07/README.md`（总表）｜`docs/plans/adhoc-20261110-day6全面审查.md`（问题源）
> 上游依据：PRD FR-DLG-01/03/05/06｜技术设计文档 §3.4/§6/§8.3｜`docs/team_tasks/day06/contracts.md` §4.2/§5
> 本卡只含后端开发；验收由开发自证。

---

## 0. 一句话

把 Day 6 审查中最伤正确性的三件事收口：**回合终态与打断能落库、上下文不再因 NULL 崩、状态/记忆/指标/改绑并发不再静默失真**。

## 1. 写作用域（独占）

```text
app/services/response_service.py
app/services/history_service.py
app/audio/signaling.py              # 仅 interrupt / ack 分支与异常包裹
app/core/context.py
app/services/state_service.py
app/services/memory_service.py
app/services/metrics_service.py
app/services/staff_service.py
app/api/staff.py
app/api/schemas/staff.py
tests/test_response_service.py
tests/test_context_builder.py
tests/test_state_service.py
tests/test_memory_service.py
tests/test_staff_reassign.py
```

**禁触**：`app/services/turn_service.py`（A3）、`app/system_one/**`（A）、`app/agents/**`（C）、`app/scheduler/**`（C2）、`frontend/**`（D）、`0013` 及以前迁移。

---

## 2. B1 · 回合终态与打断闭环（1.25 人日）

### 2.1 目标

关闭审查 **P1-1、P1-3** 与 **P2-7~11、P3-27/28/30/32**：末段 `played` / 打断都必须调用 `finish_turn`，`assistant_delivered_text` 不再恒 NULL，`response_completed` 不再双写，C→S `interrupt` 真正生效。

### 2.2 现状证据

- `history_service.finish_turn()` 生产零调用；`turn_service` 第 5 步只 `commit_response(delivered_text=None, status="playing")`。
- `response_service` 末段 `played` 只置 `PHASE_ENDED`，不回写投影；`canceled` 分支直接 return，不改状态。
- `ResponseService.interrupt()` 有实现，但生产零调用；`signaling.py` 无 `interrupt` 消息类型。
- `response_completed` 由统一出口与响应层各写一次，口径矛盾。

### 2.3 实施步骤

1. **先加契约测试**（红）：
   - 末段 `played` → `conversation_turns.status=completed`、`assistant_delivered_text` 非 NULL；
   - 打断 → `status=interrupted`、`delivered_text` 为 `[0..last_played]` 连续前缀；
   - 重复 ack/迟到 ack → 不重复写事件；
   - `response_completed` 每轮恰好 1 条。
2. `response_service.py`：
   - 抽出 `_settle_turn(state, *, status, delivered_text)`：调用 `HistoryStore.commit_response` + `finish_turn`；
   - 末段 `played`：先写 `response_segment_delivered`，再写唯一 `response_completed`，再 `commit_response(delivered_text=..., status="completed")`、`finish_turn("completed")`；
   - `canceled`：校验 ①②③ 后，置 `PHASE_ENDED`、`delivery_status="interrupted"`，写 `response_interrupted`，按水位截断 `delivered_text`，`commit_response` + `finish_turn("interrupted")`；`segment_id` 与水位不一致时告警并以水位为准（P2-8）；
   - `interrupt()`：幂等；已结束 no-op；bump epoch；取消 cancellable；写事件；结算；不再只改内存；
   - `response_completed` 双写：统一出口（A3 改）只保留 `response_started`/生成事实；响应层唯一写 `response_completed`；本卡在 `commit_response` 侧检测已存在则只补投影（P2-10）；
   - epoch/水位回写 Redis：`session:{thread_id}:epoch` 与 `response:{response_id}` 最小字段，TTL 2h；重启后从 Redis 恢复，避免全拒（P2-9，单进程/重启范围；多 worker 登记 Day 9）。
3. `signaling.py`：
   - 新增 C→S `interrupt` 分支：`{type,response_id,reason}`；校验设备/会话归属；调 `ResponseService.interrupt()`；异常包裹，不炸连接；
   - `identity_confirm/client_diag/media` 等 handler 统一 try/except（P3-28）；
   - `segment_ack` 现有四步校验不动，只补 `canceled` 终态语义。
4. `history_service.py`：
   - `read_turns` limit 上限 50（P3-32）；超限截断 + 日志；
   - `finish_turn` 幂等：已终态不覆盖；`completed` 不得覆盖 `interrupted`。
5. `response_service.py` 公开方法用 `.get()`/显式错误，避免 KeyError 穿透（P3-30）；`_safe_append_event` 不吞 `CancelledError`（P3-27）。
6. 断连补偿：`start()` 失败重试 1 次 + `response_delivery_failed` 事件/告警；完整重放留 Day 9 登记（P2-11）。

### 2.4 交付物

- `response_service.py` 终态/打断重构 + Redis 回写。
- `signaling.py` `interrupt` 分支 + 异常包裹。
- `history_service.py` limit/幂等。
- 扩展 `tests/test_response_service.py`、`tests/test_history_store.py`。

### 2.5 验收（命令级）

```bash
uv run pytest -q tests/test_response_service.py tests/test_history_store.py
# 必过用例：
# 1) played_last_segment_finishes_turn
# 2) interrupted_turn_truncates_delivered_text
# 3) duplicate_played_does_not_duplicate_completed
# 4) canceled_same_epoch_played_rejected
# 5) interrupt_idempotent
# 6) redis_epoch_restore
```

```sql
-- 真实一轮后
SELECT status, assistant_delivered_text IS NOT NULL AS has_delivered
FROM conversation_turns ORDER BY started_at DESC LIMIT 1;
-- 期望：completed | t

SELECT count(*) FROM conversation_events
WHERE turn_id = :turn_id AND event_type = 'response_completed';
-- 期望：1
```

- 打断后 200ms 内服务端状态为 `interrupted`，`delivered_text` 只含已 `played` 前缀。
- 重复/迟到 ack 不产生第二条 `response_completed`，不推进水位。
- `ResponseService.interrupt` 生产调用点 ≥1（signaling 分支）。

### 2.6 依赖与交接

- 依赖 A1 的 `interrupt` 契约；与 A2/A3 并行。
- 交接 D1：`interrupt` 消息、`response_interrupted` 事件、`delivery_status` 取值。
- 交接 A3：`TurnOutcome` 可带 `route_layer`，但不改响应流字段。

---

## 3. B2 · 上下文 NULL/预算 + 状态降级 + 记忆生产接线（1.25 人日）

### 3.1 目标

关闭 **P1-2** 与 **P2-13~18/21、P3-29/35**：空送达/打断组合不再崩，Redis 故障语义可达，记忆提取真正进生产，预算不再双重失真。

### 3.2 实施步骤

1. `app/core/context.py`：
   - `assistant = getattr(t, "assistant_delivered_text", "") or ""`；`user_text` 同样 `or ""`；`None + str` 不再发生；
   - `status=="interrupted"` 时只在非空文本后追加标记，空文本不拼；
   - generic 轮短路 `waiting_for` 任务绑定：`is_generic` 时不注入“优先解释为用药确认”（P2-16）；
   - 四处裸 `except Exception: return ""` 改为捕获具体异常 + `logger.warning` + `degraded` 标记（P2-17）；
   - 预算：`history_turns` 与 `waiting_for` 计入 `budget_used`；中文 token 估算改为可复算口径（2 字符≈1 token 只作粗略值，新增 `estimate_tokens` 单元测试）（P2-18）；
   - 每轮 `read_turns` 只调 1 次，memories/facts 去重查询，摘要与历史去重（P2-21）；
   - 已知 dataclass 不再防御式 `getattr`；零记忆不误报 degraded（P3-35）。
2. `app/services/state_service.py`：
   - `snapshot()` 不再吞掉需要外层的 RedisError：加 `strict` 参数或 `_snapshot_raw`；`apply(is_medication_task=True)` 能真正抛 `StateUnavailable`（P2-13）；
   - Redis 写失败降级内存后，恢复时把内存版本回写/合并到 Redis；`snapshot` 读 Redis 前先比较内存版本，避免旧 Redis 覆盖（P2-14）；
   - `from_dict` 加 schema 校验（版本、必填 dict、字段类型）；Lua 返回 -2 不走无条件 SET，改为冲突/重试；`_memory_store` 加 LRU/TTL 上限（P2-15）；
   - `state.apply` 失败时保留已写事实，写 `turn_failed` 兜底，不全轮回滚（P3-29）。
3. `app/services/memory_service.py` + `turn_service` 调用边界：
   - 暴露 `async extract_after_turn(*, elder_id, thread_id, turn_id, user_text, assistant_text) -> int`；
   - 用 `asyncio.create_task` 或后台队列在回合结算后执行，不阻塞首包；失败只记日志；
   - `insert_candidate` 必须带 `source_turn_ids`；召回强制 `elder_id + status='active' + 有效期`；
   - 本日只做“偏好/事实”一类低风险候选，不做冲突治理/遗忘（留 Day 8）。
4. 与 B1 的边界：B1 在终态结算后调用 `extract_after_turn`；若 B2 未完成，B1 用 `MemoryExtractionPort` stub，不阻塞。

### 3.3 交付物

- `context.py` 修复 + 预算口径。
- `state_service.py` 故障语义/回写/校验。
- `memory_service.py` 生产入口 + 异步接线。
- 扩展 `tests/test_context_builder.py`、`tests/test_state_service.py`、`tests/test_memory_service.py`。

### 3.4 验收（命令级）

```bash
uv run pytest -q tests/test_context_builder.py tests/test_state_service.py tests/test_memory_service.py
# 必过用例：
# 1) interrupted_with_null_delivered_does_not_crash
# 2) llm_messages_never_content_null
# 3) generic_turn_skips_waiting_for_binding
# 4) redis_down_medication_raises_state_unavailable
# 5) redis_recovery_merges_memory_version
# 6) budget_counts_history_and_waiting_for
# 7) memory_extract_after_turn_persists_candidate
```

- 构造 `assistant_delivered_text=NULL + status=interrupted`，上下文构建不抛异常，消息内容为 `""` 或带标记的非空字符串，绝不是 `None`。
- Redis 停止 + `is_medication_task=True`：抛 `StateUnavailable`，不是 `StateVersionConflict`。
- 记忆提取：一轮对话后 `memory_chunks` 至少 1 条候选，`source_turn_ids` 非空；首包延迟不因提取增加 >50ms（本地测）。
- 超预算用例：低优先级先裁，`safety/policy`、`identity`、`active_task`、`current_turn` 不裁。

### 3.5 依赖与交接

- 无硬依赖，可与 A 线并行；B1 的 memory 调用是软依赖。
- 交接 C1：`recall_for_turn` 函数签名与返回结构。
- 交接 A3：`decision_logs` 不需要 context 改动；若路由需要 `waiting_for`，按现有 `StateSnapshot.task` 读取。

---

## 4. B3 · metrics 白名单 + E20 并发 + API 边界（0.5 人日）

### 4.1 目标

关闭 **P1-12、P2-19/20、P3-33/34**：`response_segment` 能落库；E20 并发不产生 0 责任护工；接口参数有边界。

### 4.2 实施步骤

1. `metrics_service.py`：DB 白名单加入 `response_segment`；`record_stage` 不再每段 WARNING；补一条落库断言。
2. `staff_service.py`：
   - `reassign_elders` 对 `elder_id` 排序后 `SELECT ... FOR UPDATE` 锁长者行，或在同一事务锁 `elder_staff_assignments`；两个 admin 并发摘不同护工只允许一个成功（P2-19）；
   - 加乐观并发：用 `elders.updated_at` 或 `version` 做 CAS；后提交者 409，不静默覆盖（P2-20）；
   - 409/403/404 失败路径写 `audit_logs`（操作者、trace_id、原因、差集）；
   - admin 不能停用自己/最后一个 admin（P2-20 自我保护）。
3. `api/staff.py` / `api/schemas/staff.py`：
   - `role/status` 用 `Literal`/枚举校验，非法值 422（P3-33）；
   - `elder_ids` 上限 200，超出 422（P3-34）；
   - 409/403 映射明确，不返回 500。
4. `AdminUsersPage` 的完整分页由 D2 做；本卡只保证后端契约与并发正确。

### 4.3 验收（命令级）

```bash
uv run pytest -q tests/test_metrics_service.py tests/test_staff_reassign.py
# 必过：response_segment 落库；并发摘护工 1 成功 1 409；最后 admin 保护；elder_ids 201 个 422
```

```sql
SELECT count(*) FROM metrics WHERE stage='response_segment' AND created_at > now() - interval '5 min';
-- 期望：> 0（一轮分段后）
```

- 并发测试：两个事务同时移除不同 primary_care，最终该长者至少 1 位责任护工；失败方 409 且审计有行。
- `GET /api/staff?role=superman` 返回 422，不是 200 空列表。
- `PUT /api/staff/{id}/elders` 传 201 个 UUID 返回 422。

### 4.4 依赖与交接

- 无前置；与 B1/B2 同人串行，可与 A/C/D 并行。
- 交接 D2：409/403/422 的错误码与字段；交接 A3：`decision_logs` 不需要 metrics 白名单。

---

## 5. 完成记录模板

```text
任务：B1/B2/B3
状态：已完成 / 阻塞
证据：
- 代码：<commit>
- 命令：<pytest 输出 / SQL 结果>
- 数据：<turn_id / trace_id / 并发结果>
- 未完成：<无 / 具体项 + 原因 + 下一步>
```

## 6. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版：B1 终态/打断、B2 上下文/状态/记忆、B3 metrics/E20/API；覆盖审查 P1-1/2/3、P2-7~21、P3-27~35 |
