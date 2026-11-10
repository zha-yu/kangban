# 交接件 · A3 → B1：终态结算还差两步（`generated_text` 正在丢）

> 发起：dev-A（2026-11-10 17:26）　｜　接收：dev-B1（终态结算 / `response_service`）
> 背景：dev-B 的 `handoff-B-to-A-终态结算patch需求.md`（J2）要求 A 删掉第 5 步非 generic 的
> `commit_response` 双写。**A 已按交接删除并合入**（commit `80fd7a7`，`turn_service` 第 5 步现在
> 只发 `response_id`，generic 直写保留）。删除后暴露出 B 侧尚未落地的两步——不补上，
> **非 generic 轮次的助手原话会从事件流水里消失**。

---

## 0. 一句话

末段 `played` 现在只写 `response_completed({"total_segments": N})`；`commit_response` 看到事件
已存在就**只补投影、不补 payload**（只追加语义），所以 `generated_text` 永远不会落库，
`conversation_turns.assistant_delivered_text` 也恒为 NULL（P1-3 未闭、P2-7/P2-10 只完成一半）。

---

## 1. 现状证据（本机 HEAD `80fd7a7`）

| 位置 | 现状 | 问题 |
| --- | --- | --- |
| `app/services/response_service.py` L471-481（末段 `played`） | 写 `response_completed({"total_segments": state.total})`，`delivery_status="delivered"` | payload **没有** `generated_text` / `delivered_text` / `response_status`；没有 `commit_response`、没有 `finish_turn` |
| `app/services/response_service.py` L486-540（`interrupt`） | 写 `response_interrupted`，`status=PHASE_ENDED`、`delivery_status="interrupted"` | 同样没有结算调用；`delivered_text` 前缀已算好（`state.delivered_text`）却没用上 |
| `app/services/history_service.py` L401-419 | 看到 `response_completed` 已存在 → 只 `_apply_delivered` + 打一条 `history_generated_text_not_persisted` warning | **不会**把 `generated_text` 补进 payload（事件只追加，不改写） |
| `app/services/turn_service.py` 第 5 步 | 只发 `response_id`，不写库 | 生成事实的落库责任已整体交给响应层 |

结论：**要让 `generated_text` 留下，只能在写那条 `response_completed` 的那一刻就把 payload 写全**，
事后补不了（`commit_response` 刻意不改写事件）。

---

## 2. 需要补的两处（建议直接照抄）

### 2.1 补齐依赖：注入结算口（`ResponseService` 现在拿不到 DB 会话）

`ResponseService.__init__` 目前只有 `deliver` / `events` / `tts_queue`。建议注入一个回调，
**不要**让 `ResponseService` 自己持 `AsyncSession`：

```python
# 装配层（app/main.py）实现；ResponseService 只认这个签名
SettleFn = Callable[..., Awaitable[None]]

async def _settle_turn(
    *, thread_id: str, turn_id: str, response_id: str,
    generated_text: str, delivered_text: str | None, status: str,
) -> None:
    async with session_factory() as session:          # 独立短事务
        await history_service.commit_response(
            session, turn_id=turn_id, response_id=response_id,
            generated_text=generated_text, delivered_text=delivered_text, status=status,
        )
        await history_service.finish_turn(session, turn_id=turn_id, status=status)
        await session.commit()
```

> **为什么必须独立短事务**：`ResponseService` 跑在 turn 事务之外（第 7 步下发之后），
> 若复用轮次会话的 `SELECT … FOR UPDATE` 行锁会互锁——`turn_service` docstring 里
> `trace a5dc0863` 记的就是这条（`response_started` 后 40s 零段下发 → `ws_dead` 45s 判死）。

### 2.2 末段 `played`（L471-481）

