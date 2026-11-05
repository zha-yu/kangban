"""A1 · N1/N2/N3 验收测试：Mock 适配器、FunASR 2pass 协议适配器、工厂归一。

覆盖（计划 §T1.2 的四条假 WS server 用例 + C1 语义）：

| 用例 | 断言 |
| --- | --- |
| 正常流 | 握手帧逐字段相等、音频帧原样、收尾顺序、online→PARTIAL / offline→FINAL |
| 超时 | `receive_timeout` 到点产出 `ERROR(ASRUnavailable)`，不会把调用方挂住 |
| 乱序帧 | offline 终稿先到 → 只产出一个 FINAL，迟到的 online 不再吐事件 |
| 非法 JSON | `ERROR(ASRProtocolError)`，且错误文案不回显对端原文 |
| confidence | 观测日志里是 `None`（JSON 渲染成 `null`），不是 `0.0` |

假服务端用本机回环 `websockets.serve`（真实握手、真实帧），超时/禁 ping 两条用
`monkeypatch` 换掉 `websockets.connect` 塞假连接 —— 超时用例需要「永不回帧」的对端，
真服务端做不到（关服会变成「对端断开」，断言就串到另一条分支上去了）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from websockets.asyncio.server import ServerConnection, serve

from app.audio import asr
from app.audio.asr import (
    ASREvent,
    ASREventKind,
    ASRProtocolError,
    ASRProvider,
    ASRUnavailable,
    CloudASRProvider,
    FunASRWSProvider,
    MockASRProvider,
    build_asr,
    load_mock_script,
)
from app.core.config import Settings

# 协议口径写死在这里（不引用 asr.FUNASR_HANDSHAKE）：实现被顺手改了要能红。
EXPECTED_HANDSHAKE: dict[str, object] = {
    "mode": "2pass",
    "chunk_size": [5, 10, 5],
    "audio_fs": 16_000,
    "wav_format": "pcm",
    "itn": True,
}


# ------------------------------- 公共夹具 -------------------------------
@dataclass
class Capture:
    """服务端抓到的东西：握手帧、音频帧、收尾帧、子协议。"""

    handshake: dict[str, object] | None = None
    audio: list[bytes] = field(default_factory=list)
    tail: list[dict[str, object]] = field(default_factory=list)
    subprotocol: str | None = None


Reply = Callable[[ServerConnection, Capture], Awaitable[None]]


@asynccontextmanager
async def _funasr_server(reply: Reply) -> AsyncIterator[tuple[Capture, str]]:
    """起一个本机假 FunASR：收到 `is_end` 后按 `reply` 回帧，然后关连接。"""
    capture = Capture()

    async def handler(connection: ServerConnection) -> None:
        capture.subprotocol = connection.subprotocol
        capture.handshake = json.loads(await connection.recv())
        async for message in connection:
            if isinstance(message, bytes):
                capture.audio.append(message)
                continue
            payload = json.loads(message)
            capture.tail.append(payload)
            if payload.get("is_end") is True:
                await reply(connection, capture)
                return

    server = await serve(handler, "127.0.0.1", 0, subprotocols=["binary"])
    port = server.sockets[0].getsockname()[1]
    try:
        yield capture, f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def _silence(count: int) -> AsyncIterator[bytes]:
    """假音频：count 个标准帧（60ms / 1920B）。"""
    for _ in range(count):
        yield b"\x00" * asr.FRAME_BYTES


async def _collect(provider: ASRProvider, chunks: int = 3) -> list[ASREvent]:
    """消费到迭代器自然结束（吐尽 FINAL/ERROR）：清理与埋点都在那一刻落地。"""
    events = await provider.stream(_silence(chunks))
    return [event async for event in events]


def _observations(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "app.audio.observe"]


def _dead_port() -> int:
    """拿一个刚被释放的端口：连它必被拒，用来验「建连失败」分类。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class FakeConnection:
    """假连接 `websockets.connect` 的替身。

    `reply=None` 表示**永不回帧**（压超时分支）；给 `reply` 则等 `is_end` 到了才回帧，
    贴近真实时序 —— 否则发送任务还没跑完就被终稿打断，断言 `sent` 会飘。
    """

    subprotocol = "binary"

    def __init__(self, reply: list[str | bytes] | None = None) -> None:
        self.sent: list[str | bytes] = []
        self.closed = False
        self.reply = reply
        self._end_seen = asyncio.Event()

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[str | bytes]:
        if self.reply is None:
            await asyncio.Event().wait()  # 永不回帧，只能靠 receive_timeout 收口
        else:
            await self._end_seen.wait()  # 吃完 is_end 再回帧，贴近真实时序
            for message in self.reply:
                yield message

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)
        if isinstance(message, str) and json.loads(message).get("is_end") is True:
            self._end_seen.set()

    async def close(self) -> None:
        self.closed = True


