# dev-C · 前端 pad 语音与 3D（执行计划）

> 状态：进行中　｜　版本：v1.4　｜　日期：2026-11-08
> 关联：docs/team_tasks/day05/dev-c-前端pad语音与3D.md（任务卡 v1.1）、docs/plans/day05/day-05-延迟基线与护栏.md（Day5 总计划 v1.3）、docs/plans/day05/dev-b-护栏与降级.md（dev-B 执行计划 v3.1，N2 口径已冻结）
> 上游依据：PRD §5.3（AC-40/42）、FR-3D-01、FR-VPR-01~03（验收）｜技术设计文档 §8 Day 5（实现口径）｜dag-项目工作流总览与联调节点.md §2.1（N1/N2/N5 接口规范）
> 变更记录见文末

---

## 0. 已确认裁决（2026-11-07 主理人，本计划执行基线）

| # | 事项 | 裁决 |
| --- | --- | --- |
| C-D1 | 联调依赖策略 | **先桩后并**：dev-A（A-2 契约修订）与 dev-B（B-2 降级事件）未落地前，dev-C 先按已冻结规范 / N1 规范草案完成端侧改动与本地自测，联调通知到达后只做对齐联演，人等链不空转 |
| C-D2 | 演示职责 | **纳入**：M5.4 Iter2 演示中 dev-C 主操 pad，演示演练配合与 tag `iter2-demo` 前的 pad 走查列入本计划（S5） |
| C-D3 | 声纹实测人员 | **dev-C 与同事配合**：dev-C 本人 + 一位同事的录音完成同人/非同人两条路径实测 |
| C-D4 | C-1 降级提示交互（2026-11-07 S0 确认） | **方案 A：同一条字幕原地覆盖升级**——slow 占位话术先显，unavailable/timeout 到达后按 code 优先级 `unavailable > slow` 原地覆盖，长者只看到一条；不采用「两条都显」 |

---

## 1. 项目认知摘要（dev-C 视角）

- **产品边界**（PRD）：pad 为 1920×1080 长者端大字界面，承担语音对话、3D 数字人展示、声纹身份确认、用药/健康/告警入口；**不做医疗诊断、不替代 SOS**。
- **本日目标**（TD §8 Day 5 + 任务卡）：延迟基线测量（M5.2 入档的分母数据）、三档超时降级前端表现、声纹闭环真人实测（M5.4 演示硬前置）、口型取证。
- **协议层现状**（frontend/src/pad/protocol.ts，Day3 C2 契约冻结）：WS 信令复用 `/ws/signal`；`client_diag` 为仅日志上行；协议改动必须先改契约再同步类型文件。
- **与 dev-B 的 N2 口径差异（重要）**：任务卡 C-1 写的是「`degrade` 事件按 stage 渲染」，但 dev-B 执行计划 v2.0→v3.1 已裁决**方案 B**：不新建 degrade 事件，**复用 WS error 通道**扩 `ErrorMsg.code` 枚举三档 `asr_slow` / `llm_slow` / `tts_slow`（话术在 `_ERROR_TALKBACK` 常量）。C-1 按 dev-B 冻结口径实现，任务卡差异在 S0 向主理人报备。

---

## 2. 现状核查（代码级证据，2026-11-07 实测）

