"""signaling.py 信令层测试：契约形状（TypedDict）与 C→S 分支分发。

为什么单独成文件：任务卡（day07/dev-c-触达语音接线.md、dev-c-触达played闭环.md）
的验收命令写的是 `pytest tests/test_medication_graph.py tests/test_signaling.py -q`；
2026-11-09 应主理人要求，把原本散落在 test_response_service.py 的
「signaling.py 扩展」段落（segment_ack / touch_played 分支 + TtsTextMsg 形状断言）
整体迁到这里——这些测试只依赖 signaling 模块，与 response_service 无关，
放在一起纯属历史原因。

覆盖范围（只测进程内的形状与分发；带真实 WS 连接的鉴权/身份确认测试仍在
test_audio_ws_auth.py、test_audio_identity_ws.py）：
1. TtsTextMsg v2（回复流五字段）与 v3（触达四字段）可选性契约；
2. SegmentAckMsg / TouchPlayedMsg 必填形状；
3. segment_ack 与 touch_played 分支：合法分发 / 非法形状拒之门外 / 未装配安全忽略。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from app.audio.signaling import (
    SegmentAckMsg,
    TouchPlayedMsg,
    TtsTextMsg,
    _handle_segment_ack,
    _handle_touch_played,
    set_segment_ack_handler,
    set_touch_played_handler,
)


def _ack_message(
    response_id: str,
    *,
    epoch: int,
    segment_id: int,
    status: str = "playing",
    at_ms: int = 100,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "segment_ack",
        "response_id": response_id,
        "epoch": epoch,
        "segment_id": segment_id,
        "status": status,
        "at_ms": at_ms,
    }
    return message


def _touch_played_message(
    touch_id: str = "t1", event_id: str = "e1", attempt_no: int = 1
) -> dict[str, Any]:
    return {
        "type": "touch_played",
        "touch_id": touch_id,
        "event_id": event_id,
        "attempt_no": attempt_no,
    }


# ---------------------------------------------------------------------------
# 契约形状：TypedDict 注解断言（3.12 TypedDict 在类创建期无法识别 NotRequired）
# ---------------------------------------------------------------------------


def test_tts_text_v2_keeps_old_fields_and_adds_five_optional() -> None:
    annotations = TtsTextMsg.__annotations__
    assert {"type", "text", "degraded"} <= set(annotations)  # 老字段不动
    assert {"response_id", "epoch", "segment_id", "index", "total"} <= set(annotations)
    for field_name in ("response_id", "epoch", "segment_id", "index", "total"):
        assert "NotRequired" in str(annotations[field_name])  # v2 五字段全部可选


def test_segment_ack_msg_contract_shape() -> None:
    annotations = SegmentAckMsg.__annotations__
    assert set(annotations) == {
        "type",
        "response_id",
        "epoch",
        "segment_id",
        "status",
        "at_ms",
    }
    at_ms_ann = str(annotations["at_ms"])
    assert (
        "int" in at_ms_ann and "NotRequired" not in at_ms_ann
    )  # at_ms 必填（与 protocol.ts 一致），其余必填


def test_tts_text_v3_adds_touch_fields_as_not_required() -> None:
    annotations = TtsTextMsg.__annotations__
    assert {"touch_id", "touch_event_id", "touch_attempt_no", "audio"} <= set(annotations)
    for field_name in ("touch_id", "touch_event_id", "touch_attempt_no", "audio"):
        assert "NotRequired" in str(annotations[field_name])  # v3 四字段全部可选


def test_touch_played_msg_contract_shape() -> None:
    annotations = TouchPlayedMsg.__annotations__
    assert set(annotations) == {"type", "touch_id", "event_id", "attempt_no"}
    attempt_ann = str(annotations["attempt_no"])
    assert "int" in attempt_ann and "NotRequired" not in attempt_ann  # 三字段全必填


# ---------------------------------------------------------------------------
# C→S 分支分发：segment_ack（Day 6）与 touch_played（Day 7 TD-2）同层互不干扰
# ---------------------------------------------------------------------------


class _FakeConn:
    """_handle_*_ack 所需的最小连接面（trace/device/downstream）。"""

    def __init__(self) -> None:
        self.trace_id = "trace-signal-branch"
        self.device_id = "dev-1"
        self.downstream: asyncio.Queue[dict[str, Any]] = asyncio.Queue()


async def test_segment_ack_branch_dispatches_valid_message_to_handler() -> None:
    conn = _FakeConn()
    received: list[tuple[Any, Mapping[str, Any]]] = []

    async def handler(connection: Any, message: Mapping[str, Any]) -> None:
        received.append((connection, dict(message)))

    set_segment_ack_handler(handler)
    try:
        message = _ack_message("r1", epoch=8, segment_id=2)
        await _handle_segment_ack(conn, message)
    finally:
        set_segment_ack_handler(None)

    assert len(received) == 1
    assert received[0][0] is conn
    assert received[0][1] == message
    assert conn.downstream.empty()  # 正常分发不回 error


async def test_segment_ack_branch_rejects_malformed_shape_before_handler() -> None:
    conn = _FakeConn()
    called = False

    async def handler(connection: Any, message: Mapping[str, Any]) -> None:
        nonlocal called
        called = True

    set_segment_ack_handler(handler)
    try:
        bad_status: dict[str, Any] = _ack_message("r1", epoch=8, segment_id=2, status="paused")
        bad_epoch: dict[str, Any] = {
            "type": "segment_ack",
            "response_id": "r1",
            "epoch": "8",
            "segment_id": 2,
            "status": "playing",
        }
        missing_field: dict[str, Any] = {"type": "segment_ack", "response_id": "r1"}
        for message in (bad_status, bad_epoch, missing_field):
            await _handle_segment_ack(conn, message)
    finally:
        set_segment_ack_handler(None)

    assert called is False
    errors = [conn.downstream.get_nowait() for _ in range(3)]
    assert all(error["type"] == "error" and error["code"] == "internal" for error in errors)


async def test_segment_ack_branch_without_handler_is_safely_ignored() -> None:
    conn = _FakeConn()
    set_segment_ack_handler(None)

    await _handle_segment_ack(conn, _ack_message("r1", epoch=1, segment_id=0))

    assert conn.downstream.empty()  # 只 warn，不回执不关连接


async def test_touch_played_branch_dispatches_valid_message_to_handler() -> None:
    conn = _FakeConn()
    received: list[tuple[Any, Mapping[str, Any]]] = []

    async def handler(connection: Any, message: Mapping[str, Any]) -> None:
        received.append((connection, dict(message)))

    set_touch_played_handler(handler)
    try:
        message = _touch_played_message()
        await _handle_touch_played(conn, message)
    finally:
        set_touch_played_handler(None)

    assert len(received) == 1
    assert received[0][0] is conn
    assert received[0][1] == message
    assert conn.downstream.empty()  # 正常分发不回 error


async def test_touch_played_branch_rejects_malformed_shape_before_handler() -> None:
    conn = _FakeConn()
    called = False

    async def handler(connection: Any, message: Mapping[str, Any]) -> None:
        nonlocal called
        called = True

    set_touch_played_handler(handler)
    try:
        bad_attempt: dict[str, Any] = _touch_played_message(attempt_no=True)  # bool 冒充 int
        bad_event: dict[str, Any] = _touch_played_message(event_id=None)  # type: ignore[arg-type]
        missing_field: dict[str, Any] = {"type": "touch_played", "touch_id": "t1"}
        for message in (bad_attempt, bad_event, missing_field):
            await _handle_touch_played(conn, message)
    finally:
        set_touch_played_handler(None)

    assert called is False
    errors = [conn.downstream.get_nowait() for _ in range(3)]
    assert all(error["type"] == "error" and error["code"] == "internal" for error in errors)


async def test_touch_played_branch_without_handler_is_safely_ignored() -> None:
    conn = _FakeConn()
    set_touch_played_handler(None)

    await _handle_touch_played(conn, _touch_played_message())

    assert conn.downstream.empty()  # 回执是尽力而为的上报：未装配只 warn，不回执不关连接
