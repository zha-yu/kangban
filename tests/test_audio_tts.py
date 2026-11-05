"""TTS 适配层回归测试：Mock 音频格式、时长规则、工厂分派、ffmpeg 兜底与降级路径。

钉住四条验收口径（Day 3 任务卡 A1 · 计划 §T1.3/§T1.4）：

1. `MockTTSProvider` 输出 **16kHz / 16bit / 单声道** s16le PCM —— 采样率与声道靠「字节数 ↔ 时长」
   互换校验（立体声会正好翻倍，8bit 会正好减半），位深靠整块解成 int16 后看峰值量级；
2. 时长 `max(0.4s, 0.06s × 字数)`，随字数**单调不减**，且 0.4s 地板之后严格线性增长；
3. 工厂 `build_tts()`：空值/未知值归一到 mock，`edge`/`cloud` 正确分派，settings 能透传；
4. ffmpeg 缺失（monkeypatch PATH）→ `TTSUnavailable("ffmpeg not found")`，**不联网、不依赖 ffmpeg**。

网络用例（真 edge-tts 合成）标 `@pytest.mark.slow`，`-m "not slow"` 下不跑；真实 ffmpeg 转码用例
只依赖本机二进制、不依赖网络，因此留在默认集合里，缺 ffmpeg 时由 fixture skip 而不是静默通过。
"""

from __future__ import annotations

import array
import itertools
import logging
import shutil
import subprocess
import sys
import types
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.audio import tts as tts_mod
from app.audio.tts import (
    PCM_CHANNELS,
    PCM_FRAME_BYTES,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    CloudTTSProvider,
    EdgeTTSProvider,
    MockTTSProvider,
    TTSUnavailable,
    build_tts,
    mock_duration_seconds,
    mp3_to_pcm,
)
from app.core.config import Settings

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="本机没有 ffmpeg：跳过真实转码用例（ffmpeg 缺失路径另有专门用例覆盖）",
)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _samples(pcm: bytes) -> list[int]:
    """把 s16le PCM 整块解成 int16 采样序列（解不开就说明位深/对齐不对）。"""
    decoded = array.array("h")
    decoded.frombytes(pcm)
    return decoded.tolist()


def _duration_s(pcm: bytes) -> float:
    """按 16kHz/16bit/单声道换算 PCM 时长。"""
    return len(pcm) / (PCM_SAMPLE_RATE * PCM_FRAME_BYTES)


def _zero_crossings(samples: list[int]) -> int:
    """统计过零次数，用于估算一段波形的平均频率（越高频过零越多）。"""
    return sum(1 for left, right in itertools.pairwise(samples) if (left < 0) != (right < 0))


def _install_fake_edge_tts(monkeypatch: pytest.MonkeyPatch, mp3: bytes) -> list[dict[str, str]]:
    """把 `edge_tts` 换成只吐内存 mp3 的假模块，返回记录到的调用参数（不联网）。"""
    calls: list[dict[str, str]] = []

    class FakeCommunicate:
        """假 edge-tts 的 Communicate：按 chunk 吐出预先准备好的真 mp3 字节。"""

        def __init__(self, text: str, voice: str, *, rate: str = "+0%") -> None:
            calls.append({"text": text, "voice": voice, "rate": rate})

        async def stream(self) -> AsyncIterator[dict[str, Any]]:
            # 真实 edge-tts 的流里混着 WordBoundary 元数据帧，provider 必须只挑 audio 帧。
            yield {"type": "WordBoundary", "offset": 0, "duration": 0}
            for offset in range(0, len(mp3), 4096):
                yield {"type": "audio", "data": mp3[offset : offset + 4096]}

    module = types.ModuleType("edge_tts")
    module.Communicate = FakeCommunicate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "edge_tts", module)
    return calls


