# Task 07 · 负责长者列表与详情（console，真实对接）

> 状态：已完成　｜　版本：v1.1　｜　日期：2026-11-06
> 关联：docs/plans/day04/f3-负责长者列表.md（实施计划）｜docs/team_tasks/day04/README.md（本卡代号 F3）
> 上游依据：接口契约_身份与权限.md §E6/E7/R4/S-05/S-07（字段唯一依据）｜PRD §3.1（护工 P0：负责长者列表与详情）｜前端页面方案 §3.2
> 变更记录见文末

## 0. 任务定位与边界

- **做**：console 护工「负责长者」列表页（分页 + 状态筛选）与详情卡，**真实对接**已交付的 E7/E6 接口（服务端自带角色范围过滤，AC-18）。
- **不做**：建档/编辑（E5/E8，admin 第 3 批管理页）；健康摘要入口（数据未交付）；后端任何改动；Mock。
- 写作域：`frontend/src/console/elders-api.ts`、`frontend/src/console/pages/EldersPage.tsx`、`frontend/src/console/api.ts`（仅导出 apiGet）、`frontend/src/console/layout/AppLayout.tsx`（启用长者项）、`frontend/src/console/App.tsx`（视图切换）、`frontend/src/console/index.css`。**禁触**：`app/**`、`src/pad/**`、`src/family/**`、基线文档正文。

## 1. 任务拆解

| # | 任务 | P | 说明 |
| --- | --- | --- | --- |
| T7-1 | api.ts 导出 apiGet + elders-api.ts | P0 | 通用鉴权 GET 封装；E7 分页列表 / E6 详情类型与调用 |
| T7-2 | EldersPage | P0 | 分页列表、status 四态筛选、行点击详情卡（R4 全字段 + 房间床号，空值显示 —） |
| T7-3 | 导航接线 | P0 | AppLayout「负责长者」启用可点；App care_worker 视图状态 |
| T7-4 | 验证 | P0 | build 三入口 + 主理人手动测试 |

## 2. 关键口径

1. E7 列表项不返敏感字段（S-07）；E6 详情含 `birth_date`/`gender`/`persona_id`/`session_seq`/`created_at` + `room_no`/`bed_no`（S-05）。
2. `status` ∈ active/away/transferred/archived；筛选用后端 query 参数，前端不做二次过滤。
3. 错误统一文案（403/404/网络），不回显 trace_id（task-05 既有约定）。

## 3. 验收标准

```
A · 列表：护工登录 → 「负责长者」显示范围内长者；分页/总数正确；状态筛选生效
B · 详情：点击行 → 详情卡全字段展示，空值显示 —
C · 容错：403/404/网络错统一文案；登录过期自动续期后重放
D · 工程：pnpm build 三入口通过；零硬编码 hex；零后端文件改动
```

## 4. 测试验证要求

主理人手动测试（护工账号）；数据为库内已有演示长者；契约冲突以 `app/api/elders.py` 为准并卡上留痕。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：真实对接 E7/E6，T7-1–T7-4 拆解与 A–D 验收 |
| v1.1 | 2026-11-06 | **交付完成**：apiGet 通用封装 + elders-api.ts + EldersPage（分页/筛选/详情卡）+ 侧栏「负责长者」启用与视图切换；pnpm build 三入口通过、console 产物零硬编码 hex；主理人手动测试通过 |
