# Task 12 · 药单维护（console 医生，Mock 先行）

> 状态：已完成　｜　版本：v1.1　｜　日期：2026-11-06
> 关联：docs/plans/day04/f8-药单维护.md（实施计划）｜docs/team_tasks/day04/README.md（本卡代号 F8）
> 上游依据：PRD FR-MED-01（P0）/ §4.5 药单权限 / §1.6 有意决策｜AC-27｜技术设计文档 §3 `medication_plans` 表 / Day 7 迁移批次｜前端页面方案 §3.2
> 变更记录见文末

## 0. 任务定位与边界

- **做**：医生侧药单维护：增（version=1）/ 改（version+1）/ 停（stopped，二次确认）/ 查；与护工只读页共享同一 Mock store（医生改动护工页可见）。
- **不做**：健康趋势图（P1）、药品疑问（后续卡）、护工侧改动、后端改动、真实接口对接。
- 写作域：`frontend/src/console/medication-api.ts`（扩展写操作）、`frontend/src/console/pages/MedicationEditPage.tsx`、`frontend/src/console/layout/AppLayout.tsx`、`frontend/src/console/App.tsx`（doctorView）、`frontend/src/console/index.css`。**禁触**：`app/**`、`src/pad/**`、`src/family/**`、基线文档正文、既有业务页逻辑。

## 1. 任务拆解

| # | 任务 | P | 说明 |
| --- | --- | --- | --- |
| T12-1 | medication-api 写操作 | P0 | createPlan（version=1）/ updatePlan（version+1）/ stopPlan（stopped）；共享 store |
| T12-2 | MedicationEditPage | P0 | 长者下拉联动、列表（active 前 stopped 后）、新增/修改弹窗、停用二次确认 |
| T12-3 | 导航接线 | P0 | 医生侧栏「药单维护」启用；doctorView 增加 medication-edit |
| T12-4 | 验证 | P0 | tsc + build + 主理人手动测试 |

## 2. 关键口径

1. **权限**（§4.5）：医生增改停查；无删除语义，停用不可逆恢复不提供（教学级，留痕）。
2. **AC-27 快照**：修改 version+1，历史提醒保留提交时快照不受影响（Mock 原地更新，多版本行属提醒表职责）。
3. **cron 录入**（f8 D3）：文本输入 + 格式提示，`cronToText` 展示转换；不做时间选择器。
4. **教学数据**：页面标注「教学演示数据，不构成真实处方」。

## 3. 验收标准

```
A · 列表：长者下拉 E7 联动；active 前 stopped 后；version 可见
B · 写操作：新增/修改/停用即时生效且护工只读页同源可见；停用有二次确认
C · 校验：药品名/剂量/cron/开始日期必填；错误 danger-soft/ink 配对
D · 工程：tsc 零错误 + vite build 通过；零硬编码 hex；零后端文件改动
```

## 4. 测试验证要求

主理人手动测试（医生账号）；真实接口交付后仅替换 `medication-api.ts` 内部实现，页面层不感知。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：Mock 先行，T12-1–T12-4 拆解与 A–D 验收 |
| v1.1 | 2026-11-06 | **交付完成**：medication-api 扩展 create/update+1/stop（共享 store）+ MedicationEditPage（列表、新增/修改弹窗、停用二次确认）+ 医生侧栏启用；tsc/vite 验证通过；主理人手动测试通过 |
