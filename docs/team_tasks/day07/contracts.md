# Day 7 契约增量 · 触达语音接线（TD-1）与 played 闭环（TD-2）

> 状态：已冻结　｜　版本：v1.0　｜　日期：2026-11-09
> 关联：docs/team_tasks/day07/dev-c-触达语音接线.md、docs/team_tasks/day07/dev-c-触达played闭环.md
> 上游依据：PRD §4.5（用药触达 T0/+5/+10/+15）、FR-MED-05｜技术设计文档 §6（medication_confirmation_graph）
> 变更记录见文末

## 1. 触达消息（S→C，TD-1）

载体沿用 `tts_text`（触达不属于任何回复流，**不带** `response_id` / `epoch` / `segment_id`，
与 day06/contracts.md §4.1 的回复流严格隔离）。新增四个**可选**字段，仅在触达消息出现；
缺失时旧客户端按整段一播、只渲染大字幕——降级路径零改动。

```jsonc
{
  "type": "tts_text",
  "text": "到吃药时间了，请确认氨氯地平是否已经服用。",
  "degraded": false,
  "touch_id": "9f0c…hex",          // 触达唯一 message_id（uuid4 hex）；touch_played 回带
  "touch_event_id": "<uuid>",      // 用药事件（medication_events.reminder_id）字符串，回传用
  "touch_attempt_no": 1,           // 第几次触达（1=T0 … 4=T+15），回传用
  "audio": {                        // 仅 TTS 合成成功时携带；缺省 = 纯字幕降级
    "format": "pcm_s16le",
    "sample_rate": 16000,
    "data_b64": "<base64>"          // s16le / 单声道 PCM 整段（C1 口径 16k/16bit/mono）
  }
}
```

- 合成失败（`TTSUnavailable` 等）→ 不带 `audio` 字段，消息形状与 Day 6 完全一致，
  `medication_attempts.tts_result` 记 `tts_failed`（CHECK 白名单已含，无需迁移）。
- 触达音频走 WS 文本帧 + base64，**不进** WebRTC 音频轨、不进回复流播放队列；
  前端用独立输出通道播放，与对话回复互不打断、互不串流。
- 没绑设备（`no_bound_device`）时不做合成（省一次 TTS 调用）。

## 2. touch_played（C→S，TD-2）

pad **播完**触达音频（音频缓冲自然播完）后上报；用户打断、播放失败、离线**不回**
（宁缺勿假）。仅字幕降级路径（无 `audio` 字段）不回。

```jsonc
{
  "type": "touch_played",
  "touch_id": "9f0c…hex",      // 原样回传 tts_text.touch_id
  "event_id": "<uuid>",        // 原样回传 touch_event_id
  "attempt_no": 1              // 原样回传 touch_attempt_no
}
```

服务端处理：`app/audio/signaling.py` 新增 `touch_played` 分支（与 `segment_ack` 同层，
互不干扰）→ 装配层回调 → `medication_service.mark_attempt_played(event_id, attempt_no)`
**幂等 UPDATE**：重复 / 迟到回执无操作、不报错。形状非法照 `segment_ack` 同款处理
（warn + error internal，不关连接）；回执是尽力而为的上报，落库失败只记日志不回执。

## 3. `medication_attempts.receipt` 白名单加值 `played`（TD-2 裁决留痕）

**裁决点**：任务卡 TD-2 留了「receipt 加新值 `played` vs 复用 `tts_finished`」的裁决。
本契约按**新增 `played`**冻结：`tts_finished` 是 0010 占位口径的历史值，语义是「卡片/播报
送达」，表达不了「触达语音真被听到」；复用会让两件事共用一个词，复盘时无法区分。
schema 变更（CHECK 加值）按主理人授权范围由 dev-C 自裁落地（迁移 `0013_touch_played_receipt`，
可回滚），**登记待主理人复核**，复核否决时只改白名单与 `mark_attempt_played` 的落值，
不动消息契约。

- `receipt` 白名单（迁移 0013 后）：`card_rendered` / `tts_finished` / `none` /
  `voice_dispatched` / `device_offline` / `no_bound_device` / **`played`**。
- 语义：`played` 只允许从 `voice_dispatched` 升级——`device_offline` / `no_bound_device`
  的触达音频没出过门，不能被回执「洗白」；已 `played` 再收回执幂等无操作。
- played **不参与**重试与终态判定（TD-2 只做留痕闭环，等待逻辑是否消费另立裁决）。

## 4. 客户端纪律（dev-D）

- 收到带 `touch_id` + `audio` 的 `tts_text`：大字幕照常渲染（现有路径零改动），
  音频解 base64 → 独立 AudioContext 播出；`onended` 自然播完才发 `touch_played`。
- 打断 / 播放失败 / 页面关闭：不发（宁缺勿假）；`touch_played` 发送失败不重试不阻塞。
- 带 `touch_id` 但无 `audio`（降级）：只渲染字幕，不发 `touch_played`。
- `touch_played` 与回复流 `segment_ack` 互不替代：触达消息不进 PlaybackQueue。

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 初稿：TD-1 触达消息（touch_id/touch_event_id/touch_attempt_no/audio）+ TD-2 touch_played + receipt 加 played（自裁留痕待复核） |
