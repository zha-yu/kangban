"""A3 · WebRTC 媒体层骨架（Day 3 M1/M2 的无依赖部分）。

本文件当前刻意**只实现不依赖 C1/C2 的骨架**：

- `MediaConfig`：只读 `app.core.config.Settings` 的真实字段，构造 aiortc ICE 配置；
- `MediaState` / `MediaFailureReason` / 媒体层异常：给后续 M3–M5 复用；
- `MediaSession` + `MediaSessionRegistry`：`connection_id` 级 registry、running loop 捕获、
  任务登记、幂等关闭、`active_sessions` / `pending_tasks` 归零；
- 音频纯工具：PyAV 有状态重采样、20 ms 规范帧、60/30 ms 双缓冲、600 ms preRoll、
  TTS 下行 PCM 的格式校验。

**尚未实现（等 C1/C2 冻结后再接）**：`handle_offer`、`begin_turn` / `end_turn`、
`enqueue_tts_pcm`、`barge_in` 等公开接口，以及任何 `ASRProvider` / `ASREvent` / WS 消息类型。
本文件不 import `signaling.py` / `asr.py`，避免把猜测接口写成既成事实。

约束（与 A3 任务卡一致）：

- 不写库、不发 WebSocket、不切 REST、不写业务回复；
- 只读真实 Settings，引用不存在的 `WEBRTC_ICE_SERVERS` 即错；
- localhost 只用 host candidate，不依赖未启动的 coturn；
- 日志只带 `trace_id / connection_id / turn_id / state / reason`，不打印 token、SDP、
  完整 ICE candidate 或原始音频。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Any

import av
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from app.audio.asr import ASRError, ASREvent, ASREventKind, ASRProvider, build_asr
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 音频常数
# 16 bit / mono / 16 kHz 是 A3 内部唯一规范格式；所有帧长都由它推导。
TARGET_SAMPLE_RATE = 16_000
SAMPLE_WIDTH_BYTES = 2

CANONICAL_FRAME_MS = 20
CANONICAL_FRAME_SAMPLES = 320
CANONICAL_FRAME_BYTES = 640

ASR_FRAME_MS = 60
ASR_FRAME_SAMPLES = 960
ASR_FRAME_BYTES = 1_920

VAD_FRAME_MS = 30
VAD_FRAME_SAMPLES = 480
VAD_FRAME_BYTES = 960

PRE_ROLL_MS = 600
PRE_ROLL_FRAMES = PRE_ROLL_MS // ASR_FRAME_MS  # 10 个 60 ms 帧

# 下行占位：answer 必须带音频 m-line；TTS 写轨在 M4 接入前先输出静音。
DOWNLINK_SAMPLE_RATE = 48_000
DOWNLINK_FRAME_MS = 20
DOWNLINK_FRAME_SAMPLES = 960

# 一个 turn 最多缓存 500 个 60 ms 帧（30 秒语音），超出即背压并记录丢弃。
MAX_ASR_QUEUE_FRAMES = 500

_LOOPBACK_ICE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_ICE_URL_SCHEMES = frozenset({"stun", "turn", "turns"})

AsrEventCallback = Callable[[str, ASREvent], Awaitable[None]]


# ------------------------------------------------------------------ 状态与异常


class MediaState(StrEnum):
    """PeerConnection / 媒体会话状态（对齐 A3 执行计划 §3.4）。"""

    OPEN = "open"
    OFFER_RECEIVED = "offer_received"
    ANSWER_CREATED = "answer_created"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class MediaFailureReason(StrEnum):
    """结构化失败原因；只表达媒体层事实，不发明 C2 未冻结的 WS 错误码。"""

    ICE_FAILED = "ice_failed"
    ICE_TIMEOUT = "ice_timeout"
    TRACK_ENDED = "track_ended"
    FORMAT_ERROR = "format_error"
    BACKPRESSURE = "backpressure"
    CLOSED = "closed"
    LOOP_MISMATCH = "loop_mismatch"
    CONFIG_ERROR = "config_error"
    STATE_ERROR = "state_error"
    TASK_EXCEPTION = "task_exception"


class MediaError(Exception):
    """媒体层错误基类：只表达「这次媒体操作被拒」，不构造 HTTP/WS 响应。"""


class MediaLoopMismatch(MediaError):
    """在非创建时的 running loop 上调用媒体对象。

    aiortc 内部任务绑定创建时的 loop；跨 loop 调用不会「凑合能跑」，而是明确失败，
    否则表现是任务永不执行、PC 状态卡死。
    """


class MediaClosedError(MediaError):
    """会话已关闭或正在关闭，后续操作被拒。"""


class MediaStateError(MediaError):
    """状态不允许该操作（如重复创建 PC、重复 connection_id）。"""


class MediaFormatError(MediaError):
    """PCM / 帧格式不满足契约（奇数长度、WAV 头、非 16k mono s16 等）。"""


class MediaConfigError(MediaError):
    """媒体配置不合法（只含数值边界，不回显凭据）。"""


# ------------------------------------------------------------------ 结构化日志


def _log_event(
    event: str,
    *,
    trace_id: str,
    connection_id: str,
    turn_id: str | None,
    state: str,
    reason: str | None,
    **extra: object,
) -> None:
    """固定五个媒体日志字段，额外字段只允许 ID / 计数 / 状态，不放音频与凭据。"""
    fields: dict[str, object] = {
        "trace_id": trace_id or "-",
        "connection_id": connection_id,
        "turn_id": turn_id,
        "state": state,
        "reason": reason,
    }
    fields.update(extra)
    logger.info(event, extra=fields)


# ------------------------------------------------------------------ 配置


def _ice_url_host(url: str) -> str:
    """从 `stun:host:port` / `turn:host:port` 取 host；解析不了返回空串。

    不用 `urlsplit`：ICE URL 的 scheme 后用冒号而不是 `//`，标准 URL 解析器拿不到 host。
    """
    scheme, separator, remainder = url.partition(":")
    if not separator or scheme.lower() not in _ICE_URL_SCHEMES:
        return ""
    host_part = remainder.lstrip("/")
    if host_part.startswith("["):
        end = host_part.find("]")
        return host_part[1:end].lower() if end > 0 else ""
    return host_part.split(":", 1)[0].strip().lower()


def _is_loopback_ice_url(url: str) -> bool:
    """localhost / 127.0.0.1 / ::1 的 STUN/TURN 不加入配置。

    本机验收靠 aiortc 默认的 host candidate；把未启动的 `stun:localhost:3478` 塞进去
    只会让 ICE 等超时。非本机 URL 才表示真正的 STUN/TURN 依赖。
    """
    host = _ice_url_host(url)
    return not host or host in _LOOPBACK_ICE_HOSTS


@dataclass(frozen=True, slots=True)
class MediaConfig:
    """媒体层实际使用的 Settings 投影；只读，不新增配置字段。"""

    stun_url: str
    turn_url: str
    turn_username: str
    turn_credential: str
    turn_credential_ttl_seconds: int
    ice_timeout_seconds: int
    audio_bitrate_kbps: int
    vad_aggressiveness: int

    def __post_init__(self) -> None:
        if self.ice_timeout_seconds <= 0:
            raise MediaConfigError("webrtc_ice_timeout_seconds must be positive")
        if self.audio_bitrate_kbps <= 0:
            raise MediaConfigError("webrtc_audio_bitrate_kbps must be positive")
        if not 0 <= self.vad_aggressiveness <= 3:
            raise MediaConfigError("webrtc_vad_aggressiveness must be in 0..3")
        if self.turn_credential_ttl_seconds < 0:
            raise MediaConfigError("turn_credential_ttl_seconds must not be negative")

    @classmethod
    def from_settings(cls, settings: Settings) -> MediaConfig:
        """按真实字段名读取；**不存在 `WEBRTC_ICE_SERVERS`**。"""
        return cls(
            stun_url=settings.stun_url,
            turn_url=settings.turn_url,
            turn_username=settings.turn_username,
            turn_credential=settings.turn_credential,
            turn_credential_ttl_seconds=settings.turn_credential_ttl_seconds,
            ice_timeout_seconds=settings.webrtc_ice_timeout_seconds,
            audio_bitrate_kbps=settings.webrtc_audio_bitrate_kbps,
            vad_aggressiveness=settings.webrtc_vad_aggressiveness,
        )

    def ice_servers(self) -> list[RTCIceServer]:
        """构造 aiortc ICE server 列表。

        - STUN：URL 非空且不是 loopback 才加入；
        - TURN：URL / 用户名 / 凭据 / TTL 全部非空且不是 loopback 才加入。
        """
        servers: list[RTCIceServer] = []
        if self.stun_url.strip() and not _is_loopback_ice_url(self.stun_url):
            servers.append(RTCIceServer(urls=[self.stun_url]))
        if (
            self.turn_url.strip()
            and self.turn_username.strip()
            and self.turn_credential.strip()
            and self.turn_credential_ttl_seconds > 0
            and not _is_loopback_ice_url(self.turn_url)
        ):
            servers.append(
                RTCIceServer(
                    urls=[self.turn_url],
                    username=self.turn_username,
                    credential=self.turn_credential,
                )
            )
        return servers


# ------------------------------------------------------------------ 音频纯工具


@dataclass(frozen=True, slots=True)
class PcmFrame:
    """一段 PCM16 little-endian 帧；`data` 是原始字节，不含容器头。"""

    data: bytes
    sample_rate: int = TARGET_SAMPLE_RATE
    channels: int = 1
    sample_width: int = SAMPLE_WIDTH_BYTES

    @property
    def sample_count(self) -> int:
        return len(self.data) // (self.sample_width * self.channels)

    @property
    def duration_ms(self) -> float:
        return self.sample_count * 1000.0 / self.sample_rate


def _make_frame(data: bytes, *, duration_ms: int) -> PcmFrame:
    expected = int(TARGET_SAMPLE_RATE * duration_ms / 1000) * SAMPLE_WIDTH_BYTES
    if len(data) != expected:
        raise MediaFormatError(
            f"expected {expected} bytes for {duration_ms} ms mono s16, got {len(data)}"
        )
    return PcmFrame(data=data)


class PcmFramer:
    """把任意长度的 PCM16 字节流聚合成固定长度帧；尾片可零填充。"""

    def __init__(
        self,
        *,
        frame_bytes: int = CANONICAL_FRAME_BYTES,
        duration_ms: int = CANONICAL_FRAME_MS,
    ) -> None:
        if frame_bytes <= 0 or frame_bytes % SAMPLE_WIDTH_BYTES != 0:
            raise ValueError("frame_bytes must be a positive multiple of 2")
        self._frame_bytes = frame_bytes
        self._duration_ms = duration_ms
        self._buffer = bytearray()

    def feed(self, pcm: bytes) -> list[PcmFrame]:
        if len(pcm) % SAMPLE_WIDTH_BYTES != 0:
            raise MediaFormatError("PCM16 data must have even length")
        self._buffer.extend(pcm)
        frames: list[PcmFrame] = []
        while len(self._buffer) >= self._frame_bytes:
            chunk = bytes(self._buffer[: self._frame_bytes])
            del self._buffer[: self._frame_bytes]
            frames.append(_make_frame(chunk, duration_ms=self._duration_ms))
        return frames

    def flush(self) -> PcmFrame | None:
        """把不足一帧的尾片零填充后发出；没有尾片返回 None。"""
        if not self._buffer:
            return None
        padding = self._frame_bytes - len(self._buffer)
        data = bytes(self._buffer) + b"\x00" * padding
        self._buffer.clear()
        return _make_frame(data, duration_ms=self._duration_ms)

    def reset(self) -> None:
        self._buffer.clear()


class AudioNormalizer:
    """aiortc `AudioFrame` → 16k/16bit/mono 的 20 ms 规范帧。

    PyAV 的 `AudioResampler` 是**有状态**的：输入格式变化时重建；输出样本先进入
    `PcmFramer` 再切帧。禁止 `[::3]`、`np.repeat` 等抽点/插值替代重采样。
    """

    def __init__(self) -> None:
        self._resampler: av.AudioResampler | None = None
        self._input_signature: tuple[str, str, int] | None = None
        self._framer = PcmFramer()

    def feed(self, frame: av.AudioFrame) -> list[PcmFrame]:
        signature = self._frame_signature(frame)
        if self._resampler is None or signature != self._input_signature:
            self._resampler = av.AudioResampler(
                format="s16", layout="mono", rate=TARGET_SAMPLE_RATE
            )
            self._input_signature = signature

        resampler = self._resampler
        if resampler is None:  # pragma: no cover - 上面的分支已保证
            raise MediaFormatError("audio resampler is not initialised")

        frames: list[PcmFrame] = []
        for output in resampler.resample(frame):
            frames.extend(self._framer.feed(_audio_frame_bytes(output)))
        return frames

    def flush(self) -> list[PcmFrame]:
        """结束输入：先冲 resampler 内部延迟，再零填充 20 ms 尾帧。"""
        frames: list[PcmFrame] = []
        if self._resampler is not None:
            for output in self._resampler.resample(None):
                frames.extend(self._framer.feed(_audio_frame_bytes(output)))
        tail = self._framer.flush()
        if tail is not None:
            frames.append(tail)
        self.reset()
        return frames

    def reset(self) -> None:
        self._resampler = None
        self._input_signature = None
        self._framer.reset()

    @staticmethod
    def _frame_signature(frame: av.AudioFrame) -> tuple[str, str, int]:
        fmt = frame.format.name if frame.format is not None else ""
        layout = frame.layout.name if frame.layout is not None else ""
        return str(fmt or ""), str(layout or ""), int(frame.sample_rate)


def _audio_frame_bytes(frame: av.AudioFrame) -> bytes:
    """从重采样后的 s16/mono 帧取 PCM 字节。

    `s16` 是 packed 单平面；多平面说明重采样目标不对，直接报格式错误，不做静默拼接。
    """
    if len(frame.planes) != 1:
        raise MediaFormatError("resampled audio frame must have exactly one plane")
    # AudioPlane 的底层 buffer 可能带对齐填充；必须按 samples 截断，不能整块当成 PCM。
    expected = frame.samples * SAMPLE_WIDTH_BYTES
    raw = bytes(frame.planes[0])
    if len(raw) < expected:
        raise MediaFormatError("resampled audio plane is shorter than its sample count")
    data = raw[:expected]
    if len(data) % SAMPLE_WIDTH_BYTES != 0:
        raise MediaFormatError("resampled PCM16 data must have even length")
    return data


class DualFrameSplitter:
    """同一 20 ms 规范流分叉成 60 ms ASR 帧与 30 ms VAD 帧。

    只做字节聚合，不二次重采样：3 个 20 ms 帧 = 1 个 60 ms ≈ 2 个 30 ms 帧。
    """

    def __init__(self) -> None:
        self._asr_buffer = bytearray()
        self._vad_buffer = bytearray()

    def feed(self, frame: PcmFrame) -> tuple[list[PcmFrame], list[PcmFrame]]:
        self._validate_source(frame)
        self._asr_buffer.extend(frame.data)
        self._vad_buffer.extend(frame.data)
        return (
            self._drain(self._asr_buffer, ASR_FRAME_BYTES, ASR_FRAME_MS),
            self._drain(self._vad_buffer, VAD_FRAME_BYTES, VAD_FRAME_MS),
        )

    def flush(self) -> tuple[list[PcmFrame], list[PcmFrame]]:
        """结束 turn：两个缓冲的尾片都零填充成整帧，最多增加 59 ms 静音。"""
        return (
            self._flush(self._asr_buffer, ASR_FRAME_BYTES, ASR_FRAME_MS),
            self._flush(self._vad_buffer, VAD_FRAME_BYTES, VAD_FRAME_MS),
        )

    def discard(self) -> None:
        """barge-in：丢弃未发送残留，不补零填充。"""
        self.reset()

    def reset(self) -> None:
        """开始新 turn 前清空两个缓冲。"""
        self._asr_buffer.clear()
        self._vad_buffer.clear()

    @staticmethod
    def _validate_source(frame: PcmFrame) -> None:
        if (
            frame.sample_rate != TARGET_SAMPLE_RATE
            or frame.channels != 1
            or frame.sample_width != SAMPLE_WIDTH_BYTES
        ):
            raise MediaFormatError("splitter input must be 16 kHz/16 bit/mono PCM")

    @staticmethod
    def _drain(buffer: bytearray, frame_bytes: int, duration_ms: int) -> list[PcmFrame]:
        frames: list[PcmFrame] = []
        while len(buffer) >= frame_bytes:
            chunk = bytes(buffer[:frame_bytes])
            del buffer[:frame_bytes]
            frames.append(_make_frame(chunk, duration_ms=duration_ms))
        return frames

    @classmethod
    def _flush(cls, buffer: bytearray, frame_bytes: int, duration_ms: int) -> list[PcmFrame]:
        frames = cls._drain(buffer, frame_bytes, duration_ms)
        if buffer:
            padding = frame_bytes - len(buffer)
            data = bytes(buffer) + b"\x00" * padding
            buffer.clear()
            frames.append(_make_frame(data, duration_ms=duration_ms))
        return frames


class PreRollBuffer:
    """600 ms preRoll：只保存最近 10 个 60 ms ASR 帧。"""

    def __init__(self, capacity_frames: int = PRE_ROLL_FRAMES) -> None:
        if capacity_frames <= 0:
            raise ValueError("capacity_frames must be positive")
        self._frames: deque[PcmFrame] = deque(maxlen=capacity_frames)

    def append(self, frame: PcmFrame) -> None:
        if (
            frame.sample_rate != TARGET_SAMPLE_RATE
            or frame.channels != 1
            or frame.sample_width != SAMPLE_WIDTH_BYTES
            or frame.sample_count != ASR_FRAME_SAMPLES
        ):
            raise MediaFormatError("pre-roll only accepts 60 ms 16 kHz mono s16 frames")
        self._frames.append(frame)

    def recent(self) -> tuple[PcmFrame, ...]:
        return tuple(self._frames)

    def clear(self) -> None:
        self._frames.clear()

    @property
    def duration_ms(self) -> float:
        return sum(frame.duration_ms for frame in self._frames)


def validate_downlink_pcm(pcm: bytes) -> None:
    """校验 TTS 下行原始 PCM：偶数长度、无 RIFF/WAVE 容器头、非空。

    采样率由 C1 契约保证为 16 kHz；raw PCM 本身无法从字节反推采样率，不做猜测。
    """
    if not pcm:
        raise MediaFormatError("downlink PCM must not be empty")
    if len(pcm) % SAMPLE_WIDTH_BYTES != 0:
        raise MediaFormatError("downlink PCM must have even length")
    if pcm[:4] in (b"RIFF", b"WAVE"):
        raise MediaFormatError("downlink PCM must not contain a RIFF/WAVE header")


# ------------------------------------------------------------------ 下行占位轨与 ASR 帧迭代


class SilentAudioTrack(MediaStreamTrack):
    """下行静音占位音轨。

    `answer` 必须带音频 m-line，浏览器才会接受 `sendrecv`；TTS 写轨尚未接入前先输出
    48 kHz/20 ms/960 sample 静音。M4 接入 TTS 后由真实 PCM 队列替换本类。
    """

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._pts = 0

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        # 按实时节奏出帧，避免 aiortc 发送器空转占满 CPU。
        await asyncio.sleep(DOWNLINK_FRAME_MS / 1000)
        if self.readyState != "live":
            raise MediaStreamError
        frame = av.AudioFrame(format="s16", layout="mono", samples=DOWNLINK_FRAME_SAMPLES)
        frame.sample_rate = DOWNLINK_SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, DOWNLINK_SAMPLE_RATE)
        self._pts += DOWNLINK_FRAME_SAMPLES
        return frame


async def _asr_frame_iterator(queue: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
    """把会话队列暴露成 C1 `ASRProvider.stream(audio)` 需要的异步迭代器。"""
    while True:
        item = await queue.get()
        if item is None:
            return
        yield item


# ------------------------------------------------------------------ 会话与 registry


class MediaSession:
    """一个 `connection_id` 的媒体会话。

    已接：running loop 捕获、PC 创建、offer/answer、收轨重采样分帧、600 ms preRoll、
    `begin_turn` 启动 ASR、ASR 事件回调、幂等关闭与资源观测。
    未接：ICE candidate 的显式收发（aiortc 1.15 先把本地 candidate 写进 answer SDP）、
    `end_turn` flush、TTS PCM 写轨与 `barge_in` —— 等 A2/A4 联调与 M4/M5 再接。
    """

    def __init__(
        self,
        connection_id: str,
        trace_id: str,
        config: MediaConfig,
        *,
        asr_provider: ASRProvider | None = None,
        on_asr_event: AsrEventCallback | None = None,
    ) -> None:
        if not connection_id:
            raise ValueError("connection_id must not be empty")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise MediaLoopMismatch("MediaSession must be created inside a running loop") from exc

        self.connection_id = connection_id
        self.trace_id = trace_id or "-"
        self.config = config
        self._loop = loop
        self._state = MediaState.OPEN
        self._turn_id: str | None = None
        self._pc: RTCPeerConnection | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._asr_provider = asr_provider
        self._on_asr_event = on_asr_event
        self._pre_roll = PreRollBuffer()
        self._splitter = DualFrameSplitter()
        self._normalizer = AudioNormalizer()
        self._asr_queue: asyncio.Queue[bytes | None] | None = None
        self._asr_task: asyncio.Task[None] | None = None
        self._audio_track: MediaStreamTrack | None = None
        self._playback_track: SilentAudioTrack | None = None
        self._offer_seen = False
        self._asr_frames_total = 0
        self._vad_frames_total = 0
        self._asr_dropped_frames = 0

    @property
    def state(self) -> MediaState:
        return self._state

    @property
    def turn_id(self) -> str | None:
        return self._turn_id

    @property
    def pending_tasks(self) -> int:
        return len(self._tasks)

    @property
    def peer_connection(self) -> RTCPeerConnection | None:
        return self._pc

    @property
    def asr_frames_received(self) -> int:
        return self._asr_frames_total

    @property
    def vad_frames_received(self) -> int:
        return self._vad_frames_total

    @property
    def asr_frames_dropped(self) -> int:
        return self._asr_dropped_frames

    def create_peer_connection(self) -> RTCPeerConnection:
        """创建 aiortc PC；一个连接只允许一个，重复创建是状态错误。"""
        self._ensure_loop()
        self._ensure_open()
        if self._pc is not None:
            raise MediaStateError("peer connection already exists for this connection")

        servers = self.config.ice_servers()
        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        # 先挂下行占位轨：浏览器 offer 为 sendrecv 时，answer 才会回传音频 m-line。
        self._playback_track = SilentAudioTrack()
        self._pc.addTrack(self._playback_track)
        self._install_handlers(self._pc)
        _log_event(
            "peer connection created",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason="peer_connection_created",
            ice_server_count=len(servers),
            host_only=not servers,
        )
        return self._pc

    def _install_handlers(self, pc: RTCPeerConnection) -> None:
        """挂 track / connectionstate 处理器；不直接发 WS、不写业务逻辑。"""

        @pc.on("track")
        def _on_track(track: MediaStreamTrack) -> None:
            if track.kind != "audio":
                track.stop()
                return
            if self._state in (MediaState.CLOSING, MediaState.CLOSED):
                track.stop()
                return
            self._audio_track = track
            self.spawn(self._consume_track(track))

        @pc.on("connectionstatechange")
        async def _on_connection_state_change() -> None:
            self._sync_connection_state(pc.connectionState)

    def _sync_connection_state(self, connection_state: str) -> None:
        if connection_state == "connected":
            self._state = MediaState.CONNECTED
        elif connection_state == "failed":
            self._state = MediaState.FAILED
        # 不把 "closed" 直接写成 CLOSED：aiortc 可能自行关闭连接，A2 仍需调用
        # close 才能取消任务/清缓冲；close 自己负责把状态落到 CLOSED。
        _log_event(
            "peer connection state changed",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason="connection_state",
            connection_state=connection_state,
        )

    async def handle_offer(self, sdp: str) -> str:
        """A2 传入浏览器 offer；返回 answer SDP。重复 offer 直接拒绝（D4）。"""
        self._ensure_loop()
        self._ensure_open()
        if not isinstance(sdp, str) or not sdp.strip():
            raise MediaFormatError("offer SDP must not be empty")
        if self._offer_seen:
            raise MediaStateError("duplicate offer for this connection")

        pc = self._pc
        if pc is None:
            pc = self.create_peer_connection()

        _log_event(
            "offer received",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason="offer_received",
            sdp_length=len(sdp),
            mline_count=sdp.count("m="),
            has_fingerprint="a=fingerprint:" in sdp,
        )
        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        except Exception as exc:
            self._state = MediaState.FAILED
            raise MediaFormatError("offer SDP was rejected") from exc
        self._offer_seen = True
        self._state = MediaState.OFFER_RECEIVED

        try:
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
        except Exception as exc:
            self._state = MediaState.FAILED
            raise MediaFormatError("answer creation failed") from exc

        self._state = MediaState.ANSWER_CREATED
        local = pc.localDescription
        if local is None:
            raise MediaStateError("answer SDP is not available")
        _log_event(
            "answer created",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason="answer_created",
            sdp_length=len(local.sdp),
            mline_count=local.sdp.count("m="),
        )
        return local.sdp

    async def begin_turn(self) -> str:
        """开始新 ASR turn：生成 turn_id，先补 600 ms preRoll，再持续喂 60 ms 帧。"""
        self._ensure_loop()
        self._ensure_open()
        self._cancel_turn(reason="turn_replaced")
        # 上一 turn 的 <60 ms 残片不再跨 turn 复用；preRoll 仍保留最近 600 ms。
        self._splitter.reset()

        turn_id = uuid.uuid4().hex
        self._turn_id = turn_id
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=MAX_ASR_QUEUE_FRAMES)
        self._asr_queue = queue
        pre_roll_frames = self._pre_roll.recent()
        for frame in pre_roll_frames:
            queue.put_nowait(frame.data)

        provider = self._asr_provider or build_asr()
        self._asr_provider = provider
        self._asr_task = self.spawn(self._consume_asr(turn_id, provider, queue))
        _log_event(
            "asr turn started",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=turn_id,
            state=self._state.value,
            reason="turn_started",
            pre_roll_frames=len(pre_roll_frames),
            pre_roll_ms=sum(frame.duration_ms for frame in pre_roll_frames),
        )
        return turn_id

    def _cancel_turn(self, *, reason: str) -> None:
        old_turn = self._turn_id
        if self._asr_task is not None:
            self._asr_task.cancel()
            self._asr_task = None
        if self._asr_queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._asr_queue.put_nowait(None)
            self._asr_queue = None
        self._turn_id = None
        if old_turn is not None:
            _log_event(
                "asr turn cancelled",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=old_turn,
                state=self._state.value,
                reason=reason,
            )

    async def _consume_asr(
        self,
        turn_id: str,
        provider: ASRProvider,
        queue: asyncio.Queue[bytes | None],
    ) -> None:
        try:
            try:
                events = await provider.stream(_asr_frame_iterator(queue))
            except ASRError as exc:
                await self._emit_asr_event(
                    turn_id, ASREvent(kind=ASREventKind.ERROR, result=None, error=exc)
                )
                return
            async for event in events:
                await self._emit_asr_event(turn_id, event)
        except asyncio.CancelledError:
            raise
        except ASRError as exc:
            await self._emit_asr_event(
                turn_id, ASREvent(kind=ASREventKind.ERROR, result=None, error=exc)
            )
        except Exception as exc:
            _log_event(
                "asr task failed",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason=MediaFailureReason.TASK_EXCEPTION.value,
                error_type=type(exc).__name__,
            )
        finally:
            _log_event(
                "asr turn finished",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason="turn_finished",
            )

    async def _emit_asr_event(self, turn_id: str, event: ASREvent) -> None:
        # 不打印转写文本；只记录长度、is_final 与 confidence（允许 None）。
        if event.error is not None:
            extra: dict[str, object] = {
                "error_type": type(event.error).__name__,
                "text_len": 0,
                "confidence": None,
            }
        elif event.result is not None:
            extra = {
                "is_final": event.result.is_final,
                "text_len": len(event.result.text),
                "confidence": event.result.confidence,
            }
        else:
            extra = {"text_len": 0, "confidence": None}
        _log_event(
            "asr event",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=turn_id,
            state=self._state.value,
            reason=event.kind.value,
            **extra,
        )

        if self._on_asr_event is None:
            return
        try:
            await self._on_asr_event(turn_id, event)
        except Exception as exc:
            _log_event(
                "asr event callback failed",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=turn_id,
                state=self._state.value,
                reason="callback_error",
                error_type=type(exc).__name__,
            )

    async def _consume_track(self, track: MediaStreamTrack) -> None:
        try:
            while True:
                frame = await track.recv()
                if not isinstance(frame, av.AudioFrame):
                    raise MediaFormatError("audio track returned a non-audio frame")
                self._process_uplink_frame(frame)
        except MediaStreamError:
            self._flush_uplink_tail()
            _log_event(
                "audio track ended",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason=MediaFailureReason.TRACK_ENDED.value,
            )
        except asyncio.CancelledError:
            raise
        except MediaFormatError as exc:
            _log_event(
                "audio format error",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason=MediaFailureReason.FORMAT_ERROR.value,
                error_type=type(exc).__name__,
            )
        except Exception as exc:
            _log_event(
                "audio track failed",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason=MediaFailureReason.TASK_EXCEPTION.value,
                error_type=type(exc).__name__,
            )

    def _process_uplink_frame(self, frame: av.AudioFrame) -> None:
        for canonical in self._normalizer.feed(frame):
            asr_frames, vad_frames = self._splitter.feed(canonical)
            self._dispatch_frames(asr_frames, vad_frames)

    def _flush_uplink_tail(self) -> None:
        for canonical in self._normalizer.flush():
            asr_frames, vad_frames = self._splitter.feed(canonical)
            self._dispatch_frames(asr_frames, vad_frames)
        asr_frames, vad_frames = self._splitter.flush()
        self._dispatch_frames(asr_frames, vad_frames)

    def _dispatch_frames(self, asr_frames: list[PcmFrame], vad_frames: list[PcmFrame]) -> None:
        self._asr_frames_total += len(asr_frames)
        self._vad_frames_total += len(vad_frames)
        for asr in asr_frames:
            self._pre_roll.append(asr)
            self._push_asr_frame(asr.data)

    def _push_asr_frame(self, data: bytes) -> None:
        queue = self._asr_queue
        if queue is None or self._turn_id is None:
            return
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            self._asr_dropped_frames += 1
            if self._asr_dropped_frames == 1 or self._asr_dropped_frames % 10 == 0:
                _log_event(
                    "asr frame dropped",
                    trace_id=self.trace_id,
                    connection_id=self.connection_id,
                    turn_id=self._turn_id,
                    state=self._state.value,
                    reason=MediaFailureReason.BACKPRESSURE.value,
                    dropped_frames=self._asr_dropped_frames,
                )

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """把媒体任务绑定到创建时的 loop，并登记到会话；关闭时统一取消。"""
        self._ensure_loop()
        self._ensure_open()
        task = self._loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    async def close(self, *, reason: str = "closed") -> None:
        """幂等关闭：CLOSING → 取消任务 → 关 PC → CLOSED。重复调用直接返回。"""
        self._ensure_loop()
        if self._state in (MediaState.CLOSING, MediaState.CLOSED):
            return

        self._state = MediaState.CLOSING
        _log_event(
            "media session closing",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason=reason,
            pending_tasks=len(self._tasks),
        )

        self._cancel_turn(reason="closed")
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

        self._normalizer.reset()
        self._splitter.reset()
        self._pre_roll.clear()
        if self._audio_track is not None:
            self._audio_track.stop()
            self._audio_track = None
        if self._playback_track is not None:
            self._playback_track.stop()
            self._playback_track = None
        self._asr_provider = None

        if self._pc is not None:
            try:
                await self._pc.close()
            except Exception as exc:  # 关闭必须继续，不能留下半关闭会话
                logger.warning(
                    "peer connection close failed",
                    extra={
                        "trace_id": self.trace_id,
                        "connection_id": self.connection_id,
                        "turn_id": self._turn_id,
                        "state": self._state.value,
                        "reason": MediaFailureReason.CLOSED.value,
                        "error_type": type(exc).__name__,
                    },
                )
        self._pc = None
        self._state = MediaState.CLOSED
        _log_event(
            "media session closed",
            trace_id=self.trace_id,
            connection_id=self.connection_id,
            turn_id=self._turn_id,
            state=self._state.value,
            reason=reason,
            pending_tasks=len(self._tasks),
        )

    def _ensure_loop(self) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise MediaLoopMismatch("media call requires a running event loop") from exc
        if running is not self._loop:
            raise MediaLoopMismatch(
                f"media session {self.connection_id} belongs to another event loop"
            )

    def _ensure_open(self) -> None:
        if self._state in (MediaState.CLOSING, MediaState.CLOSED, MediaState.FAILED):
            raise MediaClosedError(f"media session {self.connection_id} is closed")

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _log_event(
                "media task failed",
                trace_id=self.trace_id,
                connection_id=self.connection_id,
                turn_id=self._turn_id,
                state=self._state.value,
                reason=MediaFailureReason.TASK_EXCEPTION.value,
                error_type=type(error).__name__,
            )


class MediaSessionRegistry:
    """`connection_id` → `MediaSession` 的 registry；不提供模块级单例。

    调用方（A2 接线层）应持有实例并在同一 running loop 内使用；跨 loop 调用明确失败。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, MediaSession] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)

    @property
    def pending_tasks(self) -> int:
        return sum(session.pending_tasks for session in self._sessions.values())

    def create_session(
        self,
        connection_id: str,
        trace_id: str,
        *,
        config: MediaConfig | None = None,
        asr_provider: ASRProvider | None = None,
        on_asr_event: AsrEventCallback | None = None,
    ) -> MediaSession:
        """建立会话占位；不创建 PC、不做鉴权、不解析 WS。"""
        self._ensure_loop()
        if connection_id in self._sessions:
            raise MediaStateError(f"media session {connection_id} already exists")

        media_config = config or MediaConfig.from_settings(get_settings())
        session = MediaSession(
            connection_id,
            trace_id,
            media_config,
            asr_provider=asr_provider,
            on_asr_event=on_asr_event,
        )
        self._sessions[connection_id] = session
        _log_event(
            "media session created",
            trace_id=session.trace_id,
            connection_id=connection_id,
            turn_id=None,
            state=session.state.value,
            reason="session_created",
        )
        return session

    def get_session(self, connection_id: str) -> MediaSession | None:
        return self._sessions.get(connection_id)

    async def close_session(self, connection_id: str, *, reason: str = "closed") -> bool:
        """关闭并移除一个会话；未知 connection_id 视为已关闭，返回 False。"""
        self._ensure_loop()
        session = self._sessions.pop(connection_id, None)
        if session is None:
            return False
        await session.close(reason=reason)
        return True

    async def close_all(self, *, reason: str = "shutdown") -> None:
        """关闭全部会话；异常不阻断其余会话的清理。"""
        self._ensure_loop()
        sessions = list(self._sessions.values())
        self._sessions.clear()
        if sessions:
            await asyncio.gather(
                *(session.close(reason=reason) for session in sessions),
                return_exceptions=True,
            )

    def _ensure_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise MediaLoopMismatch("media session registry requires a running loop") from exc
        if self._loop is None:
            self._loop = loop
        elif loop is not self._loop:
            raise MediaLoopMismatch("media session registry belongs to another event loop")


