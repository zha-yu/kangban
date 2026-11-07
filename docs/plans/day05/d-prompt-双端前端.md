# Day 5 dev-D · AI 编码助手执行提示词

> 状态：草稿　｜　版本：v1.0　｜　日期：2026-11-07
> 关联：docs/plans/day05/d-双端前端推进计划.md（唯一口径）；docs/plans/day05/d-DAG.md（节点依赖唯一事实源）；docs/team_tasks/day05/dev-d-前端console与family.md（任务卡）
> 上游依据：docs/接口契约_身份与权限.md v1.8（已冻结）｜docs/接口契约_家属端数据.md v1.0（草案，待冻结）｜docs/前端页面方案.md v1.0｜PRD FR-ADM-01/06、§3.1、§6.1 Day 5｜技术设计文档 §8.3 Day 5
> 变更记录见文末

以下正文可整体复制给能够读取和修改当前仓库的 AI 编码助手。执行时以仓库内文件为准；本提示词不构成修改任何契约或基线文档的授权。

---

## 任务目标

你是康伴（SilverMate）仓库的 **dev-D 前端编码执行者**，负责 Day 5 的 console / family 双端任务。请严格按 `docs/plans/day05/d-DAG.md` 的拓扑顺序与 `docs/plans/day05/d-双端前端推进计划.md` 的 W0–W5 定义，完成三项任务：

- **D-1 admin 管理页**：console admin 角色接真实端点（建员工 / 建家属 / 绑定长者 / 设备建档四流程），当前占位置灰 → 可用，是 **M5.4 Iter2 演示的硬依赖，最高优先级，无前置可立即开工**。
- **D-2 family Mock→真实切换层**：为 HomePage / ReportsPage / NotificationsPage 建数据源抽象（Mock / 真实双轨），真实分支**待冻结**，等待 dev-B B-3 校对稿（N4）后补口径。
- **D-3 console 五 Mock 页字段对齐**：依据 dev-A A-4 契约草案核对工单 / 告警 / 健康 / 药单 / 复核页字段，差异双向确认后修 Mock 结构。

外部输入未就绪时（A-4 草案、B-3 校对稿、契约冻结评审），只完成不依赖口径的骨架与盘点节点并如实报告阻塞；**不得越权改文件、不得凭经验自造字段、不得用 Mock 冒充真实端点验收、不得伪造联调或走查结果**。

## 一、开工前必读与事实优先级

按顺序完整阅读：

1. `AGENTS.md`：事实来源铁律、§2 分层硬约束、§4 禁区 12 条、§5 高风险文件、§8 编码与协作约定、§10 计划落盘与看板规则。
2. `docs/plans/day05/d-双端前端推进计划.md`：本任务唯一口径（§3.0 写作用域、§4 对接矩阵、§6 自检总表、§7 风险）。
3. `docs/plans/day05/d-DAG.md`：唯一节点依赖事实源（含写锁 L1–L8、门禁 G-A–G-G、回退 F1–F8）。
4. `docs/team_tasks/day05/dev-d-前端console与family.md`：任务卡（D-1/D-2/D-3 验收原文、N3/N4/M5.4 联调要求）。
5. `docs/team_tasks/day05/dag-项目工作流总览与联调节点.md`：N3 / N4 对接即检查单、M5.4 里程碑。
6. `docs/接口契约_身份与权限.md`（已冻结 v1.8）：§4 E12/E15/E13/E16/E17/E18/E19 请求响应形状、§6 权限矩阵、§9 裁决 A-01。
7. `docs/接口契约_家属端数据.md`（草案 v1.0，**待冻结**）：§2 字段、§4 五端点（F0/F1/F2/N1/N4）、§6 自裁 S-11。
8. `docs/前端页面方案.md`：§1 token、§3.2/§3.3 页面口径、§4 组件规范、§5「console/family 样式注意点 + 不引入组件库 + 不做什么」。
9. `docs/康伴3D数字人_PRD.md`：FR-ADM-01/06、§3.1 权限矩阵、§6.1 Day 5 完成定义；`docs/康伴3D数字人_技术设计文档.md` §3 表定义（D-3 字段出处）。
10. 代码现状：`frontend/src/console/App.tsx`、`layout/AppLayout.tsx`、`api.ts`、`elders-api.ts`、五个 `*-api.ts` 及对应 `pages/*.tsx`、`index.css`；`frontend/src/family/api.ts`、`pages/*.tsx`；`frontend/src/theme/tokens.css`、`vite.config.ts`、`package.json`。

