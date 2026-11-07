# Task dev-C · 前端 · pad 端（超时表现、延迟测量、声纹联调）

> 状态：未开始　｜　版本：v1.1　｜　日期：2026-11-07
> 上游依据：PRD §5.3（AC-40/42）、FR-3D-01｜技术设计文档 §8 Day 5｜docs/plans/day05 S3、S6
> 联调节点：N1/N2/N5；详见 dag-项目工作流总览与联调节点.md
> v1.1 变更：按主理人裁决（2026-11-07）新增 C-5/C-6 两项——C2/C5 契约修订的端侧配套上报（wake_ts 随 auth 首帧、playback 经 client_diag），契约修订本体由 dev-A 在 A-2 执行（docs/plans/day05/a-后端观测与数据.md §3.1）

## 任务清单

| # | 任务 | 前置依赖 | 交付物 | 验收标准 |
| --- | --- | --- | --- | --- |
| C-1 | **三档超时前端表现**：ASR 超时提示「没听清再说一遍」；LLM 首 token 超时播「我想一想」占位；TTS 首包超时降级文字展示 | dev-B B-2 的 `degrade` 事件规范（N2） | pad 超时表现实现 + 录屏 | ① 收到 `degrade` 事件按 `stage` 渲染对应提示，文案取事件 `message`（与后端常量同源）② 三档逐个联演，无一次白屏或静默 ③ 提示样式走 pad 大字规范（1920×1080） |
| C-2 | **20 次延迟测量执行**：固定种子设备 + 王奶奶人设，跑 20 次「今天天气怎么样」，端侧 wake_ts 随打点上送 | dev-A A-2 完成通知（N1）、A-3 SQL 口径 | 测量原始记录（20 次×分段）+ 录屏留证 | ① 每次 trace_id 可在 metrics 表回查 ② 端侧分段（wakeup/connection/playback）数据完整 ③ 原始记录交 dev-A 出基线表，不得手工修饰数字 |
| C-3 | **声纹闭环真人实测**：pad 录入页操作 3 段真人录音 → 同人第二次登录命中 → 非同人走主动确认 | dev-A（identity/voiceprint 后端）配合（N5）；阈值已裁决 0.55（PRD v2.8） | 实测记录（含 voice 终态与 cosine 值） | ① 同人命中路径通、cosine 值入记录 ② 非同人触发「请问是哪位在说话？」 ③ 任一步失败按 C4 降级放行不卡死 ④ 实测记录回填 day04/task-01 卡并由原执行者更新状态行 |
| C-4 | **口型达标取证**（已裁决维持能量近似口径）：播放任一 TTS 录屏口型开合 | 无 | 录屏留证（供演示与 AC 复核） | 口型随音频能量开合可见（PRD FR-3D-01 口径，不升级 viseme） |
| C-5 | **端侧 wake_ts 上报**（主理人裁决 R2，2026-11-07）：KWS 命中时刻随 auth 首帧上送；KWS 未命中/不可用时不填该字段，走后端 WS accept 兜底 | dev-A 契约修订完成（C2 `AuthMsg` 追加可选 `wake_ts`，A-2 内执行） | pad auth 首帧改动（KWS 命中→auth 带 `wake_ts`） | ① KWS 命中路径 metrics 表 `wakeup` 行 `source=client` 且 wake_ts 有值 ② 未命中路径 `source=backend` 兜底不缺行 ③ 字段名与 N1 规范一致 |
| C-6 | **playback 段打点上报**（主理人裁决 R3，2026-11-07）：pad 播放 TTS 时经 client_diag 上报 playback 耗时 | dev-A 契约修订完成（C5 client_diag 追加 `wakeup`/`playback` stage，A-2 内执行）；TTS 下行链路在播 | pad client_diag 上报改动 | ① 每次播放产生 `stage=playback` 打点、字段名与 `record_stage` 规范一致 ② 不含转写全文与隐私字段 ③ dev-A 侧 metrics 表可按 trace_id 回查 |

## 联调要求

- **N1**：A-2（打点收口 + 契约修订）通知后执行 C-5/C-6 端侧配套改动，随即开测 C-2；端侧打点字段名与后端 `record_stage` 规范严格一致。
- **N2**：与 dev-B 联演三档降级并录屏，回填 B-2 验收证据。
- **N5**：声纹实测需 dev-A 后端在线配合；实测通过是 M5.4 演示硬前置。
- 完成定义：任务表全绿 + `pnpm --dir frontend build` 通过 + 汇报清单（对照 docs/plans/day05 S3/S6 自检项）。
