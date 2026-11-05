> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-05
> 关联：docs/team_tasks_day03/a4-pad-frontend.md（任务卡）｜docs/team_tasks_day03/README.md（分工总览）｜docs/team_tasks_day03/contracts.md（C1/C2/C3 契约稿）
> 上游依据：PRD §2.1（1920×1080 大字）、§2.2、§4.11、FR-UI-01/09/10、FR-TC-10、AC-40（验收）｜技术设计文档 line 1016–1023 Day 3 落地清单（实现）｜day-03 计划 v1.5 T0/T4｜adhoc-20261105 方案 v1.3 M1–M3（裁决口径）
> 变更记录见文末

---

# A4 · pad 前端 Day 3 工作推进计划（小时级）

## 0. 一句话目标

从零搭起 `frontend/` MPA 工程并完成 pad 端 Day 3 全部交付：骨架（1920×1080 大字幕 + 状态条）→ 登录页（device_id + device_secret 直连 E4）→ AudioWorklet 重采样 + TEN-VAD 端侧切句 → WebRTC 客户端与打断 → 降级 REST 客户端，14:30 三方联调打通，17:00 随全组收口提交。**全程不写任何后端代码，不装 three-vrm。**

## 0.1 项目认知基线（本计划的事实依据，全部来自 PRD 与技术设计文档）

| 维度 | 口径 |
| --- | --- |
| 产品定位 | CCRC 长者卧室 3D 数字人陪伴系统，pad 为长者端，参考尺寸 10 英寸 1920×1080、浏览器全屏、大字（PRD §2.1） |
| 语音主链路 | WebRTC 全双工音频轨（AEC/NS/AGC）+ 信令 WS `WS /ws/signal` 首帧鉴权（C2）；降级链路为 `POST /api/audio/utterance` REST 整段上传（C3）（PRD §2.2、adhoc §一/§二） |
| VAD 口径 | 端侧 TEN-VAD WASM 为主 VAD（主理人裁决），加载失败回落服务端 webrtcvad 并打 `vad_mode=fallback` 标记 |
| 身份口径 | pad 登录凭证 = `device_id + device_secret` 双因子直连既有 E4 契约 `POST /api/device-sessions`（adhoc v1.3 最终裁决，零迁移）；elder_id 一律服务端注入，前端不传（禁区 9）；token 存 sessionStorage 不进 localStorage |
| 通道生命周期 | PeerConnection 常驻，随语音开合的是 ASR 会话：speechstart 发 `asr_start`（preRoll 600ms 随帧补发）、speechend（0.8s 静音）发 `asr_end`（adhoc M3） |
| 回复形态 | Day 3 回复为**写死话术**，不接 LLM 路由/Agent（day-03 计划 §0） |
| 依赖现状 | 后端 Day 2 已交付 E1–E11（270 条测试绿）；`app/audio/` 待 A1/A2/A3/A5 建设；`frontend/` 不存在，从零初始化；教师参考模块在 `spikes/reference/asr/`，TEN-VAD WASM 资产在 `spikes/reference/asr/funasr-runtime-resources/models/sherpa-onnx/sherpa-onnx-wasm-simd-v2.0.1-ten-vad/` |

## 1. A4 任务清单与依赖关系

### 1.1 任务分解总表