# ------------------------------- 口径与工厂 -------------------------------
def test_frame_contract_is_1920_bytes() -> None:
    # 60ms × 16kHz × 16bit × 单声道 = 1920B（C1 钉死，A3 按这个切帧）
    assert asr.FRAME_BYTES == 1920
    assert asr.FRAME_BYTES == 16_000 * 60 // 1000 * 2 * 1


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("", MockASRProvider),
        ("mock", MockASRProvider),
        ("  MOCK  ", MockASRProvider),
        ("funasr", FunASRWSProvider),
        ("  FUNASR ", FunASRWSProvider),
        ("cloud", CloudASRProvider),
        ("CLOUD", CloudASRProvider),
    ],
)
def test_build_asr_normalizes_provider(kind: str, expected: type[ASRProvider]) -> None:
    assert isinstance(build_asr(Settings(asr_provider=kind)), expected)


def test_build_asr_unknown_provider_falls_back_to_mock(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.audio.asr")
    assert isinstance(build_asr(Settings(asr_provider="whisper")), MockASRProvider)
    assert "unknown ASR_PROVIDER" in caplog.text


def test_build_asr_funasr_defaults_to_local_7002() -> None:
    provider = build_asr(Settings(asr_provider="funasr", asr_base_url="   "))
    assert isinstance(provider, FunASRWSProvider)
    assert provider.url == "ws://localhost:7002"  # 7001 是 laya 的口，别串


def test_build_asr_funasr_honours_configured_base_url() -> None:
    provider = build_asr(Settings(asr_provider="funasr", asr_base_url=" ws://10.0.0.5:7002 "))
    assert isinstance(provider, FunASRWSProvider)
    assert provider.url == "ws://10.0.0.5:7002"


def test_build_asr_without_argument_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asr, "get_settings", lambda: Settings(asr_provider="funasr"))
    assert isinstance(build_asr(), FunASRWSProvider)


