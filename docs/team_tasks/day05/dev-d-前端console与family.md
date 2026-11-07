# Task dev-D · 前端 · console/family 双端（admin 页、契约对齐、切换层）

> 状态：未开始　｜　版本：v1.0　｜　日期：2026-11-07
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
