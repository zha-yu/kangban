# Task 08 · 告警通知中心（console 护工，Mock 先行）

> 状态：已完成　｜　版本：v1.1　｜　日期：2026-11-06
> 关联：docs/plans/day04/f4-告警通知中心.md（实施计划）｜docs/team_tasks/day04/README.md（本卡代号 F4）
> 上游依据：PRD FR-ALT-03（P0）｜AC-23（通知状态边界）｜技术设计文档 §3 `notifications` 表 / Day 8 迁移批次 / 裁决 B5｜前端页面方案 §3.2
> 变更记录见文末

## 0. 任务定位与边界

- **做**：护工侧通知列表（staff 目标）：状态/类型筛选、AC-23 状态机时间线、delivered→人工确认、已读标记；Mock 先行（notifications 表 Day 8 未交付），`alerts-api.ts` 唯一切换点。
- **不做**：webpush/VAPID 订阅（P1，裁决 B5）、家属端通知、统计视图（FR-ALT-05 P2）、后端任何改动、真实接口对接。
- 写作域：`frontend/src/console/alerts-api.ts`、`frontend/src/console/pages/AlertsPage.tsx`、`frontend/src/console/layout/AppLayout.tsx`（启用项）、`frontend/src/console/App.tsx`（视图）、`frontend/src/console/index.css`。**禁触**：`app/**`、`src/pad/**`、`src/family/**`、基线文档正文、既有业务页。

## 1. 任务拆解

| # | 任务 | P | 说明 |
| --- | --- | --- | --- |
| T8-1 | alerts-api.ts | P0 | notifications 类型 + 8 条虚构 Mock（5 态 + 各 type + is_mock + failed）+ markRead/acknowledge |
| T8-2 | AlertsPage | P0 | 筛选、状态时间线（generated→pending→delivered→acknowledged）、确认按钮仅 delivered、已读标记 |
| T8-3 | 导航接线 | P0 | 侧栏「告警通知中心」启用；App careView 增加 alerts |
| T8-4 | 验证 | P0 | build 三入口 + 主理人手动测试 |

## 2. 关键口径（AC-23）

1. 状态机 `generated → pending → delivered → acknowledged` 严格顺序；前端确认按钮仅 delivered 可见可点，禁止跳步。
2. **送达 ≠ 确认**（delivered 由客户端回执产生）、**确认 ≠ 工单完成**；只有人工操作算确认（记录 acknowledged_by/at）。
3. 打开详情 = 已读（read_at），已读与确认两态并列展示、互不等价。
4. failed 单展示 retry_count（30s 重试、3 次失败 → failed）；is_mock 单加「演示」徽标，不污染 status 枚举。
5. payload 落库已脱敏，前端原样展示；Mock 数据全部虚构教学数据。

## 3. 验收标准

```
A · 列表：侧栏进通知中心；状态/类型筛选正确；时间倒序
B · 状态机：generated/pending 无确认入口；delivered → 人工确认 → acknowledged（记录确认人与时间）
C · 三态区分：已读（read_at）与确认（acknowledged_*）并列展示；确认≠工单完成提示
D · 容错展示：failed + retry_count 可见；is_mock「演示」徽标
E · 工程：pnpm build 三入口通过；零硬编码 hex；零后端文件改动
```

## 4. 测试验证要求

主理人手动测试（护工账号，Mock 数据）；真实接口交付后仅替换 `alerts-api.ts` 内部实现，页面层不感知；契约冲突以 PRD / 技术文档为准并卡上留痕。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：Mock 先行，T8-1–T8-4 拆解与 A–E 验收 |
| v1.1 | 2026-11-06 | **交付完成**：alerts-api.ts（类型对齐 notifications 表 + 8 条虚构 Mock + markRead/acknowledge）+ AlertsPage（AC-23 状态时间线、确认仅 delivered、已读≠确认）+ 导航接线；tsc/vite 验证通过；主理人手动测试通过 |