**事实优先级**：PRD 定验收，技术设计文档定实现，冻结契约定对外口径；大纲不得作为字段、阈值、接口或页面口径依据。计划与代码现状不一致时，先记录差异，不得自行改写基线文档或契约正文。

## 二、已核实的输入条件（截至 2026-11-07，开工时重新核实）

- 后端已就绪：`POST /api/staff`(E12)、`POST /api/guardians`(E15)、`POST /api/elders/{id}/guardians`(E13)、`GET /api/rooms`(E18)、`POST /api/devices`(E16)、`GET /api/devices`(E17)、`POST /api/elders/{id}/bed`(E19)、`POST /api/elders`(E5)、设备绑定(E9)。**D-1 无后端阻塞**。
- `/api/family/*` 5 端点**后端未实现**（Day 8 批次）；`work_orders*`、`notifications`、`health_records`、`medication_*`、`daily_reports` 无 ORM 无迁移。**D-2/D-3 本日一律保持 Mock**。
- `console/App.tsx:116-120` admin/guardian 落 `PlaceholderHome`；`layout/AppLayout.tsx` `NAV_BY_ROLE.admin` 四项全部 `enabled:false`。
- `console/elders-api.ts` 已封装 `fetchRooms/createDevice/fetchDevices/bindDevice/createElder/assignBed`（含一次性 `device_secret` 返回），**必须复用，不改其签名**。
- `console/api.ts` 提供 `apiGet/apiGetWithStatus/apiPost`；`ApiErrorKind` 仅 `"invalid" | "network"`，**无 `forbidden`**（family 侧有）。处置见决策 K2。
- `family/api.ts:236` `export const USE_MOCK = true` 恒真；类型与 5 个业务函数已按契约草案落地，真实分支已写好但后端未实现。
- `console/index.css` 存在既有缺陷：引用了 `tokens.css` 未定义的 `--color-border/--radius-lg/--radius-md/--color-surface-soft/--color-danger/--color-primary`；`MedicationEditPage/ReviewQueuePage` 使用未定义类 `.rv-mask/.rv-dialog/.btn-ghost`。**只登记不修**（不属本任务），且新增样式不得沿用。
- `console/elders-api.ts` 的 `createDevice` 非 2xx 时抛数字 `res.status` 而非 `ApiError`——在 `AdminDevicesPage` 内做类型守卫兜底，登记差异，**不改该文件**。
- 前端**无测试框架、无 lint 脚本**（`package.json` scripts 仅 dev/build/preview）。自检以构建门禁 + 静态 grep + 浏览器走查 + 录屏为准；**不得虚构测试命令，不得引入测试框架**。
- `docs/reviews/` 为空目录（评审稿实体在工作区外），不要引用其中任何内容作为依据。

## 三、允许与禁止的文件范围

只允许新建：

- `frontend/src/console/admin-api.ts`（E12/E15/E13 封装）
- `frontend/src/console/pages/AdminUsersPage.tsx`、`AdminDevicesPage.tsx`
- `frontend/src/family/data-source.ts`（或等价的数据源拆分模块）
- `docs/plans/day05/d-启动门槛核对.md`、`d-字段差异清单.md`、`d-家属端契约差异表.md`、`d-家属端切换层说明.md`、`d-汇报清单.md`、`d-admin页走查记录.md`

只允许修改：

- `frontend/src/console/App.tsx`、`layout/AppLayout.tsx`、`index.css`（**写锁 L1，仅 admin 分流与新增样式**；D-3 若需改 `index.css` 类名，并入同一次改动）
- `frontend/src/family/api.ts`、`pages/{HomePage,ReportsPage,NotificationsPage}.tsx`（写锁 L2）
- 五个 `console/{workorders,alerts,health,medication,review}-api.ts` 及必要 `pages/*.tsx`（**写锁 L3，仅 D-3 且须双向确认后**）
- `docs/team_tasks/day05/dev-d-前端console与family.md`（只允许追加变更记录、更新 `> 状态：` 行）

