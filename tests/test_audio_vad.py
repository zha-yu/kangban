"""app/audio/vad.py 单元测试。

**关键覆盖**（spikes/vad/spike_webrtcvad.py 已验证过底层 webrtcvad 行为，
这里不重复测底层，重点测**包装层**的前置校验、帧常量、VadSession 状态机）：

1. 非法帧长安全跳过（不抛 _webrtcvad.Error 冒泡）
2. 静音帧不触发任何吐段（preRoll 满 20 帧后继续滑动）
3. 浊音帧触发 speechstart → speechend → flush 返回完整段
4. preRoll 缓冲：吐段包含之前的 600ms 静音帧（句首保护）
5. 强制切段 30s 封顶
6. reset / flush 语义正确
7. aggressiveness 0-3 全区间可实例化
"""

from __future__ import annotations

import math
import struct

from app.audio.vad import (
    FRAME_MS,
    FRAME_SAMPLES,
    MAX_SPEECH_MS,
    PRE_ROLL_FRAMES,
    PRE_ROLL_MS,
    SAMPLE_RATE,
    VadSession,
)

# ---------------------------------------------------------------------------
# 测试数据构造（与 spikes/vad/spike_webrtcvad.py 同算法，已验证能被 webrtcvad 识别）
# ---------------------------------------------------------------------------


def _pcm(samples: list[int]) -> bytes:
    """[-32768, 32767] 采样序列 → PCM16 little-endian bytes。"""
    return struct.pack(f"<{len(samples)}h", *samples)


def silence_frame(index: int = 0) -> bytes:
    """30ms / 16k 静音帧（低能量非零，幅值 5 的 1Hz 正弦波）。

    webrtcvad aggressiveness=2 对**全 0 帧**判定不稳定（随机 True/False），
    但对极低能量非零帧稳定判为无语音（实测 20/20 均 False）。
    用 index 参数让波形微变，更贴近真实环境底噪（纯数学波形仍非真实噪声，
    但对 webrtcvad 足够稳定）。
    """
    samples: list[int] = []
    for n in range(FRAME_SAMPLES):
        t = (index * FRAME_SAMPLES + n) / SAMPLE_RATE
        samples.append(int(5 * math.sin(2 * math.pi * 1.0 * t)))  # 1Hz, amplitude=5
    return _pcm(samples)


def voiced_frame(index: int) -> bytes:
    """近似浊音的合成信号（150Hz 基频 + 共振峰谐波 + 3Hz 包络起伏）。

    原算法：spikes/vad/spike_webrtcvad.py::voiced_frame，已验证 webrtcvad
    在 aggressive=2 时多数帧能判定为 speech。
    """
    samples: list[int] = []
    for n in range(FRAME_SAMPLES):
        t = (index * FRAME_SAMPLES + n) / SAMPLE_RATE
        envelope = 0.6 + 0.4 * math.sin(2 * math.pi * 3.0 * t)
        value = (
            0.55 * math.sin(2 * math.pi * 150.0 * t)
            + 0.25 * math.sin(2 * math.pi * 450.0 * t)
            + 0.12 * math.sin(2 * math.pi * 1200.0 * t)
            + 0.08 * math.sin(2 * math.pi * 2400.0 * t)
        )
        samples.append(int(max(-1.0, min(1.0, value * envelope)) * 20000))
    return _pcm(samples)


# ---------------------------------------------------------------------------
# 1. 帧常量与前置校验（包装层最核心的安全网）
# ---------------------------------------------------------------------------


class TestFrameConstants:
    def test_constants_align(self) -> None:
        """所有帧常量与 16k / 30ms 基准一致。"""
        assert SAMPLE_RATE == 16_000
        assert FRAME_MS == 30
        assert FRAME_SAMPLES == 16_000 * 30 // 1000  # 480
        assert PRE_ROLL_MS == 600
        assert PRE_ROLL_FRAMES == 600 // 30  # 20
        assert MAX_SPEECH_MS == 30_000

    def test_aggressiveness_0_through_3_all_valid(self) -> None:
        for mode in range(4):
            session = VadSession(aggressiveness=mode)
            assert session.aggressiveness == mode

    def test_illegal_frame_skipped_silently(self) -> None:
        """最关键的安全网：非法帧长 → 跳过 + 不抛异常。

        spike 已验证原生抛 _webrtcvad.Error（不是 ValueError），
        包装层必须靠 valid_rate_and_frame_length 前置拦截。
        """
        session = VadSession()
        broken = _pcm([0] * 100)  # 100 样本 ≈ 6.25ms，非法帧长
        # 直接调 feed_frame —— 不应该抛任何异常
        result = session.feed_frame(broken)
        assert result is None  # 跳过，不产生段
        # 之后合法帧继续工作
        result2 = session.feed_frame(silence_frame(0))
        assert result2 is None


# ---------------------------------------------------------------------------
# 2. 静音 / 浊音判定
# ---------------------------------------------------------------------------


class TestSilenceDetection:
    def test_pure_silence_never_emits_segment(self) -> None:
        """连续 100 帧静音 → 不触发 speechend，flush 返回 None。"""
        session = VadSession()
        for i in range(100):
            assert session.feed_frame(silence_frame(i)) is None
        assert session.flush() is None

    def test_pre_roll_window_mainteins_sliding(self) -> None:
        """连续静音帧超过 preRoll 窗口后，deque 自动溢出最旧帧。"""
        session = VadSession()
        n_frames = PRE_ROLL_FRAMES + 100
        for i in range(n_frames):
            session.feed_frame(silence_frame(i))
        # flush 前 session._in_speech 应该还是 False（没浊音）
        assert session.is_speech is False
        assert session.flush() is None


