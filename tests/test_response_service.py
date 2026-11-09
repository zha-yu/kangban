"""Day 6 N07 · ResponseStream 测试：分段 / segment_ack 四步 / 打断五步顺序。

所有协作者都是测试替身（deliver / EventSink / TtsQueue / Cancellable 只记录调用），
不触碰真实 WS、Redis、PostgreSQL。身份与 thread 全部为测试显式注入的服务端值。

关键纪律（任务卡 §7 风险 R-3）：**打断顺序断言验证的是调用顺序而非结果**——
假对象在每次被调时记录「调用名 + 当时 session_epoch」，用 epoch 探针证明
「停队列发生在 epoch+1 之前、取消任务与事件追加发生在 epoch+1 之后」。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from app.audio.signaling import (
    SegmentAckMsg,
    TtsTextMsg,
    _handle_segment_ack,
    set_segment_ack_handler,
)
from app.core.context import reset_trace_id, set_trace_id
from app.services.response_service import (
    MAX_SEGMENT_CHARS,
    ResponseHandle,
    ResponseService,
    TurnContext,
    split_segments,
)

_THREAD = "thread-1"


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeDeliver:
    """signaling.deliver 的替身：记录 (device_id, message)，可控制在线/离线。"""

    def __init__(self, *, online: bool = True) -> None:
        self.online = online
        self.messages: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, device_id: str, message: Mapping[str, Any]) -> bool:
        self.messages.append((device_id, dict(message)))
        return self.online


class FakeEventSink:
    """EventSink 替身：记录 (event_type, payload, 调用时刻 epoch)。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], int | None]] = []
        self.epoch_probe: Callable[[], int] | None = None
        self.fail = False

    async def append_event(
        self,
        *,
        thread_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        turn_id: str | None = None,
        response_id: str | None = None,
    ) -> int:
        if self.fail:
            raise RuntimeError("event sink down")
        epoch = self.epoch_probe() if self.epoch_probe is not None else None
        self.events.append((event_type, dict(payload), epoch))
        return len(self.events)


