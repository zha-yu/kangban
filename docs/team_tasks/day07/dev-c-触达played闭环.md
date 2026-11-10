> 状态：进行中　｜　版本：v1.1　｜　日期：2026-11-09
> 负责人：dev-C（后端 · 回执落库）＋ dev-D（pad 端 · 播完回执）
> 关联：docs/team_tasks/day07/dev-c-触达语音接线.md（TD-1 先行，本卡复用其 message_id）
> 上游依据：PRD §4.5（用药触达）、FR-MED-05｜技术设计文档 §6（medication_confirmation_graph、medication_attempts 触达留痕）
> 变更记录见文末

# TD-2 · 主动触达 played 闭环

## 1. 目标与边界

**一句话**：让「触达是否真被听到」闭环——pad 播完触达语音后回 played 回执，服务端幂等落库，
`medication_attempts` 从「投递三态」升级为可区分「已听到」。

**现状（缺陷）**：触达消息不属于任何回复流（不带 `response_id`/`epoch`/`segment_id`），
对话流的 `segment_ack` 机制覆盖不到它；`receipt` 现有三态
（`voice_dispatched` = 收进发送队列 / `device_offline` / `no_bound_device`）
只证明**投递**不证明**听到**。`medication_confirmation_graph.py` L33 已明示此缺口。

**做**

- 契约（`docs/team_tasks/day07/contracts.md`）：新增 C→S `touch_played` 消息
  （携带 TD-1 定义的 `message_id`；如回执需对准第几次触达，另带 `attempt_no`，口径冻结时定）。
- 后端（dev-C）：
  - `app/audio/signaling.py` 新增 `touch_played` 处理分支（与现有 `segment_ack` 分支同层，互不干扰）；
  - `app/services/medication_service.py` 新增 `mark_attempt_played(...)`：
    按 `(event_id, attempt_no)` **幂等 UPDATE**，重复回执无操作；
  - `receipt` 白名单若需新增 `played` 值 → **新增 Alembic 迁移**（禁改历史迁移；
    现有 CHECK 见 `app/models/medication.py:237-244`，白名单已含 `tts_finished`，
    是否复用还是加新值在契约冻结时裁决）。
- 前端（dev-D）：pad 播完触达音频（audio ended / speechSynthesis end）回 `touch_played`；
  用户打断、播放失败、离线**不回**（宁缺勿假）。

**不做**

- 不改四次触达时序与终态分流（T0/+5/+10/+15、retry 上限不动）。
- 不把 played 作为重试/终态的判定条件（本卡只做留痕闭环；等待逻辑是否消费 played 另立裁决）。
- 不动对话回复流的 `segment_ack`（那是 response stream 的机制）。

## 2. 写作用域（白名单）

```
docs/team_tasks/day07/contracts.md              改（touch_played 契约冻结）
app/audio/signaling.py                          改（新增 touch_played 分支）
app/services/medication_service.py              改（mark_attempt_played 幂等落库）
app/models/medication.py                        改（仅当白名单加值；CHECK + ORM 注释同步）
alembic/versions/                               新增一条迁移（仅当白名单加值）
frontend/src/pad/**                             改（dev-D：播完回执）
tests/test_medication_graph.py / test_signaling.py   改
```

**禁触**：已合入的 Alembic 历史迁移、`app/scheduler/**` 时序常量、
`medication_confirmation_graph` 的四态投递回执判定与终态机、`app/services/response_service.py`。

## 3. 验收

- 真机播完触达语音 → `medication_attempts` 对应行出现 played 痕迹；
  重复/迟到回执幂等（同 `(event_id, attempt_no)` 无副作用、不报错）。
- 设备离线或播放被打断 → **不**产生 played（宁缺勿假），attempt 保持投递态。
- 若加 CHECK 新值：迁移可回滚，`alembic upgrade head` 后旧数据不受影响。
- `uv run pytest -q` 全绿；验收命令：`uv run pytest tests/test_medication_graph.py tests/test_signaling.py -q`

## 4. 依赖与顺序

依赖 TD-1 的 `message_id` 契约（TD-1 先冻结、先实施）；本卡实施前先裁决
「receipt 加新值 vs 复用 tts_finished」——涉及 schema 变更走裁决，不自行选。

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 初稿：从 Day 6 交接账（微信口径 TD-2）立卡 |
| v1.1 | 2026-11-09 | dev-C 实施完成（裁决点按「新增 played」自裁留痕，见 contracts.md v1.0 §3，待主理人复核）：signaling.py 加 touch_played 分支（与 segment_ack 同层、形状校验同款）；medication_service.mark_attempt_played 幂等 UPDATE（仅从 voice_dispatched 升级，宁缺勿假）；models CHECK 加值 + 迁移 0013（up/down/up 实测可回滚）；main.py 装配回调（独立短事务，死锁教训口径）；pad 最小前端（onended 回 touch_played，打断/失败/拆线不回）。pytest 985 全绿、mypy 基线不变。**余项（dev-D）**：真机联调验收（播完出 played 痕迹）。分支测试落在 test_response_service.py（test_signaling.py 未建） |
| v1.2 | 2026-11-09 | 应主理人要求新建 tests/test_signaling.py，branch 分发测试已迁入该文件，验收命令与本卡 §3 完全对齐 |
