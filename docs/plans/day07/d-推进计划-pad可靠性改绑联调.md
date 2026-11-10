> 状态：进行中　｜　版本：v1.1　｜　日期：2026-11-10
> 关联：`docs/team_tasks/day07/dev-d-day07.md`（任务卡，唯一任务事实源）｜`docs/team_tasks/day07/dev-d-触达真机联调.md`（D3 执行明细卡）｜`docs/team_tasks/day07/README.md`（Day7 总表）｜`docs/team_tasks/day07/contracts.md`（TD-1/TD-2 契约，已冻结）
> 上游依据：PRD FR-UI-04/10、FR-MED-05/06｜技术设计文档 §4/§6｜`docs/plans/adhoc-20261110-day6全面审查.md`（P1-4/5/6、P2-22~26、P3-36~40 收口依据）
> 变更记录见文末

# Day 7 · dev-D 工作推进计划（pad 可靠性、console 改绑与联调收口）

## 0. 已确认裁决（2026-11-10 主理人确认，执行不再复议）

| # | 裁决 | 影响 |
| --- | --- | --- |
| J-D1 | **VAD threshold 先写 `0.38`**（低于现值 0.6，放宽收音灵敏度）→ **同日真机复测后主理人调整为 `0.45`**（0.38 易误触发、0.60 漏收音；取代先设值） | S1.3 落地为可配置常量；20 句近/远场样本标定仍登记为遗留项待真人复测 |
| J-D2 | **D2 允许临时脚本造数**（150 位长者，经后端 API 造，不入库） | S2.3 联调造数用一次性脚本，脚本本体不入 Git，证据贴进 `d-联调记录.md` |
| J-D3 | **D3 按计划硬推进**（不等 A3/C2/B1 全部完成） | S3.0 只做依赖状态记录；未就绪项当场催办并在联调记录登记阻塞，缺什么记什么 |
| J-D0 | **执行纪律**：本计划按步骤推进，每完成一个 ✋确认点停下来等主理人确认后再进下一步 | 全程不写业务代码以外越权内容；后端文件一律禁触 |

## 1. 项目认知与代码现状基线（2026-11-10 实证快照）

dev-D 独占写作范围 8 个文件，审查缺陷在当前代码中的实证位置：

| 文件 | 现状 | 审查项与位置 |
| --- | --- | --- |
| `frontend/src/pad/audio/playback-queue.ts` | 分段队列已实现（Day 6 N16，20 轮 ack 54/54） | P2-24（`totalSegments:null` 时末段判定恒跳过、无兜底、无 diag，L114）；P3-36（`isSegmentedTts` 不校验 epoch 类型 L34-36；`flushCanceled`/`markTrackEnded` 不 `cancelRetry` L124-139）；P3-39（playing ack 在 `handleSegment` 收到段即发 L112，早于实际起播） |
| `frontend/src/pad/realtime.ts` | TD-1/TD-2 触达链路已落地（L163-184、L301-325）；打断仍只本地停播 | P2-22（L163-184：`muted=false`/`play()`/置 speaking 发生在 `handleSegment` 淘汰判定**之前**）；P2-23（`setupSession` L297 直接覆盖 `unbindDeviceChange`，旧引用不解绑；`teardownSession` 不解绑）；P3-38（`reopenMic` 无并发守卫 L354；`setupSession` mic.start 失败无 client_diag 无 offer L222-227；`stop()` 不复位 `ttsPlaying` L123-130；teardown 不清 `audioEl.onended`；tts_text 路径 `play().catch(() => {})` 静默 L179）；P1-3 前端半边（speechstart 只本地停播，未发 `interrupt` 消息） |
| `frontend/src/pad/audio/vad.ts` | Worker 封装版正常工作 | P1-6（运行期 `worker.onerror` 只 console.warn L117-119；ready 后 `init_error` 被忽略） |
| `frontend/public/vad-assets/vad-worker.js` | threshold 0.6（L89） | P1-4（注释口径不一，需标定）；P1-5（`feedWindow` L118-131 无 try/catch，一次抛错 `used` 卡 256 永久砖死）；P3-37（`handleReset` L133-139 不发 speech_end）；无心跳遥测 |
| `frontend/src/pad/protocol.ts` | TD-1/TD-2 类型已加（L98-107、L171-201） | 缺 C→S `interrupt` 类型（字段待 A1 契约） |
| `frontend/src/console/pages/AdminUsersPage.tsx` | E20 全量覆盖改绑 | P2-25（L107 `pageSize:100` 只取前 100 位候选；保存提交全量勾选数组）；P2-40（`refreshStaffList` L133-140 无并发保护）；`createStaff` 成功后不刷新员工列表（L193-212） |
| `frontend/src/console/admin-api.ts` | K2-B 页面级文案映射 | P2-26（401 文案继承登录页「手机号或密码不正确」L114-117；400 统一「请刷新页面」L97-98） |
| `frontend/src/console/App.tsx` | 工单/健康/药单等 Mock 注释在 | 本日只移除与 E20 相关的 Mock 假设，其余不动 |