| # | 任务 | 前置依赖 | 被依赖方 | 出处 |
| --- | --- | --- | --- | --- |
| T-A | MPA 工程骨架（pad 入口、1920×1080 布局、大字幕、状态条三态） | 无（工程初始化可最先开工） | 全部前端任务的地基 | a4 卡交付物 1 |
| T-B | 登录页（E4 换证 + 三类错误映射 + sessionStorage） | T-A；后端 :8000 可起（Day 2 已就绪）；E4 契约已冻结 | 主界面入口 | a4 卡交付物 2 |
| T-C | AudioWorklet 重采样（48k→16k/60ms 帧 + 600ms preRoll 环形缓冲） | T-A；参照 `spikes/reference/asr/src/asr/web/audio-worklet.js` | T-D、T-E、T-G 的采集底座 | a4 卡交付物 3 |
| T-D | TEN-VAD WASM 接入（切句 + asr_start/asr_end + 回落开关） | T-C；C2 冻结（09:00 契约会） | T-E 的切句信号、T-F 的打断检测 | a4 卡交付物 4 |
| T-E | WebRTC 客户端（getUserMedia → offer → C2 信令换 answer/ICE → 收轨播 TTS）+ WS 状态机/心跳/重连 | T-B（token）；**C2 冻结**；A2 信令可用；A3 answer/ICE 下行 | 三方联调主体 | a4 卡交付物 5 |
| T-F | 打断（本地说话 → 停 TTS 播放渲染 + 字幕切换） | T-D（speechstart 信号）+ T-E（TTS 播放中状态） | V4 联调验收项 | a4 卡交付物 6 |
| T-G | 降级 REST 客户端（建连失败 → C3 整段上传，401/413/503 处理不白屏） | T-C（preRoll 整段 WAV）；C3 冻结；A5 路由就绪 | V1/V5 验收项 | a4 卡交付物 7 + 联调对接点 |
| T-H | 静默刷新/过期处理 | 09:00 契约会上 A2 的设备会话 refresh 核对结论 | T-E 重连逻辑 | a4 卡交付物 7 |

### 1.2 依赖时序（与全组节点对齐）

```
09:00–09:30 契约冻结会（C2/C3 为 A4 的两个消费契约，必须到会确认）
   ├── T-A / T-B（无硬前置，骨架与登录页可先行 / 契约会后立即收口）
   ├── T-C / T-D（只需教师参考模块，不依赖 C2，可全程并行）
   └── T-E / T-F / T-G / T-H（依赖 C2/C3 冻结 + A2/A3/A5 产出）
14:30–16:30 三方联调（A4↔A2↔A3，A1 支撑 FunASR 真链路）← T-A~T-H 全部就位
17:00–17:30 收口会（全组 pytest + build 校验 + 一次性提交）
```

## 3. 各任务执行细节与交付物要求

### T-A · MPA 工程骨架

**步骤**
1. `frontend/` 下初始化 Vite + React + TypeScript 工程；`vite.config.ts` 按 MPA 配置 `rollupOptions.input`（`index.html` 声明 pad 入口；console/family 目录仅占位**不建**入口）。
2. `src/pad/` 建 1920×1080 固定布局壳（浏览器全屏演示，PRD §2.1 大字字号体系）。
3. 大字幕组件：文本来源为 C2 的 `asr_partial` / `asr_final` / `tts_text`，字号适配长者远距可读（FR-UI 系列，不自创交互）。
4. 状态条三态组件：正在听 / 正在想 / 正在说，另含连接态「连接中 / 已断开」（adhoc §3.3）。

**交付物质量要求**：pad 专属依赖不得进其他入口构建产物（分层硬约束 4）；组件无业务逻辑内联，信令/采集逻辑分层（`protocol.ts` / `ws.ts` / `audio/` / `webrtc.ts`）。

**自检**：`pnpm --dir frontend dev` 打开页面无报错；布局 1920×1080 无滚动条错位。

### T-B · 登录页

**步骤**
1. 大字表单：`device_id` + `device_secret` 两个输入框（粘贴长 UUID，多行留白、可清焦重输）。
2. 提交 → `POST /api/device-sessions`（E4：`{device_id, device_secret}` → `{access_token, expires_in: 86400, device:{id, room_id, elder_id}}`）。
3. 错误映射（adhoc §2.4，逐条实现）：404「设备不存在，请检查 device_id 是否输入正确」（可重输）；403「设备已被管理员停用，请联系工作人员」（不重试）；429「尝试次数过多，请 1 分钟后再试」（倒计时解锁）；网络错误给可读提示不静默。
4. 成功：token + expires_at 存 **sessionStorage**（不进 localStorage），跳主界面并触发 getUserMedia。
5. 无后端时的 mock 自测开关（本地状态机验证三类错误 UI）。

**交付物质量要求**：错误提示为长者可读话术；每次重试刷新 trace（沿用 `X-Trace-Id`）；不打印 token 到 console。

**自检**：对 Day 2 真后端换证成功一次；404/403/429 各触发一次提示正确（后端造数）。