class FakeTtsQueue:
    """TtsQueue 替身：记录 stop(response_id) 调用与当时的 epoch。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.epoch_probe: Callable[[], int] | None = None

    def stop(self, response_id: str) -> None:
        epoch = self.epoch_probe() if self.epoch_probe is not None else None
        self.calls.append((response_id, epoch))


class FakeCancellable:
    """在途 LLM/TTS 任务替身：记录 cancel() 调用与当时的 epoch。"""

    def __init__(self) -> None:
        self.calls: list[int | None] = []
        self.epoch_probe: Callable[[], int] | None = None

    def cancel(self) -> bool:
        epoch = self.epoch_probe() if self.epoch_probe is not None else None
        self.calls.append(epoch)
        return True


class Harness:
    """service + 全部替身的组装器；替身共享 epoch 探针（读 service 内部 epoch）。"""

    def __init__(self, *, elder_id: str | None = "elder-a", online: bool = True) -> None:
        self.deliver = FakeDeliver(online=online)
        self.sink = FakeEventSink()
        self.tts_queue = FakeTtsQueue()
        self.service = ResponseService(
            deliver=self.deliver,
            events=self.sink,
            tts_queue=self.tts_queue,
        )

        def probe() -> int:
            return self.service.session_epoch(_THREAD)

        self.sink.epoch_probe = probe
        self.tts_queue.epoch_probe = probe
        self.cancellable = FakeCancellable()
        self.cancellable.epoch_probe = probe
        self.turn = TurnContext(
            thread_id=_THREAD,
            turn_id="turn-1",
            device_id="dev-1",
            elder_id=elder_id,
        )

    async def start(self, text: str = "你好。今天天气真不错！") -> ResponseHandle:
        return await self.service.start(self.turn, _result(text))


class _Result:
    """GenerationResultLike 最小实现。"""

    def __init__(self, text: str, degraded: bool = False) -> None:
        self.text = text
        self.degraded = degraded


def _result(text: str, degraded: bool = False) -> _Result:
    return _Result(text=text, degraded=degraded)


def _ack_message(
    response_id: str,
    *,
    epoch: int,
    segment_id: int,
    status: str = "playing",
    at_ms: int | None = 100,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "segment_ack",
        "response_id": response_id,
        "epoch": epoch,
        "segment_id": segment_id,
        "status": status,
    }
    if at_ms is not None:
        message["at_ms"] = at_ms
    return message


# ---------------------------------------------------------------------------
# 分段：40 字边界 / 长句逗号补切 / 。！？；切点
# ---------------------------------------------------------------------------


def test_split_respects_40_char_boundary() -> None:
    exact = "好" * MAX_SEGMENT_CHARS
    assert split_segments(exact) == (exact,)  # 恰 40 字：单段

    over = "好" * (MAX_SEGMENT_CHARS + 1)  # 41 字、无逗号可补切：硬切兜底
    segments = split_segments(over)
    assert len(segments) == 2
    assert all(len(segment) <= MAX_SEGMENT_CHARS for segment in segments)
    assert "".join(segments) == over


def test_split_at_sentence_punctuation_keeps_marks() -> None:
    text = "你好。今天天气真不错！吃过了吗？我们在外面坐一会儿；走吧"
    segments = split_segments(text)

    assert segments == (
        "你好。",
        "今天天气真不错！",
        "吃过了吗？",
        "我们在外面坐一会儿；",
        "走吧",
    )
    assert "".join(segments) == text


def test_split_long_sentence_falls_back_to_comma() -> None:
    clause_a = "甲" * 20
    clause_b = "乙" * 25  # 20 + 1(逗号) + 25 = 46 > 40，逗号补切成两段
    text = f"{clause_a}，{clause_b}"

    segments = split_segments(text)

    assert len(segments) == 2
    assert all(len(segment) <= MAX_SEGMENT_CHARS for segment in segments)
    assert "".join(segments) == text  # 标点保留，拼接还原原文
    assert segments[0].endswith("，")


def test_split_mixed_and_empty_input() -> None:
    long_sentence = "甲" * 30 + "，" + "乙" * 15 + "好的"  # 48 字长句带逗号
    text = f"好的。{long_sentence}"

    segments = split_segments(text)

    # 「好的。」独立成段；48 字长句按逗号补切成两段（buf 合不进就先出段）
    assert segments == ("好的。", "甲" * 30 + "，", "乙" * 15 + "好的")
    assert "".join(segments) == text
    assert all(len(segment) <= MAX_SEGMENT_CHARS for segment in segments)
    assert split_segments("   ") == ()
    assert split_segments("") == ()


# ---------------------------------------------------------------------------
# start：tts_text v2 五新字段 + 老字段保留 + response_started 事件
# ---------------------------------------------------------------------------


async def test_start_delivers_v2_message_per_segment() -> None:
    h = Harness()
    handle = await h.start("你好。今天天气真不错！")  # 2 段

    assert [msg["text"] for _, msg in h.deliver.messages] == ["你好。", "今天天气真不错！"]
    assert {device for device, _ in h.deliver.messages} == {"dev-1"}
    for index, (_, message) in enumerate(h.deliver.messages):
        # 老字段保留（老客户端忽略新字段仍能整段一播）
        assert message["type"] == "tts_text"
        assert isinstance(message["text"], str)
        assert message["degraded"] is False
        # v2 五新字段
        assert message["response_id"] == handle.response_id
        assert message["epoch"] == handle.epoch == 1  # 会话首代
        assert message["segment_id"] == index
        assert message["index"] == index
        assert message["total"] == 2

    # response_started 事件随首次下发写入
    event_types = [event_type for event_type, _, _ in h.sink.events]
    assert "response_started" in event_types
    started = next(payload for name, payload, _ in h.sink.events if name == "response_started")
    assert started["response_id"] == handle.response_id
    assert started["degraded"] is False

    # 初始快照：未收到任何 ack
    assert handle.total == 2
    assert handle.segment_id == -1 == handle.index
    assert handle.at_ms is None
    assert handle.delivered_text == ""
    assert handle.delivery_status == "unknown"
    assert handle.last_played_segment == -1


async def test_start_rejects_empty_text_and_bad_context() -> None:
    h = Harness()
    with pytest.raises(ValueError, match="non-empty"):
        await h.start("   ")
    with pytest.raises(ValueError, match="server-generated safe token"):
        await h.service.start(
            TurnContext(thread_id="", turn_id="turn-1", device_id="dev-1"), _result("你好")
        )
    with pytest.raises(ValueError, match="non-empty server values"):
        await h.service.start(
            TurnContext(thread_id=_THREAD, turn_id="", device_id="dev-1"), _result("你好")
        )
    assert h.deliver.messages == []
    assert h.sink.events == []


# ---------------------------------------------------------------------------
# ack 四步：unknown_response / stale_epoch / wrong_status / not_monotonic
# ---------------------------------------------------------------------------


async def test_ack_unknown_response_rejected(caplog: pytest.LogCaptureFixture) -> None:
    h = Harness()
    with caplog.at_level(logging.WARNING, logger="app.services.response_service"):
        accepted = await h.service.ack(
            device_id="dev-1",
            response_id="no-such-response",
            epoch=1,
            segment_id=0,
            status="playing",
        )
        # 跨设备引用同一 response 同样按 unknown_response 拒绝
        handle = await h.start()
        cross = await h.service.ack(
            device_id="dev-other",
            response_id=handle.response_id,
            epoch=1,
            segment_id=0,
            status="playing",
        )

    assert accepted is False
    assert cross is False
    rejected = [r for r in caplog.records if r.getMessage() == "response_ack_rejected"]
    assert len(rejected) == 2
    assert all(r.rejected_reason == "unknown_response" for r in rejected)


async def test_ack_stale_epoch_rejected(caplog: pytest.LogCaptureFixture) -> None:
    """打断后 epoch 已 +1，携带旧 epoch 的迟到 ack 走② stale_epoch（跨代淘汰）。"""
    h = Harness()
    handle = await h.start()
    token = set_trace_id("trace-ack")
    try:
        await h.service.interrupt(handle.response_id)  # epoch: 1 → 2
        with caplog.at_level(logging.WARNING, logger="app.services.response_service"):
            accepted = await h.service.ack(
                device_id="dev-1",
                response_id=handle.response_id,
                epoch=handle.epoch,  # 旧代 epoch=1
                segment_id=0,
                status="playing",
            )
    finally:
        reset_trace_id(token)

    assert accepted is False
    record = next(r for r in caplog.records if r.getMessage() == "response_ack_rejected")
    assert record.rejected_reason == "stale_epoch"
    assert record.trace_id == "trace-ack"  # 结构化日志带 trace_id


async def test_ack_wrong_status_rejected_after_interrupt() -> None:
    """N20 当场改动题场景：伪造当前 epoch 的迟到 ack 仍被③ wrong_status 拦截。"""
    h = Harness()
    handle = await h.start()  # epoch=1
    await h.service.interrupt(handle.response_id)  # epoch → 2，status → ended

    accepted = await h.service.ack(
        device_id="dev-1",
        response_id=handle.response_id,
        epoch=h.service.session_epoch(_THREAD),  # 伪造当前 epoch=2
        segment_id=0,
        status="playing",
    )

    assert accepted is False
    # 后续新回复的 ack 不受影响：新 response 正常被接受
    new_handle = await h.start("新的一条回复")
    assert await h.service.ack(
        device_id="dev-1",
        response_id=new_handle.response_id,
        epoch=h.service.session_epoch(_THREAD),
        segment_id=0,
        status="playing",
    )


async def test_ack_not_monotonic_rejected(caplog: pytest.LogCaptureFixture) -> None:
    """基线只由 played 推进（v1.2 与「最后完整播放段」同构）：playing 不推进。"""
    h = Harness()
    handle = await h.start("第一段话。第二段话。")
    rid = handle.response_id
    epoch = h.service.session_epoch(_THREAD)

    assert await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="playing"
    )
    assert await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="playing"
    )  # playing 重复上报不推进基线，仍满足 ④（0 > -1），无状态变化
    assert h.service.handle(rid).segment_id == -1
    assert await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="played"
    )  # played 推进基线
    assert h.service.handle(rid).segment_id == 0

    with caplog.at_level(logging.WARNING, logger="app.services.response_service"):
        restart = await h.service.ack(
            device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="playing"
        )  # 已播放段不可重播：0 > 0 不成立
        replay = await h.service.ack(
            device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="played"
        )  # played 重复：0 > 0 不成立
        out_of_range = await h.service.ack(
            device_id="dev-1", response_id=rid, epoch=epoch, segment_id=99, status="playing"
        )
        negative = await h.service.ack(
            device_id="dev-1", response_id=rid, epoch=epoch, segment_id=-5, status="playing"
        )

    assert restart is False
    assert replay is False
    assert out_of_range is False  # 合法域 [0, total-1] 之外按④拒绝（v1.2 口径）
    assert negative is False
    reasons = [
        r.rejected_reason for r in caplog.records if r.getMessage() == "response_ack_rejected"
    ]
    assert reasons == ["not_monotonic", "not_monotonic", "not_monotonic", "not_monotonic"]
    # 单调基线未被动摇
    final = h.service.handle(rid)
    assert final is not None
    assert final.segment_id == 0


async def test_ack_playing_then_played_completes_delivery() -> None:
    h = Harness()
    handle = await h.start("第一段话。第二段话。")
    rid = handle.response_id
    epoch = h.service.session_epoch(_THREAD)

    assert await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="playing", at_ms=120
    )
    assert await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="played", at_ms=900
    )
    assert await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=1, status="playing", at_ms=950
    )
    done = await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=1, status="played", at_ms=2000
    )

    assert done is True
    final = h.service.handle(rid)
    assert final is not None
    assert final.delivery_status == "delivered"  # 末段 played 触发 completed
    assert final.delivered_text == "第一段话。第二段话。"  # 只拼 played 段
    assert final.last_played_segment == 1
    assert final.at_ms == 2000
    event_types = [event_type for event_type, _, _ in h.sink.events]
    assert event_types == [
        "response_started",
        "response_segment_delivered",
        "response_segment_delivered",
        "response_completed",
    ]
    completed = h.sink.events[-1][1]
    assert completed == {"total_segments": 2}
    # 末段 played 后回复已结束：再来的 ack 走③ wrong_status
    assert not await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=1, status="playing"
    )


# ---------------------------------------------------------------------------
# canceled 类 ack：只走①②③跳过④（正反两例）+ -1 哨兵
# ---------------------------------------------------------------------------


async def test_canceled_ack_skips_monotonic_and_mutates_nothing() -> None:
    h = Harness()
    handle = await h.start("第一段话。第二段话。")
    rid = handle.response_id
    epoch = h.service.session_epoch(_THREAD)
    await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="playing"
    )
    await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="played"
    )
    before = h.service.handle(rid)
    assert before is not None
    assert before.segment_id == 0

    # 正例①：canceled 带已确认段号（== last_acked，单调规则会拒 0 > 0）→ 跳过④被接受
    assert await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="canceled"
    )
    # 正例②：无一完整播放段时客户端带 -1 哨兵（0 基合法域之外）→ 仍被接受
    assert await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=-1, status="canceled"
    )

    after = h.service.handle(rid)
    assert after is not None
    assert after.segment_id == 0  # canceled 不推进单调基线
    assert after.delivered_text == "第一段话。"  # 不改已拼 played 文本
    assert after.delivery_status == "unknown"  # 陈述性上报不改状态
    assert after.last_played_segment == 0
    assert [event_type for event_type, _, _ in h.sink.events] == [
        "response_started",
        "response_segment_delivered",
    ]


async def test_canceled_ack_still_enforces_first_three_steps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """反例：跳过④≠跳过①②③——stale epoch / unknown response 的 canceled 照拒。"""
    h = Harness()
    handle = await h.start()
    await h.service.interrupt(handle.response_id)  # epoch 1 → 2
    with caplog.at_level(logging.WARNING, logger="app.services.response_service"):
        stale = await h.service.ack(
            device_id="dev-1",
            response_id=handle.response_id,
            epoch=1,
            segment_id=-1,
            status="canceled",
        )
        unknown = await h.service.ack(
            device_id="dev-1", response_id="ghost", epoch=2, segment_id=-1, status="canceled"
        )

    assert stale is False
    assert unknown is False
    reasons = [
        r.rejected_reason for r in caplog.records if r.getMessage() == "response_ack_rejected"
    ]
    assert reasons == ["stale_epoch", "unknown_response"]


# ---------------------------------------------------------------------------
# interrupt 五步：顺序断言（看调用顺序，不看结果）+ 哨兵 + 截断
# ---------------------------------------------------------------------------


async def test_interrupt_five_steps_call_order() -> None:
    """②停队列(epoch 旧) → ③epoch+1 → ④取消任务(epoch 新) → ⑤事件(epoch 新)。"""
    h = Harness()
    handle = await h.start("第一段话。第二段话。")
    h.service.register_cancellable(handle.response_id, h.cancellable)
    epoch_before = h.service.session_epoch(_THREAD)
    order: list[str] = []

    original_stop = h.tts_queue.stop

    def recording_stop(response_id: str) -> None:
        order.append("tts_stop")
        original_stop(response_id)

    original_cancel = h.cancellable.cancel

    def recording_cancel() -> bool:
        order.append("cancel_tasks")
        return bool(original_cancel())

    original_append = h.sink.append_event

    async def recording_append(**kwargs: Any) -> int:
        order.append("append_event")
        return await original_append(**kwargs)

    h.tts_queue.stop = recording_stop
    h.cancellable.cancel = recording_cancel
    h.sink.append_event = recording_append

    interrupted = await h.service.interrupt(handle.response_id)

    # 调用顺序即五步顺序
    assert order == ["tts_stop", "cancel_tasks", "append_event"]
    # ② 停队列发生在 epoch+1 之前（当时 epoch 还是旧值）
    assert h.tts_queue.calls == [(handle.response_id, epoch_before)]
    # ④ 取消任务发生在 epoch+1 之后
    assert h.cancellable.calls == [epoch_before + 1]
    # ⑤ response_interrupted 事件带 last_played_segment（无一完整播放段 → -1 哨兵）
    assert h.sink.events[-1][0] == "response_interrupted"
    assert h.sink.events[-1][1] == {
        "response_id": handle.response_id,
        "last_played_segment": -1,
    }
    assert h.sink.events[-1][2] == epoch_before + 1
    assert interrupted is not None
    assert interrupted.delivery_status == "interrupted"
    assert interrupted.delivered_text == ""


async def test_interrupt_truncates_delivered_text_to_played_segments() -> None:
    h = Harness()
    handle = await h.start("第一段。第二段。第三段。")
    rid = handle.response_id
    epoch = h.service.session_epoch(_THREAD)

    await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="playing"
    )
    await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="played"
    )
    await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=1, status="playing"
    )
    interrupted = await h.service.interrupt(rid)

    assert interrupted is not None
    assert interrupted.delivered_text == "第一段。"  # 只拼 played 段：正在播的第 1 段不算
    assert interrupted.last_played_segment == 0
    assert interrupted.delivery_status == "interrupted"
    payload = h.sink.events[-1][1]
    assert payload["last_played_segment"] == 0
    # 事件只追加：已写事件不因打断回滚（工具 succeeded 副作用保留，C-5 ⑥ 同口径）
    event_types = [event_type for event_type, _, _ in h.sink.events]
    assert event_types == [
        "response_started",
        "response_segment_delivered",
        "response_interrupted",
    ]


async def test_interrupt_is_noop_for_unknown_or_finished_response() -> None:
    h = Harness()
    handle = await h.start("你好今天天气真不错")  # 无标点：单段
    rid = handle.response_id
    epoch = h.service.session_epoch(_THREAD)

    assert await h.service.interrupt("ghost") is None

    await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="played"
    )  # 单段回复 → 已 delivered
    before = h.service.handle(rid)
    assert await h.service.interrupt(rid) == before  # 幂等 no-op，返回当前快照
    assert h.service.session_epoch(_THREAD) == epoch  # 不重复 bump
    assert h.tts_queue.calls == []  # 未再停队列
    assert [e for e, _, _ in h.sink.events].count("response_interrupted") == 0
    # 已结束的 response 不再接受新的 cancellable 登记
    assert h.service.register_cancellable(rid, h.cancellable) is False


async def test_interrupt_event_sink_failure_still_marks_interrupted() -> None:
    h = Harness()
    handle = await h.start()
    h.sink.fail = True
    interrupted = await h.service.interrupt(handle.response_id)

    assert interrupted is not None
    assert interrupted.delivery_status == "interrupted"
    assert h.service.session_epoch(_THREAD) == 2  # ③ 仍已执行


# ---------------------------------------------------------------------------
# generic 模式：同样工作，只写事件
# ---------------------------------------------------------------------------


async def test_generic_mode_streams_and_only_appends_events() -> None:
    h = Harness(elder_id=None)  # generic：elder_id 为 None
    handle = await h.start("好的，我陪您聊聊。还有想听的吗？")
    rid = handle.response_id
    epoch = h.service.session_epoch(_THREAD)

    assert [message["text"] for _, message in h.deliver.messages] == [
        "好的，我陪您聊聊。",
        "还有想听的吗？",
    ]
    assert await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="played"
    )
    interrupted = await h.service.interrupt(rid)

    # generic 不落历史（本服务本就不写 conversation_turns），事件照写不误
    final = h.service.handle(rid)
    assert final is not None
    assert final.delivery_status == "interrupted"
    assert interrupted is not None
    assert interrupted.delivered_text == "好的，我陪您聊聊。"
    event_types = [event_type for event_type, _, _ in h.sink.events]
    assert event_types == ["response_started", "response_segment_delivered", "response_interrupted"]


# ---------------------------------------------------------------------------
# 指标：每段下发与每次被接受的 ack 各一条 response_segment
# ---------------------------------------------------------------------------


def _response_segment_metrics(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.getMessage() == "metric stage_ms"
        and getattr(record, "stage", None) == "response_segment"
    ]


async def test_metrics_recorded_per_delivery_and_per_accepted_ack(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.core.metrics"):
        h = Harness()
        handle = await h.start("第一段。第二段。")
        rid = handle.response_id
        epoch = h.service.session_epoch(_THREAD)

        assert len(_response_segment_metrics(caplog)) == 2  # 每段下发一条
        await h.service.ack(
            device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="playing"
        )
        await h.service.ack(
            device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="played"
        )
        assert len(_response_segment_metrics(caplog)) == 4  # 每次被接受的 ack 再各一条
        await h.service.ack(
            device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="playing"
        )  # 重复 → not_monotonic 被拒 → 不打指标
        assert len(_response_segment_metrics(caplog)) == 4
        await h.service.ack(
            device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="canceled"
        )  # canceled 属被接受的 ack → 打一条
        assert len(_response_segment_metrics(caplog)) == 5


async def test_ack_rejection_records_no_metric(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.core.metrics"):
        h = Harness()
        handle = await h.start("第一段。")
        rid = handle.response_id
        epoch = h.service.session_epoch(_THREAD)
        await h.service.ack(
            device_id="dev-1", response_id=rid, epoch=epoch, segment_id=0, status="playing"
        )
        baseline = len(_response_segment_metrics(caplog))
        assert baseline == 2  # 1 段下发 + 1 次被接受 ack

        assert not await h.service.ack(
            device_id="dev-1", response_id="ghost", epoch=epoch, segment_id=0, status="playing"
        )  # unknown_response
        assert not await h.service.ack(
            device_id="dev-1", response_id=rid, epoch=epoch, segment_id=99, status="playing"
        )  # not_monotonic（合法域之外）
    assert len(_response_segment_metrics(caplog)) == baseline  # 被拒 ack 不打 response_segment


# ---------------------------------------------------------------------------
# signaling.py 扩展：TtsTextMsg v2 形状 + segment_ack 分支分发
# ---------------------------------------------------------------------------


def test_tts_text_v2_keeps_old_fields_and_adds_five_optional() -> None:
    # 注：本模块启用了 `from __future__ import annotations`，3.12 的 TypedDict 在
    # 类创建期无法识别 NotRequired（__optional_keys__ 恒空），因此按注解源码断言。
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
    assert "NotRequired" in str(annotations["at_ms"])  # at_ms 可选，其余必填


class _FakeConn:
    """_handle_segment_ack 所需的最小连接面（trace/device/downstream）。"""

    def __init__(self) -> None:
        self.trace_id = "trace-seg-ack"
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
