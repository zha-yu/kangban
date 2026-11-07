# Task a1 · 本地 VRM 与形象选择页（Day05 A1）

> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-07
> 关联：docs/plans/day05/a1-本地VRM与形象选择页.md（唯一实施方案，v1.1）｜frontend/src/pad/avatar/｜docs/plans/README.md（索引行）
> 上游依据：PRD FR-3D-01/FR-3D-02（验收）｜主理人裁决 2026-11-07（D1–D4、Q1–Q4，见方案 §3）
> 变更记录见文末

## 留痕：C7 消费停用（S0）

- 主理人 2026-11-07 裁决（方案 D1）：pad 端数字人改**本地资产驱动**，停止消费 `GET /api/pad/avatar-config`（C7）。后端接口与 `frontend/src/pad/avatar/avatar-config.ts` 代码保留不删，仅前端不再调用。
- 原裁决文档 `docs/plans/adhoc-20261106-数字人接入方案对比.md` 已由主理人主动删除（磁盘与 git 历史均不存在），plans/README.md 索引行同步标为「已废弃（文件已删除）」；**C7 停用留痕以本卡为准**。
- 实施时 AvatarStage 移除 `fetchAvatarConfig` 调用（方案 §4.4），`avatar-config.ts` 文件头加停用注释。

## 待办（对齐方案 §7 步骤）

- [x] S0 留痕（本卡 + plans/README.md 索引修订）
- [x] S1 spike：spikes/vrm-animation/ + 模型×动画兼容矩阵（结论见 spikes/vrm-animation/README.md：140/140 全过，依赖可合并；14 模型全为 VRM0，朝向全量标定；签名 (anim, vrm)）
- [x] S2 avatar-catalog.ts 模型清单（默认 Yoimiya，Q1–Q4 口径；R8-b 落字段：4 个无表情模型 lipsync="none"；动画白名单 common/ 6 个，world-* 不收录；tsc --noEmit 通过）
- [ ] S3 vrm-scene.ts 扩展（多槽缓存/程序化待机/vrma/朝向标定）
- [ ] S4 AvatarStage 去 C7 改造（props 化 + 降级链 + diag 保留）
- [ ] S5 AvatarPicker 选择页 + standby 入口 + localStorage 持久化
- [ ] S6 回归 + 构建门禁（console/family 产物无 three；主链路零回归）

## 验收

以方案 §10 验收清单（9 条）为准，全部通过后本卡置「已完成」。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | 首版：S0 留痕（C7 停用登记 + 丢失文档处置确认），待办对齐方案 S1–S6 |
