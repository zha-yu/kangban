"""TTS 适配层：复用 C1 契约的 `TTSProvider`，提供 Mock / edge-tts+ffmpeg / 云骨架。

Day 3 任务卡 A1（DAG 节点 N4+N5）· 计划 `docs/plans/task-a1-ASR与TTS适配层.md` §T1.3/§T1.4 ·
`docs/plans/task-a1-DAG.md` §3。

三条设计约束（改动前先问主理人）：

1. **不复制契约**：`TTSProvider` 的唯一定义在 `app/audio/asr.py` 的 C1 契约区（已冻结，消费方
   A2/A3/A5），本文件只 import 并继承。再造一份协议就会出现两个会漂移的「事实源」。
2. **零新增 Python 依赖**：edge-tts 出的 mp3 用 **ffmpeg 子进程** 解成 16k/16bit/单声道 PCM。
   不用 pydub —— 它内部同样调 ffmpeg，只会多一层依赖面而没有收益。edge-tts 本身也不进
   `pyproject.toml`，只在 `synthesize()` 里惰性 import：没装时按「环境不可用」降级
   （抛 `TTSUnavailable`），而不是 import 期炸掉整个 app。
3. **C1 口径**：`synthesize()` 返回**整段** 16kHz/16bit/单声道 s16le PCM（Day 3 非流式）。
   首包 ≈ 整段可用时刻；edge-tts 实测首包 >1s、远超 300ms 预算，已在计划 §8 P-A1-4 登记为
   「断网兜底而非主实现」，不计入 Day 5 基线。

降级链（`TTSUnavailable` 的两条出路）：调用方捕获后切「大字幕 + 预置音频」，万不得已才用浏览器
`speechSynthesis`（技术设计文档 §6 语音合成行）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
import struct
import subprocess
from typing import Final

from app.audio._observe import record_tts, start
from app.audio.asr import TTSProvider
from app.core.config import Settings, get_settings

logger = logging.getLogger("app.audio.tts")

# ---------------------------------------------------------------------------
# PCM 口径（C1 契约钉死，不要按调用方心情改）
# ---------------------------------------------------------------------------
PCM_SAMPLE_RATE: Final[int] = 16_000
PCM_SAMPLE_WIDTH: Final[int] = 2  # 16bit = 2 byte
PCM_CHANNELS: Final[int] = 1
PCM_FRAME_BYTES: Final[int] = PCM_SAMPLE_WIDTH * PCM_CHANNELS  # 单采样点 2 byte

# ---------------------------------------------------------------------------
# Mock 参数（计划 T1.3：正弦扫频 + 静音，时长 max(0.4s, 0.06s × 字数)）
# ---------------------------------------------------------------------------
MOCK_MIN_DURATION_S: Final[float] = 0.4
MOCK_SECONDS_PER_CHAR: Final[float] = 0.06
MOCK_VOICE: Final[str] = "mock-sine"
MOCK_TONE_RATIO: Final[float] = 0.7  # 前 70% 扫频、后 30% 静音，两者都能被下游用例观测到
MOCK_SWEEP_START_HZ: Final[float] = 220.0
MOCK_SWEEP_END_HZ: Final[float] = 880.0
MOCK_PEAK_RATIO: Final[float] = 0.30  # 峰值只占满量程 30%，避免削波/刺耳
MOCK_FADE_S: Final[float] = 0.005  # 5ms 线性淡入淡出，消除起播与结尾爆音

# ---------------------------------------------------------------------------
# edge-tts / ffmpeg 参数
# ---------------------------------------------------------------------------
EDGE_DEFAULT_VOICE: Final[str] = "zh-CN-XiaoxiaoNeural"
EDGE_MIN_SPEED: Final[float] = 0.5
EDGE_MAX_SPEED: Final[float] = 2.0
FFMPEG_BIN: Final[str] = "ffmpeg"
# 计划 §T1.4 钉死的转码参数：stdin 吃 mp3，stdout 吐 s16le / 16kHz / 单声道。
FFMPEG_CONVERT_ARGS: Final[tuple[str, ...]] = (
    "-i",
    "pipe:0",
    "-f",
    "s16le",
    "-ar",
    str(PCM_SAMPLE_RATE),
    "-ac",
    str(PCM_CHANNELS),
    "pipe:1",
)


class TTSUnavailable(Exception):
    """TTS 不可用（ffmpeg 缺失 / edge-tts 未装 / 合成或转码失败）。

    与 ASR 侧的二分类同理：本异常只表示「这一条链路走不通、需要降级」，不带重试语义；
    非流式整段合成没有「部分成功」这种中间态，所以不设 TTSProtocolError。
    """


# ---------------------------------------------------------------------------
# 纯函数：时长规则 / Mock 波形 / mp3→PCM
# ---------------------------------------------------------------------------


def mock_duration_seconds(text: str) -> float:
    """Mock 时长规则：``max(0.4s, 0.06s × 字数)``（计划 T1.3 钉死，不要自己改系数）。

    字数按 `strip()` 后的字符数算：只剔除**首尾**空白；句子中间的标点（`，` `。` 等）
    **仍然计入**，这是刻意的（标点本来就占语气停顿）。别按「标点不计」去推算时长。
    空串仍然给 0.4s，保证 `synthesize()` **永不返回空字节** —— A3 的下行写轨拿到空 buffer
    会直接把轨写坏。
    """
    return max(MOCK_MIN_DURATION_S, MOCK_SECONDS_PER_CHAR * len(text.strip()))


def _render_mock_pcm(duration_s: float) -> bytes:
    """生成「正弦扫频 + 静音尾」的 s16le PCM（纯 CPU、同步；异步侧用 to_thread 包）。

    扫频用**相位累加**而不是 ``sin(2πft)``：变频时后者会在频率切换点产生相位跳变，
    听起来是「咔哒」声；相位累加保证波形连续。
    """
    total_samples = max(1, round(duration_s * PCM_SAMPLE_RATE))
    tone_samples = min(total_samples, round(total_samples * MOCK_TONE_RATIO))
    fade_samples = max(1, round(MOCK_FADE_S * PCM_SAMPLE_RATE))
    peak = MOCK_PEAK_RATIO * 32767.0

    phase = 0.0
    values: list[int] = []
    for index in range(tone_samples):
        progress = index / max(1, tone_samples - 1)
        freq = MOCK_SWEEP_START_HZ + (MOCK_SWEEP_END_HZ - MOCK_SWEEP_START_HZ) * progress
        phase += math.tau * freq / PCM_SAMPLE_RATE
        # 两端各 5ms 线性淡入淡出：min(距头, 距尾) 落在淡入区就按比例衰减。
        edge_distance = min(index, tone_samples - 1 - index) + 1
        gain = min(1.0, edge_distance / fade_samples)
        values.append(round(peak * gain * math.sin(phase)))

    tone = struct.pack(f"<{len(values)}h", *values)
    return tone + bytes(PCM_FRAME_BYTES * (total_samples - tone_samples))


def _require_ffmpeg(ffmpeg_bin: str) -> None:
    """ffmpeg 不在 PATH 上就早失败：降级链路据此切「大字幕 + 预置音频」。"""
    if shutil.which(ffmpeg_bin) is None:
        raise TTSUnavailable("ffmpeg not found")


def _stderr_tail(exc: subprocess.CalledProcessError) -> str:
    """只取 stderr 末尾 200 字符：ffmpeg 的 stderr 很长，且开头往往只是 banner。"""
    raw = exc.stderr if isinstance(exc.stderr, bytes) else b""
    return raw[-200:].decode("utf-8", "replace").strip()


def mp3_to_pcm(mp3: bytes, *, ffmpeg_bin: str = FFMPEG_BIN) -> bytes:
    """把 mp3 字节解成 16k/16bit/单声道 s16le PCM（**阻塞**，异步侧请用 `asyncio.to_thread`）。

    ffmpeg 参数由计划 §T1.4 钉死；用子进程而不是 pydub 的理由见模块 docstring 第 2 条。
    """
    if not mp3:
        raise TTSUnavailable("empty mp3 payload")
    _require_ffmpeg(ffmpeg_bin)
    completed = subprocess.run(  # noqa: S603 - argv 全是常量、shell=False、音频只走 stdin
        [ffmpeg_bin, *FFMPEG_CONVERT_ARGS],
        input=mp3,
        capture_output=True,
        check=True,
    )
    pcm = completed.stdout
    if not pcm:
        raise TTSUnavailable("ffmpeg produced no pcm")
    if len(pcm) % PCM_FRAME_BYTES:
        # 半个采样点说明转码参数或 ffmpeg 版本不对，别把错位字节喂给 A3 的写轨。
        raise TTSUnavailable("ffmpeg returned unaligned pcm")
    return pcm


def _speed_to_rate(speed: float) -> str:
    """settings.tts_speed（1.0 = 原速）→ edge-tts 的 ``+20%`` / ``-15%`` 语速串。

    夹到 [0.5, 2.0]：edge-tts 只接受 ±50%/±100% 量级，配置写错时宁可按边界读，也别整段失败。
    """
    clamped = min(max(speed, EDGE_MIN_SPEED), EDGE_MAX_SPEED)
    return f"{round((clamped - 1.0) * 100):+d}%"


def _record(*, provider: str, voice: str, text: str, pcm: bytes, total_ms: float) -> None:
    """TTS 观测单点收口。非流式下首包 = 整段可用时刻，两个耗时字段同值。"""
    record_tts(
        provider=provider,
        voice=voice,
        text_len=len(text),
        pcm_bytes=len(pcm),
        first_packet_ms=total_ms,
        total_ms=total_ms,
    )


# ---------------------------------------------------------------------------
# 实现：Mock
# ---------------------------------------------------------------------------


class MockTTSProvider(TTSProvider):
    """断网可用的 Mock TTS：正弦扫频 + 静音尾，供 A3 写轨 / A5 组装在无网络无 key 时联调。

    不依赖网络、不依赖 ffmpeg、不依赖 edge-tts —— 只做纯 CPU 波形合成，因此可以在
    断网机器与 CI 上稳定复现（A3/A5 的用例全部默认走它）。
    """

    name = "mock"

    def __init__(self, *, voice: str = MOCK_VOICE) -> None:
        self.voice = voice

    async def synthesize(self, text: str) -> bytes:
        stopwatch = start()
        duration_s = mock_duration_seconds(text)
        # 6s 音频约 96000 个采样点，纯 Python 循环要十几毫秒 —— 丢线程里算，别卡住事件循环。
        pcm = await asyncio.to_thread(_render_mock_pcm, duration_s)
        total_ms = stopwatch.elapsed_ms()
        _record(provider=self.name, voice=self.voice, text=text, pcm=pcm, total_ms=total_ms)
        return pcm


# ---------------------------------------------------------------------------
# 实现：edge-tts + ffmpeg 子进程（断网/无云 key 的兜底）
# ---------------------------------------------------------------------------


class EdgeTTSProvider(TTSProvider):
    """edge-tts 合成 mp3 → ffmpeg 子进程转 16k/16bit/单声道 PCM。

    定位是**兜底**而非主实现：整段合成、非流式，首包远超 300ms 预算（计划 §8 P-A1-4）。
    """

    name = "edge"

    def __init__(
        self,
        *,
        voice: str = EDGE_DEFAULT_VOICE,
        speed: float = 1.0,
        ffmpeg_bin: str = FFMPEG_BIN,
    ) -> None:
        self.voice = voice
        self.speed = speed
        self.ffmpeg_bin = ffmpeg_bin

    async def synthesize(self, text: str) -> bytes:
        stopwatch = start()
        # 先查 ffmpeg 再联网：ffmpeg 缺失时合成出来的 mp3 也用不上，白花一次网络往返。
        _require_ffmpeg(self.ffmpeg_bin)
        mp3 = await self._synthesize_mp3(text)
        try:
            pcm = await asyncio.to_thread(mp3_to_pcm, mp3, ffmpeg_bin=self.ffmpeg_bin)
        except subprocess.CalledProcessError as exc:
            raise TTSUnavailable(f"ffmpeg convert failed: {_stderr_tail(exc)}") from exc
        total_ms = stopwatch.elapsed_ms()
        _record(provider=self.name, voice=self.voice, text=text, pcm=pcm, total_ms=total_ms)
        return pcm

    async def _synthesize_mp3(self, text: str) -> bytes:
        """只负责「文本 → mp3 字节」，转码在 `mp3_to_pcm()`。惰性 import 的理由见模块 docstring。"""
        try:
            import edge_tts  # 可选依赖：本环境已安装；未装时 import 失败走 TTSUnavailable
        except ImportError as exc:
            raise TTSUnavailable("edge-tts not installed") from exc

        buffer = bytearray()
        try:
            communicate = edge_tts.Communicate(text, self.voice, rate=_speed_to_rate(self.speed))
            async for chunk in communicate.stream():
                # edge-tts 的流里混着 WordBoundary 等元数据帧，只取音频帧。
                if chunk["type"] == "audio":
                    buffer.extend(chunk["data"])
        except Exception as exc:  # 第三方异常类型不稳定，统一映射成「可降级」
            raise TTSUnavailable(f"edge-tts failed: {exc}") from exc
        if not buffer:
            raise TTSUnavailable("edge-tts returned no audio")
        return bytes(buffer)


# ---------------------------------------------------------------------------
# 实现：云 API 骨架（等教师统一 key）
# ---------------------------------------------------------------------------


class CloudTTSProvider(TTSProvider):
    """云端 TTS 骨架（等教师统一 key）。接口形状已按 C1 固定：返回整段 PCM。

    TODO(Day 5+，拿到 key 后按此顺序补，不要改签名)：
    1. `settings.tts_base_url / tts_app_id / tts_api_key / tts_voice_id` 全部只从 Settings 读，
       禁止硬编码，也禁止落到日志；
    2. 用 httpx.AsyncClient 建长连接（首包预算 300ms），超时取 `settings.tts_timeout_ms`；
    3. 若云端直接给 PCM，就跳过 ffmpeg 分支；若给 mp3/opus，仍复用 `mp3_to_pcm()`；
    4. 云端失败统一抛 `TTSUnavailable`，由调用方切「大字幕 + 预置音频」。
    """

    name = "cloud"

    def __init__(self, *, voice: str = "", timeout_ms: int = 300) -> None:
        self.voice = voice
        self.timeout_ms = timeout_ms

    async def synthesize(self, text: str) -> bytes:
        raise NotImplementedError("CloudTTSProvider 待教师统一 key（见类 docstring 的 TODO）")


# ---------------------------------------------------------------------------
# 工厂：按 Settings 分派（TTS_PROVIDER 空值/未知值一律归一到 mock）
# ---------------------------------------------------------------------------


def build_tts(settings: Settings | None = None) -> TTSProvider:
    """按 `TTS_PROVIDER` 分派：``mock`` / ``edge`` / ``cloud``。

    空值与未知值都归一到 ``mock``：Day 3 的默认环境没有云端 key、也不保证装了 ffmpeg，
    让「没配置」变成「跑不通」会把 A3/A5 一起拖下水。未知值额外打 warning，方便发现拼写错。
    """
    config = settings or get_settings()
    kind = (config.tts_provider or "").strip().lower()

    if kind == "edge":
        return EdgeTTSProvider(
            voice=config.tts_voice_id or EDGE_DEFAULT_VOICE,
            speed=config.tts_speed,
        )
    if kind == "cloud":
        return CloudTTSProvider(voice=config.tts_voice_id, timeout_ms=config.tts_timeout_ms)
    if kind not in {"", "mock"}:
        logger.warning("unknown TTS_PROVIDER=%r, fallback to mock", kind)
    return MockTTSProvider()


# 显式导出（mypy no-implicit-reexport）：`from app.audio.tts import TTSProvider, ...` 的合法面
__all__ = [
    "CloudTTSProvider",
    "EdgeTTSProvider",
    "MockTTSProvider",
    "TTSProvider",
    "TTSUnavailable",
    "build_tts",
    "mp3_to_pcm",
]
