# Spike 结论 · @pixiv/three-vrm-animation 兼容验证（Day05 S1）

> 状态：已冻结　｜　日期：2026-11-07
> 关联：docs/plans/day05/a1-本地VRM与形象选择页.md §4.1（方案）｜docs/team_tasks/day05/a1-本地VRM与形象选择页.md（任务卡 S1）

## 环境与运行方式

- `pnpm install && pnpm dev`（端口 5174），自动执行 14 模型 × 10 动画矩阵，结果写入页面表格并挂 `window.SPIKE_RESULT`。
- 依赖固定：three 0.187.0 / @pixiv/three-vrm 3.5.5 / @pixiv/three-vrm-animation 3.5.5（与 frontend 已装版本严格一致）。

## 结论（全部通过，依赖可合并进 frontend）

| 验证项 | 结果 | 说明 |
| --- | --- | --- |
| 库兼容性 | ✅ | three-vrm-animation@3.5.5 与 three@0.187 / three-vrm@3.5.5 协同正常 |
| VRM 动画解析 | ✅ 10/10 | 含 `animations/common/` 精选 5 个 + 原始库 4 个 + **日文文件名 1 个** |
| 模型解析 | ✅ 14/14 | 全部 humanoid 完整；meta 全部为 **"0"（VRM0）** |
| 模型×动画组合 | ✅ 140/140 | `createVRMAnimationClip` + AnimationMixer 播放 5 帧无异常；全绿模型 14/14 |
| 表情可用性 | ⚠️ 4/14 为 0 | Danheng / Evelyn / Firefly / Neuvillette **expressionMap 为空**，其余 10 个 13–18 个表情；口型驱动依赖 `aa` 表情，0 表情模型无法口型（见下） |
| 日文文件名（R4） | ✅ | `おじさん構文.vrma` 直接 fetch 解析成功，**无需 ASCII 重命名副本** |
| 渲染层（材质/着色器） | ⏸ 留待 S3 | 验证环境无 WebGL（headless 代理），矩阵中渲染列显示 `−`（SKIP）；需真机浏览器验证 |

## 新发现与 S3 约束（v2 增补）

1. **签名**：`createVRMAnimationClip(vrmAnimation, vrm)` —— 动画在前、模型在后（v2 旧签名 `(humanoid, anim)` 会报 `reading 'metaVersion'`）。
2. **expressionMap 是 Record 不是 Map**：3.5.5 的 `vrm.expressionManager.expressionMap` 返回普通对象，数量用 `Object.keys(...).length`，没有 `.size`。
3. **口型兼容风险（新增 R8）**：Danheng、Evelyn、Firefly、Neuvillette 四个模型无任何表情，`aa` 口型不生效。缓解选项：a) S3 对无表情模型用下颌骨骼（`Jaw` humanoid bone）做口型近似；b) 选择页卡片标注「不支持口型」；c) catalog 中排除。**待主理人裁决**。
4. **朝向标定（R3 升级）**：14 个模型全部是 VRM0，**无一例外全部需要 `rotationYOffset` 标定**（方案 Q4 原假设「VRM1 恒 0」在本批资产中不适用），catalog 每项必填。
5. **idle 候选**（Q3）：`animations/common/idle.vrma`（自然待机，10s，Apache-2.0，manifest 已带 sha256）解析播放通过，作为叠加待机的首选；备选 `idle-2.vrma`（30s）。
6. common/ 目录共 20 个精选动画（点头/摇头/鼓掌/举手等，均 Apache-2.0），可直接进 catalog 动画白名单。

## 复跑方式

```bash
cd spikes/vrm-animation && pnpm install && pnpm dev
# 浏览器打开 http://localhost:5174/，等标题变 SPIKE_DONE，控制台读 window.SPIKE_RESULT
```