| # | 核查项 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1 | ErrorMsg 消费方式 | pad 收到 error 事件**直接展示 message**（`onSubtitle(msg.message, "notice")`），不按 code 分支 → 新增三档 slow code 天然可显，但**slow→unavailable 状态升级合并需新写** | frontend/src/pad/realtime.ts L114-116 |
| 2 | ErrorMsg.code 枚举 | 现仅 4 值（asr_unavailable/llm_timeout/tool_breaker/internal），需随 dev-B 扩三档 slow | frontend/src/pad/protocol.ts L124-128 |
| 3 | auth 首帧发送点 | `signal-socket.ts` L61 `send({ type: "auth", token })`，现无 wake_ts 字段 | frontend/src/pad/signal-socket.ts L61 |
| 4 | KWS 命中回调 | kws/kws-detector.ts 有命中回调链路，wake_ts 捕获点在此 | frontend/src/pad/kws/kws-detector.ts |
| 5 | client_diag 发送单点 | realtime.ts L68 `send({type:"client_diag", stage, ok, error})`，stage 现仅 vrm_load/webrtc/vad/asr 四值 | frontend/src/pad/realtime.ts L36-68 |
| 6 | TTS 播放时机 | `tts_text` 消息到达即播放（realtime.ts L107-112），playback 段计时起点在此 | frontend/src/pad/realtime.ts L107-112 |
| 7 | dev-A A-2 契约修订 | **全部落地（2026-11-08 复核）**：`record_wakeup` 接线 + signaling auth 解析 `wake_ts`（signaling.py L96/L530-531，day03/contracts.md v1.5）+ client_diag 七段扩展 + playback 服务端代打（commit 155b1ec）；`docs/plans/day05/a-后端观测与数据.md` 已存在。**S3 无需桩期，直接进对齐阶段** | app/core/metrics.py、app/audio/signaling.py L96/L530、docs/plans/day05/a-后端观测与数据.md |
| 8 | dev-B B-2 降级事件 | **代码已落地（2026-11-08 复核）**：`_ERROR_TALKBACK` 常量 + 三档 slow watchdog（webrtc.py L1358-1367、main.py L214-224）+ tests/test_degrade_timeout.py 存在；C-1 可直接联演 | app/audio/webrtc.py、app/main.py、tests/test_degrade_timeout.py |
| 9 | metrics 表 / 0006 迁移 | **已建（2026-11-08 复核）**：第 27 张表 metrics 三件套 + 0006 迁移（commit c783394/155b1ec）；C-2 回查前提就绪 | alembic/versions/、app/models/metrics.py |
| 10 | 声纹 pad 侧 | 录入页 VoiceprintEnroll.tsx、身份确认 IdentityConfirm.tsx 已存在；阈值 0.55 已五处落地；缺真人闭环实测记录 | frontend/src/pad/voiceprint/、pages/IdentityConfirm.tsx |
| 11 | 口型 | lipsync.ts 能量近似已在跑，C-4 仅需录屏取证，无开发 | frontend/src/pad/avatar/lipsync.ts |
| 12 | 构建门禁 | Day5 A1 收口时 `pnpm build` 三入口零报错（基线），本计划各阶段以 `tsc --noEmit` + `pnpm build` 为门禁 | day05/a1 任务卡 v1.1 |

---

## 3. 任务拆解总表（对应任务卡 C-1~C-6 + 演示）

