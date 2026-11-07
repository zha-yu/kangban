# 计划 · F1 vad Worker 隔离（f1-Day4前端联调 遗留缺陷修复）

> 状态：已完成　｜　版本：v1.1　｜　日期：2026-11-06
> 关联：docs/plans/day04/f1-Day4前端联调.md（写作域归属，pad 音频栈属其 W 域）｜docs/team_tasks/day04/task-04-VAD主链与遗留修复.md（服务端 webrtcvad 兜底域，仅知会不改动）
> 上游依据：PRD v2.6 FR-DLG-04、AC-42（TEN-VAD 端侧主 VAD 定位、≤1.5s 端到端）｜技术设计文档 v1.5（端侧主 / 服务端兜底 / 按住说话三级降级）
> 变更记录见文末

---

## 0. 问题与根因（已实证）

**现象**：呼叫小伴建连后，console 报 `Uncaught SyntaxError: Identifier 'ExitStatus' has already been declared (at sherpa-onnx-wasm-main-vad.js)`，端侧 TEN-VAD 初始化失败，静默回落服务端 webrtcvad。

**根因**：`public/kws/sherpa-onnx-wasm-kws-main.js` 与 `public/vad-assets/sherpa-onnx-wasm-main-vad.js` 均为**非 modularize** emscripten 产物，都在顶层声明 `class ExitStatus`（已 grep 两文件确认）。经典 script 顶层 class 进入全局词法环境，第二个执行的脚本同名声明即抛 SyntaxError。时序：standby 先加载 KWS → 呼叫后 [realtime.ts#L138](file:///Users/min/Desktop/repo/kangban/frontend/src/pad/realtime.ts#L138) 加载 VAD → 冲突爆发。

**为什么现在才暴露**：W6 实测时 [vad.ts](file:///Users/min/Desktop/repo/kangban/frontend/src/pad/audio/vad.ts) 超时阈值 20s，12MB wasm 未拉完即降级；阈值提至 30s 后脚本真正执行，冲突才浮现。

**影响**：对话链路不断（降级分支兜住），但只要 KWS 先加载过，端侧 VAD / 本地打断（T-F 打断）**必失效**，且主线程 wasm 编译卡在 WebRTC 建连瞬间，威胁 AC-42 的 1.5s 预算。附带的 `WASM does not support multithreading` 仅为警告（线程 2→1），无害。

## 1. 方案裁决（2026-11-06 主理人拍板：方案 A）

| 方案 | 结论 | 理由 |
| --- | --- | --- |
| **A. VAD 移入 Web Worker** | ✅ 采用 | Worker 全局作用域天然隔离标识符冲突；wasm 编译/推理移出主线程，利好建连期与 1.5s 预算；`TenVad` 公开接口不变，realtime.ts 零改动 |
| B. 隐藏 iframe 隔离 | 否 | 与主页面共享事件循环，编译仍卡主线程；跨 realm 生命周期管理比 Worker 更难写对 |
| C. MODULARIZE 重编两套 wasm | 否 | 需 emscripten 工具链重编 sherpa-onnx 源码，资产为 pinned 预构建产物，Day 4 阶段不现实 |
| D. patch 产物（IIFE 包裹） | 否 | 篡改第三方构建产物，破坏上游一致性，review 不可读 |

**可行性前提（已核实）**：
- `TenVad.speaking` 同步 getter 全仓零调用，无跨线程语义障碍；
- [capture.ts#L83-L84](file:///Users/min/Desktop/repo/kangban/frontend/src/pad/audio/capture.ts#L83-L84) 每帧 `new Int16Array(e.data)`、buffer 已转移不复用 → feed 走 transferable 零拷贝安全；
- realtime.ts 对 TenVad 的接触点仅 `load / feed / free`（L134/L138/L200），全部异步友好。

## 2. 设计

### 2.1 文件改动清单

| 文件 | 动作 | 说明 |
| --- | --- | --- |
| `frontend/public/vad-assets/vad-worker.js` | **新增** | 经典 Worker（手写纯 JS，不经 vite 打包，与现有 public 资产模式一致）：`importScripts` 加载胶水 + `self.Module` + `createVad`；256 点窗口缓冲与 `feedWindow` 状态机整体移入；消息协议见 2.2 |
| `frontend/src/pad/audio/vad.ts` | **重写** | TenVad 变为 Worker 封装：`load()` 建 Worker 等 `ready`（沿用 30s 超时，超时/失败抛 `VadUnavailableError`，realtime.ts 既有回落分支原样生效）；`feed()` postMessage transferable；`reset()` 转发消息；`free()` terminate；`speaking` 保留（镜像 worker 回报的 active 状态，注释注明现网零调用） |
| `frontend/src/pad/realtime.ts` | **零改动** | 公开 API 未变 |

### 2.2 消息协议（main ↔ worker）

| 方向 | 消息 | 语义 |
| --- | --- | --- |
| main→worker | `{type:"init"}` | Worker 建立后主线程首发，触发 importScripts 与检测器创建 |
| worker→main | `{type:"ready"}` | 检测器就绪 |
| worker→main | `{type:"init_error", message}` | 胶水加载/wasm 初始化/createVad 任一失败 |
| main→worker | `{type:"feed", pcm}` | Int16Array，transfer buffer（每帧独立 buffer，已核实安全） |
| worker→main | `{type:"speech_start"}` / `{type:"speech_end"}` | 对应 onSpeechStart / onSpeechEnd 回调 |
| main→worker | `{type:"reset"}` | 新会话前状态复位（detector.reset + 缓冲清零） |
| main→worker | `{type:"free"}` | 语义收尾，主线程随后 terminate |

### 2.3 关键口径

- Worker 为 classic（非 module），`importScripts('/vad-assets/sherpa-onnx-vad.js')` 等绝对路径在 vite dev 与 nginx 同源可用；
- wasm `numThreads: 1`（现网已证多线程不被支持，维持现状）；
- 加载超时 30s 与现值一致；超时后 terminate Worker，回落服务端 VAD，**链路不断**（不变式：本修复任何失败路径都必须落入既有 `VadUnavailableError` → realtime.ts 降级分支）；
- KWS 侧（主线程）零改动，其 `window.Module` 不再被 VAD 触碰，共享 Module 竞态顺带消除。

## 3. 分步执行（每步完成后待确认）

| 步 | 内容 | 自检 |
| --- | --- | --- |
| S1 | 新增 `vad-worker.js`（协议 + importScripts + 检测器管理） | node --check 语法过；文件确认未被 .gitignore 排除 |
| S2 | 重写 `vad.ts` 为 Worker 封装（API 不变） | `tsc` 无新增错误 |
| S3 | 构建验证（仅 pad 模块，其他成员未完成改动不阻塞） | `pnpm --dir frontend build` 通过，产物含 vad-worker.js |
| S4 | 浏览器实测（按 W6 主链路） | 见 §4 验收 |
| S5 | 文档收口 | f1-Day4前端联调.md 变更记录 + task-04 卡知会留痕 + plans/README 索引 |

## 4. 验收清单

```
standby 唤醒 → 呼叫小伴：console 无 "Identifier ... already declared"
[vad] 初始化日志显示端侧就绪，无「回落服务端 webrtcvad」warn
说话 → WS 出现 asr_start/asr_end（端侧切段生效）；TTS 播放中开口 → 本地打断生效
退出会话回 standby → KWS 唤醒词仍可再次命中；第二会话复测 VAD 正常
tsc + vite build 通过；realtime.ts diff 为空
```

## 5. 风险登记

| # | 风险 | 预案 |
| --- | --- | --- |
| R1 | Worker 内 importScripts 路径/时序异常 | 超时与 init_error 均落 `VadUnavailableError` → 既有服务端兜底接管，主链路不断 |
| R2 | vite build 对 public 纯静态 worker 无需处理，但需确认产物包含 | S3 自检项 |
| R3 | `speaking` 同步语义跨线程有延迟 | 现网零调用；留注释，若未来消费方出现需改订阅式 |
| R4 | 12MB wasm 在 worker 内二次拉取（浏览器 HTTP 缓存对 worker 同源生效） | 首次后走缓存；dev 无 gzip 时仍受 30s 超时保护 |

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：根因实证（两套非 modularize 产物顶层 `class ExitStatus` 冲突）+ 主理人拍板方案 A（Worker 隔离）+ S1–S5 步骤与验收 |
| v1.1 | 2026-11-06 | 收口：S1–S4 完成并实测通过（Worker 加载 wasm 无冲突、主链路回归正常）；顺带修 VoiceprintEnroll 裸挂载不居中 + TaskShell max-w-60vw 小视口过窄（App.tsx 包全屏居中容器、92vw） |
