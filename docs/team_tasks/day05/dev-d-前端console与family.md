# Task dev-D · 前端 · console/family 双端（admin 页、契约对齐、切换层）

> 状态：进行中　｜　版本：v1.7　｜　日期：2026-11-07
> 上游依据：PRD FR-ADM-01/06｜前端页面方案 §3.4 第 4 批（经 2026-11-07 裁决提前）｜docs/plans/day05 §4（A2/A3）
> 联调节点：N3、N4；详见 dag-项目工作流总览与联调节点.md

## 任务清单

| # | 任务 | 前置依赖 | 交付物 | 验收标准 |
| --- | --- | --- | --- | --- |
| D-1 | **admin 管理页**：console admin 角色接真实端点——员工/账号管理（E12 `POST /api/staff`、E15 `POST /api/guardians`、E13 绑定）、房间床位（E18）、模拟设备建档（E16/E17、一次性 device_secret 展示） | 无（后端端点 Day2 已就绪；console 纯 React+原生 CSS，不引组件库） | admin 角色页面（当前占位置灰→可用）+ 走查录屏 | ① admin 登录可见并走通：建员工/建家属/绑定长者/设备建档四流程 ② 非 admin 访问被拒（E1/E2 权限矩阵）③ 样式 `var()` 引用 tokens.css，无硬编码色值 ④ `pnpm --dir frontend build` 通过且 three-vrm 不进 console 构建产物 |
| D-2 | **family Mock→真实切换层**：为 HomePage/ReportsPage/NotificationsPage 建数据源抽象（Mock/真实双轨，环境开关） | dev-B B-3 校对稿（N4） | family api client 重构 + 开关说明 | ① 5 端点调用形状与《接口契约_家属端数据》一致（ elders/reports/notifications+已读）② Mock 现状不受影响，Day8 后端交付后仅切开关即可对接 ③ elder_guardians 关系判定走 `GET /api/sessions/current`，不前端臆造 elder_id |
| D-3 | **console 五 Mock 页字段对齐**：依据 dev-A 契约草案核对工单/告警/健康/药单/复核页字段 | dev-A A-4 草案（N3） | 字段差异清单 + Mock 数据修正 | ① 每个页面字段能对上契约草案出处（TD §3 表+字段）② 差异双向确认后修正 Mock 结构，页面渲染不回归 ③ 不改动后端，不新增第二套字段口径 |

## 联调要求