class TestVoicedDetection:
    def test_voiced_frames_trigger_segment(self) -> None:
        """浊音 N 帧 + 0.8s 静音 → flush 返回完整段。

        webrtcvad 有 hangover：连续浊音喂完后前 3 帧静音仍被判为 speech
        （内置句末保护），所以实际需要多喂几帧补偿。
        """
        session = VadSession()
        # 先喂 30 帧静音（确保 preRoll 填满）
        for i in range(30):
            session.feed_frame(silence_frame(i))
        # 喂 5 帧浊音
        for i in range(5):
            session.feed_frame(voiced_frame(i))
        # 喂 35 帧静音（≥ 27 + 3 hangover）→ speechend
        segment = None
        for j in range(35):
            result = session.feed_frame(silence_frame(30 + 5 + j))
            if result is not None:
                segment = result
                break

        assert segment is not None, "浊音 + 足够静音后应该吐段"
        # segment 应该非空
        assert len(segment) > 0
        # segment 长度至少包含：5 帧浊音 × 480 样本 × 2 bytes = 4800
        assert len(segment) >= 5 * FRAME_SAMPLES * 2


# ---------------------------------------------------------------------------
# 3. preRoll 句首保护
# ---------------------------------------------------------------------------


class TestPreRoll:
    def test_segment_contains_preroll_frames(self) -> None:
        """吐段里包含 preRoll 缓冲的 20 帧（句首保护生效）。"""
        session = VadSession()
        # 先喂 25 帧静音 → preRoll 满
        for i in range(25):
            session.feed_frame(silence_frame(i))
        # 喂 5 帧浊音 → speechstart，此时 preRoll 20 帧被先吐进 buffer
        for i in range(5):
            session.feed_frame(voiced_frame(i))
        # 35 帧静音（≥ 27 + 3 hangover）→ speechend
        for j in range(35):
            result = session.feed_frame(silence_frame(25 + 5 + j))
            if result is not None:
                segment = result
                break

        assert segment is not None
        # segment 长度 ≥ preRoll(20 帧) × 480 × 2 = 19200
        # 加上 5 帧浊音 × 480 × 2 = 4800
        # 总 ≥ 24000 bytes
        assert len(segment) >= (PRE_ROLL_FRAMES + 5) * FRAME_SAMPLES * 2


# ---------------------------------------------------------------------------
# 4. 强制切段 + reset/flush 语义
# ---------------------------------------------------------------------------


class TestForceCut:
    def test_max_speech_forces_segment(self) -> None:
        """MAX_SPEECH_MS / FRAME_MS = 1000 帧浊音 → 第 1000 帧强制吐段。"""
        session = VadSession()
        # preRoll 先填满
        for i in range(PRE_ROLL_FRAMES + 5):
            session.feed_frame(silence_frame(i))
        # 喂 1000 帧浊音（每 100 帧取一次检查结果）
        segment = None
        for i in range(1000):
            result = session.feed_frame(voiced_frame(i))
            if result is not None:
                segment = result
                break

        assert segment is not None, "喂满 1000 帧浊音应该触发强制切段"


class TestResetFlushSemantics:
    def test_reset_discards_all_state(self) -> None:
        """reset 后立即清空，之后的 flush 返回 None。"""
        session = VadSession()
        for i in range(25):
            session.feed_frame(silence_frame(i))
        # 还没在说话
        session.reset()
        assert session.flush() is None

    def test_reset_during_speech_discards(self) -> None:
        """喂浊音触发 speechstart → reset → flush 返回 None（段被丢）。"""
        session = VadSession()
        for i in range(30):
            session.feed_frame(silence_frame(i))
        for i in range(5):
            session.feed_frame(voiced_frame(i))
        assert session.is_speech is True
        session.reset()
        assert session.is_speech is False
        assert session.flush() is None

    def test_flush_after_silence_returns_none(self) -> None:
        """纯静音后 flush → None（没有段可吐）。"""
        session = VadSession()
        for i in range(50):
            session.feed_frame(silence_frame(i))
        assert session.flush() is None


# ---------------------------------------------------------------------------
# 5. 多会话并存（两个 VadSession 互不干扰）
# ---------------------------------------------------------------------------


class TestMultiSession:
    def test_two_sessions_independent(self) -> None:
        """两个 VadSession 实例互不串状态。"""
        s1 = VadSession()
        s2 = VadSession()

        # s1 喂浊音 + s2 全静音
        for i in range(30):
            s1.feed_frame(silence_frame(i))
            s2.feed_frame(silence_frame(100 + i))
        for i in range(5):
            s1.feed_frame(voiced_frame(i))
            s2.feed_frame(silence_frame(200 + i))

        assert s1.is_speech is True
        assert s2.is_speech is False

        # s1 静音结束吐段（35 帧 ≥ 27 + 3 hangover）；s2 flush 应还是 None
        s1_segment = None
        for j in range(35):
            r = s1.feed_frame(silence_frame(250 + j))
            if r is not None:
                s1_segment = r
                break
        assert s1_segment is not None
        assert s2.flush() is None