**契约现状**：A1 的 `docs/team_tasks/day07/contracts-system-one.md` **尚未产出**（A 线未开工）。D1 的 `interrupt` 字段先按 Day7 README §5.4 草案（`{type:"interrupt", response_id, reason:"barge_in"}`）实现并在代码注释标注「待 A1 契约冻结校对」；A1 冻结后逐字段核对，不一致只改 `protocol.ts` 与发送点。

## 2. 依赖关系与执行顺序

```text
D2（无依赖）──┐
              ├──→ D3（联调收口，硬依赖 D1+D2；软依赖 A3/C2/B1 按硬推进处理）
D1（interrupt 部分软依赖 A1 契约；VAD/播放部分无依赖）──┘
```

- **顺序**：D2 先开工（无前置、独立）→ D1（VAD/播放先做，interrupt 等 A1 契约或按草案先做）→ D3。
- **交接节点**：D1 完成 → 交接 D3（ack/打断证据）与 B1（前端实际发送字段与时机）；D2 完成 → 交接 B3（前端使用的 409/403/422 错误码）；D3 联调 → 全员（G5 门禁证据）。

---

## 3. D2 · console 改绑分页与错误 UX（0.75 人日，先行开工）

### S2.1 长者候选完整分页 + 差集保存（关 P2-25）

**操作步骤**：
1. 新增 `fetchAllElders()`：`page_size=100` 循环拉取直到取满 `total`（复用 `elders-api.ts` 既有 `listElders`，不新增第二套封装）；候选渲染改用全量结果。
2. 保存逻辑改造：`handleReassignSubmit` 不再直接提交勾选数组；计算最终集合 = `原 bound_elder_ids（含未进候选列表的已绑长者）∪ added − removed`，只对该集合调既有 `reassignElders`（PUT 契约仍为全量覆盖，**不改后端**）；未加载的长者不进入「未勾选」集合。
3. 弹窗与建档表单显示「已选择 N 位 / 共 M 位」。
4. `createStaff` 成功后 `await refreshStaffList()`。
5. `refreshStaffList` 加请求序号/cancelled 防护，旧响应不覆盖新状态（关 P2-40）。

**交付物**：`AdminUsersPage.tsx` 修改（含注释更新）。
**自检标准**：`pnpm --dir frontend build` 零 TS 错误；代码走查确认无 `pageSize:100` 单页假设、保存集合包含未加载已绑长者。
**✋确认点 2.1**：等主理人确认后进 S2.2。

### S2.2 admin-api.ts 错误文案（关 P2-26）

**操作步骤**：
1. 401 → `AdminApiError("invalid", "登录已过期，请重新登录")`，不再透传登录页文案。
2. 400 → 尽力解析响应体 detail 字段（执行时先读 `console/api.ts` 的 `apiPut/apiPost` 是否返回 body，不满足则最小扩展），提示具体原因；不再统一「请刷新页面」。
3. 409 → `reassignElders` 既有文案「该长者将失去最后一位责任护工…」保留，且弹窗不关闭、编辑态保留（现有实现核对即可）。
4. 422 → 提示参数错误字段。
5. `console/App.tsx` 仅移除与本日 E20 相关的 Mock 假设；工单/健康/药单等 Mock 不动。

**交付物**：`admin-api.ts`（必要时含 `console/api.ts` 最小扩展）修改。
**自检标准**：build 零错误；401/400/409/422 四类文案与后端实际返回码一一对应（对照 `接口契约_身份与权限.md` §7 与 E20 契约 §5.7）。
**✋确认点 2.2**：等主理人确认后进 S2.3。

### S2.3 造数验证（裁决 J-D2）

