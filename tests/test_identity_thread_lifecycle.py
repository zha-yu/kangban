"""A1 identity_service thread 生命周期顺序测试（无数据库）。

只验证事务内顺序：任何新 thread INSERT 之前，必须先结束该设备所有未结束 thread。
用 FakeSession 记录 execute/add/commit 顺序，不访问 PostgreSQL。
"""

from __future__ import annotations

import uuid

import pytest

from app.models.conversation import ConversationThread
from app.services import identity_service
from app.services.voiceprint_service import CosineMatch

_DEVICE = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ELDER = uuid.UUID("11111111-1111-1111-1111-111111111111")


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value

    def scalar_one(self) -> object:
        return self._value


class _FakeSession:
    """记录 SQL 顺序；只实现 identity_service 用到的 execute/add/commit。

    `select_results` 按 select 顺序返回：recheck 先查当前 thread，再查设备绑定长者。
    elders UPDATE 模拟 `session_seq + 1 RETURNING`，返回递增后的整数。
    """

    def __init__(self, select_results: list[object]) -> None:
        self._select_results = list(select_results)
        self.calls: list[str] = []
        self.added: list[ConversationThread] = []
        self.session_seq = 0

    async def execute(self, statement: object) -> _FakeResult:
        table_name = getattr(getattr(statement, "table", None), "name", "")
        if getattr(statement, "is_update", False):
            if table_name == "elders":
                self.calls.append("increment_seq")
                self.session_seq += 1
                return _FakeResult(self.session_seq)
            self.calls.append("close_active")
            return _FakeResult(None)
        self.calls.append("select")
        value = self._select_results.pop(0) if self._select_results else None
        return _FakeResult(value)

    def add(self, row: ConversationThread) -> None:
        self.calls.append("add")
        self.added.append(row)

    async def commit(self) -> None:
        self.calls.append("commit")


def _assert_close_before_open(session: _FakeSession) -> None:
    assert "close_active" in session.calls, session.calls
    assert "add" in session.calls, session.calls
    assert session.calls.index("close_active") < session.calls.index("add"), session.calls


async def test_verify_voice_closes_active_before_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession([_ELDER])

    async def _matched(*args: object, **kwargs: object) -> CosineMatch:
        return CosineMatch(matched=True, score=0.91, threshold=0.85, elder_id=_ELDER)

    monkeypatch.setattr(identity_service.vs, "cosine_compare_against_elder", _matched)

    decision = await identity_service.verify_session_identity(
        session,  # type: ignore[arg-type]
        device_id=_DEVICE,
        audio_bytes=b"wav",
    )

    assert decision.source == "voice"
    assert decision.elder_id == _ELDER
    assert decision.thread_id == f"{_ELDER}:1"
    assert decision.session_seq == 1
    _assert_close_before_open(session)


async def test_verify_unbound_generic_closes_active_before_open() -> None:
    session = _FakeSession([None])

    decision = await identity_service.verify_session_identity(
        session,  # type: ignore[arg-type]
        device_id=_DEVICE,
        audio_bytes=None,
    )

    assert decision.source == "generic"
    assert decision.elder_id is None
    assert decision.thread_id is not None
    assert decision.session_seq == 0
    _assert_close_before_open(session)


async def test_open_thread_from_confirm_closes_active_before_open() -> None:
    session = _FakeSession([])

    decision = await identity_service.open_thread_from_confirm(
        session,  # type: ignore[arg-type]
        device_id=_DEVICE,
        confirmed_elder_id=_ELDER,
        candidate_elder_id=_ELDER,
    )

    assert decision.source == "confirm"
    assert decision.session_seq == 1
    _assert_close_before_open(session)


async def test_timeout_generic_closes_active_before_open() -> None:
    session = _FakeSession([])

    decision = await identity_service.open_thread_as_generic_on_timeout(
        session,  # type: ignore[arg-type]
        device_id=_DEVICE,
    )

    assert decision.source == "generic"
    assert decision.thread_id is not None
    assert decision.session_seq == 0
    _assert_close_before_open(session)

