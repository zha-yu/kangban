# Day 4 模块间接口契约（代码级定义）

> 状态：C4 / C5 / C6 / C7 均已定稿（主理人裁决提前冻结，晨会签字确认）　｜　版本：v1.1　｜　日期：2026-11-06
> 关联：docs/team_tasks/day04/README.md §4（契约清单）｜docs/team_tasks_day03/contracts.md（C1–C3 上游契约，本日只消费不改）
> 上游依据：docs/plans/day-04-3D与身份闭环.md v2.4（I1–I5/§3.0）｜PRD v2.6（FR-VPR/FR-3D/FR-DLG-05/AC-42）｜技术设计文档 v1.5
> 变更记录见文末

**使用规则**（沿 day03 惯例）：各冻结人将对应契约**原样拷贝**进自己模块顶部（C4→`app/api/voiceprints.py`，C5→`app/audio/signaling.py` 追加段，C6→`app/services/dialogue_service.py`，C7→`app/api/pad.py`），下游只准 import 契约类型。定稿后任何改动必须群通告 + 冻结人任务卡留痕。

---

## C4 · voiceprint REST 契约（冻结人：A1，消费方：pad 前端、A2 identity_service）

```
Authorization: Bearer <device access_token>   # E4 签发；elder_id 一律服务端由 device 解出，客户端不传

POST /api/voiceprints/samples        # 3 段注册：句子 + 数字 + 自由说，sample_index 1/2/3
Content-Type: multipart/form-data
  audio: file          # 16kHz/16bit/单声道 WAV，≤10s（超长 413）
  sample_index: int    # 1|2|3（越界 422）

200 → {"elder_id": str, "version": int, "sample_index": int, "status": "none|partial|active"}
409 → {"detail": ..., "code": "duplicate_sample", "trace_id": ...}   # 同 (elder_id, version, sample_index) 重复上传
422 → code=validation_error（含 WAV 头非法、时长越界）
503 → code=vp_extract_unavailable（推理容器超时/失败；body 携带长者可读降级话术）

GET /api/voiceprints/status
200 → {"elder_id": str, "status": "none|partial|active", "registered": [int, ...], "version": int}
409 → code=device_unbound（设备未绑定长者——task 前端三分支之一）

DELETE /api/voiceprints              # 随时重录；写 audit_logs
204 → 无 body
```

**语义约定**：原始音频提取 embedding 后即弃、不落盘（AC-36）；embedding Fernet 加密后落库，库内只见密文；3 段齐自动置 active；幂等键 `(elder_id, version, sample_index)`。

## C5 · 身份级联 WS 消息契约 v2（冻结人：A1 提案 / A2 定稿，消费方：pad 前端、A4 metrics）

传输：复用 `WS /ws/signal`（C2 信封不变，文本帧 JSON）。以下为 **C2 消息表的追加类型**，原类型不动。

```python
class IdentityAskMsg(TypedDict):      # S→C，低置信/服务不可用时主动确认
    type: Literal["identity_ask"]
    ask_id: str                       # 本次确认的唯一 id；confirm 必须回带
    reason: Literal["low_confidence", "vp_unavailable"]
    candidates: list[dict]            # 恒 1 位（设备绑定长者 1v1）：[{"elder_id": str, "display_name": str}]
    timeout_s: int                    # 恒 30

class IdentityConfirmMsg(TypedDict):  # C→S，pad 大按钮点选
    type: Literal["identity_confirm"]
    ask_id: str
    elder_id: str                     # 必须等于 candidates[0].elder_id；否则服务端按未确认处理

class IdentityResultMsg(TypedDict):   # S→C，告知最终身份模式（前端据此显示/隐藏身份横幅）
    type: Literal["identity_result"]
    source: Literal["voice", "confirm", "generic"]
    elder_id: str | None              # generic 时为 None

class ClientDiagMsg(TypedDict):       # C→S，服务端不回执；stage=vad 另有兜底激活语义（见块后补充）
    type: Literal["client_diag"]
    stage: Literal["vrm_load", "webrtc", "vad", "asr"]
    ok: bool
    error: str                        # 已脱敏
```

**client_diag 语义补充（2026-11-06，随 W4 兜底落地同步，v1.1）**：`stage=vad, ok=false` 是服务端 VAD 兜底（`app/audio/vad.py` VadFallbackController）的**唯一显式激活信号**——A2 的 client_diag 处理路径据此调 `MediaSession.set_vad_fallback_active(True)`；`ok=true` 或连接关闭即停用（停用丢弃服务端残留，防端侧恢复后双切段冲突）；未收到 diag 不启用，不与端侧 TEN-VAD 双跑。`vrm_load/webrtc/asr` 三 stage 仅日志观测。

**级联语义**（v2.2/v2.3 裁决口径）：会话首轮强制校验（identity_service 与绑定长者 1v1 比对，cosine ≥0.85 → `identity_result source=voice`）；会话内静默复检仅身份变化时干预（换人 → 旧 thread 结束、重走首轮校验）。