**禁止修改/触碰**：`app/**`、`alembic/**`、`tests/**`、`mcp-servers/**`、`deploy/**`、`scripts/**`；`frontend/src/pad/**`（dev-C 独占）；`frontend/src/theme/tokens.css`、`vite.config.ts`、`package.json`、`pnpm-lock.yaml`、`frontend/*.html`（L4，本轮无人可改）；`frontend/src/console/api.ts`（K2-B 默认零改动）；`frontend/src/console/elders-api.ts`（只读复用）；`docs/接口契约_*.md`、三份基线文档正文、`docs/ER_数据模型设计.md`、`docs/前端页面方案.md`；`docs/team_tasks/day05/PROGRESS.md`（自动生成，禁手改）。

不要创建 Git commit、不要 push，除非用户另行明确授权。不要新增组件库、路由库、状态管理库、测试框架、CSS 框架或与任务无关的重构。

## 四、不可违反的输入与安全约束

1. **身份**：`elder_id` 一律来自服务端返回（E3 `scope_elder_ids`、F0、各端点响应），前端**零处**硬编码长者 ID 作为关系判定依据；裁决 A-01：家属读任何长者档案一律 403，前端不开档案接口、不绕过。
2. **组件库**：console/family 纯 React + 原生 CSS，**不引 MUI 等任何组件库**（MUI v9 颜色运算不支持 CSS 变量，实测白屏——前端页面方案 §5 裁决）。
3. **样式**：一律 `var()` 引用 `frontend/src/theme/tokens.css` **已定义**的变量；开工先 `cat frontend/src/theme/tokens.css` 建立变量白名单，新增样式只从白名单取值；**零硬编码十六进制色值**；不引用既有未定义变量；`index.css` 保持首行 `@import "tailwindcss"` 再 `@import "../theme/tokens.css"`。
4. **独立同构**：console 与 family 禁止互相 import，也不 import `src/pad/**`；localStorage key 沿用 `kb_console_` / `kb_family_` 前缀。
5. **复用优先**：E18/E16/E17/E19/E5/E9 一律复用 `elders-api.ts` 现有函数，**不得出现第二套封装**（AGENTS 禁区 8）。
6. **一次性密钥**：`device_secret` 仅展示一次、展示后不可再取；不写 localStorage、不写日志、不进录屏外的任何持久化。
7. **K1 开关口径**：family 双轨默认按契约 S-11——`api.ts` 内**唯一** `USE_MOCK` 常量，**不进环境变量体系**；若被要求改 `import.meta.env`/`.env` 开关，先停下：S-11 是契约自裁行，须契约负责人修订 + 评审留痕后才能改，不得自行改契约。
8. **冻结纪律**：《接口契约_家属端数据》为草案 v1.0，D-2 真实分支全部标注「待冻结」；A-4 契约草案（console 五页）落点未定前 D-3 不得定稿；**差异只登记不私改，契约正文与基线文档零改动**。
9. **双向确认**：D-3 修改 Mock 结构必须在 dev-A 确认差异清单之后；未确认项保持原样并标 ⏳。
10. **业务逻辑零改动**：D-3 只改类型与 Mock 数据形状；工单状态机、AC-23 通知状态机、健康阈值分级、药单权限（医生写/护工只读）、复核版本链**保持现状**。
11. **403 处置（K2）**：推荐方案 B——在 `admin-api.ts` 内解析 403 抛页面级文案，`console/api.ts` 零改动；若选 A（补 `forbidden` kind）必须先报告并回归登录页全部错误分支，不得两种并存。
12. **范围边界**：admin 只做四流程（E12/E15/E13/E18/E16/E17）；「教学数据重置 / 人设配置 / 审计日志 / E12 PATCH 改角色」不在本任务，菜单保持置灰，被要求时拒绝并指向后续批次。
13. **文案与隐私**：错误文案用用户语言，不回显 `trace_id` 与内部细节；不写真实长者数据进代码、Mock、日志、录屏或提交信息。
14. **看板纪律**：不手改 `PROGRESS.md`；进度只通过任务卡 `> 状态：` 行（取值仅 `未开始/进行中/已完成/已取消/待确认`）。
15. 全量 type hint；不散读环境变量；不提交媒体大文件（录屏只登记外部路径）。