| # | 任务 | 前置依赖（真实状态） | 执行落点 | 量化验收标准（照抄任务卡，补实现口径） |
| --- | --- | --- | --- | --- |
| C-1 | 三档超时前端表现 | dev-B B-2 规范**已冻结**（error 通道 + 三档 slow code）；其代码可后到，dev-C 先行 | realtime.ts error 消费分支 + App.tsx 提示渲染 | ① 收到 `asr_slow/llm_slow/tts_slow` 按 message 渲染对应提示（话术后端常量同源）② **slow→unavailable/timeout 状态升级合并**：同一 UI 位置按 code 优先级 `unavailable > slow` 覆盖文案 ③ 三档逐个联演，无一次白屏或静默 ④ 提示走 pad 大字规范（notice 通道 BigSubtitle） |
| C-2 | 20 次延迟测量执行 | dev-A A-2 打点收口通知（N1）+ A-3 SQL 口径 + **S0 冒烟通过** + metrics 表（0006）已建 | 端侧跑测，不写后端 | ① 每次 trace_id 可在 metrics 表回查 ② 端侧分段（wakeup/connection/playback）数据完整 ③ 原始记录交 dev-A 出基线表，不得手工修饰数字 ④ O-1 口径：本地 FunASR 基线如实入档不美化 |
| C-3 | 声纹闭环真人实测 | dev-A 后端在线（N5）；阈值 0.55 已落地；实测人员 C-D3 | pad 录入页 + 登录链路操作 | ① 3 段真人录音录入 → 同人第二次登录命中（voice 终态 + cosine 值入记录）② 非同人触发「请问是哪位在说话？」③ 任一步失败按 C4 降级放行不卡死 ④ 实测记录回填 day04/task-01 卡并由原执行者更新状态行 |
| C-4 | 口型达标取证 | 无 | 录屏 | 口型随音频能量开合可见（PRD FR-3D-01 口径，不升级 viseme），录屏留证 |
| C-5 | 端侧 wake_ts 上报 | dev-A 契约修订完成（AuthMsg 追加可选 wake_ts，A-2 内执行）；桩先行（C-D1） | signal-socket.ts auth 首帧 + kws-detector.ts 命中时刻捕获 | ① KWS 命中路径 metrics 表 wakeup 行 `source=client` 且 wake_ts 有值 ② 未命中/不可用路径不填该字段、`source=backend` 兜底不缺行 ③ 字段名与 N1 规范一致 |
| C-6 | playback 段打点上报 | dev-A 契约修订完成（client_diag 追加 wakeup/playback stage，A-2 内执行）；桩先行（C-D1） | realtime.ts sendDiag 单点扩展 | ① 每次播放产生 stage=playback 打点、字段名与 record_stage 规范一致 ② 不含转写全文与隐私字段 ③ dev-A 侧 metrics 表可按 trace_id 回查 |
| C-7 | 演示主操配合（C-D2 纳入） | 主理人演示脚本定稿；C-1/C-3/C-4 完成 | pad 走查 + 演示演练 | ① 5 分钟脚本一次通过（对话/声纹身份/口型/降级四项）② tag `iter2-demo` 前 pad 真链路走查结论回填 day04/a0 卡变更记录 ③ `pnpm --dir frontend build` 通过 |

---

## 4. 分阶段推进计划（每阶段完成后停下汇报，经确认再进入下一阶段）

> 执行主线：**S0 → S1（C-4 ∥ C-1）→ S2（C-3）→ S3（C-5/C-6 桩→对齐）→ S4（C-2）→ S5（演示收尾）**。S1/S2 可与 S3 桩并行穿插；汇报格式：diff 摘要 + 自检结果 + 遗留问题。

### S0 · 开工核查与报备（约 0.5h，无外部依赖）

**工作内容与步骤**：
1. 向主理人报备两件事：①任务卡 C-1「degrade 事件」口径与 dev-B 冻结口径（复用 error 通道三档 slow code）的差异，确认按 dev-B 口径实现；②任务卡引用的 `docs/plans/day05/a-后端观测与数据.md` 不存在，dev-A A-2 的契约修订细节（wake_ts 字段名、client_diag stage 枚举扩展值）当前只有 dag 文档 §2.1 N1 规范可依，请主理人确认 dev-A 落地时间窗。
2. 核对 day-05 总计划 S0 冒烟结论：对话主链（day04/task-02/04）收口状态，S4 开测前必须冒烟通过。
3. 确认测量环境就绪度：固定种子设备 + 王奶奶人设的种子数据存在性（day02 seed），缺则报主理人。
4. 建立本计划执行基线：`pnpm --dir frontend build` 当前零报错确认。

**交付物**：报备结论两条；环境就绪清单。

