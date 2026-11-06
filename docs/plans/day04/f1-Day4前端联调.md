# 计划 · F1 Day 4 前端联调（task-05，pad 主线 + 双端登录收尾）

> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-06
> 关联：docs/team_tasks/day04/task-05-Day4前端联调.md（任务唯一基线，v2.1）｜docs/team_tasks/day04/contracts.md（C4–C7）｜docs/plans/day-04-3D与身份闭环.md §3.0–§3.4
> 上游依据：PRD FR-3D-01/02、AC-15、AC-42、FR-VPR-01~03、FR-DLG-05 ｜docs/前端页面方案.md §1 token｜前序计划：day04/f1-双端登录页.md（登录线已完成，本计划不重做）
> 变更记录见文末

## 0. 认知输入与开工前事实核对（沿前序计划范式，执行期只以这些为准）

| 事实源 | 结论（与本计划直接相关） |
| --- | --- |
| day04/f1-双端登录页.md（v1.1 已完成） | 双端登录线**已交付**：MPA 双入口、console/family 两独立登录模块、token 存储/401 自动刷新全链已验收。本计划 W6 仅做回归，不重做 |
| 同上 v1.1 裁决 | **弃用 MUI，纯 React + 原生 CSS**：`@import "tailwindcss"` 使 tokens.css 的 `@theme` 输出真实 `:root` 变量，token 唯一真源不变；`@mui/@emotion` 未引入。pad 新增 UI 一律沿此口径，任务卡 v2.0 的 MUI 表述已随 v2.1 修正 |
| contracts.md C4–C7 | C4 声纹 REST 三接口（`vp_extract_unavailable` 等 503 错误码）、C5 身份级联 WS 消息（identity_ask/confirm/result，大按钮点选、30s 服务端计时）、C6 复用 tts_text 加 `degraded` 可选字段、C7 avatar-config 响应（含 `rotation_y_offset` 与 version 失效语义） |
| app/api/auth.py 实测 | E1 实际端点 `POST /api/sessions`（非 login），`{phone,password}` → `principal:{kind,id,role,name}`；kind ∈ staff\|guardian（前序计划 §3 对照表继续有效） |
| frontend/src/pad 现状 | Day 3 已交付：WS 信令、WebRTC、流式 TTS、兜底 VAD、大字幕、按住说话降级。**3D 渲染、KWS、声纹录入 UI、身份确认 UI 均未存在**，本计划从零建。执行 W0 时逐文件盘点复核，不臆断 |
| 唤醒词选型 | sherpa-onnx KWS「小伴」（plans/day-04 W5）；新依赖进 `frontend/package.json` 需在本计划登记（W0）；KWS 模型资产放 `src/pad/assets/`，入 build 预算 |
| dev server | vite 代理 `/api`→localhost:8000 已配；3D 静态走 A3 的 nginx 托管路径（dev 期用绝对 URL 直连） |

**遗留自查项（继承前序计划）**：375 物理视口未真机实测，W6 顺带补验。

## 1. 目标与边界

- **做**：pad 侧五项 P0（3D 展示接入 / 唤醒词入口 / 声纹录入引导 / 身份确认交互 / 对话收尾）+ 口型（P1）+ 双端登录回归 + 设计系统自查；全部对接 contracts.md C4–C7。
- **不做**：登录线重做（已完成）；登录后业务页（第 3/4 批）；多形象/.vrma/GLB（FR-3D-03 不排期）；引前端测试框架；任何 `app/**` 改动。
- **执行约束**：发现后端 bug 报 a0 卡；契约以 `contracts.md` 与 `app/api` 实际代码为准，差异在任务卡变更记录留痕。

## 2. 步骤拆解（W0–W6，依赖顺序即执行顺序）

### W0 · 环境准备与现状盘点（前置：无）