# --------------------------------- Mock ---------------------------------
async def test_mock_streams_partials_then_final(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    events = await _collect(MockASRProvider())

    assert [event.kind for event in events] == [
        ASREventKind.PARTIAL,
        ASREventKind.PARTIAL,
        ASREventKind.FINAL,
    ]
    texts = [event.result.text for event in events if event.result is not None]
    assert texts == ["今天", "今天天气", "今天天气怎么样"]
    assert events[-1].result is not None
    assert events[-1].result.is_final is True
    assert all(event.result is not None and event.result.confidence is None for event in events)

    observation = _observations(caplog)[-1]
    assert observation.asr_provider == "mock"
    assert observation.asr_source == "mock"
    assert observation.asr_is_final is True
    # 关键语义：拿不到置信度就是 null，不是 0.0（0.0 会被下游当成「极低置信」）
    assert observation.asr_confidence is None
    assert json.dumps({"asr_confidence": observation.asr_confidence}) == (
        '{"asr_confidence": null}'
    )


async def test_mock_empty_final_is_returned_verbatim() -> None:
    # PRD FR-UI-05：没听清就交空终稿，适配器不造词、不 strip
    events = await _collect(MockASRProvider(script=load_mock_script("ASR_EMPTY_SCRIPT")))

    assert events[-1].kind is ASREventKind.FINAL
    assert events[-1].result is not None
    assert events[-1].result.text == "   "
    assert events[-1].result.text.strip() == ""


async def test_mock_latency_delays_each_event() -> None:
    started = time.monotonic()
    events = await _collect(MockASRProvider(latency_ms=20))
    elapsed = time.monotonic() - started

    assert len(events) == 3
    assert elapsed >= 0.06  # 3 步 × 20ms；只验旋钮生效，不卡机器快慢


async def test_mock_raise_error_surfaces_from_stream() -> None:
    provider = MockASRProvider(raise_error=ASRUnavailable("mock 链接失败"))

    with pytest.raises(ASRUnavailable, match="mock 链接失败"):
        await provider.stream(_silence(1))


def test_load_mock_script_rejects_missing_constant(tmp_path: Path) -> None:
    script = tmp_path / "asr_script.py"
    script.write_text("OTHER: list[int] = [1]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="缺少常量"):
        load_mock_script("ASR_MOCK_SCRIPT", script)


def test_load_mock_script_rejects_script_without_final(tmp_path: Path) -> None:
    script = tmp_path / "asr_script.py"
    script.write_text('S = [{"text": "a", "is_final": False}]\n', encoding="utf-8")

    with pytest.raises(ValueError, match="is_final=True"):
        load_mock_script("S", script)


# -------------------------------- FunASR --------------------------------
async def _reply_normal(connection: ServerConnection, capture: Capture) -> None:
    await connection.send(json.dumps({"mode": "2pass-online", "text": "今天", "is_final": False}))
    await connection.send(json.dumps({"mode": "2pass-online", "text": "今天天气"}))
    await connection.send(json.dumps({"mode": "2pass-offline", "text": "今天天气怎么样"}))


async def test_funasr_normal_stream_follows_frozen_protocol(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    async with _funasr_server(_reply_normal) as (capture, url):
        events = await _collect(FunASRWSProvider(url=url), chunks=2)

    # 握手帧：逐字段相等，多一个字段少一个字段都算破约
    assert capture.handshake == EXPECTED_HANDSHAKE
    assert capture.subprotocol == "binary"
    # 音频帧原样透传（不重切、不补帧）
    assert capture.audio == [b"\x00" * asr.FRAME_BYTES] * 2
    # 收尾顺序：先 is_speaking=false 再 is_end=true
    assert capture.tail == [{"is_speaking": False}, {"is_end": True}]

    assert [event.kind for event in events] == [
        ASREventKind.PARTIAL,
        ASREventKind.PARTIAL,
        ASREventKind.FINAL,
    ]
    assert events[-1].result is not None
    assert events[-1].result.text == "今天天气怎么样"
    assert all(event.result is not None and event.result.confidence is None for event in events)

    observation = _observations(caplog)[-1]
    assert observation.asr_provider == "funasr"
    assert observation.asr_source == "funasr"
    assert observation.asr_is_final is True
    assert observation.asr_confidence is None
    assert json.dumps({"asr_confidence": observation.asr_confidence}) == (
        '{"asr_confidence": null}'
    )


async def test_funasr_online_frame_with_is_final_closes_stream() -> None:
    async def reply(connection: ServerConnection, capture: Capture) -> None:
        await connection.send(
            json.dumps({"mode": "2pass-online", "text": "好的", "is_final": True})
        )

    async with _funasr_server(reply) as (_capture, url):
        events = await _collect(FunASRWSProvider(url=url), chunks=1)

    assert [event.kind for event in events] == [ASREventKind.FINAL]
    assert events[0].result is not None
    assert events[0].result.text == "好的"


async def test_funasr_invalid_json_is_protocol_error() -> None:
    async def reply(connection: ServerConnection, capture: Capture) -> None:
        await connection.send("这不是 JSON")

    async with _funasr_server(reply) as (_capture, url):
        events = await _collect(FunASRWSProvider(url=url), chunks=1)

    assert [event.kind for event in events] == [ASREventKind.ERROR]
    error = events[0].error
    assert isinstance(error, ASRProtocolError)
    assert "不是合法 JSON" in str(error)
    # 隐私：错误文案只报结构与长度，不回显对端原文（原文是长者语音转写）
    assert "这不是 JSON" not in str(error)


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "2pass-online"},  # 缺 text
        {"text": "没有 mode"},  # 缺 mode
        {"mode": "2pass-weird", "text": "未知 mode"},  # 未知 mode
        ["顶层不是对象"],
    ],
)
def test_parse_funasr_frame_rejects_bad_payloads(payload: object) -> None:
    event = asr._parse_funasr_frame(json.dumps(payload))

    assert event.kind is ASREventKind.ERROR
    assert isinstance(event.error, ASRProtocolError)


