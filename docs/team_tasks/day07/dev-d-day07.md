# Task dev-D · Day 7 · pad 可靠性、console 改绑与联调收口

> 状态：未开始　｜　版本：v1.0　｜　日期：2026-11-10
> 负责人：dev-D　｜　总人日：2.75（D1 1.25 + D2 0.75 + D3 0.75）
> 关联：`docs/team_tasks/day07/README.md`（总表）｜`docs/team_tasks/day07/dev-d-触达真机联调.md`（D3 既有卡）｜`docs/team_tasks/day07/contracts.md`（TD-1/TD-2）
> 上游依据：PRD FR-UI-04/10、FR-MED-05/06｜技术设计文档 §4/§6｜`docs/plans/adhoc-20261110-day6全面审查.md`
> 本卡只含前端开发与开发自测；不设测试/产品/设计角色。

---

## 0. 一句话

把 pad 从“能播”修到“播得对、停得住、出错有证据”，把 console 改绑从“前 100 位长者”修到“完整分页不误解除”，最后用真机把触达、played、路由降级一次性验穿。

## 1. 写作用域（独占）

```text
frontend/src/pad/realtime.ts
frontend/src/pad/protocol.ts
frontend/src/pad/audio/playback-queue.ts
frontend/src/pad/audio/vad.ts
frontend/public/vad-assets/vad-worker.js
frontend/src/pad/components/**            # 仅路由降级/澄清提示所需
frontend/src/console/pages/AdminUsersPage.tsx
frontend/src/console/admin-api.ts
frontend/src/console/App.tsx              # 仅移除与本日相关的 Mock 假设
docs/plans/day07/d-联调记录.md            # D3 证据
```

**禁触**：`app/**`（后端接口问题回 B/C/A，不在前端兜底）、`app/audio/signaling.py`（B1）、`app/scheduler/**`（C2）、`frontend/src/family/**`、`vite.config.ts` 的 MPA 入口结构。

---

## 2. D1 · pad 播放/打断/VAD 可靠性（1.25 人日）

### 2.1 目标

关闭审查 **P1-4、P1-5、P1-6、P2-22/23/24、P3-36/37/38/39**：旧 epoch 不播、重连不泄漏、VAD 运行期不砖死且有遥测、tail 不永久卡“正在说”。

### 2.2 实施步骤

1. `playback-queue.ts`：
   - `handleSegment` 的旧 epoch 判断提前到 `unmute/play/置 speaking` 之前；被淘汰的消息**不播、不 ack**（P2-22）；
   - `isSegmentedTts` 校验 `epoch` 为 number，`undefined` 不污染 `maxEpoch`（P3-36）；
   - `total` 缺失时按单段收口，启动 tail 兜底并打 diag（P2-24）；
   - `flushCanceled`/`markTrackEnded` 同时 `cancelRetry`，防旧 playing ack 乱序补发（P3-36）；
   - `playing` ack 改到实际起播后发送，首段延迟指标可解释（P3-39）；
   - 兜底系数保留但加 diag 字段（估算/实测），不静默。
2. `realtime.ts`：
   - `teardown` 解绑 `devicechange`，`setup` 不覆盖未解绑引用；重连不累积 listener（P2-23）；
   - `reopenMic` 加并发守卫；`mic.start` 失败发 `client_diag`，不静默无 offer（P3-38）；
   - `stop()` 复位 `ttsPlaying`；`teardown` 清 `audioEl.onended`；`play()` 失败给用户可见提示（P3-38）；
   - 收到 `interrupt` 前本地 `stopPlayback()` 后立即发送 `{type:"interrupt",response_id,reason:"barge_in"}`，不等待回执；旧 epoch 回调丢弃（A1/B1 契约）。
