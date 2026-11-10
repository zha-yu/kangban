> 状态：已完成　｜　版本：v1.3　｜　日期：2026-11-10
> 负责人：dev-C（后端 · 合成与下发）＋ dev-D（pad 端 · 播放）
> 关联：docs/team_tasks/day07/dev-c-触达played闭环.md（本卡先行，TD-2 复用本卡的消息 id）
> 上游依据：PRD §4.5（用药触达 T0/+5/+10/+15，每次等 60s）、FR-MED-05（语音触达）｜技术设计文档 §6（业务子图 medication_confirmation_graph）
> 变更记录见文末

# TD-1 · 触达语音接线（WS）

## 1. 目标与边界

**一句话**：主动触达（用药提醒）从「只有字幕」升级为「语音 + 字幕」——服务端把触达文本经 TTS 合成 PCM，经 WS 下发音频，pad 端走播放轨出声。

**现状（缺陷）**：`medication_confirmation_graph._deliver_reminder` 经信令层 `deliver()` 只发
`{"type": "tts_text", "text": ..., "degraded": false}` 字幕消息（graph 文件 L148-164），
pad 端只渲染大字幕不出声——「语音触达」名不副实。

**做**

- 契约增量（`docs/team_tasks/day07/contracts.md`，先冻结再动代码）：
  触达消息携带音频（新增 `tts_audio` 消息或在 `tts_text` 上扩可选 `audio` 字段），
  并为触达消息引入稳定 `message_id`（TD-2 的 played ack 依赖它）。
- 后端（dev-C）：触达下发前经 `build_tts()`（现有 `app/audio/tts.py`，EdgeTTSProvider
  + `mp3_to_pcm`）合成 PCM；合成失败 → 降级为纯字幕并把 `tts_result` 记 `tts_failed`
  （`medication_attempts.tts_result` CHECK 白名单已含该值，无需迁移）。
- 前端（dev-D）：pad 收触达音频 → 喂播放轨播出；播放期间字幕同步高亮；
  仅字幕（降级）路径保持现状零改动。

**不做**

- 不改对话回复流（response stream）的播报与 ack 机制——触达消息**不带**
  `response_id` / `epoch` / `segment_id`，与回复流严格隔离。
- 不改 scheduler 触达时序（T0/+5/+10/+15 常量不动）。
- 不改 `medication_confirmation_graph` 的四态回执判定逻辑（那是 TD-2 的界面）。

## 2. 写作用域（白名单）

```
docs/team_tasks/day07/contracts.md              改（触达音频消息 + message_id 契约冻结）
app/audio/signaling.py                          改（触达下发出口注入 TTS 合成 + 新消息 TypedDict）
app/graphs/medication_confirmation_graph.py     改（仅 _deliver_reminder 消息形状与 message_id）
app/services/medication_service.py              改（tts_result 落库口径）
frontend/src/pad/**                             改（dev-D：音频播放轨 + 字幕同步）
tests/test_medication_graph.py 等对应测试        改
```

**禁触**：`alembic/versions/**`（本卡无 schema 变更）、`app/scheduler/**`（时序不动）、
`app/services/response_service.py`（回复流不动）、四次触达常量。

## 3. 验收

- 联调：设备在线时触达到达即出声（真机/浏览器），字幕同步；TTS 断网/失败时仍出字幕、不白屏、`tts_result=tts_failed`。
- 触达与对话回复互不打断、互不串流（触达消息无 response_id，前端按独立会话播）。
- `uv run pytest -q` 全绿；新增用例覆盖：合成成功 / 合成失败降级 / 设备离线三路。
- 验收命令：`uv run pytest tests/test_medication_graph.py tests/test_signaling.py -q`

## 4. 依赖与顺序

先冻结契约再动代码；本卡先行，TD-2（played 闭环）依赖本卡的 `message_id`。

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 初稿：从 Day 6 交接账（微信口径 TD-1）立卡 |
| v1.1 | 2026-11-09 | dev-C 实施完成：契约冻结（contracts.md v1.0 §1）；signaling.py 扩 `tts_text` 触达四字段 + `synthesize_touch_audio`；graph `_deliver_reminder` 注入 `tts_fn`（可测试）并落 `tts_result`；pad 最小前端（touch-audio.ts 独立播放轨 + 字幕照旧）。pytest 985 全绿、mypy 基线不变、前端 tsc 通过。**余项（dev-D）**：真机联调验收第 1 条（在线出声/字幕同步）。分支测试落在 test_response_service.py（test_signaling.py 未建，segment_ack 同层测试本就在该文件） |
| v1.2 | 2026-11-09 | 应主理人要求新建 tests/test_signaling.py：signaling 层契约形状 + segment_ack/touch_played 分支分发测试（原散落 test_response_service.py 的段落）整体迁入，验收命令与本卡 §3 完全对齐 |
| v1.3 | 2026-11-10 | 主理人裁决方案 A：真机联调验收第 1 条（在线出声/字幕同步）移交独立卡 dev-d-触达真机联调.md，本卡随代码合入（6c32846）收口为已完成 |
