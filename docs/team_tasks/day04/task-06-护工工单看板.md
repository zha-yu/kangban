# Task 06 · 护工工单看板（console 第 3 批首项，Mock 先行）

> 状态：已完成　｜　版本：v1.1　｜　日期：2026-11-06
> 关联：docs/plans/day04/f2-护工工单看板.md（实施计划）｜docs/team_tasks/day04/README.md（本卡代号 F2）
> 上游依据：PRD §4.7（工单状态机 / 2 分钟升级 / 管理员重开）｜FR-ALT-01/02（P0 验收）｜技术设计文档 §3.4 `work_orders` / `work_order_transitions`（字段唯一依据）｜docs/前端页面方案.md §3.2/§3.4/§4
> 变更记录见文末

## 0. 任务定位与边界

- **做**：console 护工工单看板单页 + 浅色侧栏骨架（主色 8% 底选中态）；接口未交付，前端 Mock 先行（TS 类型对齐 `work_orders` 表 + 内存虚构数据），集中留切换点。
- **不做**：真实接口对接（联调期替换）、医生/管理员业务页、侧栏主题细化、后端任何改动、前端测试框架。
- 写作域：`frontend/src/console/workorders-api.ts`、`frontend/src/console/layout/**`、`frontend/src/console/pages/WorkOrdersPage.tsx`、`frontend/src/console/index.css`、`frontend/src/console/App.tsx`（仅分流行）。**禁触**：`app/**`、`alembic/**`、`src/pad/**`、`src/family/**`、`console/api.ts`、基线文档正文。

## 1. 任务拆解

| # | 任务 | P | 说明 |
| --- | --- | --- | --- |
| T6-1 | Mock 层 `workorders-api.ts` | P0 | 类型 / 8 条虚构工单 / transitions 预置 / 状态机 action / 模拟延迟 |
| T6-2 | 侧栏骨架 `AppLayout.tsx` | P0 | 护工 5 项（看板高亮）、医生/管理员置灰占位；顶栏用户 + 退出；触控 ≥48px |
| T6-3 | 看板页 `WorkOrdersPage.tsx` | P0 | 红档置顶排序、筛选、「未处理」交接清单、状态机操作按钮、详情 transitions 历史 |
| T6-4 | 升级展示 | P1 | 红档未确认升级倒计时 + 满 2 分钟 escalated 徽标（纯展示，FR-ALT-01） |
| T6-5 | 装配验证 | P0 | App 分流：care_worker→看板，doctor/admin→占位；build 三入口通过 |

## 2. 关键口径（决策已定，执行期不再议）

1. **重开仅管理员**：PRD §4.7「管理员可重开并保留原因」为准，护工看板不显示重开入口（与前端页面方案 §3.2 表述冲突，按效力顺序取 PRD，f2 计划 D3 留痕）。
2. **状态机**：new→acknowledged→in_progress→resolved→closed；解决（红档强制 `actual_handled_at`+备注）；关闭必填处置结果；误报必填原因并保留原始输入。
3. **看板排序**：红档置顶 → 未处理在前 → 时间倒序（FR-ALT-02 验收：红档排在普通工单之前）。
4. **escalated 纯展示**：由 `created_at` 推算倒计时与徽标，升级逻辑归后端（FR-ALT-01），前端不建第二个工单。
5. **样式**：原生 CSS 全 `var()` 引用 token，信号色只做徽标淡底 + 深字（不做按钮底、不铺面）；表单错误 danger-soft/ink 配对、不回显 trace_id。

## 3. 验收标准

```
A · 分流
护工登录 → 侧栏「工单看板」高亮进看板；医生/管理员 → 侧栏全置灰 + 占位首页
B · 看板
红档排在黄档之前；筛选（状态/类型/红黄）与「未处理」视图正确；未处理列表即交接清单
C · 状态机
确认→开始处理→解决→关闭全走通；红档解决强制处置时间+备注；关闭必填处置结果；误报必填原因；重开仅管理员可见（Mock API 支持）
D · 留痕
任一工单详情可查完整 transitions 历史（操作人/时间/from→to/备注），误报单保留原始输入
E · 升级展示
红档未确认显示倒计时，满 2 分钟显示 escalated 徽标
F · 工程
pnpm --dir frontend build 三入口全过；grep 零硬编码 hex；console 1280×800 不破版；零后端文件改动
```

## 4. 测试验证要求

1. 手动矩阵：Chrome 最新版，console 1280×800；三角色（护工/医生/管理员）逐格过 §3。
2. Mock 数据全部虚构教学数据，不写真实长者信息。
3. 契约冲突时以 PRD / 技术文档为准，卡上变更记录留痕；真实接口交付后仅替换 `workorders-api.ts` 内部实现，页面层不感知。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：console 第 3 批首项，Mock 先行；T6-1–T6-5 拆解与 A–F 六组验收；重开权限按 PRD §4.7 收窄为管理员（f2 计划 D3 留痕） |
| v1.1 | 2026-11-06 | **交付完成**：T6-1–T6-5 全部落地（workorders-api Mock 层 / AppLayout 侧栏骨架 / WorkOrdersPage 看板 / App 分流接线；HomePage 占位页删除被 AppLayout 占位替代）；pnpm build 三入口通过、console 产物零硬编码 hex；主理人手动测试通过。真实接口交付后仅替换 workorders-api.ts 内部实现 |
