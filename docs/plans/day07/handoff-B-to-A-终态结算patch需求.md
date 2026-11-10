> 状态：已完成　｜　版本：v1.0　｜　日期：2026-11-10
> 关联：docs/team_tasks/day07/dev-a-day07.md（A3）｜docs/team_tasks/day07/README.md §4/§5.4｜docs/plans/day07/dev-b-day07-工作推进计划.md §4（交接矩阵）
> 上游依据：adhoc-20261110-day6全面审查.md P2-10（response_completed 双写）｜技术设计文档 §5.1（单写口约定）
> 交付方向：dev-B（B1 回合终态与打断闭环，已收口）→ dev-A（A3 turn_service/路由，J2 节点）

# 交接件 · `turn_service` 终态结算 patch 需求（B1 → A3，J2 节点）

> B1 已把终态结算收口到响应层（`ResponseService._settle_turn`），`turn_service.py`
> 是 **A3 独占文件**，dev-B 不动它——本文是 patch 需求清单，A3 改动时按此执行。
> 改动前请先读 §2 的现状说明：**当前系统行为是正确的**（幂等保护兜住了双写），
> patch 是为了达成 P2-10 的目标语义与记忆提取效力，不是修 bug。

## 1. 现状基线（B1 收口后，2026-11-10 实测）

| 事实 | 证据 |
| --- | --- |
| 终态唯一写口在响应层 | `response_service._settle_turn`：`commit_response`（投影）→ `finish_turn`（终态）→ 记忆提取调度；末段 played / interrupt / canceled 三路均结算 |
| `finish_turn` 生产调用 ≥1 | signaling interrupt 分支 → main `_on_interrupt` → `ResponseService.interrupt` → 结算 |
| `commit_response` 幂等 | 按 `(turn_id, response_id)` 已有 `response_completed` 幂等，重复只补投影 |
| 第 5 步仍在写 `response_completed` | `turn_service.py` 七步流第 5 步（约 726–751 行） |

## 2. ⚠️ 当前双写的实际表现（为什么系统仍正确、为什么仍要改）

第 5 步非 generic 分支先写 `response_completed`（payload：`generated_text=全文、
delivered_text=None、response_status="playing"`），响应层结算时同 `response_id`
再调 `commit_response` → 命中幂等分支**只补投影**。所以：

- 事件**恰好 1 条**（无重复）——系统正确性由幂等兜住；
- 但事件 payload 永远停留在第 5 步那条：`response_status="playing"`、`delivered_text=None`，
  最终态语义只体现在 `turn_completed` 事件里——**P2-10 的目标口径未达成**；
- generic 分支第 5 步直写是 generic 轮 `response_completed` 的**唯一写**（generic 无投影、
  结算侧跳过 commit_response），这条**不要删**，见 §3.2。

## 3. patch 需求清单（共 2 项 + 1 项提示）

### 3.1 第 5 步非 generic 分支：删除 `commit_response` 调用（P2-10）

`turn_service.py` 七步流第 5 步（约 744–751 行）的
`self._history.commit_response(..., delivered_text=None, status=DELIVERY_STATUS_PLAYING)`
**删除**。生成事实由响应层 `_settle_turn` 在终态时以正确 payload
（`response_status="completed"/"interrupted"`、`delivered_text=已播前缀`）唯一写入。

- 删除后第 5 步只剩"生成 `response_id`"，`response_id` 继续传给第 7 步 `start()`（现口径不变）；
- 12 值事件枚举里没有"生成事实"独立事件，**不要**新增 event_type——生成全文本来就该
  随终态事件落 payload（§5.1 单写口约定的原始意图）；
- 删除后请复跑：`uv run pytest -q tests/test_turn_service.py tests/test_response_service.py`
  ——若 turn_service 既有测试断言了第 5 步写事件，按新口径同步改测试（A3 权限内）。

### 3.2 `TurnContext` 补 `user_text`（B1↔B2 记忆提取输入）

`response_service.TurnContext` 已有 `user_text: str = ""` 字段（B1 Step 5a 预留）。
生产构造点 `MinimalContextBuilder.build`（`turn_service.py` 约 446 行）现在拿不到用户原文：

- `TurnRuntime`（105 行）无 `user_text` 字段，`build(runtime, snapshot, turn)` 入参也没有；
- 后果：终态结算的 `extract_after_turn` 只凭**回复文本**提取记忆，用户侧偏好
  （"我喜欢越剧"）提取效力打折。

**推荐 patch**（A3 权限内，两处小改）：
1. `TurnRuntime` 增加 `user_text: str = ""` 字段；`handle_user_turn` 构造 runtime 处带上本轮 ASR final；
2. `MinimalContextBuilder.build` 构造 `TurnContext` 时填 `user_text=runtime.user_text`。

**注意**：`ContextBuilderLike.build` 签名**不要改**（装配缝契约，动了会波及全部注入方）。

### 3.3 提示（不改代码，知晓即可）

- `StateVersionConflict.actual` 可能取 `-2`（B2 Step 2 起，损坏标记）：捕获后**盲重试会再撞 -2**，
  需配合 `StateCenter.clear()` 重建，不要写成无限重试；
- `TurnOutcome` 可带 `route_layer`，但**不改响应流字段**（路由降级走 `degraded` 标记，
  前端 D1 按标记出提示，不加协议字段）。

## 4. 验收（A3 patch 完成后）

```bash
uv run pytest -q tests/test_turn_service.py tests/test_response_service.py tests/test_history_store.py
```

- `response_completed` 每轮恰好 1 条（事件 payload 的 `response_status` 为终态值）；
- 真实一轮后 SQL（同 handoff-B-to-D §5）：`response_status` 不再出现 `playing`；
- 打断一轮后：`extract_after_turn` 的入参 `user_text` 非空（可从 debug 日志或单测替身验证）。

## 5. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | B1 收口（Step 5d）正式交接：第 5 步双写口删除 + TurnContext.user_text 传递 patch 需求 |