```python
if segment_id == state.total - 1:
    state.status = PHASE_ENDED
    state.delivery_status = "delivered"
    generated = "".join(state.segments)               # 全文（分段是下发用，全文才是事实）
    await self._safe_append_event(
        state,
        EVENT_RESPONSE_COMPLETED,
        {
            "generated_text": generated,              # ← 现在缺这三个
            "delivered_text": state.delivered_text,   #   连续已播前缀
            "response_status": "completed",
            "total_segments": state.total,
        },
    )
    await self._settle(                                # ← 新增：投影 + 终态
        thread_id=state.thread_id, turn_id=state.turn_id, response_id=response_id,
        generated_text=generated, delivered_text=state.delivered_text, status="completed",
    )
```

### 2.3 打断（L518-530）

`response_interrupted` 照旧写（那是"被打断"的事实），**另加一次结算**：

```python
await self._safe_append_event(state, EVENT_RESPONSE_INTERRUPTED, {...})
state.status = PHASE_ENDED
state.delivery_status = "interrupted"
await self._settle(                                    # ← 新增
    thread_id=state.thread_id, turn_id=state.turn_id, response_id=response_id,
    generated_text="".join(state.segments),
    delivered_text=state.delivered_text,               # 已按水位截断的连续前缀
    status="interrupted",
)
```

此时库里**没有** `response_completed`（没播完），`commit_response` 会按"不存在则新建"分支
写一条带完整 payload 的 `response_completed`——这是 `history_service` 既有语义，不需要你改它。

---

## 3. 不许动的三处（避免两边打架）

1. **A 的第 5 步不要再加回写**（`turn_service.handle_user_turn`）：那是 P2-10 要消掉的双写，
   加回来会重新引入"payload 恒为 playing"。
2. **generic 直写保留**：generic 无 `ConversationTurn` 投影，`commit_response` 会抛
   `HistoryError`；那一条 `response_completed` 直写是它的唯一写口。
3. **事件只追加**：不要用 UPDATE 去补老事件的 payload（`history_service` 的只追加纪律）。

---

## 4. 验收（三条命令，期望写清楚）

```bash
uv run pytest -q tests/test_response_service.py tests/test_history_store.py
# 期望：played_last_segment_finishes_turn / interrupted_turn_truncates_delivered_text /
#       duplicate_played_does_not_duplicate_completed 三条新用例过；且
#       tests/test_turn_service.py::test_event_flow_ordered_and_monotonic 仍过
#       （A 已按新语义改成「handle 后只有 turn_started + asr_final_committed」）

# 手工对账（真机一轮播完后）
SELECT e.event_type, e.payload_json->>'response_status' AS status,
       left(e.payload_json->>'generated_text', 20)      AS gen_head,
       t.assistant_delivered_text, t.status
FROM conversation_events e JOIN conversation_turns t USING (turn_id)
WHERE e.turn_id = :turn_id ORDER BY e.seq;
# 期望：response_completed 恰好 1 条，payload 有 generated_text、status=completed；
#       assistant_delivered_text 非 NULL 且等于已播前缀；t.status=completed

# 打断一轮后
# 期望：status=interrupted、assistant_delivered_text = 已播前缀、无重复 response_completed
```

---

## 5. 如果今天落不了（合并顺序建议）

- **首选**：B1 这两步与 A 的两个 commit 同批或先合——否则 develop 上非 generic 轮次会丢
  `generated_text`（只影响复盘与上下文投影，不影响当轮回复下发）。
- **次选**：A 的两个 commit（`1e75cd9` / `80fd7a7`）先不合，等 B1 一起进；
  A 侧不打算把双写加回来（那会退回"payload 恒为 playing"的老问题）。
- 无论哪种，**不要**只合 `response_service` 而不合 `turn_service`：两者都是"第 5 步语义"的两半，
  分开合会让同一对话出现两套"谁写 `response_completed`"。

---

## 6. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版（A3 收口）：指出删双写后暴露的两处缺口、给出注入式 `_settle` 方案与两段精确补丁、说明为何 payload 必须一次写全（`commit_response` 不改写事件）、列出不许动的三处与三条验收命令、给合并顺序建议 |
