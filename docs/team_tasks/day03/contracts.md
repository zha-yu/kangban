# Day 3 模块间接口契约（代码级定义）

> 状态：C1 / C2 / C3 均已冻结　｜　版本：v1.4　｜　日期：2026-11-06
> 关联：docs/team_tasks_day03/README.md §3（契约清单与冻结人）
> 上游依据：day-03 计划 v1.5 §2.3（ASR 契约与置信度口径）｜adhoc-20261105 方案 v1.3 M1–M3｜接口契约_身份与权限.md E4
> 变更记录见文末

**使用规则**：本文件是 09:30 契约会的确认稿。确认后各冻结人将对应契约**原样拷贝**进自己模块顶部（C1→`app/audio/asr.py`，C2→`app/audio/signaling.py`，C3→`app/api/audio.py`），下游只准 import 契约类型，不得引用实现细节。确认后任何改动必须群通告 + 冻结人任务卡留痕。

---

## C1 · ASR/TTS 适配层契约（冻结人：A1，消费方：A2/A3/A5）

```python
from dataclasses import dataclass
from typing import Literal
from enum import Enum


@dataclass(frozen=True, slots=True)
class ASRResult:
    """一次语音识别的结果。"""

    text: str                    # 转写文本；partial 为中间态文本，final 为终稿
    is_final: bool               # True=终稿（句级），False=中间结果
    # 置信度：引擎提供时取 0.0~1.0；FunASR 协议层拿不到 → 恒为 None，不造 0.0 假值。
    # 纯观测指标（进 trace），不参与任何业务决策；云 API 接入后直接透传。
    confidence: float | None


class ASREventKind(str, Enum):
    PARTIAL = "partial"          # 中间结果（可驱动字幕预显，业务不依赖）
    FINAL = "final"              # 终稿：路由层/业务处理的唯一触发事件
    ERROR = "error"              # 携带 ASRError 子类，触发降级链路


@dataclass(frozen=True, slots=True)
class ASREvent:
    kind: ASREventKind
    result: ASRResult | None     # ERROR 时为 None
    error: "ASRError | None"     # 非 ERROR 时为 None


class ASRError(Exception):
    """ASR 错误基类。message 面向日志，不直接透出给长者。"""


class ASRUnavailable(ASRError):
    """服务不可达/超时/容器未启动 → 可退避重试（≤2 次，指数退避），重试耗尽走降级话术。"""


class ASRProtocolError(ASRError):
    """协议错帧/握手失败/响应非法 → 不可重试，直接降级话术并打 trace 标记。"""


class ASRProvider:
    """ASR 适配器协议。实现：FunASRAdapter / MockASRAdapter / CloudASRAdapter(骨架)。"""

    async def stream(
        self, audio: "AsyncIterator[bytes]"
    ) -> "AsyncIterator[ASREvent]":
        """输入 16kHz/16bit/单声道 60ms PCM 帧流，输出事件流。
        约束：不阻塞事件循环；连接级失败抛 ASRError 子类；identity 由调用方 runtime_context 注入，
        本协议不接受任何客户端传 elder_id。"""
        ...


class TTSProvider:
    """TTS 适配器协议。实现：EdgeTTSAdapter(整段合成，非流式) / CloudTTSAdapter(骨架)。"""

    async def synthesize(self, text: str) -> bytes:
        """输出 16kHz/16bit/单声道 PCM 整段音频（Day 3 非流式口径，首包预算受限已登记）。"""
        ...
```

**语义约定**：`FINAL` 是业务唯一触发点；`PARTIAL` 仅用于字幕预显。工厂按 `ASR_PROVIDER/TTS_PROVIDER` 分派（mock|funasr|cloud），Mock 用于 pytest 全链路。

## C2 · 信令 WS 消息契约（冻结人：A2，消费方：A3/A4）

传输：`WS /ws/signal`，文本帧 JSON，每消息一个对象。方向标记：C→S 客户端发，S→C 服务端发。

```python
from typing import TypedDict, Literal


class AuthMsg(TypedDict):          # C→S，连接后 300ms 内必须首发
    type: Literal["auth"]
    token: str                     # 设备会话 access_token（E4 签发）；token 不进 URL


class AuthOkMsg(TypedDict):        # S→C
    type: Literal["auth_ok"]
    elder_id: str | None           # 展示用；业务归属一律服务端裁决
    room_id: str


class AuthFailMsg(TypedDict):      # S→C 后立即 4401 关闭
    type: Literal["auth_fail"]
    code: Literal["timeout", "invalid", "expired"]


class OfferMsg(TypedDict):         # C→S，WebRTC 客户端发起
    type: Literal["offer"]
    sdp: str


class AnswerMsg(TypedDict):        # S→C
    type: Literal["answer"]
    sdp: str


class IceMsg(TypedDict):           # 双向
    type: Literal["ice"]
    candidate: str                 # ICE candidate JSON 字符串


class PingMsg(TypedDict):          # C→S，20s 周期
    type: Literal["ping"]

class PongMsg(TypedDict):          # S→C；45s 无任何帧 → 服务端判死清理
    type: Literal["pong"]


class AsrStartMsg(TypedDict):      # C→S，TEN-VAD speechstart（preRoll 帧随其后补发）
    type: Literal["asr_start"]

class AsrEndMsg(TypedDict):        # C→S，speechend（0.8s 静音）
    type: Literal["asr_end"]


class AsrPartialMsg(TypedDict):    # S→C，字幕预显
    type: Literal["asr_partial"]
    text: str

class AsrFinalMsg(TypedDict):      # S→C，终稿字幕
    type: Literal["asr_final"]
    text: str

class TtsTextMsg(TypedDict):       # S→C，大字幕渲染数字人回复（打断时前端立即停播）
    type: Literal["tts_text"]
    text: str

class ErrorMsg(TypedDict):         # S→C，服务端降级/异常通知
    type: Literal["error"]
    code: Literal["asr_unavailable", "llm_timeout", "tool_breaker", "internal"]
    message: str                   # 已脱敏的长者可读话术
```