3. `vad.ts` + `vad-worker.js`：
   - `threshold` 代码/注释/配置统一（当前实际 0.6 vs 注释 0.5）；改为可配置常量，默认值由 D1 用 20 句近/远场样本标定后写死（P1-4）；
   - `feedWindow(pending)` 包 try/catch；异常时重置 `used`/窗口并继续下一帧，不永久砖死（P1-5）；
   - `worker.onerror` / `init_error` 写 `client_diag`，触发服务端 webrtcvad 兜底，不再只 `console.warn`（P1-6）；
   - `handleReset` 发 `speech_end`，不埋雷（P3-37）；
   - 增加“N 分钟零 speechstart”心跳 diag（例如 120s），便于复测。
4. `protocol.ts`：只加 `interrupt` 类型与字段，字段名以 A1 契约为准；不加第二套路由消息。
5. 路由降级提示（最小 UI）：收到 `degraded`/`route_layer=fallback` 时显示“网络慢，正在换种方式回答”常量提示，不新增协议字段；若 A3 未下发该标记，则本项只做预留，不阻塞 D1。

### 2.3 交付物

- `playback-queue.ts`、`realtime.ts`、`vad.ts`、`vad-worker.js`、`protocol.ts` 修改。
- VAD 阈值标定记录（近/远场各 10 句，检出率/误报）。
- 20 轮 ack 回归记录（可放 `docs/plans/day07/d-联调记录.md`）。

### 2.4 验收（命令级）

```bash
pnpm --dir frontend build
# 零 TS 错误；pad/console/family 三产物均出
```

- 20 轮真实/模拟回复：`segment_ack` 上报率 100%（54/54 或本轮实际段数），无重复/乱序。
- 人为注入旧 epoch `tts_text`：不发声、不 ack、不改变 `ttsPlaying`。
- VAD：注入 `feedWindow` 异常后，下一帧仍能产出 speech 事件；`client_diag` 有 `vad_runtime_error`。
- 打断：本地 200ms 内静音，发送 `interrupt`；服务端 `response_interrupted` 可查。
- `total` 缺失：一次回复结束后状态条不再永久“正在说”。

### 2.5 依赖与交接

- 依赖 A1 `interrupt` 契约；B1 的服务端行为是软依赖（可先按契约开发）。
- 交接 D3：ack/打断证据；交接 B1：前端实际发送字段与时机。

---

## 3. D2 · console 改绑分页与错误 UX（0.75 人日）

### 3.1 目标

关闭 **P2-25/26/40**：长者候选不再固定前 100 位，保存不再全量覆盖导致第 101 位误解除；401/400 文案可执行；刷新有并发保护。

### 3.2 实施步骤

1. `AdminUsersPage.tsx`：
   - 长者候选改为完整分页（`page_size` 100，循环拉取或“加载更多”），或改为增量 diff；**禁止**只取前 100 位后全量覆盖；
   - 保存只提交变更的 `added/removed` 差集；未加载的长者不进入“未勾选”集合；
   - 显示“已选择 N 位/共 M 位”，避免用户误解；
   - `createStaff` 成功后刷新员工列表；`refreshStaffList` 加 `cancelled`/请求序号，防旧响应覆盖新状态（P2-40）。
2. `admin-api.ts`：
   - 401 映射为“登录已过期，请重新登录”，不显示“手机号或密码不正确”（P2-26）；
   - 400 带具体字段/原因，不再统一“请刷新页面”；
   - 409 显示“该长者将失去最后一位责任护工”，并保留当前编辑态；
   - 422 显示参数错误字段。
3. `console/App.tsx`：只移除与本日 E20 相关的 Mock 假设；工单/健康/药单等 Mock 不在本日扩大改造。

### 3.3 交付物

- `AdminUsersPage.tsx`、`admin-api.ts` 修改。
- 分页/差集保存的组件测试或手工记录（开发自证）。

### 3.4 验收（命令级）

```bash
pnpm --dir frontend build
```

- 造 150 位长者：候选可看到第 101–150 位；保存只提交变更差集，未加载长者不被解除。
- 401：提示重新登录，不出现密码错误文案；400/409/422 文案与后端错误码一致。
- 连续快速刷新 5 次：最终列表与最后一次请求一致，无旧响应覆盖。
- `createStaff` 成功后员工列表立即出现新员工。

