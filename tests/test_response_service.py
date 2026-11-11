"""Day 6 N07 · ResponseStream 测试：分段 / segment_ack 四步 / 打断五步顺序。

所有协作者都是测试替身（deliver / EventSink / TtsQueue / Cancellable 只记录调用），
不触碰真实 WS、Redis、PostgreSQL。身份与 thread 全部为测试显式注入的服务端值。

关键纪律（任务卡 §7 风险 R-3）：**打断顺序断言验证的是调用顺序而非结果**——
假对象在每次被调时记录「调用名 + 当时 session_epoch」，用 epoch 探针证明
「停队列发生在 epoch+1 之前、取消任务与事件追加发生在 epoch+1 之后」。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

import pytest

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
        self.cancel_next = False  # S4 试跑修复验证：注入 CancelledError（BaseException 系）

    async def append_event(
        self,
        *,
        thread_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        turn_id: str | None = None,
        response_id: str | None = None,
    ) -> int:
        if self.cancel_next:
            self.cancel_next = False
            raise asyncio.CancelledError
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


async def test_safe_append_event_reraises_cancellation() -> None:
    """P3-27：事件追加被取消必须重抛（asyncio 取消传播），不再吞掉。

    Day 6 曾按「吞掉不炸主链路」修复（S4 试跑②）；Day 7 任务卡 P3-27 明确改为
    重抛——取消即整个回合任务被取消（如 ws_superseded），剩余分段本就不应继续
    下发，吞掉反而丢失取消语义。
    """
    h = Harness()
    h.sink.cancel_next = True  # response_started 追加恰好被取消
    with pytest.raises(asyncio.CancelledError):
        await h.start("你好。今天天气真不错！")
    # 取消传播：取消点之后的分段不再下发
    assert h.deliver.messages == []


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
    # P2-10（B1）：payload 写入时即写全（唯一写口 + 事件只追加不回填）
    assert completed == {
        "total_segments": 2,
        "generated_text": "第一段话。第二段话。",
        "delivered_text": "第一段话。第二段话。",
        "response_status": "completed",
    }
    # 末段 played 后回复已结束：再来的 ack 走③ wrong_status
    assert not await h.service.ack(
        device_id="dev-1", response_id=rid, epoch=epoch, segment_id=1, status="playing"
    )


# ---------------------------------------------------------------------------
# canceled 类 ack：只走①②③跳过④（正反两例）+ -1 哨兵
# ---------------------------------------------------------------------------


async def test_canceled_ack_settles_interrupted_and_ends_response() -> None:
    """canceled 是终态上报（Day 7 P2-7）：置 interrupted、以水位截断、写事件、结算。

    Day 6 语义「canceled 陈述性上报不改状态」已由 Day 7 任务卡改写为终态结算；
    终态后同代 played 与再次 canceled 均被 ③ wrong_status 拒绝（P2-7）。
    """
    h = SettlementHarness()
    handle = await h.start("第一段话。第二段话。")
    rid = handle.response_id
    await h.play(rid, 0, status="playing")
    await h.play(rid, 0)

    # 段号==水位 → 被接受并结算为 interrupted
    assert await h.play(rid, 0, status="canceled")
    final = h.service.handle(rid)
    assert final is not None
    assert final.delivery_status == "interrupted"
    assert final.delivered_text == "第一段话。"  # 按水位截断
    assert final.segment_id == 0
    assert _event_names(h).count("response_interrupted") == 1
    assert _event_names(h).count("response_completed") == 0
    assert len(h.store.finishes(turn_id="turn-1", status="interrupted")) == 1

    # 终态后：同代 played / 再次 canceled 均被拒绝
    assert not await h.play(rid, 1)
    assert not await h.play(rid, 0, status="canceled")


async def test_canceled_watermark_mismatch_uses_watermark() -> None:
    """P2-8：canceled 段号与水位不一致 → 告警并以水位为准（不按客户端段号截断）。"""
    h = SettlementHarness()
    handle = await h.start("第一段。第二段。")
    rid = handle.response_id
    await h.play(rid, 0, status="playing")
    await h.play(rid, 0)

    # 客户端上报已播到第 1 段，但服务端水位只有 0 → 以水位为准
    assert await h.play(rid, 1, status="canceled")
    final = h.service.handle(rid)
    assert final is not None
    assert final.delivered_text == "第一段。"
    assert final.last_played_segment == 0


async def test_canceled_with_no_played_segments_settles_empty_prefix() -> None:
    """无一完整播放段：canceled（-1 哨兵）→ 结算 interrupted、投影空串。"""
    h = SettlementHarness()
    handle = await h.start("第一段。第二段。")
    rid = handle.response_id

    assert await h.play(rid, -1, status="canceled")
    final = h.service.handle(rid)
    assert final is not None
    assert final.delivery_status == "interrupted"
    assert final.delivered_text == ""
    assert len(h.store.finishes(turn_id="turn-1", status="interrupted")) == 1


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
# Day 7 B1 契约（先红）：回合终态结算端口 / response_completed 唯一 / canceled 终态
# ---------------------------------------------------------------------------


class FakeHistoryStore:
    """B1 结算端口替身（Step 5 注入 ResponseService，替代 HistoryStore 适配层）。

    契约形状与 `history_service` 语义同构：
    - `commit_response(delivered_text=...)`：只写已送达投影，**不改**轮次状态；
    - `finish_turn(status=...)`：轮次终态唯一写口（幂等不覆盖由 Step 5c 保证）。
    """

    def __init__(self) -> None:
        self.commit_calls: list[dict[str, Any]] = []
        self.finish_calls: list[dict[str, Any]] = []

    async def commit_response(
        self,
        *,
        turn_id: str,
        response_id: str,
        generated_text: str,
        delivered_text: str | None,
        status: str,
    ) -> None:
        self.commit_calls.append(
            {
                "turn_id": turn_id,
                "response_id": response_id,
                "generated_text": generated_text,
                "delivered_text": delivered_text,
                "status": status,
            }
        )

    async def finish_turn(self, *, turn_id: str, status: str) -> None:
        self.finish_calls.append({"turn_id": turn_id, "status": status})

    def commits(self, *, turn_id: str, status: str) -> list[dict[str, Any]]:
        return [c for c in self.commit_calls if c["turn_id"] == turn_id and c["status"] == status]

    def finishes(self, *, turn_id: str, status: str) -> list[dict[str, Any]]:
        return [f for f in self.finish_calls if f["turn_id"] == turn_id and f["status"] == status]


class SettlementHarness:
    """带结算端口的组装器：替身与 Harness 相同，另注入 FakeHistoryStore。"""

    def __init__(self, *, elder_id: str | None = "elder-a") -> None:
        self.deliver = FakeDeliver()
        self.sink = FakeEventSink()
        self.tts_queue = FakeTtsQueue()
        self.store = FakeHistoryStore()
        self.service = ResponseService(
            deliver=self.deliver,
            events=self.sink,
            tts_queue=self.tts_queue,
            history_store=self.store,
        )
        self.turn = TurnContext(
            thread_id=_THREAD, turn_id="turn-1", device_id="dev-1", elder_id=elder_id
        )

    async def start(self, text: str) -> ResponseHandle:
        return await self.service.start(self.turn, _result(text))

    async def play(self, rid: str, segment_id: int, *, status: str = "played") -> bool:
        """以当前会话 epoch 发送一次 ack（played 默认）。"""
        return await self.service.ack(
            device_id="dev-1",
            response_id=rid,
            epoch=self.service.session_epoch(_THREAD),
            segment_id=segment_id,
            status=status,
        )


def _event_names(h: SettlementHarness) -> list[str]:
    return [event_type for event_type, _, _ in h.sink.events]


async def test_played_last_segment_finishes_turn() -> None:
    """契约①（P1-1）：末段 played → 轮次终态 completed + delivered 投影非 NULL。"""
    h = SettlementHarness()
    handle = await h.start("第一段话。第二段话。")
    rid = handle.response_id

    assert await h.play(rid, 0, status="playing")
    assert await h.play(rid, 0)
    assert await h.play(rid, 1, status="playing")
    assert await h.play(rid, 1)

    # 投影：assistant_delivered_text 非 NULL 且等于完整文本（只拼 played 段）
    completed_commits = h.store.commits(turn_id="turn-1", status="completed")
    assert len(completed_commits) == 1
    assert completed_commits[0]["delivered_text"] == "第一段话。第二段话。"
    assert completed_commits[0]["response_id"] == rid
    assert completed_commits[0]["generated_text"] == "第一段话。第二段话。"
    # 终态：conversation_turns.status=completed（finish_turn 唯一写口，恰好一次）
    assert len(h.store.finishes(turn_id="turn-1", status="completed")) == 1
    # response_completed 每轮恰好 1 条
    assert _event_names(h).count("response_completed") == 1
    # P2-10（B1）：事件 payload 在写入时就写全——唯一写口 + 事件只追加不回填，
    # 事后 commit_response 只补投影（handoff-B-to-A §2），generated_text 缺了就永久缺。
    completed_payloads = [p for t, p, _ in h.sink.events if t == "response_completed"]
    assert completed_payloads == [
        {
            "total_segments": 2,
            "generated_text": "第一段话。第二段话。",
            "delivered_text": "第一段话。第二段话。",
            "response_status": "completed",
        }
    ]


async def test_interrupted_turn_truncates_delivered_text() -> None:
    """契约②（P1-3）：打断 → 终态 interrupted + 投影为 [0..last_played] 连续前缀。"""
    h = SettlementHarness()
    handle = await h.start("第一段。第二段。第三段。")
    rid = handle.response_id
    await h.play(rid, 0, status="playing")
    await h.play(rid, 0)
    await h.play(rid, 1, status="playing")
    await h.service.interrupt(rid)

    interrupted_commits = h.store.commits(turn_id="turn-1", status="interrupted")
    assert len(interrupted_commits) == 1
    assert interrupted_commits[0]["delivered_text"] == "第一段。"  # 只含已 played 前缀
    assert len(h.store.finishes(turn_id="turn-1", status="interrupted")) == 1
    # 打断后不得出现 response_completed；response_interrupted 恰好 1 条
    assert _event_names(h).count("response_completed") == 0
    assert _event_names(h).count("response_interrupted") == 1


async def test_duplicate_played_does_not_duplicate_completed() -> None:
    """契约③：重复 ack / 迟到 ack 不重复写事件，completed 结算恰好一次。"""
    h = SettlementHarness()
    handle = await h.start("第一段话。第二段话。")
    rid = handle.response_id
    await h.play(rid, 0, status="playing")
    await h.play(rid, 0)
    await h.play(rid, 1, status="playing")
    assert await h.play(rid, 1)  # 末段 played：结束

    # 重复 played：③ wrong_status 拒绝（response 已 ended）
    assert not await h.play(rid, 1)
    # 迟到 ack（即使伪造当前 epoch）同样被拒
    assert not await h.play(rid, 0)

    assert _event_names(h).count("response_completed") == 1
    assert len(h.store.commits(turn_id="turn-1", status="completed")) == 1
    assert len(h.store.finishes(turn_id="turn-1", status="completed")) == 1


async def test_canceled_same_epoch_played_rejected() -> None:
    """契约③续（P2-7）：canceled 是终态上报——结算 interrupted，再拒绝同代 played。"""
    h = SettlementHarness()
    handle = await h.start("第一段。第二段。")
    rid = handle.response_id
    await h.play(rid, 0, status="playing")
    await h.play(rid, 0)

    # canceled 上报（同代、段号==水位）：置终态 interrupted，按水位截断投影
    assert await h.play(rid, 0, status="canceled")
    canceled_commits = h.store.commits(turn_id="turn-1", status="interrupted")
    assert len(canceled_commits) == 1
    assert canceled_commits[0]["delivered_text"] == "第一段。"
    assert len(h.store.finishes(turn_id="turn-1", status="interrupted")) == 1
    assert _event_names(h).count("response_interrupted") == 1

    # canceled 后同代 played 必须被拒绝（P2-7）：response 已 ended
    assert not await h.play(rid, 1)
    # 永不出现 response_completed
    assert _event_names(h).count("response_completed") == 0


async def test_interrupt_idempotent() -> None:
    """契约③续：重复 interrupt 幂等——不重复写事件、不重复结算、不重复 bump epoch。"""
    h = SettlementHarness()
    handle = await h.start("第一段。第二段。")
    rid = handle.response_id
    await h.play(rid, 0, status="playing")
    await h.play(rid, 0)
    await h.service.interrupt(rid)
    epoch_after_first = h.service.session_epoch(_THREAD)
    before = h.service.handle(rid)

    again = await h.service.interrupt(rid)

    assert again == before  # no-op 返回当前快照
    assert h.service.session_epoch(_THREAD) == epoch_after_first  # 不重复 bump
    assert _event_names(h).count("response_interrupted") == 1
    assert len(h.store.finishes(turn_id="turn-1", status="interrupted")) == 1
    assert len(h.store.commits(turn_id="turn-1", status="interrupted")) == 1


async def test_interrupt_rejects_foreign_device() -> None:
    """§5.4 归属校验：非接收该回复的设备发起打断 → no-op、不落库、不炸连接。

    signaling 的 interrupt 分支必传 device_id；N14 内部调用不传（None = 不校验）。
    """
    h = SettlementHarness()
    handle = await h.start("第一段。")
    rid = handle.response_id

    # 异设备打断：no-op
    assert await h.service.interrupt(rid, device_id="dev-other") is None
    current = h.service.handle(rid)
    assert current is not None
    assert current.delivery_status == "unknown"  # 未被打断
    assert _event_names(h).count("response_interrupted") == 0
    assert h.store.finish_calls == []  # 不落库
    assert h.service.session_epoch(_THREAD) == 1  # 不 bump epoch

    # 本设备打断：正常五步 + 结算
    interrupted = await h.service.interrupt(rid, device_id="dev-1")
    assert interrupted is not None
    assert interrupted.delivery_status == "interrupted"
    assert len(h.store.finishes(turn_id="turn-1", status="interrupted")) == 1


# ---------------------------------------------------------------------------
# Day 7 Step 5a 续：Redis epoch/水位恢复回写（P2-9）+ 记忆提取调度点（B1↔B2）
# ---------------------------------------------------------------------------


class FakePersistence:
    """EpochPersistence 替身：dict 仿 Redis，可预置「重启前」状态。"""

    def __init__(self, seed: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(seed or {})
        self.sets: list[tuple[str, str, int | None]] = []

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.data[key] = value
        self.sets.append((key, value, ex))


class FakeMemoryPort:
    """MemoryExtractionLike 替身：记录 extract_after_turn 调用参数。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def extract_after_turn(
        self,
        *,
        elder_id: str | None,
        thread_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
    ) -> int:
        self.calls.append(
            {
                "elder_id": elder_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "user_text": user_text,
                "assistant_text": assistant_text,
            }
        )
        return 1


