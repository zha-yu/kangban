# A4 · pad 前端

> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-05
> 关联：docs/team_tasks_day03/README.md（分工总览）
> 上游依据：day-03 计划 T0（骨架+登录页+HTTPS 策略）与 T4（VAD+打断）｜adhoc 方案 M1（登录终稿：device_id+device_secret 直连 E4）｜PRD §2.1（1920×1080 大字）
> 变更记录见文末

## 职责与写作用域

- `frontend/` 全部：`vite.config.ts`、`index.html`、`src/pad/**`（MPA 入口，仅建 pad，console/family 目录占位不建）
- **禁触**：后端任何文件；`three-vrm` 依赖**不装**（pad 专属依赖不得进其他入口——分层硬约束 4）

## 前置依赖

无硬前置（可先用本地 mock 自测）；14:30 三方联调前需 C2（WS 消息表）冻结。HTTPS：全程本机 `localhost:5173`（getUserMedia 可用），跨机 mkcert 登记待办。

## 交付物

1. MPA 骨架：pad 入口、1920×1080 布局、大字幕组件、状态条（正在听/正在想/正在说）
2. 登录页：device_id + device_secret 表单 → `POST /api/device-sessions`（E4）；错误映射（**401 统一口径**：设备不存在/密钥不匹配/已锁定同码，防存在性探测——2026-11-05 主理人裁决；429 倒计时）；token 存 sessionStorage；成功跳主界面
3. AudioWorklet 重采样：48k 采集 → 16k/60ms 帧 + 600ms preRoll 环形缓冲
4. TEN-VAD WASM 接入（移植教师模块，参照 `spikes/reference/asr/src/asr/web/vad/`，WASM 资产 `spikes/reference/asr/funasr-runtime-resources/models/sherpa-onnx/sherpa-onnx-wasm-simd-v2.0.1-ten-vad/`）：speechstart → 发 `asr_start`（preRoll 随帧补发）；speechend（0.8s 静音）→ `asr_end`；**加载失败回落服务端 webrtcvad**（开关 + 降级标记）
5. WebRTC 客户端：getUserMedia（AEC/NS/AGC 开启）→ offer → 经 C2 信令换 answer/ICE → 收音频轨播 TTS
6. 打断：检测到本地说话 → 停止 TTS 播放渲染，字幕切换
7. 静默刷新/过期处理：按 A2 的设备会话 refresh 核对结论执行（无 refresh 通道则过期回登录页）

## 验收标准

- `pnpm --dir frontend build` 产出 MPA 多页且 pad 包不包含其他入口代码
- 登录页真实换证成功（对 Day 2 后端）；错误提示正确（401 统一口径 / 429 倒计时）
- `three-vrm` 未出现在任何 `package.json` 与构建产物
- 手工：localhost 页面麦克风权限申请 → VAD 切段 → 打断即时生效（V4 联调项）

## 时间节点

09:30 前骨架+登录页（mock 自测）→ 12:00 前 Worklet+TEN-VAD → 14:30 三方联调 → 17:00 收口提交。

## 联调对接点

- C2 全部消息（A2 定稿）；offer/answer/ice 对接 A3
- 降级模式：WS/WebRTC 建连失败 → 按 C3（A5）走 REST 整段上传
- 大字幕文本来源：C2 的 `tts_text`

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-05 | 初稿 |
| v1.1 | 2026-11-05 | 教师参考模块入库 `spikes/reference/asr/`，TEN-VAD 参照与 WASM 资产路径落定 |
| v1.2 | 2026-11-05 | 主理人裁决：登录错误映射统一 401 口径（设备不存在/密钥不匹配/已锁定同码，防存在性探测），废除原「404 重输/403 停用」两行；依据 E4 实测（错密钥与未知设备均 401）与接口契约文档 E4 行；同步修订 adhoc-20261105 §2.4 |