**语义约定**：首帧必须 `auth`（300ms 超时 → 4401 + 审计）；`auth` 前的任何其他消息直接 4401；trace_id 在 auth 通过时绑定，全连接日志携带；WS 断开清理 ASR 会话/TTS 在途任务/帧缓冲（PeerConnection 由 A3 自行关闭）。

### C2 契约会裁决记录（2026-11-05，A2 冻结）

四个开放点的当会裁决（已冻结，后续改动走群通告 + 双方任务卡留痕）：

1. **心跳方向**：维持单向——客户端 20s 发 `ping`，服务端回 `pong`；45s 无**任何**帧（业务帧同样续命）判死。服务端不做主动探测；A4 若需下行活性检测再走契约变更补 S→C ping。
2. **auth 后重复发 auth**：按非法凭证处理 → `auth_fail code=invalid` + 4401 关闭（防连接内换 token 旁路）。
3. **同 device_id 二次连接**：踢旧留新——新连接接管注册表槽位，旧连接以自定义关闭码 4002 关闭；注册表操作加锁防竞态。
4. **非法/未知消息**：回 `error code=internal`（脱敏长者可读话术）**不关连接**，打 warn 日志（含 trace_id）；连续异常仅观测不熔断。

另：设备会话无 refresh 通道（E2 仅收人账号 refresh_token，已核实 `app/api/auth.py`）——WS 层不做设备 token 续期，临期/过期统一 `auth_fail code=expired`，客户端重走 E4 换证。结论已同步 A4 卡（a4-pad-frontend v1.2 交付物 7）。

## C3 · 降级 REST 契约（冻结人：A5，消费方：A4）

```
POST /api/audio/utterance
Content-Type: multipart/form-data
Authorization: Bearer <device access_token>   # E4 签发；elder_id 不由客户端传

  audio: file    # 16kHz/16bit/单声道 WAV（按住说话整段，含 preRoll）
  duration_ms: int（客户端计时，服务端仅入 trace 不信任）

200 → {"text": str, "reply": {"text": str, "audio_url": str | None}}
      # text=ASR 终稿；reply.text=回复话术（降级链路为模板话术）；
      # audio_url=服务端合成音频的本地可取地址；无 TTS 时为 null
错误 → 401（token 失效）｜413（超长音频，>30s 拒收）｜422（WAV 头非法/表单校验失败，`VALIDATION_ERROR`，a0-F-02 补记）｜503（ASR 不可用，body 携带降级话术）
```

**语义约定**：本接口是 WebRTC 主链路不可用时的降级形态，成功响应也必须保证长者端可渲染（不白屏）；幂等不要求（天然一次性）；限流复用 `rate_limit_per_minute`。

---

## 契约会确认记录

| 契约 | 冻结人 | 状态 | 备注 |
| --- | --- | --- | --- |
| C1 | A1 | **已冻结**（2026-11-05 09:30 前） | 已逐字拷入 `app/audio/asr.py` 行 39–92 并随 `a4cb857` 提交；A2/A3/A5 用 `from app.audio import build_asr, build_tts, ASREvent, ASREventKind, ASRResult` 消费。消费约定见 `app/audio/__init__.py` 模块 docstring |
| C2 | A2 | 已确认（v1.0 原样冻结） | 四开放点裁决见「C2 契约会裁决记录」；已拷入 app/audio/signaling.py 顶部 |
| C3 | A5 | 已冻结 | reply 形状定稿为 audio_url（非直返 PCM），TTS 决议落定后实现；类型已拷入 app/api/audio.py |


## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-05 | 初稿：C1/C2/C3 代码级定义，待契约会确认 |
| v1.1 | 2026-11-05 | C2 冻结：确认表状态更新 + 新增「C2 契约会裁决记录」（心跳单向 / 重复 auth 拒 / 踢旧留新 / 非法消息不关连接）+ 登记设备会话无 refresh 结论；C2 正文 TypedDict 未改动一字 |
| v1.2 | 2026-11-05 | C1 由 A1 冻结并落地（`app/audio/asr.py` 行 39–92，随 `a4cb857` 提交）；C2/C3 仍待各自冻结人确认 |
| v1.3 | 2026-11-05 | dev 合入 feature-zy 后状态对账：确认表内 C2（A2，v1.1 裁决四点）与 C3（A5，reply 定稿 audio_url）均已冻结，头部状态行据此同步；另修复合并残留——signaling.py 误留 A1 早期「仅消息类型」版 docstring 与重复 import，main.py 补回 `audio_api.router` 装配行（/api/audio 降级上传此前未挂载） |
| v1.4 | 2026-11-06 | C3 错误码补记 422：C3 冻结时未收录「非 WAV/表单校验失败」分支，a0 卡联调遗留 F-01/F-02 已实现（统一错误体 `{detail, code, trace_id}`、WAV 头非法 422 `VALIDATION_ERROR`，随 41e078c / a224579 合入）；Day 4 全面核验发现契约与实现不一致，本版只补错误码行，不改 C3 其他语义；C3 冻结人 A5，改动由 A4 代记并随联调窗口群内通告 A5 追认（契约使用规则「群通告 + 冻结人任务卡留痕」） |