async def test_redis_epoch_restore() -> None:
    """P2-9：进程重启后从 Redis 恢复会话 epoch；epoch 与水位回写带 TTL。"""
    persistence = FakePersistence(seed={"session:thread-1:epoch": "5"})  # 重启前 epoch=5
    deliver = FakeDeliver()
    sink = FakeEventSink()
    tts_queue = FakeTtsQueue()
    store = FakeHistoryStore()
    service = ResponseService(
        deliver=deliver,
        events=sink,
        tts_queue=tts_queue,
        history_store=store,
        persistence=persistence,
    )
    turn = TurnContext(thread_id=_THREAD, turn_id="turn-1", device_id="dev-1", elder_id="elder-a")

    handle = await service.start(turn, _result("你好。今天天气真不错！"))

    # 重启恢复：新代 = 持久化 epoch + 1（而非归 0 后的 1）
    assert handle.epoch == 6
    assert persistence.data["session:thread-1:epoch"] == "6"
    assert persistence.sets and persistence.sets[0][2] == 7200  # TTL 2h

    # 旧代 epoch=5 的迟到 ack 被淘汰；恢复后的新代 ack 被接受（不再全拒）
    assert not await service.ack(
        device_id="dev-1", response_id=handle.response_id, epoch=5, segment_id=0, status="playing"
    )
    assert await service.ack(
        device_id="dev-1", response_id=handle.response_id, epoch=6, segment_id=0, status="playing"
    )

    # response:{id} 水位回写：注册时与水位推进时各写一次
    record = json.loads(persistence.data[f"response:{handle.response_id}"])
    assert record["thread_id"] == _THREAD
    assert record["epoch"] == 6
    assert record["last_acked_segment"] == -1  # playing ack 不推进水位