**操作步骤**：
1. 写一次性造数脚本（curl 走后端 API 建 150 位长者），脚本只进 `/tmp` 或会话临时目录，**不入 Git**。
2. 执行验证：候选可见第 101–150 位；保存只影响勾选变化（未加载长者不被解除）；连续快速刷新 5 次最终列表一致；`createStaff` 成功后新员工立即出现。
3. 证据（操作截图/请求记录）贴入 `docs/plans/day07/d-联调记录.md` D2 节。

**交付物**：D2 验证记录。
**自检标准**：任务卡 §3.4 四条逐条可勾。
**✋确认点 2.3（D2 收口）**：等主理人确认后进 D1。

---

## 4. D1 · pad 播放/打断/VAD 可靠性（1.25 人日）

### S1.1 playback-queue.ts 六项修复

**操作步骤**：
1. `isSegmentedTts` 增加 `typeof msg.epoch === "number"`：epoch 缺失/非法不进分段路径、不污染 `maxEpoch`（P3-36）。
2. 淘汰判定前置配合：`PlaybackQueue` 暴露 `isStale(msg)`（或等价判定方法），供 realtime 在 unmute/play/置 speaking **之前**调用；被淘汰段不播、不 ack、不改 `ttsPlaying`（P2-22，与 S1.2.2 联动）。
3. `total` 缺失按单段收口：`totalSegments: msg.total ?? segmentId + 1`，起 tail 兜底并发 `sendDiag(false, "total_missing_single_segment")`（P2-24）。
4. `flushCanceled`/`markTrackEnded` 内调用 `cancelRetry()`，防旧 playing ack 乱序补发（P3-36）。
5. playing ack 改到实际起播后发送：段到达仅记录 pending 状态，由 realtime 在 audioEl 实际起播（`onplaying`/`play()` 成功）后调用 `notifyPlaybackStarted()` 补发（P3-39）。
6. tail 兜底结算打 diag 标记（`tail_fallback_estimated`，估算/实测口径不静默）。

**交付物**：`playback-queue.ts` 修改。
**自检标准**：build 零错误；单文件走查 6 项逐条对上；无新增协议字段。
**✋确认点 1.1**：等主理人确认后进 S1.2。

### S1.2 realtime.ts 七项修复

**操作步骤**：
1. `teardownSession`/`stop()` 解绑 `devicechange`；`setupSession` 建新监听前先解绑旧引用，重连不累积 listener（P2-23）。
2. tts_text 处理顺序调整：分段消息先过队列淘汰判定（S1.1.2），通过后才 `muted=false`/`play()`/置 speaking/渲染字幕（P2-22）。
3. `reopenMic` 加并发守卫（进行中直接返回）（P3-38）。
4. `setupSession` 中 `mic.start` 失败：`sendDiag("webrtc", false, ...)` + 用户提示，不静默无 offer（P3-38）。
5. `stop()` 复位 `ttsPlaying=false`；`teardownSession` 清 `audioEl.onended=null`、`muted=true`（P3-38）；tts_text 路径 `play()` 失败给「点击屏幕后开始播放语音」提示（与 ontrack 路径一致）（P3-38）。
6. **打断上行（P1-3 前端半边 + A1/B1 契约）**：speechstart 本地 `stopPlayback()` 后立即 `socket.send({type:"interrupt", response_id, reason:"barge_in"})`，不等待回执；旧 epoch 回调丢弃。`response_id` 由 PlaybackQueue 暴露当前活跃 response（S1.1 配套）；字段名待 A1 契约冻结后逐字段校对。
7. 路由降级提示预留：收到降级标记时显示常量文案「网络慢，正在换种方式回答」，不新增协议字段；A3 未下发则只做预留，不阻塞 D1（任务卡 §2.2.5）。

**交付物**：`realtime.ts` 修改。
**自检标准**：build 零错误；重连 3 次后 `devicechange` listener 不累积（DevTools `getEventListeners` 或行为验证）；打断后本地 200ms 内静音且 `interrupt` 消息可在服务端日志/WS 抓包可见。
**✋确认点 1.2**：等主理人确认后进 S1.3。

### S1.3 vad.ts + vad-worker.js 五项修复（裁决 J-D1）

