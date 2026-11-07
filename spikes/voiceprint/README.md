# CAM++ Voiceprint Spike 结论（Day 4 A1 · 2026-11-06）

## 🎯 Spike 目标

验证 3D-Speaker CAM++ 声纹模型在本项目环境下能否加载、提取 embedding，
并确认 `AGENTS §7` 硬性阈值 **0.85** 的可分性。

## ✅ 基础验证

| 项目 | 结果 |
|---|---|
| CAM++ 模型加载 | ✅ sherpa-onnx 可加载，模型文件 27 MB |
| embedding 维度 | **192 维**（固定） |
| TEN-VAD 切纯语音 | ✅ 正常工作 |
| 推理容器部署形态 | ✅ 可行（sherpa-onnx 纯 CPU，不装 torch） |

## ⚠️ 关键发现：阈值 0.85 不可行

| 度量 | CAM++ 结果 | PRD/AGENTS 裁决 | 结论 |
|---|---|---|---|
| 同人 cosine（spk1_a ↔ spk1_b） | **0.5679** | ≥ 0.85 | ❌ **差距太大** |
| 异人 cosine max | -0.0146 | < 0.85 | ✅ |
| gap（同人 - 异人 max） | 0.5824 | — | 可分性 gap 本身够大，但**同人 cosine 绝对值远低于阈值** |

### 阈值扫描

| 阈值 | 同人≥T | 异人<T | 结论 |
|---|---|---|---|
| 0.50 | ✅ | ✅ | 可行 |
| 0.55 | ✅ | ✅ | 可行（CAM++ 原生口径） |
| 0.60 | ❌ | ✅ | 不可行 |
| 0.85 | ❌ | ✅ | **❌ 完全不可行** |

## 💡 根因分析

参考项目 README 第 19 行**明确记载**：

> CAM++ 声纹模型（阈值 0.55 是教师口径，本项目硬性数值 0.85，Day 4 重标）

即：
- **CAM++ 原生阈值 0.55**（教师用它做 speaker verification）
- **0.85 是本项目硬性数值**（PRD §3.2 + AGENTS §7 写死），但**这个阈值大概率是为另一模型（resemblyzer/ECAPA-TDNN 等）定的**，CAM++ 在 0.85 下同人样本 cosine 根本达不到

## 🔍 resemblyzer 备选验证（失败）

尝试 resemblyzer（256 维）在同一样本上跑：
- ❌ Windows 上需要 MSVC 14.0 编译 webrtcvad 依赖链（同主服务 webrtcvad 问题）
- ❌ 依赖 torch 120MB+（且 resemblyzer 也不再维护）

## 📋 待裁决项（12:30 午站会必须处理）

1. **阈值调回 0.55？**——违反 AGENTS §7（硬性数值写死 0.85），需要**主理人裁决**才能改
2. **换 CAM++ 更大的模型？**——比如 CAM++2 / ECAPA-TDNN 可能同人 cosine 更高，但需要额外 spike 验证
3. **换 sherpa-onnx 官方的 speaker embedding 模型？**——`3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` 是唯一现成的中文 speaker verification 模型，sherpa-onnx 没有更大的了
4. **阈值 0.85 的依据**——需要找出来源（PRD §3.2 写死，但没有标明是哪个模型的经验值）

## 📊 可执行的下一步

**方案 A（阈值调整）**：主理人裁决阈值下调至 CAM++ 可行区间（0.55 左右），同步更新 config.py 默认值 + AGENTS §7 + PRD 修订记录
- 收益：最快，CAM++ 模型 + sherpa-onnx 纯 CPU 容器部署形态完全可行
- 风险：PRD §3.2 "cosine ≥0.85" 是验收标准，改动需要评审

**方案 B（换模型）**：找一个同人 cosine 在 0.85+ 的模型
- sherpa-onnx 生态里没有更大的中文 speaker verification 模型
- ECAPA-TDNN 可能，但需要 onnxruntime + 重新 spike
- 风险：Day 4 时间窗口有限

**方案 C（继续用 CAM++ 0.55 阈值，待裁决后再改）**：先按 0.55 把服务写好，午站会决定是否改阈值还是卡模型。**阈值写在 Settings 里，运行时可调，代码不写死**

## ✅ 裁决结果（2026-11-07 主理人）

**采纳方案 A**：阈值 0.85 → **0.55**（CAM++ 教师口径）。已同批落地：
- PRD §3.2 / FR-VPR-01 / FR-VPR-02（样例 0.6→0.5）/ AC-28 —— 修订记录 **v2.8**
- 技术设计文档 §1.1/§3.2/§5.1/§8 Day 4 —— 修订记录 **v1.7**
- AGENTS §6 路由表 + §7 硬性数值表；`docs/接口契约_身份与权限.md` §2（v1.9）
- `app/core/config.py` `voiceprint_similarity_threshold = 0.55`（运行时仍经 Settings 可调）

> 待办：声纹闭环**真人录音验收仍未测**（录入 3 段 → 同人命中 → 非同人主动确认），列入 day05 计划 S0 第一优先（`docs/plans/day05/day-05-延迟基线与护栏.md`）。

## 📁 Spike 文件

- `spike_extract.py` — CAM++ + TEN-VAD（v2，结论有效）
- `spike_resemblyzer.py` — resemblyzer 对比（Windows 编译失败，无有效结论）