# ---------------------------------------------------------------------------
# 分节奏下发（audio_sink 注入）：段音频开始流送 → 才发对应 tts_text
# ---------------------------------------------------------------------------


async def test_paced_delivery_sends_each_segment_after_its_audio() -> None:
    """audio_sink 注入：sink(段 i) 先于段 i 下发执行，文字随段音频开始流送到达。"""
    h = Harness()
    sink_seen: list[tuple[str, int]] = []  # (段文本, 调用时已下发消息数)

    async def sink(segment_text: str) -> bool:
        sink_seen.append((segment_text, len(h.deliver.messages)))
        return True

    handle = await h.service.start(h.turn, _result("第一段。第二段。"), audio_sink=sink)

    for _ in range(200):  # 节奏任务后台跑：等两段都下发
        if len(h.deliver.messages) == 2:
            break
        await asyncio.sleep(0.005)
    assert [msg["text"] for _, msg in h.deliver.messages] == ["第一段。", "第二段。"]
    # 音频先于文字：下发段 0 时 0 条已发；下发段 1 时仅段 0 已发
    assert sink_seen == [("第一段。", 0), ("第二段。", 1)]
    # 消息口径与全量下发完全一致（response_id/epoch/序号）
    for index, (_, message) in enumerate(h.deliver.messages):
        assert message["response_id"] == handle.response_id
        assert message["epoch"] == handle.epoch
        assert message["segment_id"] == index == message["index"]
        assert message["total"] == 2