### 3.5 依赖与交接

- 无前置；与 D1 同人串行，可与 A/B/C 并行。
- 交接 B3：前端使用的 409/403/422 错误码；若后端未按契约返回，回 B3 修，不在前端猜。

---

## 4. D3 · 触达/路由联调修复与收口（0.75 人日）

> 本任务承接既有 `docs/team_tasks/day07/dev-d-触达真机联调.md`，但**不是测试角色**：开发者负责把前后端接线跑通、定位并修复前端缺陷，把后端缺陷回写 C1/C2/A3/B1。

### 4.1 目标

用真机/浏览器把 TD-1（触达语音）、TD-2（played 回执）、C2（到点调度）、A3（路由降级）串起来，发现的问题当场归位到对应任务卡，不口头遗留。

### 4.2 实施步骤

1. 前置：`docker compose up -d postgres redis` + 后端 8000 + sidecar 7001 + pad（HTTPS 或 localhost；真机走局域网 HTTPS，满足 `getUserMedia`）。
2. 造一条 `schedule_cron='* * * * *'` 的 active 药单；真机在线等到点。
3. 验证并记录：
   - 触达字幕渲染 + 独立音频通道播出，不进入 `PlaybackQueue`、不触发 `segment_ack`；
   - 播完 `touch_played` → `medication_attempts.receipt=played`；打断/关页/未播完不回；
   - TTS 失败降级：无 `audio` 字段，只出字幕，`tts_result=tts_failed`；
   - 对话回复与触达并发：互不打断、不串流；
   - 路由：低置信澄清只出现一次；sidecar 关闭时回复仍可用且前端有降级提示；`decision_logs` 有 `fallback` 行。
4. 缺陷处理：
   - 前端问题（播放/状态/ack/提示）→ 本卡直接修；
   - 后端问题（终态/触达/调度/路由）→ 回 B1/C1/C2/A3，附 `trace_id` + 请求/响应原文；
   - 不把后端缺陷用前端兜底掩盖（例如前端伪造 `played`）。
5. 写 `docs/plans/day07/d-联调记录.md`：环境、步骤、SQL 结果、问题清单、归属、修复 commit。

### 4.3 交付物

- 联调记录（含 SQL/日志/截图路径）。
- 前端缺陷修复 commit。
- 后端缺陷回写清单（对应任务 ID）。

### 4.4 验收（命令级）

```sql
SELECT attempt_no, receipt, tts_result
FROM medication_attempts
ORDER BY created_at DESC LIMIT 4;
-- 期望：至少 1 行 receipt=played；降级场景 tts_result=tts_failed

SELECT route_layer, intent, confidence, latency_ms, model_version
FROM decision_logs
ORDER BY created_at DESC LIMIT 5;
-- 期望：至少 1 行 fallback（关闭 sidecar 时）；正常时有 rule/system_one/agent
```

- 触达音频自然播完 → `played`；打断/关页 → 不产生 `played`；重复播完不报错。
- TTS 降级只出字幕，不白屏。
- 低置信澄清只一次；第二次进入 Agent。
- 所有发现的问题有归属任务，无“待查”口头项。

### 4.5 依赖与交接

- 依赖 D1、D2、C2、A3；B1 的终态/打断是联调前置。
- 交接全员：联调记录作为 G5 门禁证据。

---

## 5. 完成记录模板

```text
任务：D1/D2/D3
状态：已完成 / 阻塞
证据：
- 代码：<commit>
- 命令：<pnpm build / 20 轮 ack / SQL>
- 数据：<attempt receipt / decision_logs 行 / trace_id>
- 未完成：<无 / 具体项 + 原因 + 下一步>
```

## 6. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版：D1 pad 播放/打断/VAD、D2 console 改绑、D3 触达/路由联调；覆盖审查 P1-4/5/6、P2-22~26、P3-36~40 |
