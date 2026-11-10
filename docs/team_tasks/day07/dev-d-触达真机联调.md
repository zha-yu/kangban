# Day 7 · dev-D · 触达真机联调（TD-1/TD-2 收口验收）

> 状态：已完成　｜　版本：v1.1　｜　日期：2026-11-10
> 关联：docs/team_tasks/day07/dev-c-触达语音接线.md、docs/team_tasks/day07/dev-c-触达played闭环.md
> 上游依据：docs/team_tasks/day07/contracts.md v1.0 §1/§2（消息与回执口径）｜两张 dev-C 卡 v1.3（联调项移交本卡）
> 变更记录见文末

> **Day 7 执行调整（2026-11-10）**：本卡作为 `dev-d-day07.md` 的 D3 执行明细，不另计人日；除 TD-1/TD-2 真机验收外，增加 A3 路由降级与 C2 到点触达联调。发现后端缺陷回写 B1/C1/C2/A3，不在前端兜底伪造回执；本卡仍是开发联调，不设测试角色。

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
| v1.1 | 2026-11-10 | **已完成收口**：TD-1（真机收到触达卡片+大字幕+TTS 独立通道播出）与 TD-2（`medication_attempts.receipt=played` 落库，`played` 严格 +1、打断/关页不升级、重复回执幂等零重复组，基线差集法）双双 PASS，证据在 `docs/plans/day07/d-联调记录.md` L3 节。执行调整新增的 A3 路由降级联调因 A2 sidecar 未就绪阻塞，已在联调记录 L5 登记并催办 dev-A（前端降级提示已预留 `msg.degraded` 触发），不阻塞本卡；C2 到点触达联调 PASS（L2 复跑）。前置缺陷 L6#6 落库断裂已修复并随 commit 2e41242 收口 |