@pytest.fixture(scope="session")
def sample_mp3(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """用本机 ffmpeg 现造一段 0.5s 440Hz mp3，充当「edge-tts 的输出」。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("本机没有 ffmpeg：无法生成 mp3 夹具")
    out = tmp_path_factory.mktemp("tts") / "sine.mp3"
    subprocess.run(  # noqa: S603 - argv 全为常量，输入只有 labfi 生成的测试音频
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    return out.read_bytes()


# ---------------------------------------------------------------------------
# 1. Mock 输出格式：16kHz / 16bit / 单声道
# ---------------------------------------------------------------------------


def test_pcm_format_constants_are_16k_16bit_mono() -> None:
    assert PCM_SAMPLE_RATE == 16_000
    assert PCM_SAMPLE_WIDTH == 2
    assert PCM_CHANNELS == 1
    assert PCM_FRAME_BYTES == PCM_SAMPLE_WIDTH * PCM_CHANNELS


async def test_mock_pcm_is_16k_16bit_mono() -> None:
    text = "今天天气不错，我们出去走走吧"  # 15 字 → 0.9s，高于 0.4s 地板
    pcm = await MockTTSProvider().synthesize(text)

    assert pcm, "Mock TTS 不得返回空字节（A3 写轨拿到空 buffer 会写坏轨）"
    # 位深：s16le 必须能整块解成 int16，且峰值达到 16bit 量级（8bit 数据凑长度会在这里露馅）
    assert len(pcm) % PCM_FRAME_BYTES == 0
    samples = _samples(pcm)
    assert max(samples) > 0.25 * 32767
    assert min(samples) < -0.25 * 32767
    # 采样率 + 单声道：字节数 = 时长 × 16000 × 2；若是立体声会正好翻倍，8bit 会正好减半
    expected_s = mock_duration_seconds(text)
    assert len(pcm) == round(expected_s * PCM_SAMPLE_RATE) * PCM_FRAME_BYTES
    assert _duration_s(pcm) == pytest.approx(expected_s, abs=1 / PCM_SAMPLE_RATE)


async def test_mock_pcm_is_sweep_then_silence() -> None:
    pcm = await MockTTSProvider().synthesize("扫频加静音")
    samples = _samples(pcm)
    tone_part = samples[: int(len(samples) * tts_mod.MOCK_TONE_RATIO)]

    # 尾段必须是全零静音（后 30%）
    tail = samples[int(len(samples) * 0.8) :]
    assert tail
    assert not any(tail)

    # 前段是扫频：后 10% 的过零次数应明显多于前 10%（220Hz → 880Hz，理论 4 倍）
    head_window = tone_part[: len(tone_part) // 10]
    tail_window = tone_part[-len(tone_part) // 10 :]
    assert _zero_crossings(tail_window) > _zero_crossings(head_window)


async def test_mock_works_without_ffmpeg_or_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # PATH 只剩一个空目录 → ffmpeg / edge-tts 全不可见，Mock 仍须可用（断网联调前提）
    monkeypatch.setenv("PATH", str(tmp_path))

    pcm = await MockTTSProvider().synthesize("断网也能跑")

    assert pcm
    assert _duration_s(pcm) == pytest.approx(mock_duration_seconds("断网也能跑"), abs=0.01)


# ---------------------------------------------------------------------------
# 2. 时长规则：max(0.4s, 0.06s × 字数)，随字数单调
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_s"),
    [
        ("", 0.4),  # 空串仍给地板时长，保证不返回空字节
        ("你好", 0.4),  # 2 字 = 0.12s，被 0.4s 地板抬起
        ("一二三四五六", 0.4),  # 6 字 = 0.36s，仍在地板下
        ("一二三四五六七", 0.42),  # 7 字 = 0.42s，刚过地板
        ("好" * 20, 1.2),
        ("  ", 0.4),  # 纯空白按 0 字算
    ],
)
def test_mock_duration_rule(text: str, expected_s: float) -> None:
    assert mock_duration_seconds(text) == pytest.approx(expected_s)


def test_mock_duration_monotonic_in_text_length() -> None:
    lengths = (0, 1, 5, 6, 7, 10, 20, 40)
    durations = [mock_duration_seconds("好" * n) for n in lengths]

    assert durations == sorted(durations), "时长必须随字数单调不减"
    assert durations[-1] > durations[0]
    # 过 0.4s 地板后按 0.06s/字 严格线性增长
    beyond_floor = [mock_duration_seconds("好" * n) for n in (7, 10, 20, 40)]
    assert all(later > earlier for earlier, later in itertools.pairwise(beyond_floor))


async def test_mock_pcm_length_tracks_text_length() -> None:
    provider = MockTTSProvider()
    byte_lengths = [len(await provider.synthesize("好" * n)) for n in (7, 10, 20)]

    assert byte_lengths == sorted(byte_lengths)
    assert len(set(byte_lengths)) == len(byte_lengths), (
        "字数不同必须出不同长度，否则 A5 无法按时长排帧"
    )


# ---------------------------------------------------------------------------
# 3. 观测：start() / record_tts() 真的被调用
# ---------------------------------------------------------------------------


async def test_mock_records_observation(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.audio.observe")
    text = "记录一条观测"

    pcm = await MockTTSProvider().synthesize(text)

    records = [record for record in caplog.records if record.getMessage() == "tts observation"]
    assert len(records) == 1
    record = records[0]
    assert record.tts_provider == "mock"
    assert record.tts_voice == tts_mod.MOCK_VOICE
    assert record.tts_text_len == len(text)
    assert record.tts_pcm_bytes == len(pcm)
    assert record.tts_total_ms >= 0.0
    assert record.tts_first_packet_ms == record.tts_total_ms  # 非流式：首包 = 整段


# ---------------------------------------------------------------------------
# 4. 工厂分派
# ---------------------------------------------------------------------------


def test_build_tts_empty_provider_falls_back_to_mock() -> None:
    # 空值是 Day 3 的默认环境（.env 里 TTS_PROVIDER 未配置），必须落到 mock 而不是抛错
    assert isinstance(build_tts(Settings(tts_provider="")), MockTTSProvider)


@pytest.mark.parametrize(
    ("raw", "expected_type"),
    [
        ("", MockTTSProvider),
        ("mock", MockTTSProvider),
        ("MOCK", MockTTSProvider),
        ("  edge  ", EdgeTTSProvider),
        ("edge", EdgeTTSProvider),
        ("cloud", CloudTTSProvider),
        ("Cloud", CloudTTSProvider),
    ],
)
def test_build_tts_dispatch(raw: str, expected_type: type) -> None:
    assert isinstance(build_tts(Settings(tts_provider=raw)), expected_type)


def test_build_tts_unknown_provider_warns_and_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="app.audio.tts")

    provider = build_tts(Settings(tts_provider="azure"))

    assert isinstance(provider, MockTTSProvider)
    assert "unknown TTS_PROVIDER" in caplog.text


def test_build_tts_passes_settings_to_edge() -> None:
    provider = build_tts(
        Settings(tts_provider="edge", tts_voice_id="zh-CN-YunxiNeural", tts_speed=1.2)
    )

    assert isinstance(provider, EdgeTTSProvider)
    assert provider.voice == "zh-CN-YunxiNeural"
    assert provider.speed == 1.2


def test_build_tts_edge_falls_back_to_default_voice() -> None:
    provider = build_tts(Settings(tts_provider="edge", tts_voice_id=""))

    assert isinstance(provider, EdgeTTSProvider)
    assert provider.voice == tts_mod.EDGE_DEFAULT_VOICE


def test_build_tts_without_argument_reads_global_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_mod, "get_settings", lambda: Settings(tts_provider="edge"))

    assert isinstance(build_tts(), EdgeTTSProvider)


@pytest.mark.parametrize(
    ("speed", "expected_rate"),
    [(1.0, "+0%"), (1.25, "+25%"), (0.8, "-20%"), (2.5, "+100%"), (0.1, "-50%")],
)
def test_speed_to_rate_clamps_and_formats(speed: float, expected_rate: str) -> None:
    # settings.tts_speed 是浮点倍率，edge-tts 要的是 "+20%" 这种百分号串；越界夹到 ±100%/50%
    assert tts_mod._speed_to_rate(speed) == expected_rate


# ---------------------------------------------------------------------------
# 5. ffmpeg 缺失 → TTSUnavailable（不联网、不需要真 ffmpeg）
# ---------------------------------------------------------------------------


async def test_edge_without_ffmpeg_raises_tts_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(TTSUnavailable, match="ffmpeg not found"):
        await EdgeTTSProvider().synthesize("没有 ffmpeg 就别联网了")


def test_mp3_to_pcm_without_ffmpeg_raises_tts_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(TTSUnavailable, match="ffmpeg not found"):
        mp3_to_pcm(b"\x00\x01")


def test_mp3_to_pcm_rejects_empty_payload() -> None:
    with pytest.raises(TTSUnavailable, match="empty mp3 payload"):
        mp3_to_pcm(b"")


@requires_ffmpeg
async def test_edge_without_package_raises_tts_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # sys.modules 里放 None 会让 `import edge_tts` 抛 ImportError（无论本机装没装）
    monkeypatch.setitem(sys.modules, "edge_tts", None)  # type: ignore[arg-type]

    with pytest.raises(TTSUnavailable, match="edge-tts not installed"):
        await EdgeTTSProvider().synthesize("没装 edge-tts")


# ---------------------------------------------------------------------------
# 6. 真实 ffmpeg 转码链路（本地二进制，不走网络）
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_mp3_to_pcm_with_real_ffmpeg(sample_mp3: bytes) -> None:
    pcm = mp3_to_pcm(sample_mp3)

    assert pcm
    assert len(pcm) % PCM_FRAME_BYTES == 0
    # 0.5s 源音频经 mp3 编解码会多一点帧对齐填充，容差放到 ±0.15s
    assert _duration_s(pcm) == pytest.approx(0.5, abs=0.15)
    assert max(abs(sample) for sample in _samples(pcm)) > 1000  # 440Hz 正弦确实被解出来了


@requires_ffmpeg
async def test_edge_provider_end_to_end_with_fake_package(
    monkeypatch: pytest.MonkeyPatch,
    sample_mp3: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 假 edge-tts + 真 ffmpeg：钉住「惰性 import → 挑 audio 帧 → 子进程转码 → 观测」整条链
    calls = _install_fake_edge_tts(monkeypatch, sample_mp3)
    caplog.set_level(logging.INFO, logger="app.audio.observe")
    provider = EdgeTTSProvider(voice="zh-CN-XiaoxiaoNeural", speed=1.2)

    pcm = await provider.synthesize("你好，我是康伴")

    assert pcm
    assert len(pcm) % PCM_FRAME_BYTES == 0
    assert _duration_s(pcm) == pytest.approx(0.5, abs=0.15)
    assert calls == [{"text": "你好，我是康伴", "voice": "zh-CN-XiaoxiaoNeural", "rate": "+20%"}]
    observations = [record for record in caplog.records if record.getMessage() == "tts observation"]
    assert [record.tts_provider for record in observations] == ["edge"]


@requires_ffmpeg
async def test_edge_provider_maps_broken_audio_to_tts_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_edge_tts(monkeypatch, b"this is not an mp3")

    with pytest.raises(TTSUnavailable, match="ffmpeg convert failed"):
        await EdgeTTSProvider().synthesize("坏音频")


# ---------------------------------------------------------------------------
# 7. 云骨架 + 真实 edge-tts 网络用例（slow）
# ---------------------------------------------------------------------------


async def test_cloud_provider_is_skeleton() -> None:
    with pytest.raises(NotImplementedError):
        await CloudTTSProvider(voice="", timeout_ms=300).synthesize("云端待接入")


@pytest.mark.slow
async def test_edge_live_synthesis_over_network() -> None:
    """真 edge-tts 合成（要外网 + 装了 edge-tts）；`-m "not slow"` 下不跑。"""
    pytest.importorskip("edge_tts", reason="未安装 edge-tts：跳过真实合成用例")

    pcm = await EdgeTTSProvider().synthesize("你好")

    assert pcm
    assert _duration_s(pcm) > 0.3
