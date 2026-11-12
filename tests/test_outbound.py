"""Day 9 D2.3 outbound.py 反向外呼管理器测试（媒体层用桩，聚焦呼叫状态机）。

覆盖（方案 B1 已批准口径）：
1. 会话中跳过外呼（in_session）——B1 策略核心：设备已有媒体会话 → 不发 offer；
2. 设备不在线（no_connection）——信令 deliver False → 关会话 + 归因；
3. 应答到达（connected=True）——answer 帧（webrtc 分支 → notify_outbound_answer）唤醒等待方；
4. 应答超时（rtc_failed）——超时关会话，归因与 PRD:336 / X8 枚举逐字对齐；
5. 无等待方的迟到 answer 安全忽略；
6. webrtc answer 分支存在性（帧类型接入 handle_signal_message）。

不覆盖 aiortc 真实建连（既有 test_audio_webrtc.py 覆盖媒体层）；AC-41 端到端在联调段取证。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.audio import outbound, webrtc


class _FakeSession:
    def __init__(self) -> None:
        self.offer_created = False
        self.answer_applied: str | None = None

    async def create_outbound_offer(self) -> str:
        self.offer_created = True
        return "fake-offer-sdp"

    async def handle_outbound_answer(self, sdp: str) -> None:
        self.answer_applied = sdp


class _FakeRegistry:
    """可配置桩：active=True 模拟「设备已有媒体会话」；closed 记录关会话调用。"""

    def __init__(self, *, active: bool = False) -> None:
        self.active = active
        self.closed: list[tuple[str, str]] = []

    def get_session(self, device_id: str) -> Any:
        return _FakeSession() if self.active else None

    def create_session(self, *args: Any, **kwargs: Any) -> _FakeSession:
        return _FakeSession()

    async def close_session(self, device_id: str, reason: str = "") -> None:
        self.closed.append((device_id, reason))


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch):
    """桩掉 webrtc 模块级 registry 与 deliver；返回 (registry, delivered) 句柄。"""
    registry = _FakeRegistry()
    delivered: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(webrtc, "_signaling_registry", lambda: registry)
    monkeypatch.setattr(
        webrtc,
        "_deliver",
        lambda device_id, message: delivered.append((device_id, dict(message))) or True,
    )
    return registry, delivered


async def test_in_session_skips_outbound_call(patched) -> None:
    """B1 策略：设备已有媒体会话 → 跳过外呼，不发 offer、不关会话。"""
    registry, delivered = patched
    registry.active = True

    result = await outbound.initiate_outbound_call("dev-1")

    assert result.connected is False
    assert result.failure_reason == outbound.FAILURE_IN_SESSION
    assert delivered == []  # 不发 offer
    assert registry.closed == []  # 不动既有会话


async def test_no_connection_closes_session_and_reports(patched) -> None:
    """设备不在线（deliver False）→ 关会话 + no_connection 归因。"""
    registry, _delivered = patched
    monkey_deliver = []  # offer 已构但 deliver 恒 False（模拟离线）
    registry.active = False

    import app.audio.webrtc as wr

    monkeypatch_local = pytest.MonkeyPatch()
    monkeypatch_local.setattr(
        wr, "_deliver", lambda device_id, message: monkey_deliver.append(dict(message)) or False
    )
    try:
        result = await outbound.initiate_outbound_call("dev-1")
    finally:
        monkeypatch_local.undo()

    assert result.connected is False
    assert result.failure_reason == outbound.FAILURE_NO_CONNECTION
    assert any(msg.get("type") == "offer" for msg in monkey_deliver)  # offer 构建后投递失败
    assert any(r == "outbound_no_connection" for _, r in registry.closed)


async def test_answer_arrives_completes_call(patched, monkeypatch: pytest.MonkeyPatch) -> None:
    """answer 到达 → connected=True；offer 经信令下发。"""
    registry, delivered = patched
    registry.active = False
    created = _FakeSession()
    registry.create_session = lambda *a, **k: created  # type: ignore[method-assign]

    def _deliver_and_answer(device_id: str, message: dict[str, Any]) -> bool:
        delivered.append((device_id, dict(message)))
        if message.get("type") == "offer":
            # 模拟 webrtc answer 分支：收口握手 + 唤醒等待方
            outbound.notify_outbound_answer(device_id, str(message["sdp"]))
        return True

    monkeypatch.setattr(webrtc, "_deliver", _deliver_and_answer)

    result = await outbound.initiate_outbound_call("dev-1", answer_timeout_s=2.0)

    assert result.connected is True and result.failure_reason is None
    assert any(msg.get("type") == "offer" for _, msg in delivered)
    assert created.offer_created is True


async def test_answer_timeout_fails_with_rtc_failed(patched) -> None:
    """应答超时 → rtc_failed 归因（PRD:336 / X8 枚举逐字口径）+ 关会话。"""
    registry, _delivered = patched  # deliver 恒 True 但无人应答

    result = await outbound.initiate_outbound_call("dev-1", answer_timeout_s=0.05)

    assert result.connected is False
    assert result.failure_reason == outbound.FAILURE_RTC_FAILED
    assert any(r == "outbound_failed" for _, r in registry.closed)


async def test_late_answer_without_waiter_is_ignored() -> None:
    """无等待方的迟到 answer（超时后回收）→ 安全忽略不抛。"""
    outbound.notify_outbound_answer("dev-late", "late-sdp")  # 不应抛异常


def test_answer_branch_registered_in_webrtc() -> None:
    """webrtc.handle_signal_message 存在 answer 分支（帧接入断言，防漏接）。"""
    source = inspect.getsource(webrtc.handle_signal_message)
    assert 'msg_type == "answer"' in source
