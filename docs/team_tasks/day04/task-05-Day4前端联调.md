# Task 05 · Day 4 前端全量（pad 联调主线 + 双端登录）

> 状态：未开始　｜　版本：v2.0　｜　日期：2026-11-06
> 关联：docs/team_tasks/day04/README.md ｜ contracts.md（C4–C7 对接唯一依据）｜docs/plans/day-04-3D与身份闭环.md §3.0 图①–⑥ 与 §3.1–§3.4 时序
> 上游依据：PRD FR-3D-01/02、AC-15、AC-42、FR-VPR-01~03、FR-DLG-05 ｜前端页面方案.md（设计 token 冻结 2026-11-05）｜技术设计文档 frontend/pad 职责行
> 变更记录见文末

## 0. 任务定位与边界

- **本卡 = Day 4 全部前端**，主战场是 **pad 端**（与 task-01~04 后端逐条咬合的联调主线），双端登录页为次线（后端 E1/E2/E3 Day 2 已交付，可随时开工）。
- 负责人 F1（前端），独立执行；联调窗口 14:30 与后端四人对接，对接点全部走 contracts.md C4–C7 与 E1/E2/E3。
- 写作域：`frontend/src/pad/**`、`frontend/src/console/**`、`frontend/src/family/**`、`frontend/vite.config.ts`、`frontend/index.html`。**禁触**：`app/**`、`alembic/**`、`docs/**`（基线问题只报告）、`src/shared-theme/` 之外新增共享模块。

## 1. 任务拆解与优先级（P0 为 14:30 联调窗口前必须完成）

| # | 任务 | P | 对接契约 | 前置 |
| --- | --- | --- | --- | --- |
| P5-0 | MPA 入口补全：vite `rollupOptions.input` 补 console/family；pad 入口已有 | P0 | — | 无 |
| P5-1 | **3D 展示接入**：挂载时请求 C7 `avatar-config` → 加载 `.vrm` → 与 5s 竞速 → 失败切静态头像并上报 `client_diag` | P0 | C7、A2 的 I2 | task-03 交付 |
| P5-2 | **唤醒词入口**：sherpa-onnx KWS「小伴」端侧检测（主入口）+ 呼叫按钮（降级入口），命中/点击 → WS 建连 | P0 | C2（建连后消息不变） | 无 |
| P5-3 | **声纹录入引导页**：现场录 3 段（各 1–5s，本人·如情可制提示）→ 逐段 POST C4 → 409 段号冲突提示重录该段 / 503 提示稍后再试 / 3 段齐进对话 | P0 | C4（REST） | task-01 接口交付 |
| P5-4 | **身份确认交互**：收 `identity_ask`（ask_id）→ 大字幕提问 + 单个「是我」大按钮（≥48px）→ 回 `identity_confirm`；收 `identity_result` 按 source 分流（voice/confirm → 正常模式；generic → 通用模式横幅）；30s 超时由服务端下发，前端不自建计时兜底 | P0 | C5（WS） | task-02 identity 消息交付 |
| P5-5 | **对话主界面收尾**：greeting 服务端主动下发即播；`tts_text.degraded=true` → 降级横幅（沿 C6）；按住说话整段上传降级入口保持可用 | P0 | C6、C2 | Day 3 现状 |
| P5-6 | **口型**：Web Audio AnalyserNode 对 TTS 输出算 RMS 能量 → 驱动 `expressionManager`（`aa` 单通道，静默 >300ms 归零） | P1 | 无后端依赖 | P5-1 |
| P5-7 | 双端登录页（员工端 console + 家属端 family，两个独立模块、禁止互相 import）：表单校验（手机号 11 位 1 开头失焦校验、密码 ≥6 位）、401 统一 Alert（danger-soft/ink 配对、不回显 trace_id）、token 存储 + refresh 自动续期、loading 防重复提交 | P1 | E1/E2/E3（app/api/auth.py） | P5-0 |
| P5-8 | 设计系统与响应式打磨：组件全 `var()` 引用 token、零硬编码 hex、family 375px 无横滚正文 ≥16px 触控 ≥48px、大字幕沿用 Day 3 口径 | P1 | — | 全部 |

## 2. 技术实现要点