**操作步骤**：
1. threshold 常量化为 `VAD_THRESHOLD = 0.38`，代码/注释/文件头三处统一；注释登记「0.38 为先设值（主理人裁决 2026-11-10），20 句近/远场标定待真人复测」（P1-4）。
2. `feedWindow(pending)` 包 try/catch：异常时重置 `pending`/`used`、继续下一帧，不永久砖死；异常经新消息类型上报主线程（P1-5）。
3. 运行期遥测（P1-6）：worker 新增 `runtime_error` 消息（协议只增不改）；`vad.ts` 暴露 `onRuntimeError` 回调；`realtime.ts` 收到后 `sendDiag("vad", false, "vad_runtime_error: …")` 触发服务端 webrtcvad 兜底；ready 后的 `init_error` 与 `worker.onerror`（运行期）同样转发，不再只 console.warn。
4. `handleReset` 若处于说话态先发 `speech_end` 再复位（P3-37）。
5. 心跳 diag：worker 内 120s 零 speechstart → 上报一次（`vad_no_speech_120s`），便于复测（任务卡 §2.2.3）。

**交付物**：`vad.ts`、`vad-worker.js` 修改；threshold 裁决登记（本计划 §0）。
**自检标准**：build 零错误；注入 `acceptWaveform` 异常（DevTools 断点或临时改动）后下一帧仍产出 speech 事件、`client_diag` 有 `vad_runtime_error`；reset 后无悬挂说话态。
**✋确认点 1.3**：等主理人确认后进 S1.4。

### S1.4 protocol.ts interrupt 类型

**操作步骤**：新增 `InterruptMsg`（C→S）并入 `ClientSignal` 联合类型；字段以 A1 契约为准，冻结前按 Day7 README §5.4 草案并注释标注。不加第二套路由消息。

**交付物**：`protocol.ts` 修改。**自检标准**：build 零错误；类型与发送点一致。
**✋确认点 1.4**：等主理人确认后进 S1.5。

### S1.5 构建门禁与 20 轮 ack 回归

**操作步骤**：
1. `pnpm --dir frontend build`：零 TS 错误，pad/console/family 三产物均出（G4 门禁）。
2. 20 轮真实/模拟回复回归：`segment_ack` 上报率 100%，无重复/乱序；记录段数。
3. 边界注入：人为旧 epoch `tts_text`（DevTools 改 WS 帧或调试钩子）→ 不发声、不 ack、不改 `ttsPlaying`；`total` 缺失 → 一次回复后状态条收口；打断 → 200ms 内静音。
4. 全部证据写入 `docs/plans/day07/d-联调记录.md` D1 节（20 轮记录 + 标定登记）。

**交付物**：D1 回归记录、`d-联调记录.md`（D1 部分）。
**自检标准**：任务卡 §2.4 五条逐条可勾。
**✋确认点 1.5（D1 收口）**：等主理人确认后进 D3。

---

## 5. D3 · 触达/路由联调修复与收口（0.75 人日）

### S3.0 依赖状态检查（裁决 J-D3 硬推进）

**操作步骤**：开工时检查并记录 A3（路由+`decision_logs`）、C2（到点触达 `install_scheduler`）、B1（打断终态）交付状态到 `d-联调记录.md`；未就绪项当场催办对应负责人，联调按计划照跑，缺什么依赖在记录中登记阻塞项，不等待、不口头遗留。

### S3.1 环境准备与造数

**操作步骤**：
1. `docker compose up -d postgres redis`；后端 8000；sidecar 7001（若 A2 未就绪则记录降级预期）；pad 真机走局域网 HTTPS `https://10.55.50.115`（getUserMedia 硬要求），浏览器 localhost 兜底（真机不可用时登记补测，不冒充真机）。
2. 造一条 `schedule_cron='* * * * *'` 的 active 药单；真机在线等到点。

### S3.2 五项联调验证（记录现象 + SQL 证据）

| # | 验证项 | 通过口径 |
| --- | --- | --- |
| 1 | 触达播出 | 字幕渲染 + 独立音频通道播出；不进 PlaybackQueue、不触发 `segment_ack` |
| 2 | played 闭环 | 播完 `touch_played` → `medication_attempts.receipt=played`；打断/关页/未播完不回；重复播完不报错 |
| 3 | TTS 降级 | 无 `audio` 字段只出字幕，`tts_result=tts_failed`，不白屏 |
| 4 | 触达与回复并发 | 互不打断、不串流 |
| 5 | 路由降级 | 低置信澄清只一次；sidecar 关闭时回复仍可用且有前端降级提示；`decision_logs` 有 `fallback` 行 |