- **N3**：A-4 草案到手当天完成 D-3 对齐，差异清单回传 dev-A，口径以 TD §3 为准。
- **N4**：B-3 校对稿到手后 D-2 按冻结口径落切换层；契约冻结评审（主理人组织）前不擅自定稿。
- **M5.4**：admin 页走查纳入演示预演，走查结果回填本卡变更记录。
- 完成定义：任务表全绿 + `pnpm --dir frontend build` 通过 + 汇报清单（对照 docs/plans/day05 S7 自检项）。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.1 | 2026-11-07 | dev-D 本轮执行：D-1 代码完成（`admin-api.ts` E12/E15/E13 + `AdminUsersPage` 建员工/建家属/绑定长者 + `AdminDevicesPage` E18/E16/E17 + 一次性 `device_secret` + `App/AppLayout/index.css` 装配），`pnpm --dir frontend build` 通过、three-vrm 零进 console/family 产物；D-2 完成数据源拆分与 5 函数双轨（`USE_MOCK` 唯一 1 处，页面零改动），真实分支标注「待冻结」；D-3 完成五页字段盘点（TD §3 表名+行号 100%），Mock 代码零改动。**阻塞**：N5 四流程真实走查与三角色 403 反向验证（本机无 PostgreSQL/Docker/后端）；N8（B-3 未交付、冻结评审未开）；N10/N11（A-4 未交付、落点未裁定）。详见 `docs/plans/day05/d-admin页走查记录.md` 与 `d-汇报清单.md`。状态置「进行中」，三项均未全绿 |
| v1.2 | 2026-11-07 | N5 解除并完成真实走查：启动 Docker Desktop + postgres/redis + uvicorn 后，admin 四流程 4/4 通过（E12/E15/E13/E18/E16/E17 实测 200/201，DB 回查 4/4）；越权 3/3 被拒（care_worker/doctor 403 `ROLE_FORBIDDEN`，guardian 按身份契约 S-24 为 401 `TOKEN_INVALID`，任务卡「三种角色均 403」措辞与冻结契约不一致，已登记不改后端）；非 admin（care_worker/doctor/guardian）登录无白屏 3/3；一次性密钥关闭态取证，含明文截图已删除；family Mock 三页浏览器回归通过（切换器/日报列表+详情/通知未读 2→1）。**未完成**：演示视频录屏（工具仅截图）、family 真实 375px 视口验证（工具不支持）、N8/N10/N11（外部输入未到）。详见 `d-admin页走查记录.md` v1.1 与 `d-汇报清单.md` v1.2 |
| v1.3 | 2026-11-07 | 补 family 375px 真实视口验证：无头 Chrome CDP 375×812，日报/通知/详情三页 `scrollWidth=375`、溢出元素 0、正文 16px、触控 48px，**通过**。admin 视频录屏尝试 CDP 抓帧写 AVI，因 headless 表单重复提交 + 无编码器未产出，仍为未录制。临时验证脚本与带 token 的 Chrome profile 已删除；本地库仅保留 admin/界面护工/测试医生/界面家属及 1 条绑定。详见 `d-家属端切换层说明.md` v1.3、`d-汇报清单.md` v1.3 |
| v1.4 | 2026-11-07 | 按用户指示补登记 `docs/plans/README.md` 索引：6 份 `d-*.md`（启动门槛核对 / 字段差异清单 / 家属端契约差异表 / 家属端切换层说明 / admin 页走查记录 / 汇报清单）已加入索引表，状态 5 进行中 + 1 已完成，链接逐条 `Test-Path` 通过；v1.3 中「README 索引未更新」的遗留作废 |
| v1.5 | 2026-11-07 | 补 D-3 五 Mock 模块渲染基线（6 页面视图）：工单看板 7 单/告警 8 条/健康代填绿档/药单两页空态/复核队列 2 条+版本链 1 项/药单维护新增弹窗；登记 B1 药单 Mock 与真实长者 ID 不匹配、B2 弹窗无样式实测、B3 版本链仅 1 项、B4 医生范围缺口、B5 健康容器样式缺失。**Mock 代码零改动**；本地测试库补医生负责关系（非代码/迁移） |
| v1.6 | 2026-11-07 | 按用户指示先本地 commit（`880212d`，不 push）再 merge `origin/develop`（merge `3c6db7a`，14 commits，无冲突）。接收远端：后端 guardrails/三档超时、pad 本地 VRM、console 控件 40px + `--radius-control` + H4 token 修复、家属端契约 v1.1。dev-D 侧：admin 勾选/圆角对齐 40px 与 `--radius-control`；D-2 文档更新到契约 v1.1（B-3 部分闭环）；构建通过（4.43s）；console/index.css 未定义 token=0、hex=0。Review 发现 P1（ASR watchdog 启动在 connect/handshake 之后、guardrails 注释顺序与实现不符）等，未改他人代码 |
| v1.7 | 2026-11-07 | 按用户“执行吧”继续处理可落项：① 补 `.rv-mask`/`.rv-dialog`/`.btn-ghost`/`.wo-dialog-title` 弹窗样式，浏览器实测居中模态框 + 遮罩关闭通过（B2 关闭）；② `.gitignore` 增加 `**/__MACOSX/`、`**/.vite/`，删除 `frontend/public/**/__MACOSX` 与 `spikes/vrm-animation/.vite`；③ 修正 `app/agents/guardrails.py` 判定顺序注释（与实现/测试对齐）；构建通过（4.14s），`tests/test_guardrails.py` 9 passed。P1 ASR watchdog 启动时机未改（属 dev-B 音频行为，需其确认）。未 push |