## 五、DAG 执行规则

1. 先解析 `d-DAG.md` 的节点与边，按拓扑序执行；一个节点的全部硬依赖为 `done` 才能开始。节点状态只用 `done / partial / blocked / failed / pending / in_progress`。
2. 每完成一个节点立即跑该节点门禁（§7 门禁表），全绿才进下一节点；被 X0/X1/X2/X3 阻塞的节点标 `blocked`，**继续做不受阻的工作**（D-1 关键路径优先）。
3. 不得通过改 DAG、删依赖、放宽门禁、用 Mock 冒充真实端点、修改他人文件来消除阻塞。

### G0 · 启动门槛（W0）

- 向主理人确认三件事并写入 `d-启动门槛核对.md`：① A-4 契约草案落点（新建独立契约文件 vs 并入现有文件，主计划 §6 #6 未闭环）；② 《接口契约_家属端数据》冻结评审时点；③ B-3 校对稿交付时点。无答复就标 ⏳ 并写明阻塞哪一步。
- 跑基线构建 `pnpm --dir frontend build` 并记录原始输出（作为不回归对照）。
- 建三份空登记表（差异清单 / 切换层说明 / 汇报清单）。
- **此阶段代码改动必须为 0**。

### N1 · admin-api 封装（W1-a）

- 新建 `console/admin-api.ts`：`createStaff`（E12 `{name, role, phone, password, elder_ids?}`，role ∈ admin|care_worker|doctor，phone 11 位，password 8–128 位）、`createGuardian`（E15，先建号拿 `guardian_id`）、`bindGuardian`（E13 `POST /api/elders/{elder_id}/guardians`）。
- 403/409/404/422 分支处理走 K2-B：本模块内解析状态码抛页面级错误文案；响应形状严格对照身份契约 §4。

### N2 · AdminUsersPage（W1-b）

- 员工建档（含 `elder_ids` 分配，补 G2）、家属建号、家属绑定长者（超 2 位 409 提示）三个表单区块；表单校验在提交前后端双层，失败文案可读、不含内部细节。
- 长者候选数据来自现有真实接口（E7 / E18），不臆造。

### N3 · AdminDevicesPage（W1-b）

- 房间床位列表（E18，复用 `fetchRooms`）→ 设备建档（E16，复用 `createDevice`）→ 设备列表（E17，复用 `fetchDevices`）→ **一次性 `device_secret` 展示一次**（含「已关闭，无法再次查看」态）。
- 对 `createDevice` 抛数字 status 的既有行为做类型守卫兜底；不改 `elders-api.ts`。

### N4 · 装配（W1-c，写锁 L1）

- `AppLayout.tsx`：`NAV_BY_ROLE.admin` 的「用户与角色」「模拟设备」两项 `enabled:true` 并给 key；其余两项保持置灰。
- `App.tsx`：新增 `AdminView` 分支（`"users" | "devices"`），admin 登录渲染对应页面；care_worker / doctor 分支零改动；guardian 仍占位提示。
- `index.css`：仅追加 admin 新增样式；只引用 tokens 白名单变量；危险操作不铺红底（`text-danger-ink` + 描边 + 二次确认）；表格行高 ≥40px、正文 14–16px、触控 ≥48×48px。

### N5 · 走查与越权反向验证（W1-d/e）

- 后端可用时（`docker compose up -d postgres redis` + `uv run uvicorn app.main:app --port 8000`）：admin 登录逐条走通四流程并录屏；用 care_worker / doctor / guardian 三种凭证访问 E12/E15/E13 确认 403、前端无白屏。
- 后端不可用时：N5 标 `blocked`，**不得用 Mock 冒充真实端点验收**；可先完成 N6/N9 旁支。
- 产出 `d-admin页走查记录.md`（四流程 + ≥3 条越权行）与录屏（外存，只登记路径）。

### N6 · family 数据源抽象骨架（W2-a，不依赖 B-3）

- 把 `family/api.ts` 中业务类型与 Mock 数据迁出为 `data-source.ts`（或等价模块）；`api.ts` 只留：认证域、**唯一 `USE_MOCK`**、类型导出、5 个业务函数签名。
- 页面 import 路径与调用签名**不变**；三业务页渲染与迁移前完全一致。

