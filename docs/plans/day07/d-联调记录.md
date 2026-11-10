# Day 7 · dev-D 联调记录（d-联调记录）

> 状态：进行中　｜　记录人：dev-D（AI 协作）　｜　日期：2026-11-10
> 关联：`docs/plans/day07/d-推进计划-pad可靠性改绑联调.md`（计划）｜`docs/team_tasks/day07/dev-d-day07.md`（任务卡）
> 用途：D1 / D2 / D3 各项验证证据与问题登记；D3 收口后作为 G5 门禁证据链一部分。

---

## D2 · console 改绑分页与错误 UX（S2.1–S2.3）

### 代码修改

| 项 | 文件 | 内容 |
| --- | --- | --- |
| P2-25 | `frontend/src/console/pages/AdminUsersPage.tsx` | `fetchAllElders()` 完整分页（page_size=100 循环取满 total）；保存集合 = 勾选 ∪ 未进候选列表的已绑长者；「已选择 N 位 / 共 M 位」显示；createStaff 成功后刷新员工列表 |
| P2-40 | 同上 | `refreshStaffList` 请求序号防旧响应覆盖 |
| P2-26 | `frontend/src/console/admin-api.ts` | 401 →「登录已过期，请重新登录」；400/422 解析 `detail` 提示具体原因；409 保留编辑态（既有实现核对） |

构建门禁：`pnpm --dir frontend build` 零 TS 错误，pad/console/family 三产物均出（S2.1、S2.2 各跑一次）。

### 造数验证（2026-11-10，后端 localhost:8000，管理员 13700000001）

一次性脚本（/tmp/d2_seed.py、/tmp/d2_reassign.py、/tmp/d2_reassign2.py，不入 Git）经 API 执行：

| # | 验证项 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | E5 建 150 位压测长者 | ✅ created=150 failed=0 | `D2压测长者001–150`，E7 total=153 |
| 2 | E7 分页：第 101 位起可见 | ✅ | page1=100 条 / page2=53 条；`D2压测长者101` 在 page2 中找到并成功用于绑定 |
| 3 | E20 差集保存（新增） | ✅ 200 | `added=[压测002 elder_id]`、`removed=[]`（001 未绑入，diff 正确） |
| 4 | E20 差集保存（移除） | ✅ 200 | 共同护工兜底下 `removed=[种子长者 elder_id]`、`added=[]` |
| 5 | E20 409 拦截 | ✅ 409 | 唯一责任人移除 → `{"detail":"长者 00326698-… 移除后将失去全部责任护工","code":"CONFLICT"}`（两条，trace_id 在案） |
| 6 | 连续 5 次快速刷新一致 | ✅ | 5 次快照完全一致，bound=3 |
| 7 | createStaff 后立即出现 | ✅ | 201 建号后按 staff_id 立即在 GET /api/staff 命中 |
| 8 | 401（伪 token） | ✅ 401 | GET /api/staff 与 PUT reassign 均 `TOKEN_INVALID`；前端映射「登录已过期，请重新登录」（S2.2 代码核对） |

### 遗留与登记

- UI 层（勾选「已选 N / 共 M」显示、409 弹窗文案与编辑态保留）待 D3 联调用浏览器核验；API 层证据已齐。
- 测试数据留存：150 位 `D2压测长者*`、4 名测试员工（D2测试护工 / D2共同护工×2 / 及 1380000990x 手机号），带显式前缀便于识别与后续清理；E5 无删除端点，未直插 SQL 清理。
- `console/api.ts` 保持零改动（K2 方案 B 约束未破坏）。

---

## D1 · pad 播放/打断/VAD 可靠性（S1.1–S1.5）

### 代码修改（S1.1–S1.4，2026-11-10）