1. 通读 `contracts.md` C4–C7 全文 + `docs/plans/day-04-3D与身份闭环.md` §3.0–§3.4；
2. 盘点 `frontend/src/pad/**` 现状（WS/TTS/降级入口的现有模块边界，新代码落位不破坏既有结构）；
3. 安装 sherpa-onnx 前端依赖并登记版本；下载 KWS 模型资产入 `src/pad/assets/`；
4. 向 A3 索要 C7 响应样例与 AvatarSample_A 的 URL（未就绪则按样例 Mock 先行，见 R-3）。

**自检**：`pnpm --dir frontend build` 不回归（登录线成果不动）；`git status` 确认未触 `app/**`。

### W1 · 3D 展示接入（P0，对接 C7 + I2；前置：W0）

1. pad 挂载时请求 `GET /api/pad/avatar-config`（设备 JWT）→ 取 `avatar_url`/`fallback_image_url`/`rotation_y_offset`/`version`；
2. 加载 `.vrm`（three-vrm 已在 pad 依赖），与 **5s 竞速**：超时 abort → 切 `fallback_image_url` 静态头像 + WS 发 `client_diag`（I2，C2 信令追加）——**任何失败不白屏**；
3. VRM0 朝向修正用响应的 `rotation_y_offset` 字段，**不写死角度**；加载后背对镜头且 offset=0 → 报 a0 卡；
4. 缓存：`version` 变化才重新拉取模型；
5. **口型（P1 可后置到 W6 前）**：Web Audio `AnalyserNode` 对 TTS 输出算 RMS → `aa` 单通道 blendshape，静默 >300ms 归零，30fps、`pixelRatio≤2`。

**自检（卡 §3 A 组）**：≤5s 出人并播 greeting；断 avatar-config/nginx → 5s 切头像 + diag 上报；改 `personas.avatar_url` 数据刷新生效。

### W2 · 唤醒词入口（P0；前置：W0，可与 W1 并行）

1. sherpa-onnx KWS「小伴」常驻检测：命中 → 建 WS（C2 既有流程）→ WebRTC；
2. KWS 可用性探测失败（模型/初始化）→ 隐藏波形仅保留**呼叫按钮**，两入口正交；
3. 未命中原始音频不出端（红线）；命中到建连反馈 ≤1.5s（AC-42，20 次中 ≥18）。

**自检（卡 §3 B 组）**：命中/噪声不误触/按钮降级三场景。

### W3 · 声纹录入引导页（P0，对接 C4；前置：W0）

1. `MediaRecorder` 录 3 段（各 1–5s，段内实时时长，越界禁上传），进度 1/3→3/3；
2. 逐段 `POST` C4：409 段号冲突 → 提示重录该段；503（`vp_extract_unavailable` 等）→ 提示稍后手动重录，**不自动重试**；
3. 每段上传成功即删本地 Blob（AC-36）；3 段齐 → 服务端置 active → 进对话；
4. 401（设备 JWT 过期）沿既有刷新逻辑。

**自检（卡 §3 C 组）**：全链/409/503 三场景 + DevTools 确认本地无音频残留。

### W4 · 身份确认交互（P0，对接 C5；前置：W0，可与 W2/W3 并行）

1. 收 `identity_ask`（含 ask_id）→ 大字幕提问 + 单个「是我」按钮（≥48px）→ 回 `identity_confirm{ask_id, elder_id}`（本设备恒 1 位候选，无选择列表）；
2. 收 `identity_result`：source=voice/confirm → 正常模式；source=generic → 「通用模式：不读记忆、不写健康数据」横幅；
3. ask_id 不匹配的 result → 忽略；30s 超时由服务端下发，**前端不自建计时兜底**；
4. 静默复检换人 → 服务端重发 ask → 前端重走确认流，本地不做身份推断。

**自检（卡 §3 D 组）**：确认成功/超时 generic/他人插话复检三场景。

### W5 · 对话主界面收尾（P0，对接 C6；前置：W1）

1. greeting：身份确定后服务端主动下发，前端收到即播（不依赖 LLM，不请求任何 LLM 接口）；
2. `tts_text.degraded=true` → 降级横幅（C6 新增可选字段，向后兼容）；
3. 按住说话整段上传降级入口保持可用（网络断开场景）。