__all__ = [
    "ASR_FRAME_BYTES",
    "ASR_FRAME_MS",
    "ASR_FRAME_SAMPLES",
    "CANONICAL_FRAME_BYTES",
    "CANONICAL_FRAME_MS",
    "CANONICAL_FRAME_SAMPLES",
    "DOWNLINK_FRAME_MS",
    "DOWNLINK_FRAME_SAMPLES",
    "DOWNLINK_SAMPLE_RATE",
    "MAX_ASR_QUEUE_FRAMES",
    "PRE_ROLL_FRAMES",
    "PRE_ROLL_MS",
    "SAMPLE_WIDTH_BYTES",
    "TARGET_SAMPLE_RATE",
    "VAD_FRAME_BYTES",
    "VAD_FRAME_MS",
    "VAD_FRAME_SAMPLES",
    "AudioNormalizer",
    "DualFrameSplitter",
    "MediaClosedError",
    "MediaConfig",
    "MediaConfigError",
    "MediaError",
    "MediaFailureReason",
    "MediaFormatError",
    "MediaLoopMismatch",
    "MediaSession",
    "MediaSessionRegistry",
    "MediaState",
    "MediaStateError",
    "PcmFrame",
    "PcmFramer",
    "PreRollBuffer",
    "validate_downlink_pcm",
]
