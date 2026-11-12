"""Day 9 X1 client_receipt 分支测试（app/audio/signaling.py）。

覆盖范围（只测进程内形状/幂等/归属/降级；带真实 WS 的链路在 X1 联调 D10 12:00 验证）：
1. 形状校验：缺字段 / 非法 receipt_kind → error 帧，不进处理链；
2. 归属校验：task_id 归属长者与设备绑定不符 → receipt_ack(rejected)，不落库；
3. 幂等：重复回执返回原结果 duplicate=true，不再调 mark（X1 契约：不重复写 sent_at）；
4. delivered 落库走 notify_service.mark_notification_delivered（X1 冻结签名带 receipt_kind），
   C3 未升级前的 TypeError 降级路径仍可用；
5. 纯播报回执（notification_id=null）只 ack accepted，不写库；
6. 状态机拒绝（illegal transition）→ receipt_ack(rejected) 且不入幂等缓存（允许真实重试）。

身份铁律：帧内的 elder_id/device_id 字段一律不读（服务端注入），测试断言伪造字段不影响判定。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from app.audio import signaling
from app.audio.signaling import (
    ClientReceiptMsg,
    _handle_client_receipt,
)

_ELDER = "11111111-1111-1111-1111-111111111111"
_OTHER_ELDER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_REMINDER = "22222222-2222-2222-2222-222222222222"
_NOTIFICATION = "33333333-3333-3333-3333-333333333333"


class _FakeSession:
    """async with 可用的假 AsyncSession：只统计 commit。"""

    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def commit(self) -> None:
        self.commits += 1


class _FakeSessionFactory:
    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        session = _FakeSession()
        self.sessions.append(session)
        return session


class _StubMedicationService:
    """get_event 桩：返回按 event_id 配置的归属长者（None = 事件不存在）。"""

    def __init__(self, owner_by_event: dict[str, str]) -> None:
        self.owner_by_event = owner_by_event

    async def get_event(self, session: Any, event_id: uuid.UUID) -> dict[str, Any] | None:
        owner = self.owner_by_event.get(str(event_id))
        return {"reminder_id": event_id, "elder_id": owner} if owner else None


class _StubNotifyService:
    """mark_notification_delivered 桩：可配置 TypeError（旧签名）与状态机拒绝。"""

    def __init__(self, *, legacy_signature: bool = False, fail_transition: bool = False) -> None:
        self.legacy_signature = legacy_signature
        self.fail_transition = fail_transition
        self.calls: list[tuple[str, str | None]] = []

    async def mark_notification_delivered(
        self, session: Any, *, notification_id: uuid.UUID, receipt_kind: str | None = None
    ) -> None:
        if self.fail_transition:
            raise signaling.NotificationIllegalTransition("generated → delivered 不合法")
        self.calls.append((str(notification_id), receipt_kind))
        if self.legacy_signature and receipt_kind is not None:
            raise TypeError("unexpected keyword argument 'receipt_kind'")  # 模拟 C3 未升级


class _FakeConn:
    """_handle_client_receipt 所需的最小连接面。"""

    def __init__(self, bound: str | None = _ELDER) -> None:
        self.trace_id = "trace-receipt"
        self.device_id = "dev-1"
        self.bound_elder_id = bound
        self.receipt_seen: dict[tuple[str, str], str] = {}
        self.downstream: asyncio.Queue[dict[str, Any]] = asyncio.Queue()


def _receipt_message(
    task_id: str = _REMINDER,
    notification_id: str | None = _NOTIFICATION,
    receipt_kind: str = "card_rendered",
    **extra: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "client_receipt",
        "receipt_kind": receipt_kind,
        "task_id": task_id,
        "notification_id": notification_id,
        "ts": "2026-11-13T09:15:03.120Z",
        # 身份铁律对照：客户端伪造身份字段应被无视
        "elder_id": _OTHER_ELDER,
        "device_id": "forged-device",
    }
    message.update(extra)
    return message


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeSessionFactory, _StubNotifyService]:
    factory = _FakeSessionFactory()
    notify = _StubNotifyService()
    med = _StubMedicationService({_REMINDER: _ELDER})
    monkeypatch.setattr(signaling, "session_factory", factory)
    monkeypatch.setattr(signaling, "notify_service", notify)
    monkeypatch.setattr(signaling, "medication_service", med)
    return factory, notify


async def test_valid_receipt_marks_delivered_and_acks(patched: tuple[Any, Any]) -> None:
    factory, notify = patched
    conn = _FakeConn()

    await _handle_client_receipt(conn, _receipt_message())

    assert notify.calls == [(_NOTIFICATION, "card_rendered")]
    assert factory.sessions[-1].commits == 1
    ack = conn.downstream.get_nowait()
    assert ack == {
        "type": "receipt_ack",
        "notification_id": _NOTIFICATION,
        "status": "delivered",
        "duplicate": False,
    }
    assert conn.receipt_seen[(_NOTIFICATION, "card_rendered")] == "delivered"


async def test_duplicate_receipt_returns_original_result_without_remarking(
    patched: tuple[Any, Any],
) -> None:
    _factory, notify = patched
    conn = _FakeConn()

    await _handle_client_receipt(conn, _receipt_message())
    await _handle_client_receipt(conn, _receipt_message())

    assert len(notify.calls) == 1  # 不重复 mark（不重复写 sent_at）
    ack = conn.downstream.get_nowait()
    ack_dup = conn.downstream.get_nowait()
    assert ack["duplicate"] is False and ack["status"] == "delivered"
    assert ack_dup["duplicate"] is True and ack_dup["status"] == "delivered"


async def test_receipt_with_ownership_mismatch_is_rejected(patched: tuple[Any, Any]) -> None:
    _factory, notify = patched
    conn = _FakeConn(bound=_OTHER_ELDER)  # 绑定长者与事件归属不符

    await _handle_client_receipt(conn, _receipt_message())

    assert notify.calls == []  # 不落库
    ack = conn.downstream.get_nowait()
    assert ack["status"] == "rejected" and ack["duplicate"] is False
    assert conn.receipt_seen == {}  # 拒绝不入缓存


async def test_broadcast_receipt_without_notification_is_accepted(patched: tuple[Any, Any]) -> None:
    _factory, notify = patched
    conn = _FakeConn()

    await _handle_client_receipt(
        conn, _receipt_message(notification_id=None, task_id=f"health:{_ELDER}:2026-11-13")
    )

    assert notify.calls == []  # 纯播报不写库
    ack = conn.downstream.get_nowait()
    assert ack == {
        "type": "receipt_ack",
        "notification_id": None,
        "status": "accepted",
        "duplicate": False,
    }


async def test_health_task_ownership_matches_bound_elder(patched: tuple[Any, Any]) -> None:
    _factory, _notify = patched
    conn = _FakeConn()

    await _handle_client_receipt(
        conn, _receipt_message(notification_id=None, task_id=f"health:{_ELDER}:2026-11-13")
    )
    ok = conn.downstream.get_nowait()
    assert ok["status"] == "accepted"

    # 他人 health 任务 → 拒绝
    await _handle_client_receipt(
        conn,
        _receipt_message(notification_id=None, task_id=f"health:{_OTHER_ELDER}:2026-11-13"),
    )
    rejected = conn.downstream.get_nowait()
    assert rejected["status"] == "rejected"


async def test_legacy_signature_fallback_still_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3 未升级 receipt_kind 参数前的 TypeError 降级：仍按旧签名 mark（X1 联调前过渡）。"""
    factory = _FakeSessionFactory()
    notify = _StubNotifyService(legacy_signature=True)
    med = _StubMedicationService({_REMINDER: _ELDER})
    monkeypatch.setattr(signaling, "session_factory", factory)
    monkeypatch.setattr(signaling, "notify_service", notify)
    monkeypatch.setattr(signaling, "medication_service", med)
    conn = _FakeConn()

    await _handle_client_receipt(conn, _receipt_message())

    assert notify.calls[-1][1] is None  # 降级后第二次调用不带 receipt_kind
    ack = conn.downstream.get_nowait()
    assert ack["status"] == "delivered"