async def test_paced_delivery_sink_failure_still_delivers_text() -> None:
    """sink 返回 False（本段无音频）：字幕照发，语音缺段（字幕先行契约）。"""
    h = Harness()

    async def sink(segment_text: str) -> bool:
        return False  # 模拟 TTS 不可用 / 会话已关

    await h.service.start(h.turn, _result("第一段。第二段。"), audio_sink=sink)
    for _ in range(200):
        if len(h.deliver.messages) == 2:
            break
        await asyncio.sleep(0.005)
    assert [msg["text"] for _, msg in h.deliver.messages] == ["第一段。", "第二段。"]


async def test_paced_delivery_audio_and_text_both_failed_aborts() -> None:
    """音频与文字双失败（连接已死）：停止空转，剩余段不再合成/下发。"""
    h = Harness()
    sink_calls: list[str] = []

    async def sink(segment_text: str) -> bool:
        sink_calls.append(segment_text)
        return False

    h.deliver.online = False  # deliver 也失败 → 双失败
    await h.service.start(h.turn, _result("第一段。第二段。第三段。"), audio_sink=sink)
    for _ in range(200):
        if len(sink_calls) >= 1:
            break
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.05)
    # 首段双失败后终止：每段只调一次 sink，剩余段不空转
    # （FakeDeliver 失败也记录 → 第一段被尝试下发两次[重试 1 次]，均未送达）
    assert sink_calls == ["第一段。"]
    assert [msg["text"] for _, msg in h.deliver.messages] == ["第一段。", "第一段。"]