### T-C · AudioWorklet 重采样

**步骤**
1. 移植 `spikes/reference/asr/src/asr/web/audio-worklet.js`：48k 采集 → **线性插值**重采样到 16k（教师模块「平均累加」法替换为标准插值，day-03 计划 §1.3-3 明确要求）。
2. 60ms/帧 PCM16（960 采样）输出；600ms preRoll = 10 帧环形缓冲。
3. 麦克风 constraints（adhoc §2.3）：`echoCancellation/noiseSuppression/autoGainControl: true`、`channelCount: 1`、`sampleRate: {ideal: 48000}`；`enumerateDevices` + `devicechange` 监听拔出回落默认设备。

**自检**：console 打帧日志验证帧长 960 采样、16k；静音 10s preRoll 内容为空、说话后 preRoll 含句首音频（句首字不丢）。

### T-D · TEN-VAD WASM 接入

**步骤**
1. 移植 `spikes/reference/asr/src/asr/web/vad/`（`ten-vad.js` / `demo.js` 的 `BrowserTenVad` 模式），WASM 资产指向 `spikes/reference/asr/funasr-runtime-resources/models/sherpa-onnx/sherpa-onnx-wasm-simd-v2.0.1-ten-vad/`。
2. 参数：threshold 0.5、silence 0.8s、minSpeech 0.25s、maxSpeech 30s 强制切段（防长录音）。
3. speechstart → 发 C2 `asr_start`，preRoll 10 帧随首帧补发；逐帧 60ms PCM16 推流；speechend（0.8s 静音）→ 发 `asr_end`。
4. 兜底：WASM 拉取失败/初始化异常 → 前端打 `vad_mode=fallback` 标记 + 状态条提示，回落服务端 webrtcvad（前端只推流，切句由服务端判定）。

**自检**：说话→start、静音 0.8s→end 时序正确；WASM 路径改错可复现 fallback 路径（临时改 URL 验证后还原）。

### T-E · WebRTC 客户端与 WS 状态机

**步骤**
1. `WS /ws/signal` 建连后 300ms 内首发 `auth{token}`（首帧鉴权，token 不进 URL）；`auth_fail` 4401 处理（invalid/expired → 回登录页）。
2. `RTCPeerConnection` 建 offer（音频轨收发）→ C2 `offer{sdp}` → 收 `answer{sdp}` / `ice{candidate}` 完成建连；收音频轨接 `<audio>` 播 TTS。
3. 状态机 `idle→connecting→authenticating→connected→reconnecting→closed` 映射状态条；心跳 20s `ping`、45s 无任何帧主动断开重连；重连指数退避 1/2/4/8/15s ±20%，连续 5 次失败出「连接失败，点击重试」；`visibilitychange` 回前台立即重连。
4. WS 断开时 PeerConnection 保活不动（媒体独立于信令，adhoc §3.3-4），恢复后重发 ICE candidate 缓存。
5. 字幕渲染：`asr_partial` 预显（灰）→ `asr_final` 终稿 → `tts_text` 渲染数字人回复字幕。

**自检**：对 A2 信令完成一次 offer/answer/ICE 往返（联调前可用本地 echo WS 桩自测状态机）。

### T-F · 打断

**步骤**：TTS 播放渲染期间检测到本地 speechstart（TEN-VAD 信号）→ 立即停止 TTS 播放与字幕滚动、状态条切「正在听」、字幕切打断提示；对齐 PRD「说打断数字人立刻停」（技术验收 line 1021）。

**自检**：播放中说话，停止延迟肉眼即时（≈一帧级）；状态条与字幕同步切换。

### T-G · 降级 REST 客户端

**步骤**
1. 触发条件：WS/WebRTC 建连失败（状态机 5 次失败或 ICE 超时）→ 自动切「按住说话」降级模式（FR-UI-01）并提示。
2. 采集：按住说话整段录音（含 preRoll），打包 16k/16bit/单声道 WAV。
3. 上传：C3 `POST /api/audio/utterance`（multipart：`audio` 文件 + `duration_ms`；`Authorization: Bearer <access_token>`）。
4. 渲染 200 → `{text, reply:{text, audio_url}}`：大字幕展示 text 与 reply.text，audio_url 有值则播放；401 → 回登录页；413 →「说话太长」提示；503 → 渲染 body 降级话术。**任何分支不白屏不静默**（V5 硬验收）。

