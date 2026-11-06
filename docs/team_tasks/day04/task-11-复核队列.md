# Task 11 · 健康记录复核队列（console 医生，Mock 先行）

> 状态：已完成　｜　版本：v1.1　｜　日期：2026-11-06
> 关联：docs/plans/day04/f7-复核队列.md（实施计划）｜docs/team_tasks/day04/README.md（本卡代号 F7）
> 上游依据：PRD FR-HLT-03/04（P0）｜AC-21/22｜技术设计文档 §3 `health_records` 表 / Day 8 迁移批次｜前端页面方案 §3.2
> 变更记录见文末

## 0. 任务定位与边界

- **做**：医生侧复核队列（pending 黄/红档，红档置顶）：确认无误 / 纠错生成新版本（version+1 + severity 重算 + superseded_by 链，原值保留）；Mock 先行。
- **不做**：健康趋势图（P1）、药品疑问（后续卡）、补录入口、后端改动。
- 写作域：`frontend/src/console/review-api.ts`、`frontend/src/console/pages/ReviewQueuePage.tsx`、`frontend/src/console/layout/AppLayout.tsx`（医生分流）、`frontend/src/console/App.tsx`（doctor 视图）、`frontend/src/console/index.css`、`frontend/src/console/health-api.ts`（抽分级公共函数）。**禁触**：`app/**`、`src/pad/**`、`src/family/**`、基线文档正文。

## 1. 任务拆解

| # | 任务 | P | 说明 |
| --- | --- | --- | --- |
| T11-1 | health-api 抽分级函数 + review-api.ts | P0 | classify 抽出复用；ReviewRecord 类型 + Mock（含 130→180 纠错链）+ confirm/correct |
| T11-2 | ReviewQueuePage | P0 | 红档置顶队列、确认、纠错弹窗（预填可改、至少改一项）、版本链 |
| T11-3 | 导航接线 | P0 | 医生登录落真实页（复核队列），其余医生项仍置灰 |
| T11-4 | 验证 | P0 | tsc + build + 主理人手动测试 |

## 2. 关键口径

1. review_status 三态 pending/confirmed/corrected；复核操作仅 pending 可用。
2. 纠错 = 新版本记录（version+1、severity 按阈值重算、复核人留痕）+ 原记录 corrected + superseded_by 指向新版本（AC-22/FR-HLT-04：补录追加不覆盖、原值保留可查）。
3. review_note 为 Mock 自裁展示字段（表无此列，f7 D4 留痕）。
4. 红档置顶对齐 FR-ALT-02 精神；黄档护工/红档医生复核语义在 Mock 不做档位操作限制（f7 §2 留痕）。

## 3. 验收标准

```
A · 队列：红档置顶、时间倒序；pending 才有操作按钮
B · 确认：confirmed + 复核人 + 时间留痕，退出队列
C · 纠错：新版本 severity 重算 + 版本链（原值可查）+ 至少改一项校验
D · 工程：tsc 零错误 + vite build 通过；零硬编码 hex；零后端文件改动
```

## 4. 测试验证要求

主理人手动测试（医生账号）；真实接口交付后仅替换 `review-api.ts` 内部实现。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：Mock 先行，T11-1–T11-4 拆解与 A–D 验收 |
| v1.1 | 2026-11-06 | **交付完成**：review-api.ts（复核类型 + 纠错链 Mock + confirm/correct）+ ReviewQueuePage（红档置顶、确认、纠错弹窗、版本链）+ health-api 抽公共分级函数 + 医生分流落真实页；tsc/vite 验证通过；主理人手动测试通过 |