### S3.3 缺陷归位

前端问题（播放/状态/ack/提示）→ 本卡直接修；后端问题（终态/触达/调度/路由）→ 回 B1/C1/C2/A3，附 `trace_id` + 请求/响应原文；**不用前端兜底掩盖后端缺陷**（如伪造 `played`）。

### S3.4 联调记录与收口

写 `docs/plans/day07/d-联调记录.md`：环境、步骤、SQL 结果、问题清单、归属、修复 commit。SQL 核对（任务卡 §4.4）：

```sql
SELECT attempt_no, receipt, tts_result FROM medication_attempts ORDER BY created_at DESC LIMIT 4;
-- 至少 1 行 receipt=played；降级场景 tts_result=tts_failed

SELECT route_layer, intent, confidence, latency_ms, model_version FROM decision_logs ORDER BY created_at DESC LIMIT 5;
-- 至少 1 行 fallback（关 sidecar 时）；正常时有 rule/system_one/agent
```

**✋确认点 3.4（D3/G5 收口）**：等主理人确认后关闭本卡。

---

## 6. 联调执行步骤（独立操作手册）

> 目标：主理人可按本节独立完成 D3 端到端联调。每步含动作、观察点、预期、异常判定。

### 步骤 L1 · 起环境

| 项 | 内容 |
| --- | --- |
| 动作 | ① `docker compose up -d postgres redis`；② `uv run uvicorn app.main:app --port 8000`；③ sidecar `uv run uvicorn app.system_one.server:app --port 7001`（A2 未就绪则跳过并记录）；④ 前端 `pnpm --dir frontend dev` 或 nginx 产物；⑤ pad 真机浏览器打开 `https://10.55.50.115`（局域网 HTTPS） |
| 观察 | `/health` 返回 200 且响应头带 `X-Trace-Id`；pad 页面登录后状态条「在听」 |
| 预期 | 三服务全绿，pad 会话建立 |
| 异常判定 | `/health` 非 200 → 后端问题回 dev-A（装配）/dev-C（scheduler）；pad 无法唤起麦克风 → 检查 HTTPS 证书是否受信 |

### 步骤 L2 · 造到点药单

| 项 | 内容 |
| --- | --- |
| 动作 | 用 console（医生角色）或 SQL 给测试长者建一条 `schedule_cron='* * * * *'`、status=active 的药单；pad 保持在线 |
| 观察 | 下一分钟整点，pad 收到触达：大字幕出现「到吃药时间了…」+ 触达语音播出 |
| 预期 | 触达与对话回复可同时存在，互不打断 |
| 异常判定 | 到点无触达 → 回 C2（调度接线），附后端日志与 `trace_id`；有字幕无声音 → 前端问题本卡修（先查 `tts_text` 是否带 `audio` 字段） |

### 步骤 L3 · played 闭环（TD-2 核心证据）

| 项 | 内容 |
| --- | --- |
| 动作 | 让触达音频**自然播完**，随后执行核对 SQL |
| 观察 | `medication_attempts` 最新行 `receipt` 由 `voice_dispatched` 升级为 `played` |
| 预期 | `SELECT attempt_no, receipt, tts_result FROM medication_attempts ORDER BY created_at DESC LIMIT 4;` 出现 `played` |
| 异常判定 | 自然播完但无 `played` → 先抓 WS 帧确认 pad 是否发出 `touch_played`：发了 → 回 B1/C1（signaling 分支/落库）；没发 → 前端本卡修 |
| 边界 | 各验一次：播完前打断/关页 → 不产生 `played`；重复播完（重连后回放）→ 幂等不报错；无 `audio` 的降级触达 → 只出字幕、不回执 |

### 步骤 L4 · TTS 降级与并发

| 项 | 内容 |
| --- | --- |
| 动作 | 模拟 TTS 失败（断 TTS key 或临时关闭合成）再触发到点；随后在触达播报中与数字人正常对话 |
| 观察 | 降级触达只出字幕不白屏；对话回复与触达互不串流、互不打断 |
| 预期 | `tts_result=tts_failed`；两条音频通道独立 |
| 异常判定 | 字幕也不出 → 前端本卡修；白屏 → 前端本卡修；串流 → 前端修后仍串则回 C1（消息形状） |