### N7 · 双轨切换层（W2-b/c）

- 5 个函数各自 `if (USE_MOCK) → Mock / else → 真实 fetch`，路径严格对齐契约 §4：`GET /api/family/elders`、`GET /api/family/elders/{elder_id}/reports`、`GET /api/family/reports/{report_id}`、`GET /api/family/notifications`、`POST /api/family/notifications/{id}/read`。
- 响应形状按契约：`{items,total,page,page_size}` 外壳；`medication_stats` 四键必全；`audio_url` P0 恒 null、不渲染播放器；`payload` 未知键忽略不报错；N4 已读幂等、只写 `read_at` 不动 `status`。
- 长者切换器数据源只来自 F0，范围与 E3 `scope_elder_ids` 核对；403 走 `SCOPE_FORBIDDEN_TEXT`。
- 真实分支全部标注「待冻结」。

### N8 · 校对稿对齐与说明（W2-d/e，阻塞于 X2）

- B-3 校对稿到手后逐端点比对，产出 `d-家属端契约差异表.md`（字段/判定/自裁三行分类不混淆）；差异只登记不私改。
- 写 `d-家属端切换层说明.md`：开关位置、默认态、Day8 切换步骤、Mock 删除条件（S-11：联调通过后删 mock 分支并同提交登记）、待冻结清单。
- 契约冻结评审（X3）未开前不得删「待冻结」标注。

### N9 · 五 Mock 页字段盘点（W3-a，不依赖 A-4）

- 逐页导出五个 Mock 层全部类型与字段名，形成 `d-字段差异清单.md` 左半表：`workorders-api.ts`（work_orders / work_order_transitions）、`alerts-api.ts`（notifications）、`health-api.ts`（health_records）、`medication-api.ts`（medication_plans）、`review-api.ts`（health_records HITL / 版本链）。
- 同时登记两处既有差异：C4 `source` 枚举（TD/ER 四值 vs PRD 旧文 `staff_report/resident`）、`MedicationEditPage`/`ReviewQueuePage` 硬编码演示身份——**只登记不裁决不改**。

### N10 · 草案对齐与双向确认（W3-b/c，阻塞于 X0+X1）

- A-4 草案到手**当天**完成：逐字段标注 TD §3 表名 + 字段出处；差异三列结论（一致 / 前端多 / 前端缺）；口径以 TD §3 为准。
- 差异清单回传 dev-A（N3 联调），**取得双向确认后**才允许进 N11；未确认项标 ⏳ 不得写入代码。

### N11 · 字段修正与渲染回归（W3-d/e，写锁 L3）

- 只改类型定义与 Mock 数据形状，业务判定逻辑零改动；不新增第二套字段口径。
- 五页逐页目视回归（列表/筛选/详情/操作弹窗/版本链）并记录；若需改 `index.css` 类名，并入 N4 的同一次 L1 写锁改动。

### N12 · 联调与构建门禁（W4）

- `pnpm --dir frontend build` 全过；三项静态零（见 §六）；N3/N4「对接即检查单」逐条打勾，未过项登记责任人与时点。
- 填 `d-汇报清单.md`：对照主计划 §S7 自检项（A2「每个字段行能回答出处是 TD 哪一行」、A3「三页面走通 E12/E18 真实端点、越权被拒」、A1「三行分类未被混淆」）与任务卡完成定义。

### N13 · M5.4 预演与收口（W5）

- admin 页走查纳入演示预演，结果回填任务卡变更记录；`> 状态：` 行更新；不手改 PROGRESS。
- 计划件与索引状态更新与代码改动同一次提交（若被授权提交时）。

## 六、验收标准与命令

### 6.1 必须有的证据

- **构建**：`pnpm --dir frontend build`（`tsc --noEmit && vite build`）三入口全过。
- **静态三项 = 0**：新增/改动文件硬编码 hex、未定义 token 变量、three-vrm 进入 console/family 产物。
- **走查**：admin 四流程 + 3 种角色越权 403；family `USE_MOCK` 切 true/false 三业务页渲染一致；375px 无横向滚动。
- **录屏**：外存，路径登记，不入库。

### 6.2 命令

