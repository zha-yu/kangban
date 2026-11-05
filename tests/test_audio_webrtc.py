"""app/audio/webrtc.py 单元测试 · A3 媒体层验收。

覆盖 A3 执行计划 §6.1 的 12 个单元级场景（A3-01 ~ A3-12）。
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import av
import pytest

from app.audio.webrtc import (
    ASR_FRAME_BYTES,
    CANONICAL_FRAME_BYTES,
    DOWNLINK_FRAME_BYTES,
    DOWNLINK_FRAME_SAMPLES,
    DOWNLINK_SAMPLE_RATE,
    MAX_PLAYBACK_FRAMES,
    VAD_FRAME_BYTES,
    VAD_FRAME_SAMPLES,
    AudioNormalizer,
    DualFrameSplitter,
    MediaBackpressureError,
    MediaConfigError,
    MediaFormatError,
    MediaLoopMismatch,
    MediaSession,
    MediaSessionRegistry,
    MediaState,
    MediaStateError,
    PcmFrame,
    PcmFramer,
    PreRollBuffer,
    VadEvent,
    VadEventKind,
    _make_frame,
    _tts_pcm_to_48k_frames,
    validate_downlink_pcm,
)

# --------------------------------------------------------------------------- 工具


def _config(**overrides: Any):
    from app.audio.webrtc import MediaConfig

    kwargs = {
        "stun_url": "",
        "turn_url": "",
        "turn_username": "",
        "turn_credential": "",
        "turn_credential_ttl_seconds": 0,
        "ice_timeout_seconds": 5,
        "audio_bitrate_kbps": 32,
        "vad_aggressiveness": 2,
    }
    kwargs.update(overrides)
    return MediaConfig(**kwargs)


def _pcm_frame(duration_ms: int, *, sample_rate: int = 16_000) -> PcmFrame:
    """构造一段纯 500 Hz 正弦 PCM16 单声道帧（16 kHz）。"""
    import math

    n_samples = int(sample_rate * duration_ms / 1000)
    raw = bytearray()
    for i in range(n_samples):
        t = i / sample_rate
        val = int(3000 * math.sin(2 * math.pi * 500 * t))
        val = max(-32768, min(32767, val))
        raw.extend(struct.pack("<h", val))
    return _make_frame(bytes(raw), duration_ms=duration_ms)


def _silence_frame(duration_ms: int) -> PcmFrame:
    return _make_frame(b"\x00" * (16_000 * duration_ms // 1000 * 2), duration_ms=duration_ms)


def _audio_frame(sample_rate: int, channels: int, samples: int) -> av.AudioFrame:
    """构造一个 av.AudioFrame（s16，有正峰值避免 resampler 零输入空跑）。"""
    import math

    frame = av.AudioFrame(
        format="s16",
        layout=f"{'stereo' if channels == 2 else 'mono'}",
        samples=samples,
    )
    frame.sample_rate = sample_rate
    raw = bytearray()
    for i in range(samples * channels):
        t = i / sample_rate
        val = int(2000 * math.sin(2 * math.pi * 440 * t))
        raw.extend(struct.pack("<h", val))
    frame.planes[0].update(bytes(raw))
    return frame


def _valid_sdp() -> str:
    """aiortc 能接受的最小合法 offer SDP（含 ICE ufrag/pwd + rtcp-mux + sendrecv direction）。"""
    return (
        "v=0\r\n"
        "o=- 1730000000000 2 IN IP4 127.0.0.1\r\n"
        "s=-\r\n"
        "t=0 0\r\n"
        "a=group:BUNDLE 0\r\n"
        "a=msid-semantic: WMS testmsid\r\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111 103 104 0 8\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        "a=rtcp:9 IN IP4 0.0.0.0\r\n"
        "a=ice-ufrag:testufrag\r\n"
        "a=ice-pwd:testpassword1234567890\r\n"
        "a=fingerprint:sha-256 00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF\r\n"
        "a=setup:actpass\r\n"
        "a=mid:0\r\n"
        "a=rtpmap:111 opus/48000/2\r\n"
        "a=rtpmap:103 ISAC/16000\r\n"
        "a=rtpmap:104 ISAC/32000\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        "a=rtcp-mux:true\r\n"
        "a=sendrecv\r\n"
    )


# ===========================================================================
# A3-01 · 帧格式转换：48 kHz / 双声道 → 16 kHz / mono / s16
# ===========================================================================


class TestA301FrameConversion:
    def test_48k_stereo_to_16k_mono(self) -> None:
        normalizer = AudioNormalizer()
        total_frames: list[PcmFrame] = []
        for _ in range(5):
            src = _audio_frame(48_000, 2, 960)  # 20 ms stereo 48k
            total_frames.extend(normalizer.feed(src))
        tail = normalizer.flush()
        total_frames.extend(tail)
        assert len(total_frames) >= 1
        for f in total_frames:
            assert f.sample_rate == 16_000
            assert f.channels == 1
            assert f.sample_width == 2

    def test_60_seconds_produces_1000_frames(self) -> None:
        normalizer = AudioNormalizer()
        total_frames: list[PcmFrame] = []
        # 60 s / 20 ms = 3000 个 stereo 48k 源帧
        for _ in range(3000):
            total_frames.extend(normalizer.feed(_audio_frame(48_000, 2, 960)))
        tail = normalizer.flush()
        total_frames.extend(tail)
        # 60 s / 20 ms = 3000 个 canonical
        assert len(total_frames) >= 2990

    def test_output_frame_size_1920(self) -> None:
        """通过 DualFrameSplitter 聚合 → 60 ms ASR 帧 1920 B。"""
        splitter = DualFrameSplitter()
        for _ in range(3):
            splitter.feed(_pcm_frame(20))
        asr_frames, _ = splitter.flush()
        for f in asr_frames:
            assert len(f.data) == ASR_FRAME_BYTES  # 1920
            assert f.duration_ms == 60.0


# ===========================================================================
# A3-02 · 30 / 60 分叉：同一 60 ms 帧 → ASR 1920 B / VAD 两个 960 B
# ===========================================================================


class TestA302DualFrameSplit:
    def test_split_60ms_into_60_asr_and_2x30_vad(self) -> None:
        splitter = DualFrameSplitter()
        # feed() 每次都会 drain 满帧，收集全部
        asr_all: list[PcmFrame] = []
        vad_all: list[PcmFrame] = []
        for _ in range(3):
            a, v = splitter.feed(_pcm_frame(20))
            asr_all.extend(a)
            vad_all.extend(v)
        # flush 处理剩余尾片
        a, v = splitter.flush()
        asr_all.extend(a)
        vad_all.extend(v)

        assert len(asr_all) >= 1
        assert len(asr_all[0].data) == ASR_FRAME_BYTES  # 1920
        assert len(vad_all) >= 2
        for f in vad_all:
            assert len(f.data) == 960

    def test_rejects_non_16k_mono_s16_input(self) -> None:
        splitter = DualFrameSplitter()
        non_canonical = PcmFrame(
            data=b"\x00" * (44_100 * 2),
            sample_rate=44_100,
            channels=1,
        )
        with pytest.raises(MediaFormatError):
            splitter.feed(non_canonical)


# ===========================================================================
# A3-03 · preRoll：说话前 600 ms 缓冲 → 首 10 帧完整补发
# ===========================================================================


class TestA303PreRoll:
    def test_holds_recent_10_frames(self) -> None:
        buf = PreRollBuffer()
        # 12 帧 60 ms → 只保留最后 10
        for _ in range(12):
            buf.append(_pcm_frame(60))
        recent = buf.recent()
        assert len(recent) == 10
        assert buf.duration_ms == 600.0

    def test_rejects_non_60ms_frame(self) -> None:
        buf = PreRollBuffer()
        with pytest.raises(MediaFormatError):
            buf.append(_pcm_frame(30))


# ===========================================================================
# A3-04 · 尾帧零填充：残片 → 零填充到整帧
# ===========================================================================


class TestA304TailPadding:
    def test_10ms_tail_zero_padded(self) -> None:
        framer = PcmFramer(frame_bytes=CANONICAL_FRAME_BYTES, duration_ms=20)
        # 10 ms PCM16 = 320 B
        tail = b"\xab" * 320
        frames = framer.feed(tail)
        assert frames == []
        padded = framer.flush()
        assert padded is not None
        assert len(padded.data) == CANONICAL_FRAME_BYTES
        # 10ms 有效 + 10ms 零填充
        assert padded.data[320:] == b"\x00" * 320

    def test_zero_tail_returns_none(self) -> None:
        framer = PcmFramer()
        assert framer.flush() is None


# ===========================================================================
# A3-05 · loop 绑定：跨 loop 调用 → MediaLoopMismatch
# ===========================================================================


class TestA305LoopBinding:
    def test_create_outside_loop_raises(self) -> None:
        with pytest.raises(MediaLoopMismatch):
            MediaSession(connection_id="c1", trace_id="t1", config=_config())

    def test_create_inside_loop_ok(self) -> None:
        async def _inner() -> None:
            sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
            assert sess.state is MediaState.OPEN
            await sess.close()

        asyncio.run(_inner())


# ===========================================================================
# A3-06 · 幂等关闭：连续关闭 3 次 → 无异常，state=closed
# ===========================================================================


class TestA306IdempotentClose:
    @pytest.mark.asyncio
    async def test_close_three_times_no_error(self) -> None:
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        await sess.close()
        await sess.close()
        await sess.close()
        assert sess.state is MediaState.CLOSED


# ===========================================================================
# A3-07 · 打断：barge_in 清队列 + 旧 turn 入队拒绝
# ===========================================================================


class TestA307BargeIn:
    @pytest.mark.asyncio
    async def test_barge_in_clears_queue_and_rejects_old_turn(self) -> None:
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()
        turn_id = await sess.begin_turn()
        await asyncio.sleep(0.05)

        # 先 enqueue 一小段 100ms
        await sess.enqueue_tts_pcm(turn_id, b"\x00" * 3200)
        assert sess.playback_queue_depth > 0

        # barge_in：清队列 + 清空 playback_turn_id
        sess.barge_in()
        assert sess.playback_queue_depth == 0
        assert sess.playback_generation == 1

        # 旧 turn_id 再入队 → MediaStateError
        with pytest.raises(MediaStateError):
            await sess.enqueue_tts_pcm(turn_id, b"\x00" * 3200)

        await sess.close()


# ===========================================================================
# A3-08 · TTS 写轨：1 秒 16 kHz PCM → 50 帧 / 1 秒
# ===========================================================================


class TestA308TTSWriteTrack:
    def test_1s_16k_pcm_produces_50_frames_48k(self) -> None:
        pcm = b"\x00" * (16_000 * 2)  # 1 秒 16k mono s16
        frames = _tts_pcm_to_48k_frames(pcm)
        assert len(frames) == 50
        for f in frames:
            assert f.sample_rate == DOWNLINK_SAMPLE_RATE
            assert len(f.data) == DOWNLINK_FRAME_BYTES
            assert f.sample_count == DOWNLINK_FRAME_SAMPLES

    def test_duration_error_le_20ms(self) -> None:
        pcm = b"\x00" * (16_000 * 2)
        frames = _tts_pcm_to_48k_frames(pcm)
        total_ms = sum(f.duration_ms for f in frames)
        assert abs(total_ms - 1000.0) <= 20.0


# ===========================================================================
# A3-09 · 背压：超 15 秒音频 → 拒绝整段
# ===========================================================================


class TestA309Backpressure:
    @pytest.mark.asyncio
    async def test_over_15s_rejected_entirely(self) -> None:
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()
        turn_id = await sess.begin_turn()
        await asyncio.sleep(0.05)

        # 装满队列（750 帧 ≈ 15s），留少量空位
        sess._playback_queue = asyncio.Queue(maxsize=MAX_PLAYBACK_FRAMES)
        for _ in range(700):
            sess._playback_queue.put_nowait(_silence_frame(20))

        # 再入队 2s → 700+100=800 > 750 → 背压
        tts_pcm_2s = b"\x00" * (16_000 * 2 * 2)
        with pytest.raises(MediaBackpressureError):
            await sess.enqueue_tts_pcm(turn_id, tts_pcm_2s)

        # PC 不崩
        assert sess.state is not MediaState.FAILED
        await sess.close()


# ===========================================================================
# A3-10 · ICE 超时：不可达候选 → 5-6 秒内 FAILED
# ===========================================================================


class TestA310ICETimeout:
    @pytest.mark.asyncio
    async def test_ice_timeout_moves_to_failed(self) -> None:
        sess = MediaSession(
            connection_id="c1",
            trace_id="t1",
            config=_config(ice_timeout_seconds=2),
        )
        sess.create_peer_connection()

        answer = await sess.handle_offer(_valid_sdp())
        assert answer
        assert sess.state is MediaState.ANSWER_CREATED

        # ICE 看门狗 2s + 少量 guard
        await asyncio.sleep(2.5)
        # 允许两种中间态：FAILED（还在等 close schedule）或 CLOSED（已经跑完）
        assert sess.state in (MediaState.FAILED, MediaState.CLOSED)

        await asyncio.sleep(0.5)
        # 最终一定是 CLOSED
        assert sess.state is MediaState.CLOSED


# ===========================================================================
# A3-11 · 重复 offer：同连接第二个 offer → 默认拒绝
# ===========================================================================


class TestA311DuplicateOffer:
    @pytest.mark.asyncio
    async def test_second_offer_rejected(self) -> None:
        sess = MediaSession(connection_id="c1", trace_id="t1", config=_config())
        sess.create_peer_connection()
        await sess.handle_offer(_valid_sdp())

        with pytest.raises(MediaStateError):
            await sess.handle_offer(_valid_sdp())

        await sess.close()


# ===========================================================================
# A3-12 · trace_id 全流程一致
# ===========================================================================


class TestA312TraceId:
    @pytest.mark.asyncio
    async def test_trace_id_preserved_throughout(self) -> None:
        expected = "trace-abc-123"
        sess = MediaSession(connection_id="c1", trace_id=expected, config=_config())
        assert sess.trace_id == expected
        sess.create_peer_connection()
        assert sess.trace_id == expected
        await sess.close()
        assert sess.trace_id == expected


# ===========================================================================
# 额外：validate_downlink_pcm / MediaConfig / MediaSessionRegistry
# ===========================================================================


class TestValidateDownlinkPcm:
    def test_rejects_empty(self) -> None:
        with pytest.raises(MediaFormatError):
            validate_downlink_pcm(b"")

    def test_rejects_odd_length(self) -> None:
        with pytest.raises(MediaFormatError):
            validate_downlink_pcm(b"\x00" * 5)

    def test_rejects_wave_header(self) -> None:
        with pytest.raises(MediaFormatError):
            validate_downlink_pcm(b"RIFF....")


class TestMediaConfigValidation:
    def test_rejects_zero_timeout(self) -> None:
        with pytest.raises(MediaConfigError):
            _config(ice_timeout_seconds=0)

    def test_rejects_invalid_vad(self) -> None:
        with pytest.raises(MediaConfigError):
            _config(vad_aggressiveness=5)

    def test_loopback_stun_excluded(self) -> None:
        c = _config(stun_url="stun:localhost:3478", ice_timeout_seconds=1)
        assert c.ice_servers() == []


class TestMediaSessionRegistry:
    @pytest.mark.asyncio
    async def test_registry_lifecycle(self) -> None:
        reg = MediaSessionRegistry()
        config = _config()

        sess1 = reg.create_session("c1", "t1", config=config)
        assert reg.active_sessions == 1
        assert reg.get_session("c1") is sess1

        await reg.close_session("c1")
        assert reg.active_sessions == 0
        assert reg.get_session("c1") is None

    @pytest.mark.asyncio
    async def test_registry_duplicate_connection(self) -> None:
        reg = MediaSessionRegistry()
        config = _config()
        reg.create_session("c1", "t1", config=config)
        with pytest.raises(MediaStateError):
            reg.create_session("c1", "t1", config=config)
        await reg.close_session("c1")


# ===========================================================================
# A3-13 · A3 → A5 30 ms 帧交接：VadSession 判决 → speech_start / speech_end
# ===========================================================================


def _vad_pcm16(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def _vad_silence(index: int) -> bytes:
    """30 ms 低能量帧：幅值 5 的 1 Hz 正弦（webrtcvad 稳定判为非语音）。"""
    import math

    return _vad_pcm16(
        [
            int(5 * math.sin(2 * math.pi * 1.0 * ((index * VAD_FRAME_SAMPLES + n) / 16_000)))
            for n in range(VAD_FRAME_SAMPLES)
        ]
    )


def _vad_voiced(index: int) -> bytes:
    """30 ms 近似浊音帧：150 Hz 基频 + 谐波 + 3 Hz 包络（与 test_audio_vad 同算法）。"""
    import math

    samples: list[int] = []
    for n in range(VAD_FRAME_SAMPLES):
        t = (index * VAD_FRAME_SAMPLES + n) / 16_000
        envelope = 0.6 + 0.4 * math.sin(2 * math.pi * 3.0 * t)
        value = (
            0.55 * math.sin(2 * math.pi * 150.0 * t)
            + 0.25 * math.sin(2 * math.pi * 450.0 * t)
            + 0.12 * math.sin(2 * math.pi * 1200.0 * t)
            + 0.08 * math.sin(2 * math.pi * 2400.0 * t)
        )
        samples.append(int(max(-1.0, min(1.0, value * envelope)) * 20_000))
    return _vad_pcm16(samples)


class TestA313VadHandoff:
    @pytest.mark.asyncio
    async def test_30ms_frames_reach_vad_session_and_emit_events(self) -> None:
        events: list[VadEvent] = []

        async def _on_vad_event(connection_id: str, event: VadEvent) -> None:
            assert connection_id == "c1"
            events.append(event)

        sess = MediaSession(
            connection_id="c1",
            trace_id="t1",
            config=_config(),
            on_vad_event=_on_vad_event,
        )

        frames: list[PcmFrame] = []
        # 25 帧静音填满 A5 的 600 ms preRoll，再 5 帧浊音触发 speechstart。
        frames.extend(_make_frame(_vad_silence(i), duration_ms=30) for i in range(25))
        frames.extend(_make_frame(_vad_voiced(i), duration_ms=30) for i in range(5))
        # 35 帧静音 ≥ 27 帧判定阈值 + webrtcvad hangover → speechend。
        frames.extend(_make_frame(_vad_silence(30 + j), duration_ms=30) for j in range(35))

        for frame in frames:
            sess._dispatch_frames([], [frame])
        await asyncio.sleep(0.05)

        kinds = [event.kind for event in events]
        assert VadEventKind.SPEECH_START in kinds
        assert kinds[-1] is VadEventKind.SPEECH_END
        segment = events[-1].segment
        assert segment is not None
        assert len(segment) % VAD_FRAME_BYTES == 0
        assert len(segment) >= 5 * VAD_FRAME_BYTES
        assert sess.vad_frames_received == len(frames)

        await sess.close()
        assert sess.pending_tasks == 0
