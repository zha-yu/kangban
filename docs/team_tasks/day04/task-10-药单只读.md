# Task 10 · 药单只读（console 护工，Mock 先行）

> 状态：已完成　｜　版本：v1.1　｜　日期：2026-11-06
> 关联：docs/plans/day04/f6-药单只读.md（实施计划）｜docs/team_tasks/day04/README.md（本卡代号 F6）
> 上游依据：PRD FR-MED-01（P0）/ §4.5 药单权限 / §1.6 有意决策｜技术设计文档 §3 `medication_plans` 表 / Day 7 迁移批次｜前端页面方案 §3.2
> 变更记录见文末

## 0. 任务定位与边界

- **做**：护工侧药单**只读**列表：负责长者下拉（真实 E7）+ 该长者用药计划展示（药品/剂量/服药时间/起止/版本/状态/创建医生）；零写操作。
- **不做**：医生药单维护页、medication_events 展示、FR-MED-09 续方提醒（P2）、后端改动、真实接口对接。
- 写作域：`frontend/src/console/medication-api.ts`、`frontend/src/console/pages/MedicationPage.tsx`、`frontend/src/console/layout/AppLayout.tsx`（启用项）、`frontend/src/console/App.tsx`（视图）、`frontend/src/console/index.css`。**禁触**：`app/**`、`src/pad/**`、`src/family/**`、基线文档正文、既有业务页。

## 1. 任务拆解

| # | 任务 | P | 说明 |
| --- | --- | --- | --- |
| T10-1 | medication-api.ts | P0 | MedicationPlan 类型对齐 medication_plans + 虚构 Mock（active/stopped/多 version）+ listPlansByElder |
| T10-2 | MedicationPage | P0 | 长者下拉联动 + 只读卡片列表 + 权限/教学数据标注 |
| T10-3 | 导航接线 | P0 | 侧栏「药单只读」启用（护工五项收尾）；App careView 增加 medication |
| T10-4 | 验证 | P0 | tsc + build + 主理人手动测试 |

## 2. 关键口径

1. **权限**（§4.5 有意决策）：医生增改停查，护工/管理员只读；护工修改药单服务端 403 并记审计（FR-MED-01）——页面以零写操作体现，不做「尝试修改」入口。
2. **version 语义**（AC-27）：修改生成新版本、历史提醒保留旧快照；页面仅展示 version 数字，提示文案说明，不做版本对比。
3. **schedule_cron**：库内 cron 串，页面做「每天 HH:MM」最小转换，其余原样显示（f6 D3）。
4. **教学数据**：全部虚构，页面标注「教学演示数据，不构成真实处方」。

## 3. 验收标准

```
A · 列表：长者下拉真实 E7 且切换联动；药单字段完整展示；已停用灰色
B · 只读：零写操作按钮；权限提示 + 教学数据标注存在
C · 容错：加载失败统一文案；不回显 trace_id
D · 工程：tsc 零错误 + vite build 通过；零硬编码 hex；零后端文件改动
```

## 4. 测试验证要求

主理人手动测试（护工账号）；真实接口交付后仅替换 `medication-api.ts` 内部实现，页面层不感知。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：Mock 先行，T10-1–T10-4 拆解与 A–D 验收 |
| v1.1 | 2026-11-06 | **交付完成**：medication-api.ts（对齐 medication_plans + 7 条虚构演示药单 + cronToText 最小转换）+ MedicationPage（E7 长者下拉联动、零写操作只读卡片）+ 导航接线；tsc/vite 验证通过；主理人手动测试通过。console 护工侧栏五项（F2–F6）全部收尾 |
