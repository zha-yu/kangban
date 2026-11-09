"""Day 4 A2 · B3/B4 接线测试：Redis 生命周期、FINAL seam、A3→A2 音频交接。

- B3：`app.main` lifespan 创建并关闭 Redis/LLM/Dialogue 运行时，不依赖真实 Redis；
- B4：`signaling.dispatch_asr_event` 先投递字幕，再对 FINAL 调用 async handler；
- A3：`webrtc._asr_callback` 从 VAD segment 生成 WAV 并交给 FINAL handler。
全部为进程内测试，不访问真实 PostgreSQL/Redis/Minimax。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator

import pytest

import app.main as app_main
from app.audio import signaling, webrtc
from app.audio.asr import ASREvent, ASREventKind, ASRProvider, ASRResult
from app.audio.tts import build_tts
from app.core.config import Settings
from app.main import create_app

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeRedisClient:
    """只实现 lifespan 需要的 aclose；不连接真实 Redis。"""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _make_conn(device_id: str) -> signaling.ConnectionContext:
    conn = signaling.ConnectionContext(trace_id="a" * 32, websocket=None)  # type: ignore[arg-type]
    conn.device_id = device_id
    conn.authed = True
    signaling._registry[device_id] = conn
    return conn


@pytest.fixture
def seam() -> Iterator[dict[str, signaling.ConnectionContext]]:
    conns: dict[str, signaling.ConnectionContext] = {}
    try:
        yield conns
    finally:
        for device_id in conns:
            signaling._registry.pop(device_id, None)
            webrtc._latest_segments.pop(device_id, None)
        signaling.set_dialogue_final_handler(None)


def _connect(
    seam: dict[str, signaling.ConnectionContext], device_id: str
) -> signaling.ConnectionContext:
    conn = _make_conn(device_id)
    seam[device_id] = conn
    return conn


def _final(text: str = "你好") -> ASREvent:
    return ASREvent(
        kind=ASREventKind.FINAL,
        result=ASRResult(text=text, is_final=True, confidence=None),
        error=None,
    )


def _partial(text: str = "你") -> ASREvent:
    return ASREvent(
        kind=ASREventKind.PARTIAL,
        result=ASRResult(text=text, is_final=False, confidence=None),
        error=None,
    )


# ---------------------------------------------------------------------------
# B3：Redis / LLM / Dialogue 生命周期
# ---------------------------------------------------------------------------


async def test_lifespan_creates_and_closes_dialogue_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = FakeRedisClient()
    monkeypatch.setattr(app_main.redis_asyncio, "from_url", lambda *a, **k: fake_redis)

    application = create_app(Settings(app_env="test"))
    async with application.router.lifespan_context(application):
        assert application.state.redis is fake_redis
        assert application.state.dialogue_service is not None
        assert application.state.dialogue_orchestrator is not None
        assert application.state.llm_service is not None
        assert application.state.tts_provider is not None
        assert signaling._dialogue_final_handler is not None

    assert fake_redis.closed is True
    assert signaling._dialogue_final_handler is None


# ---------------------------------------------------------------------------
# B4：FINAL 分发与字幕顺序
# ---------------------------------------------------------------------------


async def test_dispatch_partial_does_not_call_dialogue_handler(
    seam: dict[str, signaling.ConnectionContext],
) -> None:
    conn = _connect(seam, "dev-a")
    calls: list[tuple[str, str, str, str, bytes | None]] = []

    async def handler(
        device_id: str,
        turn_id: str,
        trace_id: str,
        text: str,
        audio_bytes: bytes | None,
    ) -> None:
        calls.append((device_id, turn_id, trace_id, text, audio_bytes))

    signaling.set_dialogue_final_handler(handler)
    assert await signaling.dispatch_asr_event("dev-a", "turn-1", "trace-1", _partial()) is True

    assert calls == []
    assert conn.downstream.get_nowait() == {"type": "asr_partial", "text": "你"}


async def test_dispatch_final_subtitle_first_then_handler_once(
    seam: dict[str, signaling.ConnectionContext],
) -> None:
    conn_a = _connect(seam, "dev-a")
    conn_b = _connect(seam, "dev-b")
    calls: list[tuple[str, str, str, str, bytes | None]] = []

    async def handler(
        device_id: str,
        turn_id: str,
        trace_id: str,
        text: str,
        audio_bytes: bytes | None,
    ) -> None:
        calls.append((device_id, turn_id, trace_id, text, audio_bytes))

    signaling.set_dialogue_final_handler(handler)
    assert (
        await signaling.dispatch_asr_event(
            "dev-a", "turn-1", "trace-1", _final("你好"), b"wave-bytes"
        )
        is True
    )

    # 字幕先入本设备队列；handler 收到同一 device/turn/text/audio
    assert conn_a.downstream.get_nowait() == {"type": "asr_final", "text": "你好"}
    assert calls == [("dev-a", "turn-1", "trace-1", "你好", b"wave-bytes")]
    # 双设备隔离：B 不收到 A 的字幕，也不触发 A 的 handler
    assert conn_b.downstream.empty()


async def test_dispatch_final_survives_dispatch_cancellation(
    seam: dict[str, signaling.ConnectionContext],
) -> None:
    """S4 试跑修复①：ws_superseded 取消 dispatch 所在任务链时，在跑的一轮对话不陪葬。

    shield 让 handler 脱离取消传播继续后台执行（分段文字/TTS 喂轨由 deliver 按
    device_id 接管到新连接），取消语义照常向上传播给旧连接的任务链。
    """
    conn = _connect(seam, "dev-a")
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def handler(
        device_id: str,
        turn_id: str,
        trace_id: str,
        text: str,
        audio_bytes: bytes | None,
    ) -> None:
        started.set()
        await release.wait()
        finished.set()

    signaling.set_dialogue_final_handler(handler)
    task = asyncio.create_task(
        signaling.dispatch_asr_event("dev-a", "turn-1", "trace-1", _final("你好"), None)
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    # 旧连接任务链被取消（ws_superseded 的等价物）
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # handler 不受影响，放行后跑完（分段/喂轨随后经 deliver 落到当前连接）
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=2)
    assert conn.downstream.get_nowait() == {"type": "asr_final", "text": "你好"}


async def test_dispatch_handler_failure_does_not_break_subtitle(
    seam: dict[str, signaling.ConnectionContext],
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = _connect(seam, "dev-a")

    async def handler(
        device_id: str,
        turn_id: str,
        trace_id: str,
        text: str,
        audio_bytes: bytes | None,
    ) -> None:
        raise RuntimeError("dialogue handler failed")

    signaling.set_dialogue_final_handler(handler)
    with caplog.at_level(logging.ERROR, logger="app.audio.signaling"):
        assert (
            await signaling.dispatch_asr_event("dev-a", "turn-1", "trace-1", _final("你好")) is True
        )

    # 字幕仍已投递；异常只记录，不反抛
    assert conn.downstream.get_nowait() == {"type": "asr_final", "text": "你好"}
    assert any(record.getMessage() == "dialogue_final_handler_failed" for record in caplog.records)
    assert "dialogue handler failed" not in caplog.text


# ---------------------------------------------------------------------------
# A3 → A2：VAD segment → WAV → FINAL handler
# ---------------------------------------------------------------------------


async def test_webrtc_asr_callback_passes_wav_from_vad_segment(
    seam: dict[str, signaling.ConnectionContext],
) -> None:
    conn = _connect(seam, "dev-a")
    calls: list[bytes | None] = []

    async def handler(
        device_id: str,
        turn_id: str,
        trace_id: str,
        text: str,
        audio_bytes: bytes | None,
    ) -> None:
        calls.append(audio_bytes)

    signaling.set_dialogue_final_handler(handler)
    webrtc._latest_segments["dev-a"] = b"\x00\x00" * 800  # 100ms 16k mono PCM16

    callback = webrtc._asr_callback("dev-a", "trace-1")
    await callback("turn-1", _final("你好"))

    assert calls and calls[0] is not None
    assert calls[0].startswith(b"RIFF")
    assert b"WAVE" in calls[0][:16]
    assert conn.downstream.get_nowait() == {"type": "asr_final", "text": "你好"}
    assert "dev-a" not in webrtc._latest_segments


async def test_webrtc_partial_does_not_consume_or_pass_audio(
    seam: dict[str, signaling.ConnectionContext],
) -> None:
    _connect(seam, "dev-a")
    calls: list[bytes | None] = []

    async def handler(
        device_id: str,
        turn_id: str,
        trace_id: str,
        text: str,
        audio_bytes: bytes | None,
    ) -> None:
        calls.append(audio_bytes)

    signaling.set_dialogue_final_handler(handler)
    webrtc._latest_segments["dev-a"] = b"\x00\x00" * 800

    callback = webrtc._asr_callback("dev-a", "trace-1")
    await callback("turn-1", _partial("你"))

    assert calls == []
    assert webrtc._latest_segments["dev-a"] == b"\x00\x00" * 800


class _FallbackASRProvider(ASRProvider):
    """W4 兜底段专用桩：吞掉整段音频后固定回一个 FINAL。"""

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[ASREvent]:
        return self._events(audio)

    async def _events(self, audio: AsyncIterator[bytes]) -> AsyncIterator[ASREvent]:
        async for _frame in audio:
            pass
        yield _final("你好")


async def test_vad_fallback_segment_runs_asr_and_reaches_dialogue_handler(
    seam: dict[str, signaling.ConnectionContext],
) -> None:
    """W4 兜底段无外部回调时 → ASR FINAL → 字幕 + dialogue handler + WAV 声纹音频。"""
    conn = _connect(seam, "dev-vad")
    calls: list[tuple[str, str, str, str, bytes | None]] = []

    async def handler(
        device_id: str,
        turn_id: str,
        trace_id: str,
        text: str,
        audio_bytes: bytes | None,
    ) -> None:
        calls.append((device_id, turn_id, trace_id, text, audio_bytes))

    signaling.set_dialogue_final_handler(handler)
    registry = webrtc._signaling_registry()
    session = registry.create_session(
        "dev-vad",
        conn.trace_id,
        asr_provider=_FallbackASRProvider(),
        on_asr_event=webrtc._asr_callback("dev-vad", conn.trace_id),
    )
    try:
        await session._handle_vad_segment(b"\x00\x00" * 800)

        assert len(calls) == 1
        device_id, turn_id, trace_id, text, audio_bytes = calls[0]
        assert (device_id, trace_id) == ("dev-vad", conn.trace_id)
        assert text == "你好"
        assert turn_id
        assert audio_bytes is not None and audio_bytes.startswith(b"RIFF")
        assert conn.downstream.get_nowait() == {"type": "asr_final", "text": "你好"}
        # 兜底 turn 与 A2 回复的 TTS 写轨 turn 对齐
        assert session._playback_turn_id == turn_id
        assert "dev-vad" not in webrtc._latest_segments
    finally:
        await registry.close_session("dev-vad")


# ---------------------------------------------------------------------------
# TTS：文本 → PCM → A3 播放轨
# ---------------------------------------------------------------------------


async def test_tts_pcm_handoff_to_webrtc_session() -> None:
    provider = build_tts(Settings(tts_provider="mock"))
    pcm = await provider.synthesize("你好")
    assert pcm
    assert len(pcm) % 2 == 0

    registry = webrtc._signaling_registry()
    device_id = "tts-device-a"
    session = registry.create_session(device_id, "trace-1")
    calls: list[tuple[str, bytes]] = []

    async def fake_enqueue(turn_id: str, pcm_bytes: bytes) -> None:
        calls.append((turn_id, pcm_bytes))

    session.enqueue_tts_pcm = fake_enqueue  # type: ignore[method-assign]
    try:
        assert await webrtc.enqueue_tts_for_device(device_id, "turn-1", pcm) is True
        assert calls == [("turn-1", pcm)]
    finally:
        await registry.close_session(device_id)

    # 会话关闭后不再写轨，且不抛异常
    assert await webrtc.enqueue_tts_for_device(device_id, "turn-1", pcm) is False
