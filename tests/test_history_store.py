"""HistoryStore 契约测试（N04 / 计划 V3）。

覆盖六条判据：
1. 两轮按 `seq` 顺序读回，且上下文读的是**时间正序**；
2. partial / 空文本不入库（`begin_turn` 只接受 ASR final）；
3. 同 `turn_id` 幂等（重连重发不长出第二轮）；
4. `commit_response` 幂等，且投影只留**已送达**文本；
5. generic 模式（身份未确认）不建投影、`read_turns` 返回空、但事件仍闭合；
6. 并发 20 次 `append_event` **无重号**（行锁分配，不是 max+1）。

DB 隔离沿用仓库既有写法：`db_engine`（conftest 的 module 级共享引擎）+ savepoint，
service 内部即使 commit 也只释放 savepoint，用例结束整体回滚。
并发用例是唯一的例外——它必须让多个连接真正提交才能验证行锁，因此自带建行与清理。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models.conversation import ConversationEvent, ConversationThread, ConversationTurn
from app.models.organization import Device, Elder, Room
from app.services.history_service import (
    HistoryError,
    InvalidEventType,
    ThreadMismatch,
    ThreadNotFound,
    append_event,
    begin_turn,
    commit_response,
    finish_turn,
    read_events,
    read_turns,
)

_DEVICE_TOKEN = "test-device-token-hash"


# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """每用例一事务，结束回滚（同 `test_audio_identity_ws.db_session`）。"""
    try:
        conn = await db_engine.connect()
    except Exception as exc:  # 任何连接失败都算「无库」
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")

    trans = await conn.begin()
    sf = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    async with sf() as sess:
        try:
            yield sess
        finally:
            await sess.rollback()
    await trans.rollback()


@pytest.fixture
async def seeded(session: AsyncSession) -> dict[str, Any]:
    """两位长者 + 三个会话（两位各自识别态 + 一个 generic）。"""
    room = Room(room_no=f"H-{uuid.uuid4().hex[:6]}")
    session.add(room)
    await session.flush()

    device = Device(room_id=room.room_id, token_hash=_DEVICE_TOKEN, status="offline")
    session.add(device)
    await session.flush()

    elder_a = Elder(name="张桂芬", status="active")
    elder_b = Elder(name="李茂林", status="active")
    session.add_all([elder_a, elder_b])
    await session.flush()

    thread_a = ConversationThread(
        thread_id=f"t-a-{uuid.uuid4().hex[:6]}",
        elder_id=elder_a.elder_id,
        device_id=device.device_id,
        identity_source="voice",
    )
    thread_b = ConversationThread(
        thread_id=f"t-b-{uuid.uuid4().hex[:6]}",
        elder_id=elder_b.elder_id,
        device_id=device.device_id,
        identity_source="voice",
    )
    thread_generic = ConversationThread(
        thread_id=f"t-g-{uuid.uuid4().hex[:6]}",
        elder_id=None,
        device_id=device.device_id,
        identity_source="generic",
    )
    session.add_all([thread_a, thread_b, thread_generic])
    await session.flush()

    return {
        "device_id": device.device_id,
        "elder_a": elder_a.elder_id,
        "elder_b": elder_b.elder_id,
        "thread_a": thread_a.thread_id,
        "thread_b": thread_b.thread_id,
        "thread_generic": thread_generic.thread_id,
    }


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for clause in where:
        stmt = stmt.where(clause)
    return int((await session.execute(stmt)).scalar_one())


# ------------------------------------------------------------------ 1. 两轮顺序


async def test_two_turns_read_back_in_time_order(session: AsyncSession, seeded: dict) -> None:
    first = await begin_turn(
        session,
        thread_id=seeded["thread_a"],
        elder_id=seeded["elder_a"],
        device_id=seeded["device_id"],
        call_id="call-1",
        turn_id="turn-1",
        user_text="今天有点冷",
    )
    second = await begin_turn(
        session,
        thread_id=seeded["thread_a"],
        elder_id=seeded["elder_a"],
        device_id=seeded["device_id"],
        call_id="call-1",
        turn_id="turn-2",
        user_text="把空调调高一点",
    )

    assert (first.seq, second.seq) == (1, 2), "seq 必须从 1 起逐条递增"
    assert first.duplicate is False

    await commit_response(
        session,
        turn_id="turn-1",
        response_id="resp-1",
        generated_text="好的，我把窗关上。您也记得加件衣服。",
        delivered_text="好的，我把窗关上。",
        status="completed",
    )
    await finish_turn(session, turn_id="turn-1", status="completed")

    views = await read_turns(session, thread_id=seeded["thread_a"], elder_id=seeded["elder_a"])
    assert [v.turn_id for v in views] == ["turn-1", "turn-2"], "上下文要按时间正序，最早的在前"
    assert views[0].assistant_delivered_text == "好的，我把窗关上。"
    assert views[0].status == "completed"
    assert views[0].ended_at is not None
    assert views[1].status == "open", "未收口的轮次仍应是 open"

    events = await read_events(session, thread_id=seeded["thread_a"])
    assert [e.seq for e in events] == [1, 2, 3, 4], (
        "turn_started ×2 + response_completed + turn_completed"
    )
    assert [e.event_type for e in events] == [
        "turn_started",
        "turn_started",
        "response_completed",
        "turn_completed",
    ]


async def test_read_events_incremental_after_seq(session: AsyncSession, seeded: dict) -> None:
    for i in range(3):
        await append_event(
            session,
            thread_id=seeded["thread_a"],
            event_type="task_waiting",
            payload={"i": i},
        )

    tail = await read_events(session, thread_id=seeded["thread_a"], after_seq=1)
    assert [e.seq for e in tail] == [2, 3], "增量读只返回 seq > after_seq 的部分"
    assert tail[0].payload == {"i": 1}


# ------------------------------------------------------------------ 2. partial 不入库


async def test_begin_turn_rejects_non_final_text(session: AsyncSession, seeded: dict) -> None:
    for bad in ("", "   ", "\n\t"):
        with pytest.raises(HistoryError):
            await begin_turn(
                session,
                thread_id=seeded["thread_a"],
                elder_id=seeded["elder_a"],
                turn_id=f"turn-{uuid.uuid4().hex[:6]}",
                user_text=bad,
            )

    assert await _count(session, ConversationTurn) == 0, "被拒绝的调用不许留下轮次行"
    assert await _count(session, ConversationEvent) == 0, "也不许留下事件"


# ------------------------------------------------------------------ 3. turn_id 幂等


async def test_duplicate_turn_id_is_idempotent(session: AsyncSession, seeded: dict) -> None:
    kwargs = dict(
        thread_id=seeded["thread_a"],
        elder_id=seeded["elder_a"],
        turn_id="turn-dup",
        user_text="我吃过药了",
    )
    first = await begin_turn(session, **kwargs)
    second = await begin_turn(session, **kwargs)

    assert first.duplicate is False and first.seq == 1
    assert second.duplicate is True and second.seq is None, "重复调用没有新事件，就不该给新序号"

    assert await _count(session, ConversationTurn) == 1
    assert await _count(session, ConversationEvent) == 1


# ------------------------------------------------------------------ 4. 已送达语义 + 幂等


async def test_commit_response_keeps_delivered_only_and_is_idempotent(
    session: AsyncSession, seeded: dict
) -> None:
    await begin_turn(
        session,
        thread_id=seeded["thread_a"],
        elder_id=seeded["elder_a"],
        turn_id="turn-x",
        user_text="给我讲讲今天天气",
    )
    await commit_response(
        session,
        turn_id="turn-x",
        response_id="resp-x",
        generated_text="今天晴，二十度。风不大，适合下楼走走。",
        delivered_text="今天晴，二十度。",
        status="interrupted",
    )
    # 同 response_id 再提交一次（重连重发）：必须完全无效
    await commit_response(
        session,
        turn_id="turn-x",
        response_id="resp-x",
        generated_text="今天晴，二十度。风不大，适合下楼走走。",
        delivered_text="今天晴。",
        status="completed",
    )

    turn = await session.get(ConversationTurn, "turn-x")
    assert turn is not None
    assert turn.assistant_delivered_text == "今天晴，二十度。", "重复提交不得把已播文本改短"

    events = await read_events(session, thread_id=seeded["thread_a"])
    completed = [e for e in events if e.event_type == "response_completed"]
    assert len(completed) == 1, "同 (turn_id, response_id) 只允许一条完成事件"
    assert completed[0].payload["generated_text"] == "今天晴，二十度。风不大，适合下楼走走。"
    assert completed[0].payload["response_status"] == "interrupted"


async def test_completed_without_delivered_emits_warning(
    session: AsyncSession, seeded: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """`completed` 却没有任何已送达文本 → 必须留痕（典型原因：末段 `<audio>.ended` 未上报）。

    历史层不替调用方猜内容（猜错会把没播的话写进上下文），但也不能让这条回复
    在复盘时"明明播完了却一个字都没有"。
    """
    await begin_turn(
        session,
        thread_id=seeded["thread_a"],
        elder_id=seeded["elder_a"],
        turn_id="turn-empty",
        user_text="提醒我吃药",
    )

    with caplog.at_level(logging.WARNING, logger="app.services.history_service"):
        await commit_response(
            session,
            turn_id="turn-empty",
            response_id="resp-empty",
            generated_text="该吃降压药了。",
            delivered_text=None,
            status="completed",
        )

    assert "history_completed_without_delivered" in caplog.text
    turn = await session.get(ConversationTurn, "turn-empty")
    assert turn is not None and turn.assistant_delivered_text is None, "不得凭空补内容"

# ------------------------------------------------------------------ 5. 终态不被覆盖


async def test_finish_turn_terminal_status_is_not_overwritten(
    session: AsyncSession, seeded: dict
) -> None:
    await begin_turn(
        session,
        thread_id=seeded["thread_a"],
        elder_id=seeded["elder_a"],
        turn_id="turn-term",
        user_text="先这样吧",
    )
    await finish_turn(session, turn_id="turn-term", status="interrupted")
    await finish_turn(session, turn_id="turn-term", status="completed")

    turn = await session.get(ConversationTurn, "turn-term")
    assert turn is not None and turn.status == "interrupted", "迟到的一次收口不能改掉已有终态"
    completed = [
        e
        for e in await read_events(session, thread_id=seeded["thread_a"])
        if e.event_type == "turn_completed"
    ]
    assert len(completed) == 1


async def test_finish_turn_rejects_open(session: AsyncSession, seeded: dict) -> None:
    await begin_turn(
        session,
        thread_id=seeded["thread_a"],
        elder_id=seeded["elder_a"],
        turn_id="turn-open",
        user_text="嗯",
    )
    with pytest.raises(HistoryError):
        await finish_turn(session, turn_id="turn-open", status="open")  # type: ignore[arg-type]


# ------------------------------------------------------------------ 6. generic 模式


async def test_generic_thread_writes_events_but_no_projection(
    session: AsyncSession, seeded: dict
) -> None:
    ref = await begin_turn(
        session,
        thread_id=seeded["thread_generic"],
        elder_id=None,
        device_id=seeded["device_id"],
        turn_id="turn-g",
        user_text="你是谁呀",
    )
    assert ref.seq == 1

    assert await _count(session, ConversationTurn) == 0, "generic 不建投影"
    assert await read_turns(session, thread_id=seeded["thread_generic"], elder_id=None) == []

    await finish_turn(session, turn_id="turn-g", status="completed")
    events = await read_events(session, thread_id=seeded["thread_generic"])
    assert [e.event_type for e in events] == ["turn_started", "turn_completed"], (
        "generic 的轮次也必须闭合，否则重连补帧时永远看不到结束"
    )
    assert events[0].payload["user_text"] == "你是谁呀"
    assert events[0].payload["identity_source"] == "generic"


# ------------------------------------------------------------------ 7. 跨会话 / 跨长者不可见


async def test_cross_thread_and_cross_elder_are_invisible(
    session: AsyncSession, seeded: dict
) -> None:
    await begin_turn(
        session,
        thread_id=seeded["thread_a"],
        elder_id=seeded["elder_a"],
        turn_id="turn-a1",
        user_text="我想听越剧",
    )

    assert (
        await read_turns(session, thread_id=seeded["thread_b"], elder_id=seeded["elder_a"]) == []
    ), "拿 B 的会话读 A 的历史，必须读不到"
    assert (
        await read_turns(session, thread_id=seeded["thread_a"], elder_id=seeded["elder_b"]) == []
    ), "会话归属与 elder_id 不符时返回空，而不是把 A 的对话给 B"
    assert (
        len(await read_turns(session, thread_id=seeded["thread_a"], elder_id=seeded["elder_a"]))
        == 1
    )


async def test_begin_turn_rejects_elder_mismatch(session: AsyncSession, seeded: dict) -> None:
    with pytest.raises(ThreadMismatch):
        await begin_turn(
            session,
            thread_id=seeded["thread_a"],
            elder_id=seeded["elder_b"],
            turn_id="turn-wrong",
            user_text="我是谁",
        )


async def test_unknown_thread_and_event_type(session: AsyncSession, seeded: dict) -> None:
    with pytest.raises(ThreadNotFound):
        await append_event(session, thread_id="thread-not-exist", event_type="turn_started")
    with pytest.raises(InvalidEventType):
        await append_event(
            session,
            thread_id=seeded["thread_a"],
            event_type="not_an_event",  # type: ignore[arg-type]
        )


# ------------------------------------------------------------------ 8. 并发无重号


async def test_concurrent_appends_allocate_unique_seq(db_engine: AsyncEngine) -> None:
    """20 个连接同时追加事件，`seq` 必须恰好是 1..20，一个不重、一个不缺。

    这条用例**故意绕开 savepoint 夹具**：行锁要靠真实提交才能释放，多个连接必须看到同一行。
    因此自带建行 + 收尾清理（删 thread 会级联删事件）。
    """
    thread_id = f"t-conc-{uuid.uuid4().hex[:8]}"
    room_no = f"Z-{uuid.uuid4().hex[:6]}"

    async with AsyncSession(bind=db_engine, expire_on_commit=False) as setup, setup.begin():
        room = Room(room_no=room_no)
        setup.add(room)
        await setup.flush()
        device = Device(room_id=room.room_id, token_hash=f"hash-{thread_id}", status="offline")
        setup.add(device)
        await setup.flush()
        setup.add(
            ConversationThread(
                thread_id=thread_id,
                elder_id=None,
                device_id=device.device_id,
                identity_source="generic",
            )
        )

    semaphore = asyncio.Semaphore(5)  # 引擎池默认 5+10，控制并发避免排队超时

    async def _one(index: int) -> int:
        async with (
            semaphore,
            AsyncSession(bind=db_engine, expire_on_commit=False) as sess,
            sess.begin(),
        ):
            return await append_event(
                sess,
                thread_id=thread_id,
                event_type="asr_final_committed",
                payload={"i": index},
            )

    try:
        seqs = await asyncio.gather(*[_one(i) for i in range(20)])

        assert sorted(seqs) == list(range(1, 21)), f"seq 出现重号或断号：{sorted(seqs)}"
        async with AsyncSession(bind=db_engine) as check:
            distinct = (
                await check.execute(
                    select(func.count(func.distinct(ConversationEvent.seq))).where(
                        ConversationEvent.thread_id == thread_id
                    )
                )
            ).scalar_one()
            total = (
                await check.execute(
                    select(func.count()).where(ConversationEvent.thread_id == thread_id)
                )
            ).scalar_one()
        assert (total, distinct) == (20, 20)
    finally:
        async with (
            AsyncSession(bind=db_engine, expire_on_commit=False) as cleanup,
            cleanup.begin(),
        ):
            await cleanup.execute(
                delete(ConversationThread).where(ConversationThread.thread_id == thread_id)
            )
            await cleanup.execute(delete(Device).where(Device.token_hash == f"hash-{thread_id}"))
            await cleanup.execute(delete(Room).where(Room.room_no == room_no))