### C5 开放点裁决记录（2026-11-06 主理人提前定稿）

1. **确认交互**：pad **大按钮点选**（大字幕提问 + 单个「是我」大按钮，对应 candidates 恒 1 位）；语音应答「是我」属 L1 意图，**Day 4 不做**，留待后续迭代登记。
2. **30s 计时起点**：服务端**发出** `identity_ask` 时刻；超时服务端置 generic 并主动下发 `identity_result source=generic`（前端无需自己计时兜底）。
3. **confirm 异常**：`ask_id` 不匹配 / 身份已定后重复 confirm → 服务端忽略 + warn 日志（不回 error、不关连接，沿 C2 裁决 4 惯例）；候选集外 elder_id 一律按未确认处理。
4. **generic 模式通知**：经 `identity_result source=generic` 下发一次，前端显示通用模式横幅；服务端侧不读记忆、不写健康、不确认用药（FR-VPR-03）。
5. **metrics 打点位**（A4）：`identity_ask` 发出、`identity_confirm` 收到、`identity_result` 下发三处进 trace。

## C6 · 对话链路契约（冻结人：A2，消费方：pad 前端、A4）

```python
# WS 主链路：回复复用 C2 tts_text，音频走 WebRTC track——零新消息类型
class TtsTextMsgV2(TypedDict):        # S→C（C2 tts_text 的向后兼容扩展）
    type: Literal["tts_text"]
    text: str
    degraded: bool                    # 新增可选字段，缺省 False：True=LLM 降级模板话术（FR-DLG-05）

# REST 降级链路（C3）reply 形状不变：{"text": str, "reply": {"text": str, "audio_url": str | None}}

class ThreadInfo(TypedDict):          # 服务端内部契约（闸门①），前端无感知
    thread_id: str                    # 服务端生成；客户端/模型传入一律丢弃
    elder_id: str
    session_seq: int                  # 会话内递增；换人 → 旧 thread ended_at 落库 + 新 thread
```

**语义约定**：greeting 在 `auth_ok` + 身份确定（`identity_result`）后由服务端主动下发 `tts_text{text=personas.greeting}` + TTS 音频，**不依赖 LLM**（「打开 5 秒立起并开口说话」）；generic 身份的 greeting 用通用话术；上下文组装 = 固定前缀（`app/prompts/personas/`）+ Redis 最近 5 轮，Day 4 无记忆来源。

## C7 · avatar-config REST 契约（冻结人：A3，消费方：pad 前端）

```
GET /api/pad/avatar-config
Authorization: Bearer <device access_token>

200 → {
  "avatar_url": str | None,          # 绝对 URL（PUBLIC_BASE_URL 拼接）；None → 前端直接用内置兜底
  "fallback_image_url": str | None,  # 静态头像
  "rotation_y_offset": float,        # VRM0 朝向补偿（弧度），VRM1 恒 0.0
  "version": int                     # persona.avatar_url 变更即递增；前端据此失效缓存
}
401/403 → 统一错误体 {detail, code, trace_id}

静态资产：GET /avatars/<file>（nginx 直出，不经后端；.vrm ≤15MB，Content-Type: model/gltf-binary）
```

**语义约定**：取值链 persona → Settings → 前端内置（三层降级）；换形象只 UPDATE `personas.avatar_url` + `version+1`，零代码改动；`elder_id` 服务端解析，不采信客户端传入。

---

## 定稿确认记录

| 契约 | 冻结人 | 状态 | 备注 |
| --- | --- | --- | --- |
| C4 | A1 | **已定稿**（2026-11-06，主理人裁决提前冻结） | 晨会签字确认后拷入 `app/api/voiceprints.py` |
| C5 | A1 提案 / A2 定稿 | **已定稿** | 五个开放点裁决见「C5 开放点裁决记录」；属 C2 契约追加，A2 合入前补 task-a2 契约变更记录 |
| C6 | A2 | **已定稿** | 复用 C2/C3 零新消息类型，仅 tts_text 加可选 degraded 字段 |
| C7 | A3 | **已定稿** | 晨会签字后前端即可开发挂载 |

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿即定稿：C4–C7 代码级定义 + C5 五个开放点裁决（确认交互=大按钮点选、30s 起点=ask 下发、ask_id 不匹配忽略、generic 经 identity_result 通知、metrics 三打点位）+ C6 复用 C2/C3 零新消息类型裁决；主理人要求提前冻结，晨会改为签字确认 |
| v1.1 | 2026-11-06 | C5 正文补 client_diag 的 vad 兜底激活语义：Day 4 全面核验发现 v1.0「仅日志」表述与 W4 实现（fcb5e41，stage=vad ok=false 驱动 VadFallbackController）存在字面冲突；本版在 ClientDiagMsg 块后补语义段（激活/停用/双跑边界），不改消息类型与字段，冻结人 A2，由 A4 代记并随联调窗口群内通告追认 |