async def test_paced_delivery_interrupt_cancels_remaining_segments() -> None:
    """打断：节奏任务已登记 cancellables，第④步连任务一起取消，剩余段静默终止。"""
    h = Harness()
    first_sink_entered = asyncio.Event()

    async def sink(segment_text: str) -> bool:
        if segment_text == "第一段。":
            first_sink_entered.set()
            await asyncio.sleep(0)
            return True
        await asyncio.sleep(10)  # 后续段挂起：等 interrupt 取消
        return True

    handle = await h.service.start(h.turn, _result("第一段。第二段。第三段。"), audio_sink=sink)
    assert await asyncio.wait_for(first_sink_entered.wait(), timeout=1.0)
    for _ in range(200):  # 等第一段下发完成
        if len(h.deliver.messages) >= 1:
            break
        await asyncio.sleep(0.005)
    assert len(h.deliver.messages) == 1

    interrupted = await h.service.interrupt(handle.response_id)
    assert interrupted is not None
    assert interrupted.delivery_status == "interrupted"
    await asyncio.sleep(0.05)  # 给取消传播留一拍
    assert len(h.deliver.messages) == 1  # 第二/三段不再下发


async def test_settlement_schedules_memory_extraction() -> None:
    """B1↔B2 边界（裁决 O-B2）：终态结算调度记忆提取；generic 轮不调度。

    本步只接调用点（端口由装配注入）；Step 7 总收口把真实 extract_after_turn
    换入。提取在后台任务执行，不阻塞 ack 返回。
    """
    memory = FakeMemoryPort()
    deliver = FakeDeliver()
    sink = FakeEventSink()
    store = FakeHistoryStore()
    service = ResponseService(
        deliver=deliver,
        events=sink,
        tts_queue=FakeTtsQueue(),
        history_store=store,
        memory_port=memory,
    )
    turn = TurnContext(
        thread_id=_THREAD,
        turn_id="turn-1",
        device_id="dev-1",
        elder_id="elder-a",
        user_text="我喜欢越剧",
    )

    handle = await service.start(turn, _result("好的。"))
    assert await service.ack(
        device_id="dev-1",
        response_id=handle.response_id,
        epoch=service.session_epoch(_THREAD),
        segment_id=0,
        status="played",
    )
    # 等后台提取任务跑完
    tasks = list(service._memory_tasks)
    assert len(tasks) == 1
    await asyncio.gather(*tasks)

    assert memory.calls == [
        {
            "elder_id": "elder-a",
            "thread_id": _THREAD,
            "turn_id": "turn-1",
            "user_text": "我喜欢越剧",
            "assistant_text": "好的。",
        }
    ]


async def test_generic_turn_skips_memory_extraction() -> None:
    """generic 轮（elder_id=None）不调度记忆提取（闸门②）。"""
    memory = FakeMemoryPort()
    deliver = FakeDeliver()
    sink = FakeEventSink()
    store = FakeHistoryStore()
    service = ResponseService(
        deliver=deliver,
        events=sink,
        tts_queue=FakeTtsQueue(),
        history_store=store,
        memory_port=memory,
    )
    turn = TurnContext(thread_id=_THREAD, turn_id="turn-1", device_id="dev-1", elder_id=None)

    handle = await service.start(turn, _result("好的。"))
    await service.ack(
        device_id="dev-1",
        response_id=handle.response_id,
        epoch=service.session_epoch(_THREAD),
        segment_id=0,
        status="played",
    )
    assert service._memory_tasks == set()
    assert memory.calls == []
