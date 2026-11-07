"""Day 4 A2 · C5 身份确认信令与状态机测试。

本文件分两层：

1. **进程内状态机层（不依赖数据库）**：用 FakeWebSocket + 真实 `signaling` 读/写协程，
   覆盖 C5 的全部状态分支。FakeWebSocket 只在测试中替代传输，不替代鉴权/身份：
   连接上下文的 `bound_elder_id` 由测试显式按「服务端设备绑定」口径注入。
2. **真实 app + 真实 DB 的 WS 层**：复用 Day3 的进程内 ASGI harness，验证 C5 消息
   确实经 `/ws/signal` 端点与真实鉴权链路；PostgreSQL 不可达时按项目 conftest 惯例
   skip 并打印原因（不静默通过）。

C5 契约：`docs/team_tasks/day04/contracts.md` v1.0。验收矩阵：A2-02～A2-06。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.audio import signaling, webrtc
from app.core.security import sha256_hex, sign_device_session
from app.db import get_session as _app_get_session
from app.models.organization import Bed, Device, Elder, Room

# ---------------------------------------------------------------------------
# 0. Fake 传输 + 状态机夹具（不依赖数据库）
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """最小 WS 替身：只实现 `send_text` / `receive_text` / `close`。

    不模拟鉴权、不模拟数据库；C5 单元测试直接用服务端字段构造已鉴权连接。
    """

    def __init__(self) -> None:
        self.outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.inbound: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False
        self.close_code: int | None = None

    async def send_text(self, text: str) -> None:
        await self.outbound.put(json.loads(text))

    async def receive_text(self) -> str:
        raw = await self.inbound.get()
        if raw is None:
            raise WebSocketDisconnect(code=1000)
        return raw

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self.close_code = code
        await self.inbound.put(None)


class IdentityRig:
    """一个已鉴权连接的读/写协程与清理所有者。"""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[None]] = []
        self.conns: list[tuple[signaling.ConnectionContext, str]] = []

    async def connect(
        self,
        *,
        bound_elder_id: str | None = "elder-1",
        display_name: str | None = "张*芬",
        authed: bool = True,
        start_writer: bool = True,
        start_reader: bool = True,
    ) -> tuple[signaling.ConnectionContext, FakeWebSocket]:
        device_id = f"dev-{secrets.token_hex(6)}"
        ws = FakeWebSocket()
        conn = signaling.ConnectionContext(trace_id="a" * 32, websocket=ws)  # type: ignore[arg-type]
        conn.device_id = device_id
        conn.bound_elder_id = bound_elder_id
        conn.elder_display_name = display_name
        conn.authed = authed
        signaling._registry[device_id] = conn
        self.conns.append((conn, device_id))
        if start_writer:
            self.start_writer(conn)
        if start_reader:
            self.start_reader(conn)
        return conn, ws

    def start_writer(self, conn: signaling.ConnectionContext) -> None:
        self.tasks.append(asyncio.create_task(signaling._writer(conn)))

    def start_reader(self, conn: signaling.ConnectionContext) -> None:
        self.tasks.append(asyncio.create_task(signaling._read_loop(conn)))

    async def close(self) -> None:
        for conn, device_id in self.conns:
            timer = conn.identity_task
            signaling._cancel_identity_timer(conn)
            if timer is not None and not timer.done():
                await asyncio.gather(timer, return_exceptions=True)
            signaling._registry.pop(device_id, None)
        signaling.set_identity_confirm_handler(None)
        signaling.set_identity_timeout_handler(None)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@pytest.fixture
async def rig() -> AsyncIterator[IdentityRig]:
    identity_rig = IdentityRig()
    try:
        yield identity_rig
    finally:
        await identity_rig.close()


async def _next_frame(ws: FakeWebSocket, wait_seconds: float = 1.0) -> dict[str, Any]:
    return await asyncio.wait_for(ws.outbound.get(), timeout=wait_seconds)


async def _feed(ws: FakeWebSocket, message: Mapping[str, Any]) -> None:
    await ws.inbound.put(json.dumps(message, ensure_ascii=False))


async def _expect_no_frame(ws: FakeWebSocket, wait_seconds: float = 0.2) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ws.outbound.get(), timeout=wait_seconds)


# ---------------------------------------------------------------------------
# 1. C5 状态机：identity_ask
# ---------------------------------------------------------------------------


def test_identity_timeout_constant_is_30() -> None:
    """契约 C5：timeout_s 恒 30，且从服务端实际发送成功起算。"""
    assert signaling.IDENTITY_ASK_TIMEOUT_S == 30


@pytest.mark.parametrize("reason", ["low_confidence", "vp_unavailable"])
async def test_identity_ask_two_reasons_candidate_is_bound_elder(
    rig: IdentityRig, reason: str
) -> None:
    conn, ws = await rig.connect()

    assert (
        await signaling.request_identity_confirmation(conn.device_id or "", reason=reason) is True
    )
    ask = await _next_frame(ws)

    assert ask["type"] == "identity_ask"
    assert ask["reason"] == reason
    assert ask["timeout_s"] == 30
    assert isinstance(ask["ask_id"], str) and ask["ask_id"]
    # 候选集恒为设备绑定长者 1 位，双人间另一位不出现
    assert ask["candidates"] == [{"elder_id": "elder-1", "display_name": "张*芬"}]
    assert conn.identity_state is signaling.IdentityState.ASKING
    assert conn.pending_ask_id == ask["ask_id"]


async def test_identity_ask_duplicate_ignored(rig: IdentityRig) -> None:
    conn, ws = await rig.connect()
    assert (
        await signaling.request_identity_confirmation(conn.device_id or "", reason="low_confidence")
        is True
    )
    first = await _next_frame(ws)

    assert (
        await signaling.request_identity_confirmation(conn.device_id or "", reason="vp_unavailable")
        is False
    )
    await _expect_no_frame(ws)
    assert conn.pending_ask_id == first["ask_id"]


async def test_identity_ask_without_bound_elder_falls_to_generic(rig: IdentityRig) -> None:
    conn, ws = await rig.connect(bound_elder_id=None, display_name=None)

    assert (
        await signaling.request_identity_confirmation(conn.device_id or "", reason="vp_unavailable")
        is False
    )
    result = await _next_frame(ws)
    assert result == {"type": "identity_result", "source": "generic", "elder_id": None}
    assert conn.identity_state is signaling.IdentityState.GENERIC
    assert conn.active_elder_id is None
    assert conn.bound_elder_id is None


# ---------------------------------------------------------------------------
# 2. C5 状态机：identity_confirm 与最终结果幂等
# ---------------------------------------------------------------------------


async def test_identity_confirm_valid_sends_one_result(rig: IdentityRig) -> None:
    conn, ws = await rig.connect()
    await signaling.request_identity_confirmation(conn.device_id or "", reason="low_confidence")
    ask = await _next_frame(ws)

    await _feed(
        ws,
        {
            "type": "identity_confirm",
            "ask_id": ask["ask_id"],
            "elder_id": "elder-1",
        },
    )
    result = await _next_frame(ws)

    assert result == {"type": "identity_result", "source": "confirm", "elder_id": "elder-1"}
    assert conn.identity_state is signaling.IdentityState.IDENTIFIED
    assert conn.active_elder_id == "elder-1"
    assert conn.bound_elder_id == "elder-1"  # 设备绑定关系不被改写
    assert conn.identity_task is None  # 合法 confirm 已取消计时

    # 身份已定后重复 confirm：忽略、不回 error、不关连接
    await _feed(
        ws,
        {
            "type": "identity_confirm",
            "ask_id": ask["ask_id"],
            "elder_id": "elder-1",
        },
    )
    await _expect_no_frame(ws)
    assert not ws.closed


async def test_identity_confirm_ask_mismatch_ignored(
    rig: IdentityRig, caplog: pytest.LogCaptureFixture
) -> None:
    conn, ws = await rig.connect()
    await signaling.request_identity_confirmation(conn.device_id or "", reason="low_confidence")
    ask = await _next_frame(ws)

    with caplog.at_level(logging.WARNING, logger="app.audio.signaling"):
        await _feed(
            ws,
            {"type": "identity_confirm", "ask_id": "wrong-ask", "elder_id": "elder-1"},
        )
        await _expect_no_frame(ws)

    assert conn.identity_state is signaling.IdentityState.ASKING
    assert conn.pending_ask_id == ask["ask_id"]
    assert any(
        record.getMessage() == "identity_confirm_ignored"
        and getattr(record, "reason", "") == "ask_mismatch_or_not_asking"
        for record in caplog.records
    )
    assert not ws.closed


async def test_identity_confirm_out_of_candidate_keeps_asking(rig: IdentityRig) -> None:
    conn, ws = await rig.connect()
    await signaling.request_identity_confirmation(conn.device_id or "", reason="vp_unavailable")
    ask = await _next_frame(ws)

    # 候选集外 elder_id：按未确认处理，保持 ASKING，不改变已定身份
    await _feed(
        ws,
        {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": "elder-other"},
    )
    await _expect_no_frame(ws)
    assert conn.identity_state is signaling.IdentityState.ASKING
    assert conn.active_elder_id is None

    # 随后合法确认仍然生效
    await _feed(
        ws,
        {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": "elder-1"},
    )
    result = await _next_frame(ws)
    assert result["source"] == "confirm"


async def test_identity_confirm_invalid_shape_hits_bad_message_boundary(
    rig: IdentityRig,
) -> None:
    conn, ws = await rig.connect()
    await signaling.request_identity_confirmation(conn.device_id or "", reason="low_confidence")
    ask = await _next_frame(ws)

    await _feed(ws, {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": 123})
    error = await _next_frame(ws)
    assert error["type"] == "error" and error["code"] == "internal"

    # 坏消息不关连接、不改变身份状态
    await _feed(ws, {"type": "ping"})
    assert (await _next_frame(ws))["type"] == "pong"
    assert conn.identity_state is signaling.IdentityState.ASKING


async def test_voice_identity_once_and_candidate_checked(
    rig: IdentityRig, caplog: pytest.LogCaptureFixture
) -> None:
    conn, ws = await rig.connect()

    # 候选集外：忽略且不改变状态
    assert (
        await signaling.apply_voice_identity(conn.device_id or "", elder_id="elder-other") is False
    )
    await _expect_no_frame(ws)
    assert conn.identity_state is signaling.IdentityState.UNKNOWN

    # 命中设备绑定长者：一次 voice 结果
    assert await signaling.apply_voice_identity(conn.device_id or "", elder_id="elder-1") is True
    result = await _next_frame(ws)
    assert result == {"type": "identity_result", "source": "voice", "elder_id": "elder-1"}
    assert conn.identity_state is signaling.IdentityState.IDENTIFIED
    assert conn.active_elder_id == "elder-1"

    with caplog.at_level(logging.WARNING, logger="app.audio.signaling"):
        assert (
            await signaling.apply_voice_identity(conn.device_id or "", elder_id="elder-1") is False
        )
    await _expect_no_frame(ws)
    assert any(
        record.getMessage() == "identity_result_ignored"
        and getattr(record, "reason", "") == "duplicate_voice_result"
        for record in caplog.records
    )


async def test_apply_generic_identity_once(rig: IdentityRig) -> None:
    conn, ws = await rig.connect()

    assert await signaling.apply_generic_identity(conn.device_id or "") is True
    result = await _next_frame(ws)
    assert result == {"type": "identity_result", "source": "generic", "elder_id": None}
    assert conn.identity_state is signaling.IdentityState.GENERIC
    assert conn.active_elder_id is None
    assert conn.bound_elder_id == "elder-1"  # 设备绑定不被 generic 清空

    assert await signaling.apply_generic_identity(conn.device_id or "") is False
    await _expect_no_frame(ws)


async def test_begin_identity_recheck_allows_second_result(rig: IdentityRig) -> None:
    conn, ws = await rig.connect()
    assert await signaling.apply_voice_identity(conn.device_id or "", elder_id="elder-1") is True
    first = await _next_frame(ws)
    assert first == {"type": "identity_result", "source": "voice", "elder_id": "elder-1"}

    assert await signaling.begin_identity_recheck(conn.device_id or "") is True
    assert conn.identity_state is signaling.IdentityState.UNKNOWN
    assert conn.identity_result_sent is False
    assert conn.active_elder_id is None

    # 复检后允许再下发一次新 voice 结果
    assert await signaling.apply_voice_identity(conn.device_id or "", elder_id="elder-1") is True
    assert await _next_frame(ws) == first


async def test_identity_confirm_calls_registered_handler(rig: IdentityRig) -> None:
    conn, ws = await rig.connect()
    await signaling.request_identity_confirmation(conn.device_id or "", reason="low_confidence")
    ask = await _next_frame(ws)

    calls: list[tuple[str, str, str]] = []

    async def handler(device_id: str, trace_id: str, elder_id: str) -> None:
        calls.append((device_id, trace_id, elder_id))

    signaling.set_identity_confirm_handler(handler)
    await _feed(
        ws,
        {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": "elder-1"},
    )
    result = await _next_frame(ws)

    assert result == {"type": "identity_result", "source": "confirm", "elder_id": "elder-1"}
    assert calls == [(conn.device_id, conn.trace_id, "elder-1")]


async def test_identity_timeout_calls_registered_handler(
    rig: IdentityRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signaling, "_IDENTITY_TIMEOUT_TEST_S", 0.05)
    conn, ws = await rig.connect()

    calls: list[tuple[str, str]] = []

    async def handler(device_id: str, trace_id: str) -> None:
        calls.append((device_id, trace_id))

    signaling.set_identity_timeout_handler(handler)
    await signaling.request_identity_confirmation(conn.device_id or "", reason="vp_unavailable")
    ask = await _next_frame(ws)
    result = await _next_frame(ws, wait_seconds=1.0)

    assert result == {"type": "identity_result", "source": "generic", "elder_id": None}
    assert calls == [(conn.device_id, conn.trace_id)]
    assert ask["timeout_s"] == 30


# ---------------------------------------------------------------------------
# 3. C5 状态机：30s 超时、计时起点与断线取消
# ---------------------------------------------------------------------------


async def test_identity_ask_timeout_generic_once(
    rig: IdentityRig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可控时钟：把内部计时器缩短，不真实等待 30 秒；线上 timeout_s 仍恒 30。"""
    monkeypatch.setattr(signaling, "_IDENTITY_TIMEOUT_TEST_S", 0.1)
    conn, ws = await rig.connect()
    await signaling.request_identity_confirmation(conn.device_id or "", reason="low_confidence")
    ask = await _next_frame(ws)
    assert ask["timeout_s"] == 30

    result = await _next_frame(ws, wait_seconds=1.0)
    assert result == {"type": "identity_result", "source": "generic", "elder_id": None}
    assert conn.identity_state is signaling.IdentityState.GENERIC
    assert conn.active_elder_id is None
    # generic 不清空设备绑定关系
    assert conn.bound_elder_id == "elder-1"

    # 超时后的 confirm 按未确认处理：不再产生第二个结果
    await _feed(
        ws,
        {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": "elder-1"},
    )
    await _expect_no_frame(ws)
    assert conn.identity_state is signaling.IdentityState.GENERIC


async def test_timeout_starts_after_actual_send_not_enqueue(
    rig: IdentityRig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ask 只入队、未真实发送时不得起算，也不得提前发 generic。"""
    monkeypatch.setattr(signaling, "_IDENTITY_TIMEOUT_TEST_S", 0.05)
    conn, ws = await rig.connect(start_writer=False)

    assert (
        await signaling.request_identity_confirmation(conn.device_id or "", reason="low_confidence")
        is True
    )
    # 队列里有 ask，但没有任何 writer 真正发送
    await _expect_no_frame(ws, wait_seconds=0.15)
    assert conn.identity_state is signaling.IdentityState.ASKING

    # 启动 writer 后 ask 才真正落地，30s（此处 0.05s）从这一刻起算
    rig.start_writer(conn)
    ask = await _next_frame(ws)
    assert ask["type"] == "identity_ask"
    result = await _next_frame(ws, wait_seconds=1.0)
    assert result["source"] == "generic"


async def test_disconnect_cancels_identity_timer(rig: IdentityRig) -> None:
    conn, ws = await rig.connect()
    await signaling.request_identity_confirmation(conn.device_id or "", reason="low_confidence")
    ask = await _next_frame(ws)
    assert conn.identity_task is not None

    await signaling._cleanup(conn)

    assert conn.identity_task is None
    assert conn.identity_state is signaling.IdentityState.CLOSED
    # 断线后不得补发 generic
    await _expect_no_frame(ws)
    assert conn.pending_ask_id is None
    assert ask["type"] == "identity_ask"


# ---------------------------------------------------------------------------
# 4. C5 client_diag：只写结构化安全日志
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["vrm_load", "webrtc", "vad", "asr"])
async def test_client_diag_valid_logs_structured_fields_without_error_text(
    rig: IdentityRig,
    caplog: pytest.LogCaptureFixture,
    stage: str,
) -> None:
    conn, ws = await rig.connect()
    sensitive = "client stack trace: token=abc123 住址 A-301"

    with caplog.at_level(logging.INFO, logger="app.audio.signaling"):
        await _feed(ws, {"type": "client_diag", "stage": stage, "ok": False, "error": sensitive})
        await _expect_no_frame(ws)

    records = [r for r in caplog.records if r.getMessage() == "client_diag"]
    assert records, "合法 client_diag 必须落结构化日志"
    record = records[-1]
    assert record.trace_id == conn.trace_id
    assert record.stage == stage
    assert record.ok is False
    assert record.error_len == len(sensitive)
    # 客户端 error 原文默认不落日志
    assert sensitive not in " ".join(r.getMessage() for r in caplog.records)
    assert not hasattr(record, "error")
    assert not ws.closed


@pytest.mark.parametrize(
    "message",
    [
        {"type": "client_diag", "stage": "network", "ok": True, "error": ""},
        {"type": "client_diag", "stage": "vad", "ok": 1, "error": ""},
        {"type": "client_diag", "stage": "asr", "ok": True, "error": None},
        {"type": "client_diag", "stage": 7, "ok": True, "error": ""},
    ],
)
async def test_client_diag_invalid_shape_hits_bad_message_boundary(
    rig: IdentityRig,
    message: dict[str, Any],
) -> None:
    _conn, ws = await rig.connect()
    await _feed(ws, message)
    error = await _next_frame(ws)
    assert error["type"] == "error" and error["code"] == "internal"

    # 坏消息不关连接，连接仍可用
    await _feed(ws, {"type": "ping"})
    assert (await _next_frame(ws))["type"] == "pong"
    assert not ws.closed


async def test_client_diag_vad_failure_activates_server_fallback(rig: IdentityRig) -> None:
    conn, _ws = await rig.connect()
    registry = webrtc._signaling_registry()
    session = registry.create_session(conn.device_id or "", conn.trace_id)
    try:
        await signaling._handle_client_diag(
            conn, {"type": "client_diag", "stage": "vad", "ok": False, "error": ""}
        )
        assert session.vad_fallback_active is True

        await signaling._handle_client_diag(
            conn, {"type": "client_diag", "stage": "vad", "ok": True, "error": ""}
        )
        assert session.vad_fallback_active is False
    finally:
        await registry.close_session(conn.device_id or "")


async def test_c5_metrics_three_points(rig: IdentityRig, caplog: pytest.LogCaptureFixture) -> None:
    conn, ws = await rig.connect()
    with caplog.at_level(logging.INFO, logger="app.core.metrics"):
        await signaling.request_identity_confirmation(conn.device_id or "", reason="low_confidence")
        ask = await _next_frame(ws)
        await _feed(
            ws,
            {"type": "identity_confirm", "ask_id": ask["ask_id"], "elder_id": "elder-1"},
        )
        await _next_frame(ws)

    metrics = {
        getattr(record, "metric", ""): record
        for record in caplog.records
        if getattr(record, "metric", "")
        in (
            "identity_ask_sent",
            "identity_confirm_received",
            "identity_result_sent",
        )
    }
    assert metrics["identity_ask_sent"].ask_id == ask["ask_id"]
    assert metrics["identity_confirm_received"].ask_id == ask["ask_id"]
    assert metrics["identity_result_sent"].ask_id == ask["ask_id"]


# ---------------------------------------------------------------------------
# 5. 真实 app + 真实 DB：C5 经 /ws/signal 的链路验证（无库时 skip 并说明原因）
# ---------------------------------------------------------------------------

_DEVICE_SECRET = "device-long-secret-c5"


def _device_token(device_id: Any) -> str:
    return sign_device_session(
        device_id=str(device_id),
        token_hash_prefix=sha256_hex(_DEVICE_SECRET)[:16],
    )


class WsClient:
    """与 tests/test_audio_ws_auth.py 同口径的进程内 ASGI WS harness（最小复制）。"""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def _receive(self) -> dict[str, Any]:
        return await self._to_app.get()

    async def _send(self, message: Mapping[str, Any]) -> None:
        msg = dict(message)
        await self._from_app.put(msg)
        if msg.get("type") == "websocket.close":
            await self._to_app.put({"type": "websocket.disconnect", "code": 1000})

    async def __aenter__(self) -> WsClient:
        scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": "/ws/signal",
            "raw_path": b"/ws/signal",
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 0),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
        }
        self._task = asyncio.create_task(self._app(scope, self._receive, self._send))
        await self._to_app.put({"type": "websocket.connect"})
        accepted = await self._next_server_message()
        assert accepted["type"] == "websocket.accept"
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._to_app.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            await self._task

    async def _next_server_message(self) -> dict[str, Any]:
        assert self._task is not None
        getter = asyncio.ensure_future(self._from_app.get())
        done, _ = await asyncio.wait({getter, self._task}, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            return getter.result()
        getter.cancel()
        exc = self._task.exception()
        if exc is not None:
            raise exc
        raise RuntimeError("WS endpoint finished before expected frame")

    async def send_json(self, message: Mapping[str, Any]) -> None:
        await self._to_app.put(
            {"type": "websocket.receive", "text": json.dumps(message, ensure_ascii=False)}
        )

    async def receive_json(self) -> dict[str, Any]:
        msg = await self._next_server_message()
        assert msg["type"] == "websocket.send", f"期望数据帧，收到 {msg}"
        return json.loads(msg["text"])


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """db_engine 为 conftest 的 module 级共享引擎。

    阶段 C 去掉 TRUNCATE 后改走 create_savepoint：service 层的真实 commit 只释放
    savepoint，结束整体回滚，独立测试库零残留（模式同 test_elders_api.client）。
    """
    async with db_engine.connect() as conn:
        trans = await conn.begin()
        sf = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        async with sf() as session:
            try:
                yield session
            finally:
                await session.rollback()
        await trans.rollback()


@pytest.fixture
async def seeded_device(db_session: AsyncSession) -> dict[str, Any]:
    room = Room(room_no="C-501")
    db_session.add(room)
    await db_session.flush()
    bed = Bed(room_id=room.room_id, bed_no="1 号床")
    device = Device(room_id=room.room_id, token_hash=sha256_hex(_DEVICE_SECRET), status="offline")
    db_session.add_all([bed, device])
    await db_session.flush()
    elder = Elder(name="张桂芬", status="active")
    db_session.add(elder)
    await db_session.flush()
    bed.elder_id = elder.elder_id
    device.elder_id = elder.elder_id
    await db_session.flush()
    return {
        "elder_id": elder.elder_id,
        "device_id": device.device_id,
        "room_id": room.room_id,
    }


@pytest.fixture
async def ws_app(
    db_session: AsyncSession, seeded_device: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FastAPI]:
    from app.main import app as real_app

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    real_app.dependency_overrides[_app_get_session] = _override_get_session
    monkeypatch.setattr(signaling, "WS_AUTH_TIMEOUT_MS", 2000)
    yield real_app
    real_app.dependency_overrides.clear()


async def test_ws_identity_ask_confirm_and_diag(
    ws_app: FastAPI, seeded_device: dict[str, Any]
) -> None:
    """经真实 /ws/signal：auth → ask → confirm → 单次 result；client_diag 无回执。"""
    device_id = str(seeded_device["device_id"])
    async with WsClient(ws_app) as ws:
        await ws.send_json({"type": "auth", "token": _device_token(seeded_device["device_id"])})
        auth_ok = await ws.receive_json()
        assert auth_ok["type"] == "auth_ok"

        assert (
            await signaling.request_identity_confirmation(device_id, reason="low_confidence")
            is True
        )
        ask = await ws.receive_json()
        assert ask["type"] == "identity_ask"
        assert ask["candidates"][0]["elder_id"] == str(seeded_device["elder_id"])

        await ws.send_json(
            {
                "type": "identity_confirm",
                "ask_id": ask["ask_id"],
                "elder_id": str(seeded_device["elder_id"]),
            }
        )
        result = await ws.receive_json()
        assert result == {
            "type": "identity_result",
            "source": "confirm",
            "elder_id": str(seeded_device["elder_id"]),
        }

        # client_diag 合法：无回执；随后 ping 仍可用
        await ws.send_json({"type": "client_diag", "stage": "vrm_load", "ok": True, "error": ""})
        await ws.send_json({"type": "ping"})
        assert (await ws.receive_json())["type"] == "pong"