| 步 | 文件 | 内容 | 关闭 |
| --- | --- | --- | --- |
| S1.1 | `frontend/src/pad/audio/playback-queue.ts` | ① `isSegmentedTts` 改类型守卫（response_id/segment_id/epoch 三字段 typeof 校验，epoch 非法走整段路径）② 暴露 `isStale()` 供 unmute/play 前淘汰判定 ③ `total` 缺失单段收口（total=segment_id+1 + `total_missing_single_segment` diag，tail 兜底必起）④ `flushCanceled`/`markTrackEnded`/`reset` 内 `cancelRetry()` + 清 pending playing ⑤ playing ack 延后到实际起播（`notifyPlaybackStarted()` 补发，至多保留最新一条）⑥ tail 兜底结算打 `tail_fallback_estimated` diag；另暴露 `activeResponseId` 供打断上行取值 | P3-36、P2-22 配套、P2-24、P3-39 |
| S1.2 | `frontend/src/pad/realtime.ts` | ① setup/teardown 解绑 devicechange（stop 原有解绑保留，三处幂等）② tts_text 先过 `isStale` 再放开播放态/字幕（stale 段不播不 ack 不渲染字幕）③ `reopenMic` 并发守卫 ④ mic.start 失败 `sendDiag("webrtc","mic_start_failed:…")` ⑤ stop 复位 ttsPlaying、teardown 清 onended+muted、tts_text play() 失败提示 ⑥ speechstart 本地停播后立即上行 `interrupt`（不等回执，无活跃回复不发）⑦ 降级预留常量 `FALLBACK_NOTICE`（`msg.degraded===true` 时显示） | P2-23、P2-22、P3-38、P1-3 前端半边、任务卡 §2.2.5 |
| S1.3 | `frontend/src/pad/audio/vad.ts` + `frontend/public/vad-assets/vad-worker.js` | ① `VAD_THRESHOLD=0.38` 常量化（J-D1 先设值，20 句标定待真人复测）② 喂窗前重置 pending/used + try/catch，单次 acceptWaveform 异常不砖死 ③ worker 新增 `runtime_error` 消息（协议只增不改），vad.ts `onRuntimeError` 转发，realtime `sendDiag("vad","vad_runtime_error:…")` 触发服务端兜底；运行期 worker.onerror 一并转发 ④ handleReset 说话态先补 speech_end ⑤ 120s 零 speechstart 心跳 `vad_no_speech_120s` | P1-4、P1-5、P1-6、P3-37、任务卡 §2.2.3 |
| S1.4 | `frontend/src/pad/protocol.ts` | `InterruptMsg`（C→S）入 `ClientSignal`：`{type:"interrupt", response_id, reason:"barge_in"}`，与 Day7 README §5.4 草案逐字段核对一致（type/response_id/reason 三字段 ✓，发送时机/旧 epoch 丢弃/无活跃回复不发三项行为口径 ✓）；注释标注待 A1 契约冻结后校对 | 契约先行收口 |

### 构建门禁（G4）

`pnpm --dir frontend build` 零 TS 错误，pad/console/family 三产物均出——S1.1 / S1.2 / S1.3 / S1.5 各跑一次全绿（S1.4 零代码变更，沿用 S1.3 结论）。pad 产物 879.39 kB（chunk 警告为既有项，非本日引入）。

### 运行时回归（2026-11-10 执行）

环境：后端 `:8000/health` 200；vite dev `:5173` 200；silvermate-postgres / redis / voiceprint / funasr 容器 healthy。

**真实会话建立（浏览器 localhost:5173/pad.html，J-D0 口径）——PASS**

- 设备凭证登录成功，点「呼叫小伴」后状态条「已断开」→「正在听」并稳定保持（mic/WS/WebRTC 链路真实建立），控制台无 error、无 `[vad]`/`client_diag` 告警、无 NotAllowedError。

**20 轮模拟回复回归 + 边界注入（驱动生产 PlaybackQueue）——ALL PASS**

