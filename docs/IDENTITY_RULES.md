> 状态：已冻结（口径以 PRD §3.2 当前版本为唯一来源）｜ 版本：v1.0 ｜ 日期：2026-11-13
> 口径来源：docs/康伴3D数字人_PRD.md §3.2（唯一来源）；四层锚点模型出自 docs/康伴3D数字人_大纲.md §7.1
> 阈值依据：eval/reports/声纹调优报告.md（PRD v3.1 裁决，0.62 定稿）
> 变更记录见文末

# 身份规则（IDENTITY_RULES）

本文是说话人身份识别的口径速查文档。凡与本文不一致的表述，以 PRD §3.2 为准；本文仅做汇总与落地对照，不引入新口径。

---

## 一、四层身份锚点

说话人身份按以下四层依次收敛（模型出处：大纲 §7.1）：

```
L1 设备绑定 → L2 房间默认 → L3 声纹 → L4 主动确认
                                  ↓（仍无法确定）
                              通用模式（不带任何个人记忆）
```

**L1 设备绑定**：模拟设备与长者 1:1 绑定（归属房间不变），说话人身份由"设备绑定 1v1 + 声纹匹配"判定（PRD §3.2 / FR-ADM-01）。设备凭证换取长者端会话（有效期 24h，解绑立即失效）。

**L2 房间默认**：现口径下（PRD v2.6 裁决，2026-11-06），候选集已由"房间长者集合"收敛为设备绑定长者本人——设备绑定 1v1 即天然锁定房间默认住户，不再按房间给出候选列表。双人间另一位长者使用其自己的设备，各自 1v1。

**L3 声纹识别**：每次语音交互提取说话人 embedding，与候选集内各长者声纹做 cosine 比对，≥0.62 判定为该长者（阈值口径见第三节）。

**L4 主动确认兜底**：声纹未命中（低置信度）或声纹服务不可用时，页面主动询问"请问是哪位在说话？"，由用户选择或语音回答确认；确认失败则降级通用模式。本人被误拒时也可通过主动确认继续，不会把用户锁死（PRD §3.2"教学级声纹能做什么"）。

四层收敛通过后，Runtime Context 注入 `elder_id + source`；仍无法确定则进入通用模式（不带任何个人记忆，见第四节）。

---

## 二、三档级联

统一身份模型（PRD §3.2 原文口径）：每次语音交互的说话人身份由两个条件共同判定——长者 ID（设备绑定 1v1 圈定）+ 声纹匹配（判定具体是谁）。声纹判定后按三档级联处理，任一档失败都逐级兜底，**不静默猜人**：

| 判定档位 | 处理规则（与 PRD §3.2 判定表逐字一致） |
| --- | --- |
| ① 声纹命中某位绑定长者（≥0.62） | 进入该长者会话：加载个人记忆，健康/用药操作归属该长者 |
| ② 低置信度（<0.62） | **必须主动询问确认**："请问是哪位在说话？"，由用户选择或语音回答 |
| ②a 询问后确认成功 | 进入确认长者的会话 |
| ③ 仍无法确定 | 进入**通用模式**：不读取任何个人记忆、不写入健康记录、不确认用药 |
| 特例：声纹服务不可用 | 降级为"长者 ID + 主动确认"（页面询问），不静默猜人 |

级联行为对应的落地测试：

- ① 命中：`tests/test_identity_service.py::test_verify_voice_match`
- ② 低置信度转主动确认：`tests/test_identity_service.py::test_verify_low_confidence_to_confirm`
- ②a 确认成功开 thread：`tests/test_identity_service.py::test_open_thread_from_confirm`；冒充确认（确认长者 ≠ 候选集）降级：`tests/test_identity_service.py::test_open_thread_confirm_elder_mismatch`
- ③ 通用模式：`tests/test_identity_service.py::test_verify_elder_unbound`（设备未绑定）、`tests/test_identity_thread_lifecycle.py::test_timeout_generic_closes_active_before_open`（确认超时降级 generic）
- 特例 声纹服务不可用：`tests/test_identity_service.py::test_verify_vp_unavailable`
- 设备锁定（无音频首轮）：`tests/test_identity_service.py::test_verify_device_no_audio`

候选集口径（PRD §3.2 第 1 条，逐字）：**"模拟设备与长者 1:1 绑定，声纹候选集=绑定长者本人（恒 1 位）；双人间另一位长者使用其自己的设备，各自 1v1。本设备只认绑定长者的声纹；他人在本设备前的语音即使与库中其他长者的声纹高度相似，也不会被误认为本人。"**

---

## 三、阈值口径