**自检校验**：
- [x] C-1 实现口径已获主理人确认（dev-B error 通道口径 + 方案 A 覆盖升级，C-D4）
- [x] dev-A A-2 时间窗有明确答复（部分：打点原语已落地，剩 signaling 接线；S3 桩先行不受阻）
- [x] 种子设备/王奶奶人设就绪或有明确补齐方案（种子脚本已有固定长者张桂芬 …1102 + 设备 …1106；「王奶奶」为文档示例名，测量用现有种子，无需造数）
- [x] build 基线零报错（2026-11-07 实测 `pnpm build` 1.49s 三入口齐，pad chunk 867KB，>500KB warning 为 three.js 已知现象非报错）

### S1 · C-4 口型取证 + C-1 三档超时前端表现（C-4 约 0.5h；C-1 约 2h）

**C-4 步骤**：播放任一 TTS，录屏口型开合（能量近似口径），存档留证。

**C-1 步骤**：
1. protocol.ts：`ErrorMsg.code` 枚举追加 `asr_slow | llm_slow | tts_slow` 三值（随 dev-B B2-1 同名扩展；这是消费端类型同步，契约主体在 dev-B）。
2. realtime.ts error 分支改造：当前 `onSubtitle(msg.message, "notice")` 直显基础上，新增**状态升级合并**——按 code 优先级 `unavailable/timeout > slow` 判定：同轮先收 slow 再收 unavailable/timeout 时，覆盖提示文案（同一 UI 位置从「占位/等待」升级为「失败话术」），不叠两条、不白屏。
3. 提示渲染走现有 notice 通道（BigSubtitle 大字），1920×1080 可读。
4. 本地自测：Mock WS 下行三种 slow code + slow→失败双事件序列，逐一验证渲染。
5. 与 dev-B 联演（B2-6）：三档逐个人为制造超时（Mock latency_ms 触发），三路径（slow→成功、slow→失败、直接失败）逐个演示，录屏留证，回填 dev-B B-2 验收证据与 N2 签字。

**交付物**：C-4 录屏；C-1 实现代码 + 联演录屏 + N2 签字记录。

**自检校验**：
- [x] 三档 slow code 渲染文案与后端 `_ERROR_TALKBACK` 同源（前端只渲染 msg.message，不写死话术；degrade.ts + realtime.ts error 分支，2026-11-08）
- [x] slow→unavailable 覆盖合并生效，无双提示叠加（degrade.ts 优先级状态机，BigSubtitle 单状态位原地覆盖；本地自测 10/10 通过）
- [ ] 三档逐个联演无一次白屏或静默（录屏逐段核对）——待与 dev-B B2-6 联演
- [x] `pnpm --dir frontend build` 通过，pad chunk 无越界变化（2026-11-08 实测 1.81s，pad chunk 867.60kB 与基线一致）
- [ ] C-4 录屏口型开合可见——待真人操作录屏

### S2 · C-3 声纹闭环真人实测（约 1.5h，依赖 dev-A 后端在线）

**工作内容与步骤**：
1. 与 dev-A 约定联调窗口（N5），确认 voiceprint 容器（7003）与 identity 链路在线。
2. dev-C 本人录音 3 段录入（pad 录入页）→ 退出重登 → 验证同人命中：记录 voice 终态与 cosine 值（≥0.55 判定）。
3. 同事录音走登录 → 验证非同人触发「请问是哪位在说话？」主动确认路径。
4. 任一步失败验证按 C4 降级放行不卡死。
5. 实测记录（含两路径 cosine 值、终态、失败路径表现）回填 day04/task-01 卡，通知原执行者更新 `> 状态：` 行（PROGRESS 钩子自动刷新，不手改看板）。

**交付物**：实测记录（回填 task-01 卡）。

**自检校验**：
- [ ] 同人命中路径有 cosine 数值记录
- [ ] 非同人主动确认路径有记录
- [ ] 失败降级放行路径验证过
- [ ] day04/task-01 卡状态与实测记录已回填

### S3 · C-5/C-6 端侧上报（桩先行，对齐收口；桩约 2h，对齐约 1h）

