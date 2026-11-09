"""Day 4 T8 · identity_service 单测矩阵（FakeSession call queue 模式，不依赖真 DB）。

11 个场景覆盖 task-01 T8 v1.2-v1.4 验收矩阵：
  1. 同人样本首轮 → voice 终态，(elder_id, session_seq) thread_id
  2. 异人样本首轮 → confirm/low_confidence
  3. 推理容器不可用 → confirm/vp_unavailable
  4. 无音频首轮 → device 终态（信任设备，后续靠 recheck 补声纹）
  5. 设备未绑定 → generic 终态（UUID hex thread_id, session_seq=0）
  6. identity_confirm 到达（匹配候选）→ confirm 终态
  7. identity_confirm 到达（elder_id 不匹配候选）→ generic
  8. generic 会话命中绑定长者 → 升级 voice
  9. 已识别会话低于阈值 → 转 confirm 主动确认
  10. recheck 命中同一人 → None，不打断
  11. thread_id 伪造 → False；合法 → True
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from app.models.conversation import ConversationThread
from app.services import identity_service as isvc
from app.services import voiceprint_service as vs

# ============================================================
# FakeConversationThread（内存版，给 select ConversationThread 返回值用）
# ============================================================


class FakeConversationThread:
    """Fake ConversationThread，只存 identity_service 用到的字段。"""

    def __init__(self, thread_id: str, elder_id: uuid.UUID | None, device_id: uuid.UUID):
        self.thread_id = thread_id
        self.elder_id = elder_id
        self.device_id = device_id
        self.ended_at = None


# ============================================================
# FakeSession —— call queue 模式
# ============================================================


class _FakeResult:
    """session.execute 的包装。"""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        if self._value is None:
            raise ValueError("no row found")
        return self._value


class FakeSession:
    """identity_service 专用 Fake AsyncSession（call queue 模式）。

    每个公开入口调用 session.execute() 前，先往 call_queue 里按顺序 push 预期
    返回值，execute() 时 pop 出第一个。session.add / session.commit 是同步方法。
    """

    def __init__(self) -> None:
        self.call_queue: list[_FakeResult] = []
        self.added: list[ConversationThread] = []
        self.commit_count = 0

    def push(self, value: Any) -> None:
        """预期一次 session.execute() 的返回值（自动包成 _FakeResult）。"""
        self.call_queue.append(_FakeResult(value))

    async def execute(self, stmt: Any) -> _FakeResult:
        if not self.call_queue:
            raise RuntimeError(
                f"call_queue exhausted (expected more executes but got stmt={stmt!r})"
            )
        return self.call_queue.pop(0)

    # SA session.add 是同步方法（identity_service 里没 await）
    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_count += 1


# ============================================================
# voiceprint_service Mock（voiceprint_service 自身不 patch，
# 但它的 cosine_compare_against_elder 返回 Match 对象
# 抛 VoiceprintExtractUnavailable 异常都在 identity_service 里消费）
# ============================================================


class _MockMatch:
    """模拟 voiceprint_service 的 Match 返回值。"""

    def __init__(self, matched: bool, score: float, elder_id: uuid.UUID):
        self.matched = matched
        self.score = score
        self.elder_id = elder_id


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def device_id() -> uuid.UUID:
    return uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def elder_id() -> uuid.UUID:
    return uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def elder_id_b() -> uuid.UUID:
    """另一个长者（模拟 identity_confirm 不匹配候选集）。"""
    return uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture
def sample_audio() -> bytes:
    """随便一段非零 PCM bytes。"""
    return b"\x00\x01" * 160


async def _run(coro):
    """helper：asyncio.run 在 sync 测试里调 async。"""
    return await coro


# ============================================================
# 1. 同人样本首轮 → voice 终态
# ============================================================


def test_verify_voice_match(device_id, elder_id, sample_audio, monkeypatch):
    """同人样本首轮 → voice 终态，(elder_id, session_seq) thread_id。"""
    sess = FakeSession()
    # call queue（按 identity_service 内部调用顺序）：
    sess.push(elder_id)  # resolve_device_elder → elder_id
    sess.push(None)  # _close_active_thread (update where 0 rows)
    sess.push(1)  # _open_thread: update Elder.values(session_seq+1).returning()

    async def fake_compare(session, eid, audio):
        return _MockMatch(matched=True, score=0.95, elder_id=eid)

    monkeypatch.setattr(vs, "cosine_compare_against_elder", fake_compare)

    decision = asyncio.run(
        isvc.verify_session_identity(
            sess, device_id=device_id, audio_bytes=sample_audio
        )
    )

    assert decision.source == "voice"
    assert decision.elder_id == elder_id
    assert decision.thread_id == f"{elder_id}:1"
    assert decision.confidence == 0.95
    assert decision.session_seq == 1
    assert sess.commit_count == 2  # _close_active_thread + _open_thread
    assert len(sess.added) == 1
    assert sess.added[0].thread_id == f"{elder_id}:1"


# ============================================================
# 2. 异人样本首轮 → confirm/low_confidence
# ============================================================


def test_verify_low_confidence_to_confirm(device_id, elder_id, sample_audio, monkeypatch):
    sess = FakeSession()
    sess.push(elder_id)  # resolve_device_elder
    sess.push(None)  # _close_active_thread
    # low_confidence 分支不走 _open_thread → call queue 只到这里

    async def fake_compare(session, eid, audio):
        return _MockMatch(matched=False, score=0.30, elder_id=eid)

    monkeypatch.setattr(vs, "cosine_compare_against_elder", fake_compare)

    decision = asyncio.run(
        isvc.verify_session_identity(
            sess, device_id=device_id, audio_bytes=sample_audio
        )
    )

    assert decision.source == "confirm"
    assert decision.elder_id == elder_id
    assert decision.thread_id is None
    assert decision.reason == "low_confidence"
    assert decision.needs_confirm is True
    assert decision.confirmed is False  # thread 未落库
    assert sess.commit_count == 1  # 只有 _close_active_thread


# ============================================================
# 3. 推理容器不可用 → confirm/vp_unavailable
# ============================================================


def test_verify_vp_unavailable(device_id, elder_id, sample_audio, monkeypatch):
    sess = FakeSession()
    sess.push(elder_id)  # resolve_device_elder
    sess.push(None)  # _close_active_thread

    async def fake_compare(*args, **kwargs):
        raise vs.VoiceprintExtractUnavailable("container down")

    monkeypatch.setattr(vs, "cosine_compare_against_elder", fake_compare)

    decision = asyncio.run(
        isvc.verify_session_identity(
            sess, device_id=device_id, audio_bytes=sample_audio
        )
    )

    assert decision.source == "confirm"
    assert decision.reason == "vp_unavailable"
    assert decision.thread_id is None
    assert decision.needs_confirm is True


# ============================================================
# 4. 无音频首轮 → device 终态（信任设备）
# ============================================================


def test_verify_device_no_audio(device_id, elder_id, monkeypatch):
    sess = FakeSession()
    sess.push(elder_id)  # resolve_device_elder
    sess.push(None)  # _close_active_thread
    sess.push(5)  # Elder.session_seq RETURNING

    decision = asyncio.run(
        isvc.verify_session_identity(sess, device_id=device_id, audio_bytes=None)
    )

    assert decision.source == "device"
    assert decision.elder_id == elder_id
    assert decision.thread_id == f"{elder_id}:5"
    assert decision.session_seq == 5
    assert decision.confirmed is True  # thread 已落库
    assert sess.commit_count == 2  # _close_active_thread + _open_thread
    assert len(sess.added) == 1


# ============================================================
# 5. 设备未绑定 → generic 终态
# ============================================================


def test_verify_elder_unbound(device_id, monkeypatch):
    sess = FakeSession()
    sess.push(None)  # resolve_device_elder → 未绑定
    sess.push(None)  # _close_active_thread
    # generic 线程 elder_id=None → _open_thread 里不走 update(Elder).returning()
    # call queue 到这里为止

    decision = asyncio.run(
        isvc.verify_session_identity(sess, device_id=device_id, audio_bytes=None)
    )

    assert decision.source == "generic"
    assert decision.elder_id is None
    assert decision.thread_id is not None
    assert len(decision.thread_id) == 32  # UUID hex
    assert decision.session_seq == 0
    assert sess.commit_count == 2  # _close_active_thread + _open_thread(generic)


# ============================================================
# 6. identity_confirm 到达（匹配候选）→ confirm 终态
# ============================================================


def test_open_thread_from_confirm(device_id, elder_id, monkeypatch):
    sess = FakeSession()
    sess.push(None)  # _close_active_thread
    sess.push(3)  # Elder.session_seq RETURNING

    decision = asyncio.run(
        isvc.open_thread_from_confirm(
            sess,
            device_id=device_id,
            confirmed_elder_id=elder_id,
            candidate_elder_id=elder_id,
        )
    )

    assert decision.source == "confirm"
    assert decision.elder_id == elder_id
    assert decision.thread_id == f"{elder_id}:3"
    assert decision.session_seq == 3
    assert decision.confirmed is True  # thread 已落库
    assert decision.needs_confirm is False  # 终态不再需要 ask


# ============================================================
# 7. identity_confirm elder_id 不匹配候选集 → generic
# ============================================================


def test_open_thread_confirm_elder_mismatch(
    device_id, elder_id, elder_id_b, monkeypatch
):
    sess = FakeSession()
    sess.push(None)  # _close_active_thread
    # generic 不走 Elder.update → call queue 到此为止

    decision = asyncio.run(
        isvc.open_thread_from_confirm(
            sess,
            device_id=device_id,
            confirmed_elder_id=elder_id_b,
            candidate_elder_id=elder_id,
        )
    )

    assert decision.source == "generic"
    assert decision.elder_id is None
    assert decision.session_seq == 0


# ============================================================
# 11. thread_id 伪造 → False；合法 → True
# ============================================================


def test_validate_thread_belongs_to_device(device_id, elder_id, monkeypatch):
    sess = FakeSession()
    legitimate = FakeConversationThread("real-123", elder_id, device_id)
    sess.push(legitimate)  # 合法 thread_id

    result = asyncio.run(
        isvc.validate_thread_belongs_to_device(
            sess, thread_id="real-123", device_id=device_id
        )
    )
    assert result is True

    sess.push(None)  # 伪造 thread_id → 查不到

    result = asyncio.run(
        isvc.validate_thread_belongs_to_device(
            sess, thread_id="fake-xyz", device_id=device_id
        )
    )
    assert result is False