**自检**：三错误分支各渲染一次可读提示；mock 服务下整段上传往返成功。

### T-H · 静默刷新 / 过期处理

**步骤**：按 09:00 契约会上 A2 核对结论执行——有 refresh 通道则 token 剩余 <10min 先静默刷新再重连；**无 refresh 通道则 24h 过期 / 401 直接回登录页**（a4 卡交付物 7 原文兜底口径）。

**自检**：过期路径人工触发一次，行为与会上结论一致。

## 4. A4 自检校验总清单（16:30–17:00 逐项过）

| # | 校验项 | 通过标准 | 对应验收 |
| --- | --- | --- | --- |
| 1 | `pnpm --dir frontend build` | 产出 MPA 多页，无构建错误 | a4 卡验收 1 |
| 2 | 包隔离 | pad 构建产物不含 console/family 入口代码 | 分层硬约束 4 |
| 3 | three-vrm 禁装 | 任何 `package.json` 与构建产物中 grep 无 `three-vrm` | a4 卡验收 3 |
| 4 | 登录换证 | 对 Day 2 后端真实换证成功；404/403/429 提示正确 | a4 卡验收 2 |
| 5 | 麦克风授权 | localhost 页面权限申请成功；被拒时有可读原因+重试入口 | V5、技术验收 line 1021 |
| 6 | VAD 切段 | 说话 start / 静音 0.8s end / preRoll 句首不丢 | M3 测试标准 |
| 7 | VAD 回落 | WASM 加载失败自动 fallback 并打标记 | a4 卡交付物 4 |
| 8 | 打断 | 播放中说话即时停止 TTS 渲染，字幕切换 | V4 |
| 9 | 心跳与重连 | 20s ping；断连后退避重连；5 次失败出提示 | M2 测试标准 |
| 10 | 降级切换 | 建连失败自动切按住说话，整段上传往返渲染成功，不白屏 | V1/V5、FR-UI-01 |
| 11 | 提交纪律 | 只提交 `frontend/` 文件；后端零改动（`git status` 核对） | README §1 写作域 |

## 5. 风险与禁止事项

| # | 风险/红线 | 处置 |
| --- | --- | --- |
| R1 | getUserMedia 需 HTTPS/localhost | Day 3 全程 `localhost:5173`（主理人裁决）；跨机 mkcert 已登记待办，不自行引入证书 |
| R2 | three-vrm 误装 | 禁触清单第一项；安装依赖只允许 react/react-dom/vite/typescript 及采集必需项 |
| R3 | C2/C3 契约变动 | 冻结后改动必须群通告 + 对方确认前不合入；A4 侧以 `protocol.ts` 单点维护类型，变更只改这一处 |
| R4 | 教师 WASM 资产体积大、加载慢 | 预加载放在登录成功后；加载失败走 fallback 不阻塞主界面 |
| R5 | 联调时后端产出（A2/A3）未就绪 | 自测用本地 WS echo 桩与 mock 服务（T-A/T-B 的 mock 开关复用），不被阻塞；阻塞 >30min 升级 A5 机动 |
| R6 | 后端文件零触碰 | 禁触全部 `app/`、`docker-compose.yml`、`alembic/`；发现后端缺陷只登记反馈给对应负责人 |
| R7 | 唤醒词（AC-42） | 不实现——优先级 Day 3 前敲定且 a4 卡交付物未列，若会上定为 P0 由主理人扩卡后再排 |

## 6. 明确不在 A4 Day 3 范围

- three-vrm 数字人渲染（Day 4）；声纹录入 UI（Day 4，adhoc M4）；console/family 两入口（仅目录占位）；反向 WebRTC 外呼（Day 7，FR-UI-10）；唤醒词激活（AC-42，待定）；真实 LLM 回复（Day 6）。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-05 | 初稿：项目认知基线、T-A–T-H 任务分解与依赖、小时级时间表、各任务执行细节与交付物要求、11 项自检清单、风险红线与范围外清单 |