def test_parse_funasr_frame_rejects_binary_frame() -> None:
    event = asr._parse_funasr_frame(b"\x01\x02")

    assert event.kind is ASREventKind.ERROR
    assert isinstance(event.error, ASRProtocolError)


async def test_funasr_out_of_order_frames_yield_single_final() -> None:
    async def reply(connection: ServerConnection, capture: Capture) -> None:
        # 终稿先到、中间稿后到（协议不保证有序）；终稿之后必须收口
        await connection.send(json.dumps({"mode": "2pass-offline", "text": "先到的终稿"}))
        await asyncio.sleep(0.01)
        with contextlib.suppress(Exception):
            await connection.send(json.dumps({"mode": "2pass-online", "text": "迟到的中间稿"}))

    async with _funasr_server(reply) as (_capture, url):
        events = await _collect(FunASRWSProvider(url=url), chunks=1)

    assert [event.kind for event in events] == [ASREventKind.FINAL]
    assert events[0].result is not None
    assert events[0].result.text == "先到的终稿"


async def test_funasr_empty_final_returns_empty_text() -> None:
    async def reply(connection: ServerConnection, capture: Capture) -> None:
        await connection.send(json.dumps({"mode": "2pass-offline", "text": "   "}))

    async with _funasr_server(reply) as (_capture, url):
        events = await _collect(FunASRWSProvider(url=url), chunks=1)

    assert [event.kind for event in events] == [ASREventKind.FINAL]
    assert events[0].result is not None
    assert events[0].result.text == "   "
    assert events[0].result.text.strip() == ""


async def test_funasr_peer_close_before_final_is_unavailable() -> None:
    async def reply(connection: ServerConnection, capture: Capture) -> None:
        return  # 收到 is_end 直接挂断，不给 offline 终稿

    async with _funasr_server(reply) as (_capture, url):
        events = await _collect(FunASRWSProvider(url=url), chunks=1)

    assert [event.kind for event in events] == [ASREventKind.ERROR]
    assert isinstance(events[0].error, ASRUnavailable)
    assert "终稿前关闭" in str(events[0].error)


async def test_funasr_connect_refused_is_unavailable() -> None:
    provider = FunASRWSProvider(url=f"ws://127.0.0.1:{_dead_port()}", connect_timeout=1.0)

    with pytest.raises(ASRUnavailable, match="连接失败"):
        await provider.stream(_silence(1))


async def test_funasr_connect_kwargs_pin_ping_and_subprotocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture: dict[str, object] = {}
    connection = FakeConnection([json.dumps({"mode": "2pass-offline", "text": "好的"})])

    async def fake_connect(uri: str, **kwargs: object) -> FakeConnection:
        capture["uri"] = uri
        capture["kwargs"] = kwargs
        return connection

    monkeypatch.setattr(asr.websockets, "connect", fake_connect)
    events = await _collect(FunASRWSProvider(url="ws://funasr.test:7002"), chunks=2)

    kwargs = capture["kwargs"]
    assert isinstance(kwargs, dict)
    # 禁 ping 帧是硬要求：开了会被 FunASR 判超时踢连接
    assert kwargs["ping_interval"] is None
    assert kwargs["subprotocols"] == ["binary"]
    assert capture["uri"] == "ws://funasr.test:7002"
    assert connection.sent == [
        json.dumps(asr.FUNASR_HANDSHAKE),
        b"\x00" * asr.FRAME_BYTES,
        b"\x00" * asr.FRAME_BYTES,
        json.dumps({"is_speaking": False}),
        json.dumps({"is_end": True}),
    ]
    assert [event.kind for event in events] == [ASREventKind.FINAL]


async def test_funasr_receive_timeout_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeConnection(reply=None)  # 对端永不回帧

    async def fake_connect(uri: str, **kwargs: object) -> FakeConnection:
        return connection

    monkeypatch.setattr(asr.websockets, "connect", fake_connect)
    provider = FunASRWSProvider(url="ws://funasr.test:7002", receive_timeout=0.2)

    started = time.monotonic()
    events = await _collect(provider, chunks=1)
    elapsed = time.monotonic() - started

    assert [event.kind for event in events] == [ASREventKind.ERROR]
    assert isinstance(events[0].error, ASRUnavailable)
    assert "没有等到终稿" in str(events[0].error)
    assert elapsed >= 0.2  # 真的是等到点才收口，不是立刻放弃
    assert connection.closed is True


