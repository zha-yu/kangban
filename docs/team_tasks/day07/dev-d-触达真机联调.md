# Day 7 · dev-D · 触达真机联调（TD-1/TD-2 收口验收）

> 状态：未开始　｜　版本：v1.0　｜　日期：2026-11-10
> 关联：docs/team_tasks/day07/dev-c-触达语音接线.md、docs/team_tasks/day07/dev-c-触达played闭环.md
> 上游依据：docs/team_tasks/day07/contracts.md v1.0 §1/§2（消息与回执口径）｜两张 dev-C 卡 v1.3（联调项移交本卡）
> 变更记录见文末

## 1. 背景与目标

dev-C 已完成触达语音接线（TD-1）与 played 闭环（TD-2）的全部代码与单测
（commit 6c32846，pytest 985 全绿），本卡承接移交过来的两条**真机联调**验收项，
做完即两张上游卡的事实收口。

## 2. 验收标准（全部满足才可勾本卡完成）

1. **TD-1 · 在线出声**：真机 pad 会话中，用药触达消息到达后——
   - 大字幕正常渲染（现有路径零改动）；
   - 触达音频经独立通道播出（`audio` 字段 → AudioContext），与对话回复互不打断；
   - 触达消息**不带** `response_id`，不进 PlaybackQueue、不触发分段 ack。
2. **TD-2 · played 痕迹**：音频自然播完后，服务端 `medication_attempts.receipt`
   从 `voice_dispatched` 升级为 `played`（查库确认）；打断/关页/未播完**不**产生
   `played`；重复播完不报错（幂等）。

## 3. 步骤指引

1. 前置：`docker compose up -d postgres redis` + 后端 8000 + pad（HTTPS 或
   localhost；真机走局域网 `https://10.55.50.115`，getUserMedia 硬要求）。
2. 造一条到点用药计划（或直接调图入口触发 `remind`），真机在线收触达。
3. 播完查库：`SELECT attempt_no, receipt, tts_result FROM medication_attempts
   ORDER BY created_at DESC LIMIT 4;`
4. 边界各验一次：TTS 降级（无 `audio` 字段）只出字幕、不回执；触达与回复流
   并发，确认互不串流。

## 4. 非目标

- 不改任何后端/前端代码（发现问题回两张上游卡走修复，不在本卡内顺手改）；
- 不覆盖 scheduler 四时点编排（N10，Day 06 dev-a 卡范围）；
- 不做性能压测（延迟基线已有专卡）。

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 初稿：从两张 dev-C 卡 v1.3 移交真机联调验收项立卡（主理人裁决方案 A） |
