# Task 09 · 健康采集代填表（console 护工，提交 Mock）

> 状态：已完成　｜　版本：v1.1　｜　日期：2026-11-06
> 关联：docs/plans/day04/f5-健康代填.md（实施计划）｜docs/team_tasks/day04/README.md（本卡代号 F5）
> 上游依据：PRD FR-HLT-06（P1）/ §4.6（采集窗口、分级阈值）/ AC-32｜技术设计文档 §3 `health_records` 表 / Day 8 迁移批次｜前端页面方案 §3.2
> 变更记录见文末

## 0. 任务定位与边界

- **做**：护工健康采集代填表单（5 字段 + source 二选一），提交 Mock（health_records Day 8 未交付），分级结果卡展示；负责长者下拉真实 E7。
- **不做**：历史记录、医生复核队列、漏采工单联动、后端改动、真实提交接口。
- 写作域：`frontend/src/console/health-api.ts`、`frontend/src/console/pages/HealthFormPage.tsx`、`frontend/src/console/layout/AppLayout.tsx`（启用项）、`frontend/src/console/App.tsx`（视图）、`frontend/src/console/index.css`。**禁触**：`app/**`、`src/pad/**`、`src/family/**`、基线文档正文、既有业务页。

## 1. 任务拆解

| # | 任务 | P | 说明 |
| --- | --- | --- | --- |
| T9-1 | health-api.ts | P0 | HealthRecordInput/Result 类型；阈值常量（红/黄，f5 D4）；submitHealthRecord Mock 分级 + rule_version |
| T9-2 | HealthFormPage | P0 | 长者下拉（E7）、5 字段表单 + 校验、source 单选、提交结果卡 |
| T9-3 | 导航接线 | P0 | 侧栏「健康代填」启用；App careView 增加 health-form |
| T9-4 | 验证 | P0 | tsc + build + 主理人手动测试 |

## 2. 关键口径

1. **source 枚举**：`staff_scheduled`（定时代填）/ `staff_active`（主动代填），按技术文档 §3.4；AC-32 行文 `staff_report` 与表枚举冲突，不采用（f5 D2 留痕）。
2. **阈值常量**（硬编码，AGENTS §7）：红 = 收缩压 ≥180 或 舒张压 ≥120（单分量即红）；黄 = 收缩压 140–179 / 舒张压 90–119 / 收缩压 <90 / 舒张压 <60 / 睡眠 <5h / mood ≤2 / 非急症主诉 / 任一采集项缺失；其余绿。
3. **校验**：睡眠 0–24（>24 无效）；情绪评分 1–5；血压可全缺但全表单至少一项；血压数值 30–250 合理界。
4. **后续任务提示**（§4.6）：红 → 紧急工单 + 通知（Mock 提示文案）；黄 → 人工复核任务（黄档护工复核）；绿 → 正常入库。正式分级以服务端规则引擎为准，前端仅展示。
5. 补录追加不覆盖（AC-22）；代填非负责长者被拒绝并记审计（AC-32）——候选集由 E7 范围过滤保证。

## 3. 验收标准

```
A · 表单：长者下拉为负责长者（真实 E7）；5 字段 + source 单选齐全；校验按口径（>24h 拒绝、1–5 评分、全空拒绝）
B · 提交：返回分级结果卡（红/黄/绿徽标 + rule_version + 后续任务提示）；红档提示紧急工单
C · 容错：校验错误 danger-soft/ink 配对、不回显 trace_id
D · 工程：tsc 零错误 + vite build 通过；零硬编码 hex；零后端文件改动
```

## 4. 测试验证要求

主理人手动测试（护工账号）；Mock 数据全部虚构；真实接口交付后仅替换 `health-api.ts` 内部实现，页面层不感知。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：提交 Mock + 真实长者下拉，T9-1–T9-4 拆解与 A–D 验收 |
| v1.1 | 2026-11-06 | **交付完成**：health-api.ts（阈值常量 + Mock 分级）+ HealthFormPage（5 字段 + source 单选 + 结果卡）+ 导航接线；tsc/vite 验证通过；主理人手动测试通过 |