**桩阶段步骤**（不依赖 dev-A，C-D1 裁决）：
1. **契约权限边界**：protocol.ts 的 AuthMsg/ClientDiagStage 字段扩展是契约改动、主体在 dev-A（A-2），dev-C **不越权先改 protocol.ts**；桩期在自有文件用本地扩展类型（如 session/realtime 层内部 interface）实现逻辑。
2. C-5 桩：kws-detector.ts 命中回调处捕获 wake 时刻（monotonic 时间戳），透传至 signal-socket.ts auth 首帧组装点；KWS 未命中/不可用时不填。逻辑用 feature 开关或空值兜底，保证 dev-A 未落地时行为与现状完全一致。
3. C-6 桩：realtime.ts `tts_text` 播放开始/结束计时，经现有 `sendDiag` 通道按 N1 规范字段名组装 playback 打点；打点内容不含转写全文与隐私字段。
4. 本地自测：KWS 命中/未命中两路径、播放打点字段完整性。

**对齐阶段步骤**（dev-A A-2 完成通知后）：
5. dev-A 契约落地后，把本地扩展类型合入 protocol.ts 正式字段（与 A-2 同批次或紧随其后），字段名逐一核对 N1 规范（`wakeup/connection/asr/route/llm_first_token/tts_first_packet/playback` 七段枚举）。
6. 与 dev-A 联调验证：KWS 命中路径 metrics 表 `wakeup` 行 `source=client` 且 wake_ts 有值；未命中路径 `source=backend` 兜底不缺行；playback 打点按 trace_id 可回查。

**交付物**：C-5/C-6 端侧改动 + 对齐联调记录。

**自检校验**（v1.4：A-2 已落地，桩期跳过，直接对齐收口）：
- [x] ~~桩期线上行为与现状零差异~~——桩期跳过（A-2 已落地，2026-11-08）
- [x] protocol.ts 扩展与 dev-A A-2 落地字段名完全一致（逐字段核对：AuthMsg 可选 `wake_ts: float` epoch 秒＝signaling.py L96；ClientDiagStage 追加 `playback`＝后端 `_CLIENT_DIAG_STAGES`；ClientDiagMsg 追加 `duration_ms?: number`＝signaling.py L934 校验；**wakeup 不走 diag**——后端统一 C2 auth 首帧口径，pad 侧未加 wakeup stage）
- [ ] metrics 表 `source=client/backend` 两路径各有数据——待与 dev-A 联调回查（S3 步骤 6）
- [x] playback 打点无隐私字段（payload 自查：仅 type/stage/ok/error=""/duration_ms，无转写全文）
- [x] build 通过（2026-11-08 实测 tsc + vite 1.62s）

### S4 · C-2 20 次延迟测量执行（约 1.5h，依赖 S0 冒烟 + S3 对齐 + A-3 SQL 口径）

**工作内容与步骤**：
1. 开测前冒烟：pad 登录 → WS auth_ok → WebRTC 建连 → 说话 → asr_final 真实文本；链路不稳不进测量（day-05 总计划 S3 口径）。
2. 固定种子设备 + 王奶奶人设，跑 20 次「今天天气怎么样」，逐次记录分段耗时（端侧 wakeup/connection/playback 随打点上送）。
3. 导出 20 次 × 分段原始记录（含 trace_id 列表），录屏留证，原始数据交 dev-A 出基线表；**数字不做任何手工修饰**。
4. 如实记录 PRD §5.3 口径达标情况（≤1.5s 达标次数），O-1 口径（本地 FunASR 基线）不美化。

**交付物**：测量原始记录（20 次×分段）+ 录屏。

**自检校验**：
- [ ] 20 条 trace_id 在 metrics 表逐条可回查
- [ ] 端侧分段（wakeup/connection/playback）数据完整
- [ ] 原始记录原样交 dev-A，有交付留痕

### S5 · 演示主操与收尾（约 1.5h，依赖 S1–S4 全部完成）

