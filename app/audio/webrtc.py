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
import logging
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import av
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection

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

_LOOPBACK_ICE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_ICE_URL_SCHEMES = frozenset({"stun", "turn", "turns"})


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


# ------------------------------------------------------------------ 会话与 registry


class MediaSession:
    """一个 `connection_id` 的媒体会话骨架。

    当前只负责：running loop 捕获、PC 创建、任务登记、幂等关闭、状态与资源观测。
    不处理 offer/answer/ICE/ASR/TTS —— 这些等 C1/C2 冻结后由 M3–M5 补。
    """

    def __init__(self, connection_id: str, trace_id: str, config: MediaConfig) -> None:
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

    def create_peer_connection(self) -> RTCPeerConnection:
        """创建 aiortc PC；一个连接只允许一个，重复创建是状态错误。"""
        self._ensure_loop()
        self._ensure_open()
        if self._pc is not None:
            raise MediaStateError("peer connection already exists for this connection")

        servers = self.config.ice_servers()
        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
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

        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

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
    ) -> MediaSession:
        """建立会话占位；不创建 PC、不做鉴权、不解析 WS。"""
        self._ensure_loop()
        if connection_id in self._sessions:
            raise MediaStateError(f"media session {connection_id} already exists")

        media_config = config or MediaConfig.from_settings(get_settings())
        session = MediaSession(connection_id, trace_id, media_config)
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