1. **3D（对齐 docs/plans day-04 §1.4）**：`avatar-config` 响应里的 `avatar_url`（绝对 URL）与 `fallback_image_url` 直接用；VRM0 模型朝向修正用响应的 `rotation_y_offset` 字段（若为 0 且加载后背对镜头，报 a0 卡，前端不写死角度）；加载竞速 5s（PRD AC-15）超时即 abort 渲染管线切静态头像，**任何失败不白屏**；口型驱动每帧写一次 blendshape，30fps、`pixelRatio≤2`（预算红线，压不了再降分辨率）。
2. **唤醒词（对齐 W5）**：sherpa-onnx KWS 模型放 `frontend/src/pad/assets/`（体积入 build 预算）；KWS 可用性探测失败 → 隐藏波形提示仅保留呼叫按钮（两入口正交）；命中后先建 WS 再建 WebRTC，未命中原始音频**不出端**（PRD 红线）。
3. **声纹录入（对齐图⑥）**：`MediaRecorder` 采集，段内实时显示时长，<1s 或 >5s 禁止上传；上传成功显示第 N/3 段进度；**上传成功即删本地 Blob**（AC-36，前端不落原始音频副本）；503 时不重试自动化，提示用户稍后手动重录。
4. **身份级联（对齐图②与 C5 五裁决）**：`identity_confirm` 只回传 `ask_id` + 候选 elder_id（本设备恒 1 位候选，按钮无选择列表）；收到与本会话 `ask_id` 不匹配的 result → 忽略；generic 横幅文案「通用模式：不读记忆、不写健康数据」；复检换人时按服务端下发的 `identity_ask` 重新走确认流，前端不做本地推断。
5. **登录与样式**：console/family 纯 React + 原生 CSS，组件全 `var()` 引用 token（不引组件库，前端页面方案 v1.0 / 技术文档 v1.6 裁决）；登录态 localStorage + 401 → refresh 重放原请求 → 再 401 回登录页。

## 3. 验收标准（全部可勾选）

**A · pad 3D**
```
打开 pad → ≤5s 出现 3D 数字人并播 greeting；停掉 avatar-config 或断 nginx 静态路由 → 5s 后切静态头像 + WS 上报 client_diag，页面不白屏
换 personas.avatar_url 数据（不动代码）→ pad 刷新后加载新形象（version 缓存失效生效）
```

**B · 唤醒词**
```
说「小伴」→ ≤1.5s 出现建连反馈（20 次中 ≥18 次）；环境噪声说话不误建连；KWS 模拟不可用 → 呼叫按钮可用，两入口都能进对话
```

**C · 声纹录入**
```
3 段流程走通：进度 1/3→3/3 → active 进对话；重传同段 409 → 提示重录该段；停 voiceprint 容器 → 503 提示，本地无音频残留
```

**D · 身份级联**
```
首轮说话 → 大按钮确认 → 正常模式；30s 不点 → 服务端下发 generic，横幅出现；室内他人插话 → 静默复检走确认流；确认后对话读得到个性化记忆
```

**E · 对话闭环**
```
greeting 开场不依赖 LLM；LLM 容器停机 → 模板话术 + degraded 横幅；网络断开 → 按住说话整段上传降级可用
```

**F · 双端登录**（原 v1.0 A/B/C 组保留执行）
```
console：正确登录进占位首页显角色；错误密码 danger Alert；校验/防连点/刷新保活全过
family：375×667 无横滚、≥16px、触控 ≥48px、390/768 不破版
工程：build 产出双入口；grep 验证 three-vrm 不进 console/family 产物；组件零硬编码 hex；Console 无报错
```

## 4. 测试验证要求

1. 手动矩阵：Chrome 最新版；pad 用 1920×1080（模拟平板视口）+ 麦克风权限；console 1280×800；family 375×667 / 390×844。每格过 §3 对应组，结果记本卡备注。
2. 联调依赖：后端 uvicorn 本地起 + `docker compose up -d postgres redis`；A3 的 nginx/VRM 资产、A2 的 identity 消息、A1 的 C4 接口在 14:30 窗口联调，联调前置失败时按契约先行开发（Mock 响应）。
3. `pnpm --dir frontend build` 为准；不新增前端测试框架（引框架走 spikes 流程，本卡不做）。
4. 契约冲突时以 `contracts.md` 与 `app/api` 实际代码为准，卡上变更记录留痕。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：仅双端登录页（范围理解收窄，见 v2.0 修正） |
| v2.0 | 2026-11-06 | **重写为 Day 4 前端全量**：主战场改 pad 侧 3D/唤醒词/声纹录入/身份确认/对话收尾/口型，逐项对齐 contracts.md C4–C7 与 plans §3.0–§3.4；双端登录降为 P1 次线保留全部验收；任务拆解 P5-0–P5-8，联调窗口对齐 14:30 |
| v2.1 | 2026-11-06 | 口径对齐「移除 MUI」裁决（PRD v2.7 / 技术文档 v1.6 / 前端页面方案 v1.0）：写作域删 `shared-theme/mui-theme.ts`、P5-8 与要点 5 改纯 React + 原生 CSS（`var()` 引用 token）。注：P5-0（vite 双入口）与 P5-7（双端登录链路）已于 task-05 v1.0 执行中提前交付并验收通过 |