### 步骤 L5 · 路由与降级提示（A3 依赖项）

| 项 | 内容 |
| --- | --- |
| 动作 | ① 说一句低置信模糊话（如「帮我弄一下那个」）观察澄清；同会话再说一次仍模糊；② 临时关停 sidecar（杀 7001 进程）再说任意一句 |
| 观察 | ① 澄清只出现一次，第二次进入 Agent 回答；② 关停后回复仍可用，pad 出现「网络慢，正在换种方式回答」提示 |
| 预期 | `SELECT route_layer, intent, confidence, latency_ms, model_version FROM decision_logs ORDER BY created_at DESC LIMIT 5;` 至少 1 行 `fallback`；正常轮次可见 `rule/system_one/agent` |
| 异常判定 | 无 `decision_logs` 行 → 回 A3；澄清出现两次 → 回 A3（同一 thread 判定）；关停后无回复 → 回 A3 降级链路，`trace_id` 随附 |

### 步骤 L6 · 缺陷登记与收口

| 项 | 内容 |
| --- | --- |
| 动作 | 每个异常当场记入 `d-联调记录.md`：现象 / `trace_id` / 请求响应原文 / 归属任务号 |
| 观察 | 问题清单无「待查」口头项，全部有归属 |
| 预期 | G5 门禁证据链齐：SQL 两张表结果 + 前端行为记录 + 归属清单 |
| 异常判定 | 发现 §5 清单内后端文件问题 → 只登记不修；发现前端问题 → 本卡修后复跑对应步骤 |

---

## 7. 交接清单（联调时提醒）

| 时点 | 对象 | 交接内容 |
| --- | --- | --- |
| D1 完成 | dev-B（B1） | 前端 `interrupt` 实际发送字段与时机（本地停播后立即发、不等回执）、`response_id` 取值来源 |
| D1 完成 | dev-A（A1） | `protocol.ts` 按草案实现的 interrupt 字段清单，请 A1 契约冻结后逐字段确认 |
| D2 完成 | dev-B（B3） | 前端消费的 409/403/422/401/400 错误码与文案；若后端未按 §5.7 返回，回 B3 修，前端不猜 |
| D3 联调 | dev-A（A3） | 路由降级标记下发情况、`decision_logs` 查询口径 |
| D3 联调 | dev-C（C2/C1） | 到点触达消息形状（`tts_text` 带 `touch_*`/`audio`）、调度与重试行为 |
| D3 收口 | 全员 | `d-联调记录.md` 作为 G5 门禁证据 |

## 8. 风险与回退

| # | 风险 | 处置 |
| --- | --- | --- |
| R1 | A1 契约晚于 D1 冻结 | 按 README §5.4 草案先行，A1 冻结后只校对 `protocol.ts` 一处 |
| R2 | D3 时 A3/C2/B1 未就绪 | 硬推进照跑，缺项登记阻塞并催办；前端可独立验证项（触达渲染/降级字幕/并发）先验 |
| R3 | B1 打断修复波及响应流（ack 用例红） | D1 改动可独立回滚（前端文件单独 revert），不阻塞后端 |
| R4 | 真机环境不可用 | 浏览器 localhost 走通并如实登记，真机证据补测，不冒充 |
| R5 | 造数污染开发库 | 脚本只经 API 建显式测试前缀数据，验证后可删；不直插 SQL 改业务表 |

## 9. 完成记录模板

```text
任务：D1/D2/D3
状态：已完成 / 阻塞
证据：
- 代码：<commit>
- 命令：<pnpm build / 20 轮 ack / SQL>
- 数据：<attempt receipt / decision_logs 行 / trace_id>
- 未完成：<无 / 具体项 + 原因 + 下一步>
```

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版：D2/D1/D3 分步确认制计划（S2.1–S3.4，7 个 ✋确认点）；登记主理人三项裁决（VAD threshold=0.38 先设值、D2 造数允许、D3 硬推进）；§6 联调独立操作手册（L1–L6）；§7 交接清单；代码现状基线按 2026-11-10 快照实证 |
| v1.1 | 2026-11-10 | J-D1 裁决更新：真机复测后 threshold 调整为 0.45（取代 0.38 先设值）；L6#6 落库修复按「图运行后调用处提交」落地（授权记录见 d-联调记录.md #6） |