**工作内容与步骤**：
1. 配合主理人 5 分钟演示脚本（对话/声纹身份/口型/降级四项，dev-C 主操 pad），照脚本演练一遍，翻车点即修。
2. 演示预演兼 Day4 遗留勾验：pad 登录→auth_ok→WebRTC 建连→说话→asr_final 真实文本、按住说话→真实转写，结论回填 day04/a0 卡变更记录。
3. tag `iter2-demo` 前收尾自检：`pnpm --dir frontend build` 通过 + 任务卡 C-1~C-6 全绿 + 汇报清单（对照本计划各阶段自检项）。

**交付物**：演示演练记录；day04/a0 勾验结论回填；任务卡状态更新（`> 状态：` 行，由原负责人/主理人确认后置已完成）。

**自检校验**：
- [ ] 演示一次通过不翻车（录屏留证）
- [ ] day04/a0 真链路勾验结论已回填
- [ ] 任务表 C-1~C-6 全绿 + build 通过
- [ ] 汇报清单已交主理人

---

## 5. 联调节点核对清单（dev-C 侧，对接即验收）

| # | 节点 | 对接方 | dev-C 侧动作 | 完成判定 |
| --- | --- | --- | --- | --- |
| N1 | A-2 ↔ C-2/C-5/C-6 | dev-A | 端侧打点字段名与 `record_stage` 规范严格一致；wake_ts 随 auth 首帧、playback 经 client_diag | metrics 表回查两路径（client/backend）数据齐 |
| N2 | B-2 ↔ C-1 | dev-B | 按 dev-B v3.1 冻结口径消费 error 通道三档 slow code；slow→unavailable 状态升级合并 | 三档三路径联演录屏 + dev-C 签字回填 B-2 |
| N5 | dev-A ↔ C-3 | dev-A | 声纹闭环真人实测（同人/非同人/失败降级三路径） | 实测记录回填 task-01 卡；M5.4 演示硬前置达成 |

---

## 6. 风险预案

| # | 风险 | 概率 | 影响 | 预案 |
| --- | --- | --- | --- | --- |
| R1 | dev-A A-2 长时间不落地，C-5/C-6 桩悬空 | 中 | S3 对齐阶段空转 | 桩期行为与现状零差异（空值兜底），主线不等；向主理人升级催收 A-2 时间窗 |
| R2 | dev-B 话术常量与 pad 渲染期望不一致（如文案过长溢出大字幕） | 低 | C-1 联演返工 | 前端只渲染 message 不写死话术；联演时若溢出，走 pad 大字换行规范反馈 dev-B 调整常量 |
| R3 | 20 次测量期间链路抖动（FunASR 本地基线 2219ms 背景） | 高（O-1 已知） | P95 大概率超 1.5s | 如实入档不美化；基线表由 dev-A 按「本地 FunASR 基线」单独归档，云 key 到位后复测 |
| R4 | 声纹实测同事时间约不上 | 中 | C-3 延期，M5.4 阻塞 | S2 第一时间约窗口；备选：请主理人协调任一同事（C-D3 授权 dev-C 与同事配合） |
| R5 | 演示日网络/环境异常导致口型或降级环节翻车 | 低 | M5.4 翻车 | S5 预演一遍 + 每环节备降级路径（口型有静态头像降级 FR-3D-02、降级提示有大字幕兜底） |
| R6 | 桩实现与 dev-A 最终契约字段名漂移 | 中 | S3 对齐返工 | 桩期字段名严格照 dag §2.1 N1 规范书写；对齐阶段逐字段 diff；差异只登记不私改 |

---

## 7. 需确认问题清单

