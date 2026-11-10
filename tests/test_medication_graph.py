"""用药确认子图与用药事实服务（A08–A11 / 计划 V8）。

覆盖：
1. 正常路径：T0 触达 → `interrupt` 停住 → 回「吃了」→ 终态 `taken_reported`；
2. **重启恢复**：用同一 checkpointer 新建图实例，`get_state` 能读回等待位置，
   继续跑不重发已完成触达（`medication_attempts` 仍只有 1 条）；
3. 无回应：重试到第 4 次触达后进 `missed_unconfirmed`，`retry_count=3`、
   `needs_escalation=True`（A12 的交接位）；
4. 事件幂等：同 `(plan_id, scheduled_at)` 同内容返回同一 `reminder_id`，异内容抛冲突；
5. 终态不覆盖：已定终态再写另一个 → `TerminalStateConflict`；
6. **A12 建单**：`missed_unconfirmed` 与 `notification_failed`（AC-20，根本没送到）
   都落一张 `missed_medication` / yellow 工单（2026-11-10 主理人裁决），
   且 `needs_escalation` 与「有没有建成单」互不替代。

checkpointer 用**独立 schema + 独立池**（与生产同一条代码路径）；
业务行用 savepoint 夹具，用例结束回滚。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import make_url, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.graphs.checkpointer import medication_checkpointer
from app.graphs.medication_confirmation_graph import (
    MAX_ATTEMPTS,
    build_medication_graph,
    graph_config,
    resume_medication_task,
    start_medication_task,
)
from app.models.medication import MedicationAttempt, MedicationEvent
from app.models.organization import Device, Elder, Room
from app.models.staff import StaffAccount
from app.services import medication_service
from app.services.medication_service import (
    MedicationConflict,
    TerminalStateConflict,
)


def _test_psycopg_dsn() -> str:
    """conftest 的测试库 URL（psycopg 同步风格），checkpointer 用它建独立池。"""
    settings = get_settings()
    raw = settings.database_url_test
    if not raw:
        url = make_url(settings.database_url)
        url = url.set(database=f"{url.database}_test")
        raw = url.render_as_string(hide_password=False)
    return str(raw).replace("postgresql+asyncpg://", "postgresql://")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def saver(_test_db_bootstrap: bool) -> AsyncIterator[AsyncPostgresSaver]:
    if not _test_db_bootstrap:
        pytest.skip("PostgreSQL 不可达，跳过用药子图用例")
    async with medication_checkpointer(dsn=_test_psycopg_dsn()) as checkpointer:
        yield checkpointer


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    try:
        conn = await db_engine.connect()
    except Exception as exc:
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
    """一位长者 + 一位医生 + 一条用药计划（建事件需要这两个 FK）。"""
    elder = Elder(name="张桂芬", status="active")
    doctor = StaffAccount(name="王医生", role="doctor", password_hash="x" * 32)
    session.add_all([elder, doctor])
    await session.flush()

    from sqlalchemy import text

    plan_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO medication_plans "
            "(plan_id, elder_id, drug_name, dose, schedule_cron, start_date, created_by) "
            "VALUES (:plan_id, :elder_id, :drug, :dose, :cron, :start, :created_by)"
        ),
        {
            "plan_id": plan_id,
            "elder_id": elder.elder_id,
            "drug": "氨氯地平",
            "dose": "5mg 一片",
            "cron": "0 8 * * *",
            "start": datetime.date(2026, 11, 1),
            "created_by": doctor.staff_id,
        },
    )
    return {"elder_id": elder.elder_id, "plan_id": plan_id}


async def _new_event(session: AsyncSession, seeded: dict[str, Any]) -> uuid.UUID:
    return await medication_service.create_event(
        session,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        scheduled_at=datetime.datetime(2026, 11, 9, 8, 0, tzinfo=datetime.UTC),
        drug_snapshot={"drug_name": "氨氯地平", "dose": "5mg 一片"},
        triggered_by="scheduler",
    )


# ------------------------------------------------------------------ 1. 正常路径


async def test_taken_path_sets_terminal(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    reminder_id = await _new_event(session, seeded)
    graph = build_medication_graph(session=session, checkpointer=saver)

    first = await start_medication_task(
        graph,
        reminder_id=reminder_id,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline="2026-11-09T08:15:00+00:00",
    )
    assert "__interrupt__" in first, "首轮必须停在 wait_confirm"

    event = await medication_service.get_event(session, reminder_id)
    assert event is not None and event["status"] == "waiting_confirm"

    await resume_medication_task(graph, reminder_id=reminder_id, answer="吃了")

    event = await medication_service.get_event(session, reminder_id)
    assert event is not None
    assert event["terminal_state"] == "taken_reported"
    attempts = await medication_service.list_attempts(session, reminder_id)
    assert [a["attempt_no"] for a in attempts] == [1]
    assert attempts[0]["response"] == "吃了"


# ------------------------------------------------------------------ 2. 重启恢复


async def test_restart_recovers_without_resending(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    await _bind_device(session, seeded)  # 不绑设备就没有投递，断言投递次数会失真
    reminder_id = await _new_event(session, seeded)
    calls: list[tuple[str, dict[str, Any]]] = []
    graph = build_medication_graph(
        session=session, checkpointer=saver, deliver_fn=_fake_deliver(calls, ok=True)
    )
    await start_medication_task(
        graph,
        reminder_id=reminder_id,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline="2026-11-09T08:15:00+00:00",
    )
    # L6#9 后每台设备投两条（tts_text + task_command 卡片），计数只看语音触达
    tts_calls = [(d, m) for d, m in calls if m.get("type") == "tts_text"]
    assert len(tts_calls) == 1, "首触达投递一次"

    # 模拟进程重启：新建图实例，状态只在 checkpointer 里
    revived = build_medication_graph(
        session=session, checkpointer=saver, deliver_fn=_fake_deliver(calls, ok=True)
    )
    snapshot = await revived.aget_state(graph_config(reminder_id))
    assert snapshot.values["reminder_id"] == str(reminder_id)
    assert snapshot.values["attempt_no"] == 1

    await resume_medication_task(revived, reminder_id=reminder_id, answer="吃过了")

    attempts = await medication_service.list_attempts(session, reminder_id)
    assert len(attempts) == 1, "恢复后不得把已发过的触达再发一次"
    tts_after_restart = [(d, m) for d, m in calls if m.get("type") == "tts_text"]
    assert len(tts_after_restart) == 1, (
        "恢复后不得**再投递**一次——长者会听到同一句提醒两遍。"
        "（record_attempt 的幂等只挡记录，挡不住投递，所以要单独断言投递次数。）"
    )
    event = await medication_service.get_event(session, reminder_id)
    assert event is not None and event["terminal_state"] == "taken_reported"


# ------------------------------------------------------------------ 3. 重试到终态


async def test_no_answer_retries_then_missed(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    """A 类：消息送达了（回执 voice_dispatched）但四次都没人回答 → missed_unconfirmed。

    与「设备全离线」那条构成 AC-20 的两侧：这里必须绑设备且下发成功——
    不绑设备会落 no_bound_device，AC-20 会把终态判成 notification_failed
    （「根本没送到」），与本测场景（「送到了没吃」）不符；终态按回执集合分流
    （`_split_missed_by_receipt`）。
    """
    await _bind_device(session, seeded)
    calls: list[tuple[str, dict[str, Any]]] = []
    reminder_id = await _new_event(session, seeded)
    graph = build_medication_graph(
        session=session, checkpointer=saver, deliver_fn=_fake_deliver(calls, ok=True)
    )
    await start_medication_task(
        graph,
        reminder_id=reminder_id,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline="2026-11-09T08:15:00+00:00",
    )

    final: dict[str, Any] = {}
    for _ in range(MAX_ATTEMPTS):
        final = await resume_medication_task(graph, reminder_id=reminder_id, answer="no_response")

    event = await medication_service.get_event(session, reminder_id)
    assert event is not None
    assert event["terminal_state"] == "missed_unconfirmed"
    assert event["retry_count"] == MAX_ATTEMPTS - 1, "四次触达 = 三次重试"

    attempts = await medication_service.list_attempts(session, reminder_id)
    assert [a["attempt_no"] for a in attempts] == [1, 2, 3, 4]

    snapshot = await graph.aget_state(graph_config(reminder_id))
    assert snapshot.values["needs_escalation"] is True, "missed 必须交给 A12 建单"
    assert final.get("terminal") in (None, "missed_unconfirmed")

    # A12：finalize 写完结态后建漏服单（表里没有 source 列，靠确定性单号幂等）
    from sqlalchemy import select

    from app.models.medication import WorkOrder

    orders = list(
        (
            await session.execute(select(WorkOrder).where(WorkOrder.elder_id == seeded["elder_id"]))
        ).scalars()
    )
    assert len(orders) == 1, "一次漏服只建一张单"
    assert orders[0].type == "missed_medication"
    assert orders[0].severity == "yellow", "漏服是黄档；判红会稀释急症"
    assert orders[0].status == "new"
    assert orders[0].assignee_id is None, "该长者未配责任护工 → 未分派是真实状态"
    assert orders[0].order_no.startswith("WO-")


# ------------------------------------------------------------------ 4. 事件幂等


async def test_create_event_is_idempotent(session: AsyncSession, seeded: dict[str, Any]) -> None:
    first = await _new_event(session, seeded)
    second = await _new_event(session, seeded)
    assert first == second, "同键同内容必须返回同一条事件"

    with pytest.raises(MedicationConflict):
        await medication_service.create_event(
            session,
            plan_id=seeded["plan_id"],
            elder_id=seeded["elder_id"],
            scheduled_at=datetime.datetime(2026, 11, 9, 8, 0, tzinfo=datetime.UTC),
            drug_snapshot={"drug_name": "氨氯地平", "dose": "10mg 一片"},
            triggered_by="scheduler",
        )


# ------------------------------------------------------------------ 5. 终态不覆盖


async def test_terminal_state_is_write_once(session: AsyncSession, seeded: dict[str, Any]) -> None:
    reminder_id = await _new_event(session, seeded)
    await medication_service.set_terminal(
        session, event_id=reminder_id, terminal_state="taken_reported"
    )
    # 同值重写 = 幂等
    await medication_service.set_terminal(
        session, event_id=reminder_id, terminal_state="taken_reported"
    )
    with pytest.raises(TerminalStateConflict):
        await medication_service.set_terminal(
            session, event_id=reminder_id, terminal_state="missed_unconfirmed"
        )


# ------------------------------------------------------------------ 7. E2 触达下发


def _fake_deliver(calls: list[tuple[str, dict[str, Any]]], ok: bool):
    def _deliver(device_id: str, message: dict[str, Any]) -> bool:
        calls.append((device_id, message))
        return ok

    return _deliver


async def _bind_device(session: AsyncSession, seeded: dict[str, Any]) -> Device:
    room = Room(room_no=f"R-{uuid.uuid4().hex[:8]}")
    session.add(room)
    await session.flush()
    device = Device(room_id=room.room_id, elder_id=seeded["elder_id"], token_hash="x" * 32)
    session.add(device)
    await session.flush()
    return device


async def test_reminder_dispatches_to_bound_device(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    """设备在线：触达 = voice_dispatched，字幕 + 语音（TD-1 内嵌 audio）发给同一台设备。"""
    device = await _bind_device(session, seeded)
    reminder_id = await _new_event(session, seeded)
    calls: list[tuple[str, dict[str, Any]]] = []
    tts_calls: list[str] = []

    graph = build_medication_graph(
        session=session,
        checkpointer=saver,
        deliver_fn=_fake_deliver(calls, ok=True),
        tts_fn=_fake_tts(tts_calls, b"\x01\x00" * 160),
    )

    await start_medication_task(
        graph,
        reminder_id=reminder_id,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline="2026-11-09T08:15:00+00:00",
    )

    # L6#9 后同发 task_command 卡片：目标设备不变，计数只看语音触达那条
    tts_msgs = [(d, m) for d, m in calls if m.get("type") == "tts_text"]
    assert [d for d, _ in tts_msgs] == [str(device.device_id)], "恰好发给绑定的那台设备"
    message = tts_msgs[0][1]
    assert message["type"] == "tts_text"
    assert "氨氯地平" in message["text"], "话术要带药名（来自 drug_snapshot）"
    assert "response_id" not in message and "epoch" not in message, (
        "主动触达不属于任何回复流，不带 response_id/epoch（契约 §4.1 可选字段）"
    )
    assert tts_calls == [message["text"]], "语音合成的就是下发的那句字幕文本"
    # 卡片命令与语音触达同 taskId、同设备
    card_msgs = [(d, m) for d, m in calls if m.get("type") == "task_command"]
    assert [d for d, _ in card_msgs] == [str(device.device_id)], "卡片同发到绑定设备"
    assert card_msgs[0][1]["task_id"] == str(reminder_id), "卡片 taskId=reminder_id（前端幂等键）"

    attempts = await medication_service.list_attempts(session, reminder_id)
    assert attempts[0]["receipt"] == "voice_dispatched"


async def test_reminder_without_device_records_no_bound_device(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    """没绑设备：触达如实记 no_bound_device（运维缺口），不抛错、不阻塞等待。"""
    reminder_id = await _new_event(session, seeded)
    calls: list[tuple[str, dict[str, Any]]] = []
    graph = build_medication_graph(
        session=session, checkpointer=saver, deliver_fn=_fake_deliver(calls, ok=True)
    )

    await start_medication_task(
        graph,
        reminder_id=reminder_id,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline="2026-11-09T08:15:00+00:00",
    )

    assert calls == [], "没绑设备就不该有任何下发"
    event = await medication_service.get_event(session, reminder_id)
    assert event is not None and event["status"] == "waiting_confirm", "触达失败不阻塞进入等待确认"


async def test_all_devices_offline_yields_notification_failed(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    """设备全离线：每次触达记 device_offline，四次后是 notification_failed（AC-20）而不是漏服。

    这条是 E2 接线最要紧的保证：下发失败**不是**放弃治疗的理由——
    四次触达的语义是「尝试了四次没联系上」，联系不上的记录本身就是证据。
    但归因必须分清（PRD FR-MED-02 / AC-20）：
    提醒 A 有回执、无人应答 → `missed_unconfirmed`；提醒 B 四次回执全 `device_offline`
    → `notification_failed`（通知没送到，不是老人漏服）。建单口径两种终态一致
    （2026-11-10 主理人裁决）——没送到更需要护工补送/代确认，不能没人管。
    """
    await _bind_device(session, seeded)
    reminder_id = await _new_event(session, seeded)
    calls: list[tuple[str, dict[str, Any]]] = []
    graph = build_medication_graph(
        session=session, checkpointer=saver, deliver_fn=_fake_deliver(calls, ok=False)
    )

    await start_medication_task(
        graph,
        reminder_id=reminder_id,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline="2026-11-09T08:15:00+00:00",
    )
    # start = 触达 1；resume 4 次 = 触达 2/3/4 + 第 4 次 retry 判定用尽 → finalize
    for _ in range(MAX_ATTEMPTS):
        await resume_medication_task(graph, reminder_id=reminder_id, answer="no_response")

    attempts = await medication_service.list_attempts(session, reminder_id)
    assert [a["receipt"] for a in attempts] == ["device_offline"] * MAX_ATTEMPTS
    # L6#9 后每台设备每次触达投两条（tts_text + task_command 卡片），计数只看语音触达
    tts_calls = [(d, m) for d, m in calls if m.get("type") == "tts_text"]
    assert len(tts_calls) == MAX_ATTEMPTS, "四次触达每次都真实调用了下发（卡片消息不计数）"

    event = await medication_service.get_event(session, reminder_id)
    assert event is not None and event["terminal_state"] == "notification_failed"

    from sqlalchemy import select

    reason = (
        await session.execute(
            select(MedicationEvent.failure_reason).where(
                MedicationEvent.reminder_id == reminder_id
            )
        )
    ).scalar_one()
    assert reason == "offline", "归因写 offline（failure_reason 白名单内，无新增值/无迁移）"

    from sqlalchemy import select

    from app.models.medication import WorkOrder

    orders = list(
        (
            await session.execute(select(WorkOrder).where(WorkOrder.elder_id == seeded["elder_id"]))
        ).scalars()
    )
    assert len(orders) == 1, "设备全离线的漏服照样升级工单（人工兜底）"


# ------------------------------------------------------------------ 8. TD-1/TD-2 触达语音与 played 闭环


def _fake_tts(calls: list[str], pcm: bytes | None):
    async def _tts(text: str) -> bytes | None:
        calls.append(text)
        return pcm

    return _tts


def _pcm_of(touch_message: dict[str, Any]) -> bytes:
    import base64

    audio = touch_message["audio"]
    assert audio["format"] == "pcm_s16le" and audio["sample_rate"] == 16000
    return base64.b64decode(audio["data_b64"])


async def test_touch_message_carries_audio_and_ids(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    """TD-1：合成成功 → 触达消息带 touch_id/坐标/audio，tts_result=ok（字幕字段仍在）。"""
    await _bind_device(session, seeded)
    reminder_id = await _new_event(session, seeded)
    calls: list[tuple[str, dict[str, Any]]] = []
    tts_calls: list[str] = []
    pcm = b"\x01\x00" * 16000
    graph = build_medication_graph(
        session=session,
        checkpointer=saver,
        deliver_fn=_fake_deliver(calls, ok=True),
        tts_fn=_fake_tts(tts_calls, pcm),
    )

    await start_medication_task(
        graph,
        reminder_id=reminder_id,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline="2026-11-09T08:15:00+00:00",
    )

    message = calls[0][1]
    assert tts_calls == [message["text"]], "合成的是下发的那句触达话术"
    assert message["touch_event_id"] == str(reminder_id), "坐标原样下发（touch_played 回传用）"
    assert message["touch_attempt_no"] == 1
    assert isinstance(message["touch_id"], str) and message["touch_id"]
    assert "response_id" not in message, "触达消息仍不得混入回复流字段"
    assert _pcm_of(message) == pcm

    rows = list(
        (
            await session.execute(
                select(MedicationAttempt).where(MedicationAttempt.event_id == reminder_id)
            )
        ).scalars()
    )
    assert rows[0].tts_result == "ok" and rows[0].receipt == "voice_dispatched"


async def test_touch_tts_failed_degrades_to_subtitle(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    """TD-1：合成失败 → 无 audio 字段（Day 6 纯字幕形状），触达照常投递并记 tts_failed。"""
    await _bind_device(session, seeded)
    reminder_id = await _new_event(session, seeded)
    calls: list[tuple[str, dict[str, Any]]] = []
    graph = build_medication_graph(
        session=session,
        checkpointer=saver,
        deliver_fn=_fake_deliver(calls, ok=True),
        tts_fn=_fake_tts([], None),
    )

    await start_medication_task(
        graph,
        reminder_id=reminder_id,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline="2026-11-09T08:15:00+00:00",
    )

    message = calls[0][1]
    assert "audio" not in message, "合成失败降级纯字幕，消息形状与 Day 6 完全一致"
    assert "touch_id" in message, "message_id 仍要带——字幕降级路径的形状稳定是回执纪律的前提"
    rows = list(
        (
            await session.execute(
                select(MedicationAttempt).where(MedicationAttempt.event_id == reminder_id)
            )
        ).scalars()
    )
    assert rows[0].tts_result == "tts_failed"
    assert rows[0].receipt == "voice_dispatched", "字幕照样送到了，投递回执不变"


async def test_mark_attempt_played_upgrades_dispatched_row(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    """TD-2：voice_dispatched → played 幂等升级；重复/迟到回执是无操作不是错误。"""
    reminder_id = await _new_event(session, seeded)
    await medication_service.record_attempt(
        session, event_id=reminder_id, attempt_no=1, receipt="voice_dispatched"
    )

    assert await medication_service.mark_attempt_played(
        session, event_id=reminder_id, attempt_no=1
    ), "首条回执应发生升级"
    assert (
        await medication_service.mark_attempt_played(session, event_id=reminder_id, attempt_no=1)
        is False
    ), "重复回执幂等无操作"

    attempts = await medication_service.list_attempts(session, reminder_id)
    assert len(attempts) == 1 and attempts[0]["receipt"] == "played"


async def test_mark_attempt_played_ignores_offline_and_missing(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    """TD-2 宁缺勿假：device_offline 的触达音频没出过门，不能被回执「洗白」。"""
    reminder_id = await _new_event(session, seeded)
    await medication_service.record_attempt(
        session, event_id=reminder_id, attempt_no=1, receipt="device_offline"
    )

    assert not await medication_service.mark_attempt_played(
        session, event_id=reminder_id, attempt_no=1
    ), "离线触达不允许被标记 played"
    assert not await medication_service.mark_attempt_played(
        session, event_id=reminder_id, attempt_no=9
    ), "不存在的触达尝试如实返回 False，不抛错"

    attempts = await medication_service.list_attempts(session, reminder_id)
    assert attempts[0]["receipt"] == "device_offline"


# ------------------------------------------------------------------ 9. develop 侧合入：record_attempt 语义与重放防护


async def test_record_attempt_does_not_swallow_check_violation(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """CHECK 违反必须炸响，**不能**被当成幂等静默吞掉。

    修复前的 `record_attempt` 用 `except IntegrityError` 一把兜住所有完整性错误，
    于是「回执值不在白名单」这种数据违规会**静默消失**：触达记录没写进去，
    日志还挂着误导性的 `medication_attempt_duplicate`。排查时只能先去对时间戳，
    才发现那条记录根本没插进去。

    这条用例锁住修正后的语义：非法值要么写成功，要么明确抛错，没有第三种结局。
    """
    reminder_id = await _new_event(session, seeded)

    with pytest.raises(IntegrityError):
        await medication_service.record_attempt(
            session, event_id=reminder_id, attempt_no=1, receipt="bogus_receipt"
        )

    attempts = await medication_service.list_attempts(session, reminder_id)
    assert attempts == [], "非法回执不得留下任何记录（更不得被当成幂等重放）"


async def test_record_attempt_is_idempotent_on_same_key(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """同 (event_id, attempt_no) 重复写仍是幂等无操作——这是幂等该有的样子。"""
    reminder_id = await _new_event(session, seeded)

    await medication_service.record_attempt(
        session, event_id=reminder_id, attempt_no=1, receipt="voice_dispatched"
    )
    await medication_service.record_attempt(
        session, event_id=reminder_id, attempt_no=1, receipt="voice_dispatched"
    )

    attempts = await medication_service.list_attempts(session, reminder_id)
    assert len(attempts) == 1, "重复写不得多出一条触达记录"
    assert attempts[0]["receipt"] == "voice_dispatched"


async def test_reminder_skips_delivery_when_attempt_already_recorded(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    """已有同 (event_id, attempt_no) 记录 → **跳过投递**，只推进状态。

    真实场景：`remind` 已执行、但 checkpoint 还没持久化时进程崩溃，重启后该节点被重放。
    `record_attempt` 的幂等只挡**记录**、挡不住**投递**——不先问就会给长者再播一遍
    同一次提醒。这条用例锁住「先查后发」：已有记录 → 一次都不投递。
    """
    await _bind_device(session, seeded)  # 必须绑设备：否则走 no_bound_device，投递函数不会被调用
    reminder_id = await _new_event(session, seeded)
    # 预置该次触达记录（模拟「remind 跑过了、图状态还没存下来」）
    await medication_service.record_attempt(
        session, event_id=reminder_id, attempt_no=1, receipt="voice_dispatched"
    )

    calls: list[tuple[str, dict[str, Any]]] = []
    graph = build_medication_graph(
        session=session, checkpointer=saver, deliver_fn=_fake_deliver(calls, ok=True)
    )
    await start_medication_task(
        graph,
        reminder_id=reminder_id,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline="2026-11-09T08:15:00+00:00",
    )

    assert calls == [], "已有触达记录 → 不得再投递（长者会听到两遍）"
    attempts = await medication_service.list_attempts(session, reminder_id)
    assert len(attempts) == 1, "记录仍只有一条（幂等）"
    event = await medication_service.get_event(session, reminder_id)
    assert event is not None and event["status"] == "waiting_confirm", "状态仍要推进到等待确认"
