# Task 05 · 双端登录页（家属端 + 员工端，前端独立卡）

> 状态：已完成　｜　版本：v1.1　｜　日期：2026-11-06
> 关联：docs/team_tasks/day04/README.md（本卡为前端独立卡，不占四人后端分工）｜docs/前端页面方案.md（设计规范唯一依据，配色已冻结 2026-11-05）
> 上游依据：PRD §3.1（家属/员工账号密码登录）、FR-UI-02（触控 ≥48px）｜技术设计文档 app/core（JWT 三体系：人/家属 Access 2h + Refresh 7d）｜前端页面方案 §1 token / §3.2-3.3 页面清单 / §5 工程落地
> 变更记录见文末
>
> 备注（手动验证矩阵结果，2026-11-06 执行）：
> - A 组员工端：正确登录→「登录成功 · 角色：护工/医生/管理员」✓；错误密码→固定文案 Alert（danger 配对色）✓；非法手机号/短密码失焦提示、按钮禁用✓；刷新持久化（fetchCurrent 恢复）✓；退出清 localStorage✓
> - B 组家属端：登录/错误/加载三态与 A 组一致✓；无横向滚动（scrollWidth=innerWidth）✓；按钮/输入 48px✓。**375×667 物理视口未实测**（浏览器工具不支持视口仿真），以流式布局 + max-width 480 + 100% 宽度 CSS 保证，遗留真机自查项
> - C 组：build 全绿产出三入口✓；dist 无 three-vrm 泄漏✓；源码无硬编码 hex✓；Console 无报错✓
> - 联调：E1/E2/E3 curl 实测 + UI 全链（登录/错误/退出/双端共存隔离）✓；refresh 自动续期为 curl 级验证（access 过期在 UI 上未自然触发）；后端不可用→「网络异常」Alert 文案路径已实现
> - 联调测试账号（教学数据）：护工 13800000001 / 医生 13600000001 / 管理员 13700000001 / 家属 13900000001，密码均为 test123456

## 0. 任务定位与边界

- **交付两个相互独立的登录模块**：`frontend/src/console/`（员工端，React + MUI，桌面浏览器）与 `frontend/src/family/`（家属端，React + MUI，375px 移动优先）。两模块**各自独立实现、允许同构重复，禁止互相 import 或 import `src/pad/**`**（pad 的 three-vrm 依赖不得进这两个入口产物，PRD §2.1）。
- 本卡只做**登录页 + 登录后占位首页**（角色分流提示 + 退出按钮）。登录后的业务页（工单看板、日报等）属第 3/4 批，不在本卡。
- 后端接口已交付（Day 2），本卡**纯前端、与后端零耦合**：对接 `app/api/auth.py`（阅读该文件取实际字段，本卡不写后端代码，后端 bug 报 a0 卡处理）。

## 1. 任务拆解清单（按优先级顺序执行）

| # | 任务 | P | 前置 | 产出 |
| --- | --- | --- | --- | --- |
| F0 | MPA 入口与主题装配：`vite.config.ts` 的 `rollupOptions.input` 补 `console`、`family` 两入口；`frontend/index.html` 补入口声明；新建 `src/shared-theme/mui-theme.ts`（console/family 共用主题对象，见 §3 要点 1） | P0 | 无 | `pnpm --dir frontend dev` 可访问 `/console.html`、`/family.html` |
| F1 | **员工端登录页** `console/pages/LoginPage.tsx` + `console/App.tsx` 占位首页（显示角色与欢迎语 + 退出按钮） | P0 | F0 | 见 §4 验收 A 组 |
| F2 | **家属端登录页** `family/pages/LoginPage.tsx` + `family/App.tsx` 占位首页（同上，移动优先布局） | P0 | F0 | 见 §4 验收 B 组 |
| F3 | 登录态与刷新：两模块各自的 `api.ts`（fetch 封装：登录 / 401 统一处理 / refresh 自动续期 / current 拉取角色），token 存 `localStorage` | P1 | F1 | 刷新页面不掉登录态；access 过期自动 refresh 后重放原请求 |
| F4 | 表单校验与状态反馈打磨 + 响应式自查（§4 全部验收项过一遍） | P1 | F1、F2 | §5 测试矩阵全绿 |

## 2. 禁触清单

`src/pad/**`（只读参考 OK，禁止 import）、`app/**`、`alembic/**`、`docs/**`（发现基线问题报告，不自行改）、`src/shared-theme/` 之外的任何新增共享模块。

## 3. 技术实现要点

1. **MUI 主题（前端页面方案 §5 硬约束）**：`palette.primary` 必须显式给全四档（`main: var(--color-brand)` / `dark: brand-strong` / `light: brand-soft` / `contrastText: ink-inverse`，MUI 颜色运算解析不了裸 `var()`）；`background.default = canvas`、`paper = surface`、`shape.borderRadius = 16`；`text.primary = ink`、`secondary = ink-muted`。不引深色模式、不用信号色做按钮底。
2. **设计 token 唯一真源**：两入口首行 `@import "../theme/tokens.css"`（25 个 CSS 变量已冻结：`brand #6B5B7B` 藤紫等），**组件内禁止硬编码十六进制色值**，一律用 token 变量或 MUI palette。
3. **登录接口**（以 `app/api/auth.py` 实际签名为准）：
   - `POST /api/sessions/login`（E1，账号密码）→ 200 `{access_token, refresh_token, ...角色字段}`；401 统一错误体 `{detail, code, trace_id}`;
   - `POST /api/sessions/refresh`（E2）、`GET /api/sessions/current`（E3，带 Bearer）；
   - **角色分流**：登录成功后按 E1/E3 返回的角色跳转占位首页并显示角色名（护工/医生/管理员/家属），本卡不做权限矩阵前端拦截（后端已做）。