自动化浏览器无真实语音输入，按计划「20 轮真实/**模拟**回复回归」的模拟口径执行：dev-only harness 页（`frontend/dev/queue-harness.html`，不入 MPA 构建入口，验证后已删除）经 Vite dev 动态 import 生产 `playback-queue.ts`，驱动真实代码跑序列，浏览器读取 DOM 结果：

- **20 轮模拟回复**：段数合计 **41**，ack 总数 **82**（playing 41 + played 41，上报率 100%），diag 异常 0；逐段断言全过——段到达时无提前 playing ack（P3-39 延后生效）、`notifyPlaybackStarted()` 后 playing 恰好 1 条、played 数=段数、segment_id 单调无重复/乱序、每轮 `onResponseSettled` 触发、已知 total 轮无 tail 兜底。
- **边界注入 ①旧 epoch**：`isStale=true`、`handleSegment` 拒收、旧代次 0 条 ack、`stale_segment_dropped` diag、当前代次正常结算（P2-22/P3-36 生效）。
- **边界注入 ②total 缺失**：`total_missing_single_segment` diag、playing 发出、tail 兜底 3.4s 后 played=1、`tail_fallback_estimated` diag（P2-24 收口生效）。
- **边界注入 ③打断**：`canceled` 恰 1 条且 segment_id=最后播完段（0）、打断段不 played、打断后迟到起播通知不产生额外 ack、`activeResponseId` 清空（乱序补发防护生效）。
- **边界注入 ④epoch 非法**：缺 epoch / 非数字 epoch / 非字符串 response_id 均判定为整段路径（P3-36 前半生效）。

汇总：**=== ALL PASS ===（FAIL 0）**。

**待真机补测（R4 口径，如实登记，不冒充）**

以下需真实语音/真机环境，浏览器自动化无法覆盖：

1. 真实语音 20 轮全链路（ASR→TTS WS 分段→pad 上报 ack）：模拟回归已覆盖前端 ack 逻辑，语音输入侧待真机。
2. 打断实测：开口后 ≤200ms 静音 + WS 抓包确认 `interrupt` 上行帧（前端半边已实现，字段按 §5.4 草案，A1 契约冻结后校对）。
3. 重连 3 次 `devicechange` listener 不累积（DevTools `getEventListeners` 核验）。
4. VAD 注入 `acceptWaveform` 异常 → 下一帧仍产出 speech 事件 + `vad_runtime_error` diag（DevTools 断点改写验证）。
5. threshold 0.38 收音效果 20 句近/远场标定（J-D1 遗留，真人复测）。

---

## D3 · 触达/路由联调

（2026-11-10 执行，J-D3 硬推进口径：不等 A2/A3/C2，缺项登记阻塞催办）

### L1 · 起环境 —— PASS（sidecar 缺项登记）

- 后端 `:8000/health` 200，响应头带 `X-Trace-Id`；silvermate-postgres / redis / voiceprint / funasr 容器 healthy。
- pad 会话建立：localhost:5173/pad.html 设备凭证登录 →「呼叫小伴」→ 状态条「正在听」稳定（同 D1 S1.5 证据）。
- **sidecar `:7001` 未响应**（curl 000）→ A2 System One 未就绪，按 L1「未就绪则跳过并记录」登记。

### L2 · 造到点药单 —— FAIL，归属 C2（调度接线）

- 药单已造（SQL 路径，§6 L2 手册口径，显式测试前缀）：`plan_id=948997a1-80e6-42d8-a9a1-9705566e20b6`，长者=李奶奶（设备 0340d6c3 绑定），`drug_name='联调测试-到点触达片'`、`schedule_cron='* * * * *'`、`status=active`、`created_by=赵医生 a83eb6f1`。**保持 active 供 C2 就绪后复跑，复跑完置 stopped。**
- pad 在线等待 100 秒：触达字幕（「到吃药时间了…」/药名）**未出现**；状态条保持「正在听」，控制台无 error。
- 根因实证：**`app/scheduler/` 仅有空 `__init__.py`，无任何任务注册（C2 未实现）**；`medication_attempts` 0 行（触达链路未启动）。
- 顺带观察：pad 在线期间字幕出现「我这会儿听不太清，请稍后再说一遍。」——对话降级话术链路活着（环境音触发 speech→asr 识别失败路径），触达与回复并发的观察因触达缺失无法进行。

### L3 · played 闭环 —— 阻塞（依赖 L2 触达）

依赖 C2 产生触达，无法执行。复跑口径（C2 就绪后）：

- 触达音频自然播完后核对：`SELECT attempt_no, receipt, tts_result FROM medication_attempts ORDER BY sent_at DESC LIMIT 4;`
- **勘误**：§6 L3 手册原 SQL 写 `ORDER BY created_at DESC`，实际表列名为 `sent_at`（`medication_attempts` 无 created_at 列），以本行 SQL 为准。
- 边界三验口径不变：播完前打断/关页不产生 played；重连回放幂等；无 audio 降级触达只出字幕不回执。

### L4 · TTS 降级与并发 —— 阻塞（依赖 L2 触达 + 真机语音）

触达链路（C2）与对话并发观察（真机语音）均缺，无法执行。前端对触达消息的处理逻辑（touch_id 分支：字幕渲染不动 WebRTC 轨）已由 D1 代码走查覆盖，真机验证待补。

### L5 · 路由与降级提示 —— 阻塞（依赖 A2 sidecar + A3 + 真机语音）

sidecar 7001 未起（A2）、需真实语音输入、`decision_logs` 降级行依赖 A3 降级链路。无法执行。前端侧「网络慢，正在换种方式回答」提示已按预留实现（`msg.degraded===true` 触发），A3 下发字段冻结后校对。

### L6 · 缺陷登记收口

| # | 现象 | trace_id | 归属 | 处置 |
| --- | --- | --- | --- | --- |
| 1 | sidecar 7001 未响应 | — | dev-A（A2 System One） | 催办；L1 已跳过登记 |
| 2 | 到点药单无触达（scheduler 无任务注册） | — | dev-C（C2 调度接线） | 催办；药单已备好，C2 就绪后复跑 L2→L3 |
| 3 | §6 L3 手册 SQL 列名勘误（created_at→sent_at） | — | 本计划 | 以本记录 L3 为准，主理人决定是否修订计划 |
| 4 | 触达/路由步骤的真机语音项 | — | 联调环境 | R4 口径登记补测，与 D1 待补清单合并 |

前端本卡（dev-D）范围内：L1–L6 未发现前端缺陷，无待修项。G5 门禁证据链现状：L1 SQL/服务证据齐；L2 阻塞证据（表结构 + 空表 + 源码实证）齐；L3–L5 待依赖就绪后复跑补齐。