# --------------------------------- 云端 ---------------------------------
async def test_cloud_provider_is_not_wired_yet() -> None:
    with pytest.raises(ASRUnavailable, match="尚未接入"):
        await CloudASRProvider().stream(_silence(1))


# ------------------- 代码审查回归（2026-11-05 P0 三条） -------------------


def test_mock_script_lives_inside_package() -> None:
    """P0-1 回归：Mock 夹具必须随包发布，不能指向 `tests/`。

    `MockASRProvider` 是 `ASR_PROVIDER` 未配置时的**默认实现**；生产镜像里没有 `tests/`，
    夹具放在那里就等于默认配置在第一次 ASR 调用时抛 `FileNotFoundError`。
    """
    path = asr._MOCK_SCRIPT_PATH

    assert path.exists(), f"Mock 夹具缺失：{path}"
    assert "tests" not in path.parts, f"Mock 夹具不该在 tests/ 下：{path}"
    assert Path(asr.__file__).resolve().parent in path.parents, (
        f"Mock 夹具必须在 app/audio/ 包内：{path}"
    )
    assert load_mock_script(), "默认脚本必须能被 load_mock_script() 解析出步骤"


async def test_audio_source_error_surfaces_as_error_event(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """P0-2 回归：音频源抛错要**立刻**变成 ERROR 事件，不能拖到 15s 超时。

    修之前：异常留在 sender 任务里被 `gather(return_exceptions=True)` 吞掉，消费方只能等到
    `receive_timeout`，收到的错误是「FunASR …内没有等到终稿」—— 指向了对端，与真实原因无关。
    修之后：错误事件是 `ASRUnavailable("上行音频中断：…")`，一眼看出问题在 A3 的帧流。
    """
    caplog.set_level(logging.WARNING, logger="app.audio.asr")
    connection = FakeConnection(reply=None)  # 对端永不回帧，排除「对端先出错」这条干扰

    async def fake_connect(uri: str, **kwargs: object) -> FakeConnection:
        return connection

    async def broken_audio() -> AsyncIterator[bytes]:
        yield b"\x00" * asr.FRAME_BYTES
        raise RuntimeError("A3 的帧流断了")

    monkeypatch.setattr(asr.websockets, "connect", fake_connect)
    provider = FunASRWSProvider(url="ws://funasr.test:7002", receive_timeout=30.0)

    started = time.monotonic()
    events = await provider.stream(broken_audio())
    collected = [event async for event in events]
    elapsed = time.monotonic() - started

    assert [event.kind for event in collected] == [ASREventKind.ERROR]
    error = collected[0].error
    assert isinstance(error, ASRUnavailable)
    assert "上行音频中断" in str(error)
    assert "帧流断了" in str(error)
    assert elapsed < 5.0, f"必须立刻收口，不能等 receive_timeout；实际 {elapsed:.2f}s"
    assert any(
        record.name == "app.audio.asr" and "上行音频中断" in record.getMessage()
        for record in caplog.records
    ), "音频源故障必须留日志，否则线上仍然查不到原因"


async def test_mock_concurrent_streams_do_not_clobber() -> None:
    """P0-3 回归：同一 provider 实例并发 `stream()` 不能互相踩 drain task。

    修之前 drain task 存在 `self._drain_task` 上，第二次 `stream()` 会覆盖它；第一个生成器
    收尾时取消的是**别人的**任务 → 自己的 drain 永远挂着（asyncio 会在 GC 时报
    "Task was destroyed but it is pending"），而它的音频生产者一直卡在 yield 上。
    """
    provider = MockASRProvider()

    async def consume(audio: AsyncIterator[bytes]) -> list[ASREvent]:
        events = await provider.stream(audio)
        return [event async for event in events]

    first, second = await asyncio.gather(consume(_silence(3)), consume(_silence(3)))

    for collected in (first, second):
        assert collected, "两个并发流都必须产出事件"
        assert collected[-1].kind is ASREventKind.FINAL
        assert collected[-1].result is not None

    leaked = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and getattr(task.get_coro(), "__qualname__", "").endswith("_drain_audio")
    ]
    assert leaked == [], f"drain task 泄漏：{leaked}"
