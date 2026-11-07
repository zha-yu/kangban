# Task a1 · 本地 VRM 与形象选择页（Day05 A1）

> 状态：已完成　｜　版本：v1.1　｜　日期：2026-11-07
> 关联：docs/plans/day05/a1-本地VRM与形象选择页.md（唯一实施方案）｜frontend/src/pad/avatar/｜docs/plans/README.md（索引行）
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
- [x] S3 vrm-scene.ts 扩展（多槽 LRU 字节缓存/程序化待机眨眼呼吸/vrma 播放与自动回落/rotateVRM0 统一朝向；tsc 全绿。引依赖 @pixiv/three-vrm-animation@3.5.5 + @types/three@0.187.0（dev），删 Day4 类型 shim three-ambient.d.ts。**真机标定已通过（2026-11-07，用户经 calibrate.html 四象限页逐模型校对）**：14 模型 offset=π 全部正脸，无例外回填；idle.vrma 播放 ✓）
- [x] S4 AvatarStage 去 C7 改造（D-1 props 注入 avatar?；D-2 跳过图片层直落渐变占位；D-3 会话内不响应 localStorage 变化；unbound 相删除；token/onAuthFail props 移除；idleAnimationUrl 接上；lipsync="none" 不传口型源；tsc 通过，avatar-config.ts 仅剩文件本体无引用）
- [x] S5 AvatarPicker 选择页 + standby 入口 + localStorage 持久化（AvatarPicker.tsx：一触即选写 localStorage + onDone(changed) 回调；14 卡片 div 渐变占位（Q2），lipsync="none" 标「不支持口型」（R8-b），使用中高亮；App.tsx MainScreen：standby 期「换个形象」入口、pickerOpen && standby 分支渲染（KWS 保持监听，喊小伴自动离开选择页）、stageKey 重挂 AvatarStage 显式换装（D-3）；tsc 通过）
- [x] S6 回归 + 构建门禁（2026-11-07 通过：`pnpm build` 全量 MPA 三入口零报错；family/console/client chunk 无任何 three/three-vrm 引用，pad chunk 851KB 与 Day4 基线持平；主链路真机回归 5 项全过——登录/standby、换形象、语音+口型、SOS 常显入口、退出重登）

## 计划外加项（实施中新增，均经主理人确认）

- **指针跟随（F-1b/F-2a/F-3）**：standby 期圆形舞台内眼睛+头部跟随指针；鼠标/触摸/触控笔均生效（F-3 由「仅鼠标」调整为「触摸拖动也跟随」）；出圆/会话中回落看镜头。落点 vrm-scene.ts（注视点+头部小幅转向）+ AvatarStage.tsx（圆形命中判定）。iPad 上需 `touch-action:none` 防滚动仲裁打断 pointercancel（S6 真机回归发现并修复）。
- **Q2 立绘提前落地**：14 张角色卡 PNG（`frontend/public/assets/avatars/`，各约 100KB）由主理人提供，`avatarThumbUrl()` 接入选择页，div 占位退役。

## 验收

以方案 §10 验收清单（9 条）为准，全部通过后本卡置「已完成」。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | 首版：S0 留痕（C7 停用登记 + 丢失文档处置确认），待办对齐方案 S1–S6 |
| v1.1 | 2026-11-07 | S1–S6 全部完成，真机验收通过，状态置「已完成」；补登计划外加项（指针跟随含触摸、Q2 立绘接入） |