4. **表单验证规则**（两端一致）：手机号 11 位且 `1` 开头才允许提交（格式错误即时提示，失焦触发）；密码非空且 ≥6 位；提交中按钮 loading + disabled（防重复提交）；**错误提示统一形态**：`Alert severity="error"`，底色 `danger-soft #FEE2E2`、字 `danger-ink #B91C1C`，文案用用户语言（「手机号或密码不正确」），**禁止回显 `trace_id`/内部细节**（401 文案固定，网络错误文案「网络异常，请稍后重试」，5xx 同）。
5. **响应式与无障碍**：
   - family：375px 宽**无横向滚动**、正文字号 ≥16px、触控目标 ≥48×48px；布局移动优先（375 单列），390/768 不破版；
   - console：桌面 1280+ 为主，居中卡片式登录卡（`surface` 白卡 + `line` 1px 描边 + 1rem 圆角），768 以下不破版即可；
   - 输入框 `label` 常驻（不用 placeholder 当 label）、密码可见性切换、错误态 `aria-invalid` + 提示关联 `id`。
6. **加载与反馈状态**：登录请求 pending → 按钮 `CircularProgress`；成功 → 占位首页；401 → Alert；网络失败 → Alert（重试即再次提交）。占位首页显示「登录成功 · 角色：X」+ 退出按钮（清 localStorage 回登录页）。

## 4. 验收标准

**A 组 · 员工端（console.html）**
```
正确账号登录 → 占位首页显示角色名；错误密码 → danger 配对 Alert「手机号或密码不正确」
手机号格式错/密码 <6 位 → 失焦即提示，提交按钮不触发请求
连点提交 → 只发一次请求（loading 期间 disabled）
刷新页面保持登录态；手动清 access_token → 自动 refresh 恢复；refresh 也失效 → 回登录页
```

**B 组 · 家属端（family.html）**
```
375×667 视口：无横向滚动；正文字号 ≥16px；按钮/输入框触控 ≥48px
登录/错误/加载三态表现与 A 组一致
390×844、768 宽不破版
```

**C 组 · 工程与设计系统（两端共同）**
```
pnpm --dir frontend build 成功，产出 console 与 family 两个入口
构建产物中不含 three-vrm/three 相关 chunk（dist 内 grep 验证）
组件源码无硬编码 hex 色值（token 变量/MUI palette 之外）
对比度抽查：主按钮白字 ≥4.5:1（冻结值 ≈6.2:1）；Alert 文字在 soft 底上可读
Chrome DevTools Console 无报错与警告
```

## 5. 测试验证要求

1. **手动验证矩阵**（结果记录进本卡状态行下方的备注）：Chrome 最新版；视口 1280×800（console）、375×667 / 390×844（family）；每个矩阵格过 §4 对应组。
2. **接口联调**：本地 `uv run uvicorn app.main:app --port 8000` + `docker compose up -d postgres redis`，用 seed 数据的真实账号走通登录→刷新→退出全链；后端不可用时页面表现（网络错误 Alert）也要验。
3. **构建检查命令**：`pnpm --dir frontend build` 成功 + `grep -rl "three-vrm" frontend/dist | grep -v pad` 无输出。
4. 项目当前无前端单测框架，**不新增**；以上矩阵即验收依据（引测试框架属新依赖，走 AGENTS §1 spikes 流程，本卡不做）。

## 6. 前置依赖与对接

- 后端 E1/E2/E3 已交付（Day 2），无跨人依赖；与 task-01～04 无耦合，可立即开工。
- 若联调中发现 E1 返回结构与本卡假设不符：**以 `app/api/auth.py` 为准**，卡上变更记录留痕，不改后端。
- 与后端联调窗口（14:30）共用：登录链路联调问题在窗口内找 A2（auth 域属其邻近知识）。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：MPA 双入口 + MUI 主题装配 + 员工端/家属端两个独立登录模块 + 登录态/刷新/校验/状态反馈 + 375px 响应式；验收与测试矩阵以 app/api/auth.py 实际契约为准 |
| v1.1 | 2026-11-06 | 执行落定四处偏差，均留痕：①E1 端点实为 `POST /api/sessions`（卡 §3.3 原写 `/api/sessions/login` 为笔误，依卡 §6「以 auth.py 为准」执行）；②**主理人裁决弃用 MUI**：MUI v9 颜色运算（augmentColor/alpha）不支持 CSS 变量、`@theme` 变量在非 tailwind 入口也不会被处理成真实 CSS，双重白屏根因，改为纯 React + 原生 CSS（`src/shared-theme/` 撤销，@mui/@emotion 依赖不装；技术文档 §1 与前端页面方案 §5 的选型口径需主理人走基线修订，本文档未动基线正文）；③经主理人授权重建丢失的 `src/pad/tasks.ts`（解除全项目 tsc 阻塞，登记 a0 卡）；④token 变量生效方式：两入口 index.css 首行 `@import "tailwindcss"` + `@import "../theme/tokens.css"`，使 @theme static 输出为真实 :root 变量（组件内仍全 var() 引用，token 唯一真源不变） |