```bash
# 构建门禁（三入口）
pnpm --dir frontend build

# 硬编码 hex 自检（只查新增/改动文件，应为 0 处）
grep -rnE "#[0-9a-fA-F]{3,8}\b" <本次新增或改动的 .tsx/.css 文件列表>

# token 变量白名单比对（先取白名单，再取实际引用，差集应为空）
grep -oE "^\s*--[a-zA-Z-]+" frontend/src/theme/tokens.css
grep -rhoE "var\(--[a-zA-Z-]+" <本次新增或改动的文件列表> | sort -u

# three-vrm 不进 console/family 产物（应无输出或 "clean"）
grep -rl "three-vrm\|@pixiv" frontend/dist/ || echo "clean"

# 范围与杂项
git status --short
git diff --check
```

- 走查用 `pnpm --dir frontend dev`（127.0.0.1:5173，`/api` 代理到 8000）；admin 真实端点走查需本地后端在跑。
- **前端无测试框架：不得虚构 `vitest/jest` 命令，不得为补测试引入任何依赖**；若某验证因环境不可用未执行，如实写「未执行 + 原因」，不得写成通过。

## 七、停止条件

出现以下任一情况，停止受影响节点并报告，但仍可继续 DAG 中不受阻的工作：

- 需要 A-4 契约草案（X1）或其落点裁定（X0）才能继续，而二者未到位却被要求定稿 D-3 字段。
- 需要 B-3 校对稿（X2）或契约冻结（X3）却被要求定稿 D-2 真实分支、删除「待冻结」标注。
- 被要求修改契约正文、基线文档、`PROGRESS.md`、`tokens.css`、`vite.config.ts`、`package.json`、`src/pad/**` 或任何禁触文件。
- 被要求引入组件库 / 路由库 / 状态库 / 测试框架 / 新依赖。
- K1：被要求把 `USE_MOCK` 改成环境变量，但契约 S-11 尚未按 §6 流程修订。
- 被要求用 Mock 数据冒充 D-1 真实端点验收、或伪造走查 / 录屏 / 门禁结果。
- 需要硬编码密钥、写入真实长者数据、提交媒体大文件。

停止时写清：节点、缺失输入、阻塞依赖、需要谁作何决定、已完成的无关工作、建议的最小下一步。

## 八、最终输出格式

只在实际执行和验证后，按以下 Markdown 结构报告，不要粘贴整份代码：

```markdown
## 执行结论
<完成 / 部分完成 / 阻塞；一句话说明实际达到的范围>

## 节点状态
| 节点 | 状态 | 实际产出 | 门禁证据或阻塞原因 |
| --- | --- | --- | --- |
| G0…N13 | … | … | … |

## 文件变更
| 文件 | 变更摘要 |
| --- | --- |
<只列实际变更；附 git status 越界检查结果（白名单外改动数应为 0）>

## 验证结果
| 命令/走查项 | 结果 | 说明 |
| --- | --- | --- |
<build、三项静态零、走查各条；未执行必须写原因，不得写「通过」>

## 契约与外部依赖
| 编号 | 状态 | owner/决定 | 剩余阻塞 |
| --- | --- | --- | --- |
| X0/X1/X2/X3、K1、K2、N3/N4 | … | … | … |

## 安全与范围检查
<身份不臆造、契约正文零改动、Mock 不回归、禁触文件零改动、无硬编码密钥与真实数据、录屏未入库>

## 走查与录屏限制
<分别说明浏览器走查、真实端点、越权反向验证的证据；未验证项如实列出>

## 建议的下一步
<按 DAG 列出解锁所需的最小动作；没有则写「无」>
```

不要声称已 commit、已走查通过、已联调或全量通过，除非对应操作和证据确实发生。

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | 首版：依据 `d-双端前端推进计划.md` v1.0 与 `d-DAG.md` v1.0 生成可直接交给 AI 编码助手的执行提示词——任务目标、必读输入与事实优先级、已核实现状（开工重核）、文件白名单与禁触、15 条安全约束、G0+N1–N13 逐节点指令、构建/静态/走查验收命令（前端无测试框架，禁止虚构测试命令）、8 条停止条件、固定报告模板；明确 K1（USE_MOCK vs S-11）与 K2（403 处置 A/B）决策处置及 X0–X3 外部阻塞传播规则。 |