| # | 问题 | 状态 | 影响阶段 |
| --- | --- | --- | --- |
| 1 | 任务卡 C-1「degrade 事件」vs dev-B 冻结「error 通道三档 slow」口径差异 | ✅ 已确认（C-D4，2026-11-07）：按 dev-B error 通道口径 + 方案 A 覆盖升级交互 | S1 |
| 2 | `docs/plans/day05/a-后端观测与数据.md` 文件不存在（任务卡引用悬空），dev-A A-2 落地时间窗 | ✅ 已解决（2026-11-08 复核）：文件已存在，A-2 全部落地（wake_ts 接线 + client_diag 七段 + playback 代打，commit 155b1ec） | S3/S4 |
| 3 | 王奶奶人设 + 固定种子设备数据就绪性 | ✅ 已确认（S0，2026-11-07）：现有种子张桂芬 …1102 + 设备 …1106 可用，无需造数 | S4 |
| 4 | dev-C 桩期不越权改 protocol.ts、用本地扩展类型的边界是否认可 | ✅ 已消解（2026-11-08 复核）：dev-A A-2 已落地，S3 跳过桩期直接对齐，protocol.ts 可正式扩展 | S3 |

---

## 8. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | 首版：基于任务卡 v1.1、Day5 总计划 v1.3、dev-B 执行计划 v3.1 与 pad 端代码级核查（12 项现状证据）制定；含主理人三裁决（C-D1 先桩后并、C-D2 演示纳入、C-D3 同事配合）、S0–S5 分阶段推进（每阶段汇报制）、N1/N2/N5 联调清单与 R1–R6 风险预案 |
| v1.1 | 2026-11-07 | 主理人通报 dev-A 已加 wake_ts，复核后更新 §2 核查项 7：打点原语 `record_wakeup`（metrics.py L68）与单测已就位，signaling 接线（auth 解析 wake_ts、client_diag 扩 stage）仍未完成；§7 问题 2 影响范围相应收窄，S3 对齐时间窗预期提前 |
| v1.2 | 2026-11-07 | S0 执行完毕：新增裁决 C-D4（方案 A 字幕覆盖升级）；§7 问题 1 关闭；S0 自检四项全勾（对话主链卡仍标进行中属卡未回填、种子就绪结论、build 基线 867KB 实测）；状态改「进行中」 |
| v1.3 | 2026-11-08 | C-1 端侧实现收口：新增 frontend/src/pad/degrade.ts（slow/failure 优先级状态机，unavailable > slow 防回退）+ realtime.ts error 分支/asr_partial/onSpeechStart/teardown 接线；本地自测 10/10 通过（三档 slow 渲染、slow→失败升级、失败→slow 压制、新轮复位）；build 门禁通过（pad chunk 867.60kB 持平）。复核更新 §2 核查项 7/8/9：dev-A A-2 全部落地（S3 免桩直接对齐）、dev-B B-2 代码已落地、metrics 0006 迁移已建；§7 问题 2/3/4 关闭。任务卡状态行更新为「进行中」。遗留：C-1 与 dev-B B2-6 联演录屏、C-4 口型录屏待真人操作 |
| v1.4 | 2026-11-08 | S3 对齐阶段端侧收口（桩期跳过，commit 7e90613 为 C-1 前置）：①C-5——kws-detector 命中回调捕获 epoch 秒 wake_ts（App.tsx wakeTsRef）→ realtime.start 透传 → signal-socket auth 首帧携带、消费一次即清（重连 auth 不重发旧值走 backend 兜底），呼叫按钮路径不填；②C-6——realtime.ts tts_text 起表 / onended 与打断双路径结算，经 sendDiag(stage=playback, duration_ms) 上报（打断记实际已播时长如实入档）；③protocol.ts 正式扩展 AuthMsg.wake_ts / ClientDiagStage.playback / ClientDiagMsg.duration_ms（与 A-2 契约 v1.5 逐字段一致，wakeup 不走 diag）。build 门禁通过。遗留：metrics 表 client/backend 两路径数据回查待与 dev-A 联调（S3 步骤 6），完成后进 S4 |
