"""webrtcvad 服务端 VAD 兜底包装层。

**定位**：端侧 TEN-VAD WASM 加载失败时的唯一回落路径；主链路为端侧 VAD（A4 写作域），
本模块只做服务端兜底，不引入第二种 VAD 实现形态。

**前置校验的硬性理由**（AGENTS §8「失败分类」 + spikes/vad/spike_webrtcvad.py 验证）：
webrtcvad 原生非法帧长抛的是 **`_webrtcvad.Error`**（C 扩展类），不是 ValueError，
标准 `except ValueError` 兜不住，会直接炸到 ASGI 层返回 500。因此本模块的每个帧必须
先过 `valid_rate_and_frame_length` 前置校验，不合法就打 warning 跳过——绝不能让原生
异常冒泡。

**帧常量**（全局统一，其他模块引用此模块而不是各自硬编码）：
- SAMPLE_RATE = 16_000（全语音链路硬性 16k，PRD §2.2）
- FRAME_MS = 30（webrtcvad 固定帧长选 10/20/30ms；30ms 是官方推荐的音频级）
- FRAME_SAMPLES = 480（16000 * 30 / 1000）
- PRE_ROLL_MS = 600（句首保护，与 adhoc 方案 M3 §4.2 对齐）
- PRE_ROLL_FRAMES = PRE_ROLL_MS // FRAME_MS = 20
- PRE_ROLL_BYTES = PRE_ROLL_FRAMES * FRAME_SAMPLES * 2（16bit PCM16）
- MAX_SPEECH_MS = 30_000（单轮最长 30s，防占资源；超过强制切段，与 adhoc 方案 §4.1 对齐）

**判定阈值**（会话级可配置）：
- min_speech_frames = 3 → 连续 3 帧判定为 speechstart（90ms 说话）
- silence_frames = 27 → 连续 27 帧判定为 speechend（810ms 静音，与 0.8s 决策门限对齐）

**依赖**：webrtcvad-wheels（import 名仍是 webrtcvad，API 与原版完全一致，
pyproject.toml 已钉 `>=2.0.14`）。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

import webrtcvad

from app.core.context import get_trace_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 帧常量（全模块引用，禁止各自硬编码）
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16_000  # 硬性 16k，全语音链路统一
FRAME_MS = 30  # webrtcvad 选 30ms（次选 20ms，不支持其他）
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480 samples = 30ms @ 16k
PRE_ROLL_MS = 600  # 句首保护，防端侧 VAD speechstart 延迟
PRE_ROLL_FRAMES = PRE_ROLL_MS // FRAME_MS  # 20 frames
PRE_ROLL_BYTES = PRE_ROLL_FRAMES * FRAME_SAMPLES * 2  # 9600 bytes（PCM16）
MAX_SPEECH_MS = 30_000  # 30s 强制切段，防长录音占资源


# ---------------------------------------------------------------------------
# VadSession · 单轮语音切段器
# ---------------------------------------------------------------------------


@dataclass
class VadSession:
    """webrtcvad 单轮语音切段器。

    生命周期：
        session = VadSession(aggressiveness=3)
        session.feed_frame(frame_bytes_30ms)   # 逐帧喂入
        ...
        speech_segment = session.flush()        # 结束会话，返回累计语音段（含 preRoll）

    设计要点：
    - preRoll 环形缓冲：检测到 speechstart 时先把之前 20 帧（600ms）吐出，保证句首完整；
    - 静音/说话双状态机：连续 N 帧 speech → 开始；连续 M 帧静音 → 结束；
    - 非法帧长 → warning + skip，绝不让 `_webrtcvad.Error` 冒泡。
    """

    # adhoc-20261106：2→3（0~3 档里最强的抗噪判定），配合端侧 TEN-VAD threshold 0.6
    # 收窄收音范围，减少远处他人语音被误切段送进 ASR。
    aggressiveness: int = 3
    min_speech_frames: int = 3  # 连续 3 帧 → speechstart
    silence_frames: int = 27  # 连续 27 帧 → speechend（810ms 静音）

    # 内部状态（dataclass 自动初始化）
    _vad: webrtcvad.Vad | None = field(init=False, repr=False)
    _pre_roll: deque[bytes] = field(init=False, repr=False)
    _speech_buffer: list[bytes] = field(init=False, repr=False)
    _speech_count: int = field(init=False, default=0)
    _silence_count: int = field(init=False, default=0)
    _in_speech: bool = field(init=False, default=False)
    _total_frames: int = field(init=False, default=0)
    _illegal_count: int = field(init=False, default=0)  # 连续非法帧计数，防单帧 skip 时日志爆炸

    def __post_init__(self) -> None:
        """初始化 webrtcvad 实例 + preRoll 环形缓冲。"""
        self._vad = webrtcvad.Vad(self.aggressiveness)
        self._pre_roll = deque(maxlen=PRE_ROLL_FRAMES)
        self._speech_buffer = []

    # ------------------------------------------------------------------
    # 公共接口（其他模块唯一应该调的方法）
    # ------------------------------------------------------------------

    @property
    def is_speech(self) -> bool:
        """当前是否在说话（供查询状态条显示"正在听"等）。"""
        return self._in_speech

    def feed_frame(self, frame: bytes) -> bytes | None:
        """喂入 30ms / 16k / PCM16 帧。

        返回值：
            bytes  检测到 speechend，**先吐完一段完整语音**（含 preRoll）+ 当前结束
            None   还在缓冲中（preRoll 里 / 说话中 / 静音中都不吐）

        非法帧长：打 warning + skip，不抛异常（见模块头注释的原因）。
        """
        # --- 1. 前置校验（防 _webrtcvad.Error 冒泡）---
        frame_samples = len(frame) // 2  # PCM16 = 2 bytes / sample
        if not webrtcvad.valid_rate_and_frame_length(SAMPLE_RATE, frame_samples):
            self._illegal_count += 1
            # 每 5 帧（150ms）打一条 warning，避免连续丢包时日志爆炸
            if self._illegal_count % 5 == 1:
                trace_id = get_trace_id() or "-"
                logger.warning(
                    "vad: illegal frames detected (len=%d, consecutive=%d, trace_id=%s)",
                    len(frame),
                    self._illegal_count,
                    trace_id,
                )
            return None
        # 合法帧出现，清零连续计数器
        self._illegal_count = 0

        self._total_frames += 1

        # --- 2. 强制切段（30s 封顶）---
        if self._total_frames * FRAME_MS >= MAX_SPEECH_MS and self._in_speech:
            trace_id = get_trace_id() or "-"
            logger.info("vad: max speech duration reached, force cut (trace_id=%s)", trace_id)
            return self._finalize_speech_segment()

        # --- 3. 喂 preRoll 缓冲（无论判定是否说话，先入队）---
        self._pre_roll.append(frame)

        # --- 4. 喂 VAD 判定 ---
        assert self._vad is not None
        frame_is_speech = bool(self._vad.is_speech(frame, SAMPLE_RATE))

        if frame_is_speech:
            self._silence_count = 0
            self._speech_count += 1

            if not self._in_speech and self._speech_count >= self.min_speech_frames:
                # → speechstart：把 preRoll 先吐出来保句首
                self._in_speech = True
                self._speech_buffer.extend(self._pre_roll)
                self._speech_buffer.append(frame)
            elif self._in_speech:
                self._speech_buffer.append(frame)
            # 还没到 min_speech_frames：继续积累 preRoll，不进 buffer

        else:
            self._speech_count = 0
            self._silence_count += 1

            if self._in_speech and self._silence_count >= self.silence_frames:
                # → speechend：吐完整段语音
                return self._finalize_speech_segment()
            # 还没在说话：preRoll 已经由 deque 自动维护滑动窗口

        return None

    def flush(self) -> bytes | None:
        """强制结束当前会话。

        若还在说话 → 吐剩余语音段；若还没判定开始 → 返回 None；状态立即清零。
        A3 的 close / A2 的断线清理都应该调这个，**绝不能直接丢 VadSession 实例**
        （preRoll 缓冲里可能还有最后 600ms 没处理的帧）。
        """
        if self._in_speech:
            return self._finalize_speech_segment()
        self.reset()
        return None

    def reset(self) -> None:
        """硬重置所有状态（不等 flush 就清空 buffer）。

        慎用：reset 会丢掉所有未吐的 preRoll 和 speech buffer。只有断线重建、
        或明确想放弃剩余语音时才调；正常结束应该走 `flush()`。
        """
        self._pre_roll.clear()
        self._speech_buffer = []
        self._speech_count = 0
        self._silence_count = 0
        self._in_speech = False
        self._total_frames = 0
        self._illegal_count = 0

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _finalize_speech_segment(self) -> bytes | None:
        """吐完当前累积的语音段（含 preRoll），状态清零，返回完整 PCM16 bytes。"""
        if not self._speech_buffer:
            # 防御性：理论上不会走到这，但保险起见
            self.reset()
            return None

        segment = b"".join(self._speech_buffer)
        self.reset()
        trace_id = get_trace_id() or "-"
        logger.debug(
            "vad: speech segment finalized (bytes=%d, frames=%d, trace_id=%s)",
            len(segment),
            len(segment) // (FRAME_SAMPLES * 2),
            trace_id,
        )
        return segment


# ---------------------------------------------------------------------------
# VadFallbackController · 服务端 VAD 兜底控制器（连接级）
# ---------------------------------------------------------------------------


class VadFallbackController:
    """端侧 TEN-VAD 失效时的服务端兜底开关（W4，连接级持有）。

    激活语义（S4 自裁项，随代码同提交留痕）：
    - 以 `client_diag{stage: "vad", ok: false}`（C5 契约消息）为**唯一显式激活信号**；
    - `ok=true` 或连接关闭即停用；
    - 未收到 diag 时兜底不启用——不与端侧 VAD 双跑，避免双切段产生重复 utterance。

    生命周期：
        controller = VadFallbackController(connection_id, trace_id)
        controller.set_active(True)            # diag ok=false 驱动
        segment = controller.feed(frame)       # 逐 30ms 帧喂入；speechend 时吐完整段
        controller.close()                     # 连接关闭：flush 残留，防 preRoll 丢失

    产出段只打 `vad_fallback_segment` trace 日志（段字节数/时长/trace_id），
    不落盘（AGENTS §4.10：不落未经脱敏的原始音频）。
    """

    def __init__(self, connection_id: str, trace_id: str | None = None) -> None:
        self.connection_id = connection_id
        self.trace_id = trace_id or get_trace_id() or "-"
        self._session: VadSession | None = None
        self._active = False
        self._segments_emitted = 0

    @property
    def is_active(self) -> bool:
        """兜底是否处于激活态（供诊断查询）。"""
        return self._active

    @property
    def segments_emitted(self) -> int:
        """本连接累计产出的兜底语音段数（供观测）。"""
        return self._segments_emitted

    def set_active(self, active: bool) -> None:
        """切换兜底开关。

        激活：新建干净 VadSession（连接级复用，跨 utterance 状态自续）；
        停用：flush 并丢弃残留段——端侧 VAD 恢复后由它接管，服务端残留
        （可能只有半句）继续产出会与端侧切段冲突，宁可丢弃。
        """
        if active == self._active:
            return
        self._active = active
        if active:
            self._session = VadSession()
        else:
            if self._session is not None:
                # 丢弃返回值：停用语义 = 放弃服务端残留，见上注释
                self._session.flush()
            self._session = None
        logger.info(
            "vad fallback switched (active=%s, connection_id=%s, trace_id=%s)",
            self._active,
            self.connection_id,
            self.trace_id,
        )

    def feed(self, frame: bytes) -> bytes | None:
        """喂入 30ms / 16k / PCM16 帧。

        未激活时恒返回 None（主链路零影响）；激活后委托 VadSession，
        speechend 时吐完整语音段（含 preRoll）并打 `vad_fallback_segment` 日志。
        """
        if not self._active or self._session is None:
            return None
        segment = self._session.feed_frame(frame)
        if segment is None:
            return None
        return self._emit(segment)

    def close(self) -> bytes | None:
        """连接关闭兜底收口：flush 残留段（若还在说话中则产出末段）。

        对齐 VadSession.flush 契约：绝不能直接丢实例（preRoll 里可能有
        最后 600ms 未处理的帧）。关闭后控制器不可再复用（重新激活会新建会话）。
        """
        if self._session is None:
            self._active = False
            return None
        segment = self._session.flush()
        self._session = None
        self._active = False
        if segment is not None:
            return self._emit(segment)
        return None

    def _emit(self, segment: bytes) -> bytes:
        """产出段统一出口：计数 + trace 日志（不落盘），原样返回给下游。"""
        self._segments_emitted += 1
        duration_ms = len(segment) * 1000 // (SAMPLE_RATE * 2)
        logger.info(
            "vad_fallback_segment (bytes=%d, duration_ms=%d, segments=%d, "
            "connection_id=%s, trace_id=%s)",
            len(segment),
            duration_ms,
            self._segments_emitted,
            self.connection_id,
            self.trace_id,
        )
        return segment