**生效阈值：cosine ≥ 0.62 判定为该长者**（PRD §3.2 第 2 条 / FR-VPR-01、FR-VPR-02 / AC-28 / §6.1 Day 4 行，PRD v3.1 七处统一）。

**阈值沿革：0.85 → 0.55 → 0.62**

| 版本 | 日期 | 阈值 | 依据 |
| --- | --- | --- | --- |
| 初版 | — | 0.85 | 原始口径；后被实测证明不可行（0.80 档 FRR 已达 55.6%，0.85+ 全拒识，见调优报告 §六反证数据） |
| v2.8 | 2026-11-07 | 0.55 | CAM++ 教师口径（spikes/voiceprint 实测同人 cosine 0.517–0.568，0.85 必误拒） |
| v3.1 | 2026-11-11 | **0.62（定稿）** | 主理人裁决：首轮 3 人 9 段真人互测，同人 0.6664–0.8399 / 异人 0.2165–0.6512，0.55 档误识 5/27（FAR 18.5%）；0.62 档 FAR 3.7% / FRR 0，残余误识由三档级联"声纹失败→主动确认"兜底 |

依据：eval/reports/声纹调优报告.md（v1.3 定稿节 §六b：生效阈值 0.62，FAR 3.7% / FRR 0，样本 3 人 9 段，模型 CAM++ `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` 192 维，容器指纹 fp=f682b514，数据底稿 voiceprint_sweep_raw.json 可复跑）。

落地位置：`app/core/config.py:242` `voiceprint_similarity_threshold: float = 0.62`（运行时经 Settings 可调）；`.env.example` 示例值同步。后续变更走 PRD 修订记录。

---

## 四、通用模式边界

进入通用模式（source=generic，elder_id=None）后，三条硬边界（PRD §3.2 判定表 / FR-VPR-03，逐字）：

**不读取任何个人记忆、不写入健康记录、不确认用药。**

补充口径：

- generic 身份不得携带 elder_id（`test_dialogue_context.py::test_generic_with_elder_id_is_rejected` 强制）；device/voice/confirm 身份必须携带 elder_id（`test_personal_identity_requires_elder_id`）。
- generic 轮不调度记忆提取（闸门②，`tests/test_response_service.py::test_generic_turn_skips_memory_extraction`）。
- generic 上下文丢弃历史、人设与个人称呼，并明确要求健康/用药先确认身份（`tests/test_dialogue_context.py::test_generic_ignores_history_persona_and_display_name`）。
- generic 分支对话可正常运行不报错（`tests/test_companion_identity.py::test_generic_branch_no_longer_raises`）。

---

## 五、thread_id 生成规则

`thread_id` 由**服务端生成**，不是前端/设备传入（PRD §3.2 数据模型注，逐字）：**"`elders.session_seq` 单调递增，服务端按 `(elder_id, session_seq)` 生成会话 thread_id，防伪造（见 §4.10 三道隔离闸门 FR-ADM-06）。"**

落地口径：

- personal thread：同一事务内 `Elders.session_seq + 1` 并 `RETURNING`，thread_id 形状 `f"{elder_id}:{session_seq}"`（`app/services/identity_service.py:143` `_open_thread`）。
- generic thread：无 elder，session_seq=0，thread_id 用服务端 UUID hex 保唯一（`identity_service.py:116` `_new_generic_thread_id`）。
- 闸门①：身份确定后事务内 INSERT；任何新 thread INSERT 之前必须先结束该设备所有未结束 thread（`tests/test_identity_thread_lifecycle.py::test_verify_voice_closes_active_before_open`、`test_open_thread_from_confirm_closes_active_before_open`）。
- 闸门校验：伪造形状 / 跨长者 / 工作人员 / 未绑定身份一律拒绝（`tests/test_gates.py::test_gate1_format_layer_rejects_forged_shapes`、`test_gate1_format_layer_parses_server_shape`、`test_gate1_pure_identity_match_rejects_cross_elder_and_staff`）；thread 归属设备校验 `tests/test_identity_service.py::test_validate_thread_belongs_to_device`；无身份硬拦 `tests/test_gates.py::test_gate3_no_identity_raises_identity_not_set`。
- 主动确认环节设 30 秒超时，超时降级 generic（`tests/test_audio_identity_ws.py::test_identity_timeout_constant_is_30`、`tests/test_identity_thread_lifecycle.py::test_timeout_generic_closes_active_before_open`）。

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-13 | 首版（Day 10 节点 C4）：四层锚点 / 三档级联 / 阈值 0.62 口径与沿革 / 通用模式边界 / thread_id 服务端生成，五节齐备；三处关键口径（阈值 0.62、候选集恒 1 位、级联判定表）与 PRD §3.2 当前版本逐字核对一致 |
