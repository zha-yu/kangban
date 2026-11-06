"""app/audio/vad.py VadFallbackController + webrtc 兜底接线的单元测试（W4）。

覆盖 S4 计划的四条测试要求：

1. 兜底激活 → 喂合成语音帧 → 产出完整 utterance 段（preRoll + 语音段字节数）
2. 未激活 → feed 不产出段（主链路零回归）
3. 非法帧长 → warning 跳过不断链（复用 VadSession 既有行为）
4. 关闭时 flush 无残留、无异常

另覆盖激活语义自裁项：set_active(False) 丢弃残留；MediaSession 分发接线
（_dispatch_frames → controller → 桩下游回调）与 close flush 直等下游。

帧构造算法与 tests/test_audio_vad.py 同源（spikes/vad/spike_webrtcvad.py 已验证
webrtcvad 能稳定识别），此处复制而非 import（tests 无包结构，且两文件
各自独立演进）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct

import pytest

from app.audio.vad import (
    FRAME_SAMPLES,
    PRE_ROLL_FRAMES,
    VadFallbackController,
)
from app.audio.webrtc import MediaSession, _make_frame

# ---------------------------------------------------------------------------
# 测试帧构造（与 tests/test_audio_vad.py 同算法）
# ---------------------------------------------------------------------------


def _pcm(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def silence_frame(index: int = 0) -> bytes:
    """30ms / 16k 静音帧（低能量非零，webrtcvad aggressiveness=2 下稳定判无语音）。"""
    samples: list[int] = []
    for n in range(FRAME_SAMPLES):
        t = (index * FRAME_SAMPLES + n) / 16_000
        samples.append(int(5 * math.sin(2 * math.pi * 1.0 * t)))
    return _pcm(samples)


def voiced_frame(index: int) -> bytes:
    """近似浊音的合成信号（150Hz 基频 + 谐波 + 包络），webrtcvad 多数帧判 speech。"""
    samples: list[int] = []
    for n in range(FRAME_SAMPLES):
        t = (index * FRAME_SAMPLES + n) / 16_000
        envelope = 0.6 + 0.4 * math.sin(2 * math.pi * 3.0 * t)
        value = (
            0.55 * math.sin(2 * math.pi * 150.0 * t)
            + 0.25 * math.sin(2 * math.pi * 450.0 * t)
            + 0.12 * math.sin(2 * math.pi * 1200.0 * t)
            + 0.08 * math.sin(2 * math.pi * 2400.0 * t)
        )
        samples.append(int(max(-1.0, min(1.0, value * envelope)) * 20000))
    return _pcm(samples)


def _feed_utterance(controller: VadFallbackController) -> bytes | None:
    """标准 utterance 序列：30 静音 + 5 浊音 + ≤35 静音（≥27 + 3 hangover）。"""
    for i in range(30):
        result = controller.feed(silence_frame(i))
        if result is not None:
            return result
    for i in range(5):
        result = controller.feed(voiced_frame(i))
        if result is not None:
            return result
    for j in range(35):
        result = controller.feed(silence_frame(30 + 5 + j))
        if result is not None:
            return result
    return None


# ---------------------------------------------------------------------------
# 1. 激活 → 产出完整段
# ---------------------------------------------------------------------------


class TestActiveFallback:
    def test_active_produces_segment_with_preroll(self) -> None:
        """激活后喂标准 utterance → 吐段，字节数符合 preRoll + 语音段。"""
        controller = VadFallbackController("conn-1", "trace-1")
        controller.set_active(True)
        assert controller.is_active is True

        segment = _feed_utterance(controller)
        assert segment is not None, "激活后浊音 + 足够静音应产出段"
        # 至少包含 preRoll 20 帧 + 5 帧浊音
        assert len(segment) >= (PRE_ROLL_FRAMES + 5) * FRAME_SAMPLES * 2
        assert controller.segments_emitted == 1

    def test_deactivate_discards_residual_reactivate_fresh(self) -> None:
        """停用丢弃残留（不吐给下游）；再激活后是新干净会话，无历史泄漏。"""
        controller = VadFallbackController("conn-1", "trace-1")
        controller.set_active(True)
        for i in range(30):
            controller.feed(silence_frame(i))
        for i in range(5):
            controller.feed(voiced_frame(i))
        assert controller.segments_emitted == 0  # 还在说话中，没吐段

        controller.set_active(False)  # 残留被丢弃，不产出
        assert controller.is_active is False
        assert controller.segments_emitted == 0

        # 再激活：纯静音不因旧残留吐段
        controller.set_active(True)
        for i in range(60):
            assert controller.feed(silence_frame(i)) is None
        assert controller.segments_emitted == 0

    def test_illegal_frame_keeps_chain_alive(self) -> None:
        """非法帧长 → 跳过（复用 VadSession 前置校验），后续合法帧继续工作。"""
        controller = VadFallbackController("conn-1", "trace-1")
        controller.set_active(True)
        broken = _pcm([0] * 100)  # 非法帧长
        assert controller.feed(broken) is None

        segment = _feed_utterance(controller)
        assert segment is not None, "非法帧跳过后链路不应断"

    def test_emit_log_recorded(self, caplog: pytest.LogCaptureFixture) -> None:
        """产出段打 vad_fallback_segment 日志，含字节数与 connection_id（不落盘）。"""
        controller = VadFallbackController("conn-log", "trace-log")
        controller.set_active(True)
        with caplog.at_level(logging.INFO, logger="app.audio.vad"):
            _feed_utterance(controller)

        records = [r for r in caplog.records if "vad_fallback_segment" in r.getMessage()]
        assert records, "产出段应有 trace 日志"
        message = records[0].getMessage()
        assert "connection_id=conn-log" in message
        assert "trace_id=trace-log" in message
        assert "bytes=" in message


# ---------------------------------------------------------------------------
# 2. 未激活 → 不产出（主链路零回归）
# ---------------------------------------------------------------------------


class TestInactiveFallback:
    def test_inactive_feed_never_emits(self) -> None:
        """默认未激活：喂什么都不产出段。"""
        controller = VadFallbackController("conn-1", "trace-1")
        assert controller.is_active is False

        assert _feed_utterance(controller) is None
        assert controller.segments_emitted == 0

    def test_set_active_idempotent(self) -> None:
        """重复同向 set_active 不重建会话（幂等）。"""
        controller = VadFallbackController("conn-1", "trace-1")
        controller.set_active(True)
        controller.feed(silence_frame(0))
        controller.set_active(True)  # 幂等，不清状态
        controller.set_active(False)
        controller.set_active(False)
        assert controller.is_active is False


# ---------------------------------------------------------------------------
# 3. close flush 语义
# ---------------------------------------------------------------------------


class TestCloseFlush:
    def test_close_flushes_residual_segment(self) -> None:
        """说话中关闭 → flush 产出末段（含 preRoll），再次 close 无残留。"""
        controller = VadFallbackController("conn-1", "trace-1")
        controller.set_active(True)
        for i in range(30):
            controller.feed(silence_frame(i))
        for i in range(5):
            controller.feed(voiced_frame(i))

        segment = controller.close()
        assert segment is not None, "说话中关闭应 flush 出末段"
        # speechstart 从第 3 帧浊音起算（min_speech_frames=3），此时未喂静音 hangover，
        # 段长 = preRoll 20 帧 + speechstart 起的 3 帧浊音 = 23 帧
        assert len(segment) >= (PRE_ROLL_FRAMES + 3) * FRAME_SAMPLES * 2
        assert controller.is_active is False

        # 再次 close：无残留、无异常
        assert controller.close() is None

    def test_close_without_activation_returns_none(self) -> None:
        """未激活/无会话时 close → None，无异常。"""
        controller = VadFallbackController("conn-1", "trace-1")
        assert controller.close() is None


# ---------------------------------------------------------------------------
# 4. MediaSession 接线（分发 + 关闭收口）
# ---------------------------------------------------------------------------


def _config():
    from app.audio.webrtc import MediaConfig

    return MediaConfig(
        stun_url="",
        turn_url="",
        turn_username="",
        turn_credential="",
        turn_credential_ttl_seconds=0,
        ice_timeout_seconds=5,
        audio_bitrate_kbps=32,
        vad_aggressiveness=2,
    )


def _vad_frame(data: bytes):
    """30ms vad 帧（960 bytes PCM16）→ PcmFrame。"""
    return _make_frame(data, duration_ms=30)


class TestMediaSessionWiring:
    @pytest.mark.asyncio
    async def test_dispatch_reaches_stub_downstream_when_active(self) -> None:
        """激活 → _dispatch_frames 分发 vad 帧 → 桩下游收到完整段。"""
        collected: list[bytes] = []

        async def _stub(segment: bytes) -> None:
            collected.append(segment)

        sess = MediaSession(
            connection_id="c1", trace_id="t1", config=_config(), on_vad_segment=_stub
        )
        assert sess.vad_fallback_active is False
        sess.set_vad_fallback_active(True)
        assert sess.vad_fallback_active is True

        frames = [_vad_frame(silence_frame(i)) for i in range(30)]
        frames += [_vad_frame(voiced_frame(i)) for i in range(5)]
        frames += [_vad_frame(silence_frame(30 + 5 + j)) for j in range(35)]
        sess._dispatch_frames([], frames)
        await asyncio.sleep(0.1)  # 让 spawn 的下游任务跑完

        assert len(collected) == 1, "兜底激活应产出恰好一段"
        assert len(collected[0]) >= (PRE_ROLL_FRAMES + 5) * FRAME_SAMPLES * 2
        assert sess._vad_fallback.segments_emitted == 1

        await sess.close()

    @pytest.mark.asyncio
    async def test_inactive_dispatch_never_reaches_downstream(self) -> None:
        """未激活（默认）→ 分发不产出段，Day 3 主链路零回归。"""
        collected: list[bytes] = []

        async def _stub(segment: bytes) -> None:
            collected.append(segment)

        sess = MediaSession(
            connection_id="c1", trace_id="t1", config=_config(), on_vad_segment=_stub
        )
        frames = [_vad_frame(silence_frame(i)) for i in range(30)]
        frames += [_vad_frame(voiced_frame(i)) for i in range(5)]
        frames += [_vad_frame(silence_frame(30 + 5 + j)) for j in range(35)]
        sess._dispatch_frames([], frames)
        await asyncio.sleep(0.05)

        assert collected == []
        assert sess._vad_fallback.segments_emitted == 0
        await sess.close()

    @pytest.mark.asyncio
    async def test_close_flushes_pending_segment_to_downstream(self) -> None:
        """说话中关闭会话 → close flush 末段直等下游（不被任务取消丢失）。"""
        collected: list[bytes] = []

        async def _stub(segment: bytes) -> None:
            collected.append(segment)

        sess = MediaSession(
            connection_id="c1", trace_id="t1", config=_config(), on_vad_segment=_stub
        )
        sess.set_vad_fallback_active(True)
        # 喂到 speechstart（还没 speechend）→ 末段只能靠 close flush
        sess._dispatch_frames(
            [],
            [_vad_frame(silence_frame(i)) for i in range(30)]
            + [_vad_frame(voiced_frame(i)) for i in range(5)],
        )
        await asyncio.sleep(0.05)
        assert collected == []  # 还在说话中

        await sess.close()
        assert len(collected) == 1, "关闭应 flush 出末段交给下游"
        assert sess.vad_fallback_active is False