async def test_illegal_transition_is_rejected_and_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通知尚在 generated（C3 扫描未推 pending）→ 诚实拒绝，允许真实重试。"""
    factory = _FakeSessionFactory()
    notify = _StubNotifyService(fail_transition=True)
    med = _StubMedicationService({_REMINDER: _ELDER})
    monkeypatch.setattr(signaling, "session_factory", factory)
    monkeypatch.setattr(signaling, "notify_service", notify)
    monkeypatch.setattr(signaling, "medication_service", med)
    conn = _FakeConn()

    await _handle_client_receipt(conn, _receipt_message())

    ack = conn.downstream.get_nowait()
    assert ack["status"] == "rejected" and ack["duplicate"] is False
    assert conn.receipt_seen == {}


async def test_malformed_shape_is_rejected_before_processing() -> None:
    conn = _FakeConn()
    bad_kind = _receipt_message(receipt_kind="tts_done")
    missing_ts = _receipt_message()
    del missing_ts["ts"]
    empty_task = _receipt_message(task_id="")
    for message in (bad_kind, missing_ts, empty_task):
        await _handle_client_receipt(conn, message)
    for _ in range(3):
        error = conn.downstream.get_nowait()
        assert error["type"] == "error" and error["code"] == "internal"


def test_client_receipt_msg_contract_shape() -> None:
    """X1 帧形状：TypedDict 必填字段存在（编译期约束的运行时锚点）。"""
    hints = ClientReceiptMsg.__annotations__
    assert set(hints) == {"type", "receipt_kind", "task_id", "notification_id", "ts"}