**自检（卡 §3 E 组）**：greeting/LLM 停机横幅/断网降级三场景。

### W6 · 联调窗口与收口（前置：W1–W5；14:30 窗口）

1. 后端 uvicorn + `docker compose up -d postgres redis`（voiceprint 容器按 A1 交付节奏，未就绪时 W3 用 Mock 先行）；
2. 真实资产全链：真实 VRM（A3）+ 真实声纹（A1 容器）+ identity 级联（A2）跑通卡 §3 A–E；
3. 双端登录回归（A/F 组快速过一遍）+ 375 物理视口补验（继承遗留项）；
4. 设计系统自查：`grep -rn "#[0-9a-fA-F]\{3,8\}" src/pad src/console src/family` 除 tokens.css 外无命中；Console 无报错；
5. 收口：任务卡 `> 状态：` 行更新 + 变更记录留痕 + 本计划状态更新。

## 3. 契约对照（实现唯一依据）

C4–C7 以 `contracts.md` 代码级定义为准（本计划不复制全文，执行前通读）；E1–E3 沿前序计划 §3 对照表（`POST /api/sessions` 实测口径）。发现契约与代码不符：以代码为准、卡上留痕、不改后端。

## 4. 禁触清单（执行期全程有效）

`app/**`、`alembic/**`、`docs/**` 基线文档正文、`src/console/**` 与 `src/family/**` 登录线已交付代码（回归只读，改动仅限发现的 bug 报 a0）、`vite.config.ts` 代理与入口既有配置（新增 KWS/资产相关配置除外）。

## 5. 风险与缓解

| # | 风险 | 缓解 |
| --- | --- | --- |
| R-1 | sherpa-onnx + KWS 模型体积入 build 预算，pad 产物超重 | W0 安装后立即 build 称重；模型放 assets 并可改 CDN/后端静态路径，超标上报裁决 |
| R-2 | VRM0 朝向坑：offset=0 且背对镜头 | 用数据位不写死；复现即报 a0 卡（附模型版本） |
| R-3 | A3 的 VRM 资产 / A1 的 C4 接口 14:30 未就绪 | 按 C7/C4 样例 Mock 先行开发，窗口内切真实资产；W1/W3 与 A3/A1 解耦 |
| R-4 | A2 identity 消息交付节奏 | C5 已代码级定稿，前端按契约先行 + 本地 Mock WS 消息自测 |
| R-5 | KWS 在目标平板浏览器（HTTPS + getUserMedia）兼容性 | W2 先跑可用性探测；不可用即按钮降级，不阻塞其余步骤 |
| R-6 | 口型 RMS 在 TTS 流式播放下的能量抖动 | 300ms 静默归零 + 平滑系数，演示级即可（PRD 无 viseme 要求，Day 5 才有） |
| R-7 | 375 物理视口遗留自查项 | W6 真机/设备模拟补验，结果记任务卡备注 |

## 6. 完成定义（DoD）

1. 任务卡 §3 验收 A–E 组全绿（pad 主线），F 组（登录）回归通过；
2. `pnpm --dir frontend build` 成功：双入口产出、three-vrm/KWS 资产不进 console/family 产物（grep 验证）、pad 体积不超预算（R-1 结论留痕）；
3. 真实资产全链 + 各降级场景（A2 断 avatar-config / 停 voiceprint 503 / 停 LLM degraded / 断网按住说话）全部验证；
4. 任务卡与本计划状态更新、差异留痕、plans/README 索引同步。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：对应 task-05 v2.1（Day 4 前端全量）。继承 f1-双端登录页 v1.1 已完成事实（登录线不重做、弃用 MUI 改纯 React + 原生 CSS 裁决、E1 实测端点）；W0–W6 七步拆解（W1–W5 对应 pad 五项 P0 + 口型 P1），契约对照指向 contracts.md，R1–R7 风险，DoD 四项 |
