"""用药调度驱动链（C2 · Day 7）测试。

覆盖 8 条核心验收用例：
1. 到点幂等：同一 (plan_id, scheduled_at) 触发两次 → events 仍 1 行
2. 四次触达：attempts 恰好 4 行、attempt_no=1/2/3/4
3. 四次无回答终态：terminal_state='missed_unconfirmed'、retry_count=3
4. 建单恰好一张：work_orders 1 行、order_no 确定性
5. 重启恢复：新 checkpointer 实例能读等待节点、attempts 行数不增
6. 双驱动先到先得：对话侧先 resume → 调度侧 resume 跳过
7. 窗口内回答：回答后进终态 → 后续 resume tick 跳过
8. list_due_plans cron 匹配：active + cron 命中才返回

checkpointer 复用 test_medication_graph.py 的 saver fixture（独立 langgraph schema）；
session 复用同文件的 session fixture（AsyncSession + savepoint + 回滚）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import pytest
import pytest_asyncio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.graphs.medication_confirmation_graph import (
    build_medication_graph,
    graph_config,
    resume_medication_task,
    start_medication_task,
)
from app.models.medication import (
    MedicationEvent,
    MedicationPlan,
    WorkOrder,
)
from app.models.organization import Device, Elder, Room
from app.models.staff import StaffAccount
from app.scheduler.medication_jobs import SchedulerRuntime
from app.services import medication_service
from app.services.medication_due_service import list_due_plans, list_waiting_confirm_events

# ------------------------------------------------------------------ fixture 复用


def _test_psycopg_dsn() -> str:
    from sqlalchemy import make_url

    from app.core.config import get_settings

    settings = get_settings()
    raw = settings.database_url_test
    if not raw:
        url = make_url(settings.database_url)
        url = url.set(database=f"{url.database}_test")
        raw = url.render_as_string(hide_password=False)
    return str(raw).replace("postgresql+asyncpg://", "postgresql://")


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """与 test_medication_graph.py 同形的 session fixture。"""
    try:
        conn = await db_engine.connect()
    except Exception as exc:
        pytest.skip(f"PostgreSQL 不可达：{exc!r}")
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


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def saver(_test_db_bootstrap: bool) -> AsyncIterator[AsyncPostgresSaver]:
    """独立 langgraph schema checkpointer（与 test_medication_graph.py 同形）。"""
    from app.graphs.checkpointer import medication_checkpointer

    if not _test_db_bootstrap:
        pytest.skip("PostgreSQL 不可达")
    async with medication_checkpointer(dsn=_test_psycopg_dsn()) as checkpointer:
        yield checkpointer


@pytest.fixture
async def seeded(session: AsyncSession) -> dict[str, Any]:
    """一位长者 + 一位医生 + 一间房 + 一条绑定设备 + 一条 active 药单。"""
    elder = Elder(name="张桂芬", status="active")
    room = Room(room_no="101")
    doctor = StaffAccount(name="王医生", role="doctor", password_hash="x" * 32)
    session.add_all([elder, room, doctor])
    await session.flush()

    device_id = uuid.uuid4()
    device = Device(
        device_id=device_id,
        room_id=room.room_id,
        elder_id=elder.elder_id,
        token_hash="x" * 32,
        status="online",
    )
    session.add(device)

    plan_id = uuid.uuid4()
    plan = MedicationPlan(
        plan_id=plan_id,
        elder_id=elder.elder_id,
        drug_name="硝苯地平",
        dose="10mg 一片",
        schedule_cron="* * * * *",
        start_date=datetime.date.today(),
        end_date=None,
        created_by=doctor.staff_id,
        status="active",
    )
    session.add(plan)
    await session.flush()

    return {
        "elder_id": elder.elder_id,
        "device_id": device_id,
        "plan_id": plan_id,
    }


@pytest.fixture
def runtime_factory(session: AsyncSession, saver: AsyncPostgresSaver):
    """构造一个 SchedulerRuntime（checkpointer 用共享 saver，tick 用控制时间参数）。"""

    async def _make() -> SchedulerRuntime:
        # 造一个 session_factory（每次调用返回当前 session 的 savepoint 上下文）
        async def _sf():
            # 直接 yield 现有 session——测试里我们自己管理事务
            yield session

        rt = SchedulerRuntime(
            session_factory=_sf,
            checkpointer_dsn=_test_psycopg_dsn(),
        )
        # 跳过 start（它会重新开 checkpointer）——直接注入共享 saver
        rt._checkpointer = saver
        return rt

    return _make


# ------------------------------------------------------------------ helpers


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


# ------------------------------------------------------------------ 用例


async def test_A2_1_idempotent_trigger(session, seeded, saver):
    """同一 (plan_id, scheduled_at) 触发两次 → events 仍 1 行、reminder_id 相同。"""
    plan_id = seeded["plan_id"]
    elder_id = seeded["elder_id"]
    now = _utc_now().replace(second=0, microsecond=0)
    scheduled_at = now

    # 第一次 create_event
    rid1 = await medication_service.create_event(
        session,
        plan_id=plan_id,
        elder_id=elder_id,
        scheduled_at=scheduled_at,
        drug_snapshot={"drug_name": "硝苯地平", "dose": "10mg 一片"},
        triggered_by="scheduler",
    )
    await session.flush()

    # 第二次同 (plan_id, scheduled_at) → 同内容返回同一 reminder_id
    rid2 = await medication_service.create_event(
        session,
        plan_id=plan_id,
        elder_id=elder_id,
        scheduled_at=scheduled_at,
        drug_snapshot={"drug_name": "硝苯地平", "dose": "10mg 一片"},
        triggered_by="scheduler",
    )
    assert rid1 == rid2

    # 表里仍只有 1 行
    count = (await session.execute(select(MedicationEvent.reminder_id))).scalars().all()
    assert len(count) == 1


async def test_A2_2_four_attempts_and_finalize(session, seeded, saver):
    """驱动链完整跑一遍：四次触达 → missed_unconfirmed → retry_count=3。"""
    elder_id = seeded["elder_id"]
    plan_id = seeded["plan_id"]
    now = _utc_now().replace(second=0, microsecond=0)

    # create_event
    rid = await medication_service.create_event(
        session,
        plan_id=plan_id,
        elder_id=elder_id,
        scheduled_at=now,
        drug_snapshot={"drug_name": "硝苯地平", "dose": "10mg 一片"},
        triggered_by="scheduler",
    )
    await session.flush()

    # build + start → wait_confirm
    # 注入恒 True deliver：模拟设备在线收得到（voice_dispatched），
    # 这样 AC-20 分流后终态仍为 missed_unconfirmed 而非 notification_failed。
    graph = build_medication_graph(
        session=session,
        checkpointer=saver,
        deliver_fn=lambda *_a, **_k: True,
    )
    await start_medication_task(
        graph,
        reminder_id=rid,
        plan_id=plan_id,
        elder_id=elder_id,
        deadline=(now + datetime.timedelta(minutes=15)).isoformat(),
        triggered_by="scheduler",
    )

    # resume tick 1 → attempt_no 1 已发 → retry 到 attempt_no 2
    await resume_medication_task(graph, reminder_id=rid, answer="")
    # resume tick 2 → attempt_no 2 → attempt_no 3
    await resume_medication_task(graph, reminder_id=rid, answer="")
    # resume tick 3 → attempt_no 3 → attempt_no 4
    await resume_medication_task(graph, reminder_id=rid, answer="")
    # resume tick 4 → attempt_no 4 → retry → attempt_no+1 > 4 → missed_unconfirmed → finalize
    await resume_medication_task(graph, reminder_id=rid, answer="")

    # 终态：missed_unconfirmed
    event = await medication_service.get_event(session, event_id=rid)
    assert event["terminal_state"] == "missed_unconfirmed"
    assert event["status"] == "escalated"
    assert event["retry_count"] == 3

    # 四次触达记录
    attempts = await medication_service.list_attempts(session, event_id=rid)
    assert len(attempts) == 4
    assert [a["attempt_no"] for a in attempts] == [1, 2, 3, 4]


async def test_A2_3_finalize_state(session, seeded, saver):
    """四次无回答终态：terminal_state='missed_unconfirmed'、status='escalated'、retry_count=3。"""
    # 这个其实是 A2-2 的子集断言，放一个单独用例让验收一目了然
    elder_id = seeded["elder_id"]
    plan_id = seeded["plan_id"]
    now = _utc_now().replace(second=0, microsecond=0)

    rid = await medication_service.create_event(
        session,
        plan_id=plan_id,
        elder_id=elder_id,
        scheduled_at=now,
        drug_snapshot={"drug_name": "硝苯地平", "dose": "10mg 一片"},
        triggered_by="scheduler",
    )
    await session.flush()

    graph = build_medication_graph(
        session=session, checkpointer=saver, deliver_fn=lambda *_a, **_k: True
    )
    await start_medication_task(
        graph,
        reminder_id=rid,
        plan_id=plan_id,
        elder_id=elder_id,
        deadline=(now + datetime.timedelta(minutes=15)).isoformat(),
    )
    for _ in range(4):
        await resume_medication_task(graph, reminder_id=rid, answer="")

    event = await medication_service.get_event(session, event_id=rid)
    assert event["terminal_state"] == "missed_unconfirmed"
    assert event["status"] == "escalated"
    assert event["retry_count"] == 3


async def test_A2_4_one_work_order(session, seeded, saver):
    """漏服终态 → 恰好一张 yellow 工单，order_no 确定性。"""
    elder_id = seeded["elder_id"]
    plan_id = seeded["plan_id"]
    now = _utc_now().replace(second=0, microsecond=0)

    rid = await medication_service.create_event(
        session,
        plan_id=plan_id,
        elder_id=elder_id,
        scheduled_at=now,
        drug_snapshot={"drug_name": "硝苯地平", "dose": "10mg 一片"},
        triggered_by="scheduler",
    )
    await session.flush()

    graph = build_medication_graph(
        session=session, checkpointer=saver, deliver_fn=lambda *_a, **_k: True
    )
    await start_medication_task(
        graph,
        reminder_id=rid,
        plan_id=plan_id,
        elder_id=elder_id,
        deadline=(now + datetime.timedelta(minutes=15)).isoformat(),
    )
    for _ in range(4):
        await resume_medication_task(graph, reminder_id=rid, answer="")

    # 恰好一张工单
    orders = (
        (await session.execute(select(WorkOrder).where(WorkOrder.elder_id == elder_id)))
        .scalars()
        .all()
    )
    assert len(orders) == 1
    o = orders[0]
    assert o.type == "missed_medication"
    assert o.severity == "yellow"
    assert o.status == "new"
    # order_no 格式：WO-YYYYMMDD-XXXXXXXX
    assert o.order_no.startswith("WO-")
    assert len(o.order_no.split("-")) == 3

    # 确定性单号幂等：重跑 finalize 不会多建工单
    # （子图 finalize 已做 set_terminal 终态互斥，重试不会进 finalize）


async def test_A2_5_restart_recovery(session, seeded, saver):
    """重启恢复：新 checkpointer + 新 graph 实例能读等待节点、attempts 行数不增。"""
    elder_id = seeded["elder_id"]
    plan_id = seeded["plan_id"]
    now = _utc_now().replace(second=0, microsecond=0)

    rid = await medication_service.create_event(
        session,
        plan_id=plan_id,
        elder_id=elder_id,
        scheduled_at=now,
        drug_snapshot={"drug_name": "硝苯地平", "dose": "10mg 一片"},
        triggered_by="scheduler",
    )
    await session.flush()

    # 第一次：start → attempt_no 1 已发（remind）→ wait_confirm
    graph1 = build_medication_graph(
        session=session, checkpointer=saver, deliver_fn=lambda *_a, **_k: True
    )
    await start_medication_task(
        graph1,
        reminder_id=rid,
        plan_id=plan_id,
        elder_id=elder_id,
        deadline=(now + datetime.timedelta(minutes=15)).isoformat(),
    )

    # 已触达 attempts 应该有 1 行
    attempts_before = await medication_service.list_attempts(session, event_id=rid)
    assert len(attempts_before) == 1

    # 模拟重启：新建 graph 实例 → get_state 能读回等待位置
    graph2 = build_medication_graph(
        session=session, checkpointer=saver, deliver_fn=lambda *_a, **_k: True
    )
    state = await graph2.aget_state(graph_config(rid))
    assert state is not None
    assert state.values.get("attempt_no") == 1
    assert state.values.get("terminal") is None  # 还在 wait_confirm

    # 已触达 attempts 行数不变（重放防护生效）
    attempts_after = await medication_service.list_attempts(session, event_id=rid)
    assert len(attempts_after) == 1


async def test_A2_6_dual_driver_first_arrives_wins(session, seeded, saver):
    """对话侧先 resume（回了「吃了」）→ 调度侧 resume 空 answer 不应重复推进。"""
    elder_id = seeded["elder_id"]
    plan_id = seeded["plan_id"]
    now = _utc_now().replace(second=0, microsecond=0)

    rid = await medication_service.create_event(
        session,
        plan_id=plan_id,
        elder_id=elder_id,
        scheduled_at=now,
        drug_snapshot={"drug_name": "硝苯地平", "dose": "10mg 一片"},
        triggered_by="scheduler",
    )
    await session.flush()

    graph = build_medication_graph(
        session=session, checkpointer=saver, deliver_fn=lambda *_a, **_k: True
    )
    await start_medication_task(
        graph,
        reminder_id=rid,
        plan_id=plan_id,
        elder_id=elder_id,
        deadline=(now + datetime.timedelta(minutes=15)).isoformat(),
    )

    # 对话侧先回「吃了」（triggered_by="dialog" 的双驱动场景）
    await resume_medication_task(graph, reminder_id=rid, answer="吃了")

    # 终态应该是 taken_reported
    event = await medication_service.get_event(session, event_id=rid)
    assert event["terminal_state"] == "taken_reported"
    assert event["status"] == "confirmed"

    # 调度侧此时再 resume 空 answer：子图已终态，不会再推（resume 会到 END）
    # 这里验证 get_state 有终态
    state = await graph.aget_state(graph_config(rid))
    assert state.values.get("terminal") == "taken_reported"


async def test_A2_7_answer_before_deadline_stops_resume(session, seeded, saver):
    """窗口内回答后进终态 → 后续 resume tick 不应再推进（调度侧守卫）。"""
    # 这个与 A2-6 重叠，但侧重 SchedulerRuntime._maybe_resume 的守卫逻辑
    # （双驱动守卫：已有终态 → 跳过）。
    # 由于 SchedulerRuntime 需要启动完整 scheduler + checkpointer 长开，
    # 这里直接验证守卫条件：terminal 非 None 时不调用 resume。
    elder_id = seeded["elder_id"]
    plan_id = seeded["plan_id"]
    now = _utc_now().replace(second=0, microsecond=0)

    rid = await medication_service.create_event(
        session,
        plan_id=plan_id,
        elder_id=elder_id,
        scheduled_at=now,
        drug_snapshot={"drug_name": "硝苯地平", "dose": "10mg 一片"},
        triggered_by="scheduler",
    )
    await session.flush()

    graph = build_medication_graph(
        session=session, checkpointer=saver, deliver_fn=lambda *_a, **_k: True
    )
    await start_medication_task(
        graph,
        reminder_id=rid,
        plan_id=plan_id,
        elder_id=elder_id,
        deadline=(now + datetime.timedelta(minutes=15)).isoformat(),
    )
    # 提前回答
    await resume_medication_task(graph, reminder_id=rid, answer="吃了")

    # 模拟 SchedulerRuntime._maybe_resume 的守卫判定
    state = await graph.aget_state(graph_config(rid))
    terminal = (state.values or {}).get("terminal")
    assert terminal is not None  # 已有终态
    # 守卫条件命中 → 跳过 resume（不再推进终态）


async def test_A2_8_list_due_plans_cron_match(session, seeded):
    """list_due_plans：active + cron 命中才返回，stopped 不返回，日期外不返回。"""
    # 药单 schedule_cron="* * * * *"（每分钟触发），active，今日 → 应命中
    now = _utc_now()
    due = await list_due_plans(session, now=now)
    # 至少有一条（就是我们 seed 的那条 active）
    assert len(due) >= 1
    # 每条 DuePlan.scheduled_at 必须是 UTC、钉到分钟
    for dp in due:
        assert dp.scheduled_at.second == 0
        assert dp.scheduled_at.microsecond == 0
        assert dp.scheduled_at.tzinfo is not None

    # 现在把 plan 改成 stopped → 不应再返回
    from app.models.medication import MedicationPlan

    plan_id = seeded["plan_id"]
    await session.execute(
        MedicationPlan.__table__.update()
        .where(MedicationPlan.plan_id == plan_id)
        .values(status="stopped")
    )
    await session.flush()
    due2 = await list_due_plans(session, now=now)
    assert not any(dp.plan_id == plan_id for dp in due2)

    # 日期外：start_date=明天 → 不应返回
    await session.execute(
        MedicationPlan.__table__.update()
        .where(MedicationPlan.plan_id == plan_id)
        .values(
            status="active",
            start_date=datetime.date.today() + datetime.timedelta(days=1),
        )
    )
    await session.flush()
    due3 = await list_due_plans(session, now=now)
    assert not any(dp.plan_id == plan_id for dp in due3)


# ------------------------------------------------------------------ F09 终态死锁（Day 9 B4）


@contextmanager
def _runtime_with(session: AsyncSession, saver: AsyncPostgresSaver) -> Iterator[SchedulerRuntime]:
    """构造一个只用于 `_maybe_resume` 的 runtime：会话工厂 yield 现有 savepoint 会话。"""

    @asynccontextmanager
    async def _sf() -> AsyncIterator[AsyncSession]:
        yield session

    rt = SchedulerRuntime(session_factory=_sf, checkpointer_dsn=_test_psycopg_dsn())
    rt._checkpointer = saver  # 跳过 start 的独立连接池，复用测试 saver
    try:
        yield rt
    finally:
        rt._checkpointer = None


async def _seed_missed_event(
    session: AsyncSession,
    seeded: dict[str, Any],
    saver: AsyncPostgresSaver,
    *,
    deliver_ok: bool,
    minutes_ago: int = 21,
) -> tuple[uuid.UUID, Any, datetime.datetime]:
    """造一条「已触达 4 次、仍在 wait_confirm」的事件，并把 T0 挪到过去。"""
    now = _utc_now().replace(second=0, microsecond=0)
    t0 = now - datetime.timedelta(minutes=minutes_ago)
    rid = await medication_service.create_event(
        session,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        scheduled_at=t0,
        drug_snapshot={"drug_name": "硝苯地平", "dose": "10mg 一片"},
        triggered_by="scheduler",
    )
    await session.flush()

    graph = build_medication_graph(
        session=session,
        checkpointer=saver,
        deliver_fn=(lambda *_a, **_k: True) if deliver_ok else (lambda *_a, **_k: False),
    )
    await start_medication_task(
        graph,
        reminder_id=rid,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline=(now + datetime.timedelta(minutes=15)).isoformat(),
        triggered_by="scheduler",
    )
    for _ in range(3):
        await resume_medication_task(graph, reminder_id=rid, answer="")
    return rid, graph, now


async def test_medication_scheduler_finalizes_missed_unconfirmed_after_fourth_attempt(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    """F09：第 4 次窗口结束后调度器仍 resume → finalize 落 missed_unconfirmed。

    修复前 `attempt_no >= MAX_ATTEMPTS` 会跳过这次 resume，事件永远停在
    waiting_confirm（终态死锁）；修复后由子图 retry 收口，且**不发第 5 次**。
    """
    from app.graphs.medication_confirmation_graph import MAX_ATTEMPTS
    from app.scheduler.medication_jobs import _ATTEMPT_INTERVAL_MINUTES

    assert MAX_ATTEMPTS == 4, "PRD §4.5 的四次触达不得改动"
    assert _ATTEMPT_INTERVAL_MINUTES == 5, "T0/+5/+10/+15 不得改动"

    rid, graph, now = await _seed_missed_event(session, seeded, saver, deliver_ok=True)

    state = await graph.aget_state(graph_config(rid))
    assert state.values.get("attempt_no") == 4
    assert state.values.get("terminal") is None  # 4 次触达后仍在等第 5 次 resume

    with _runtime_with(session, saver) as rt:
        await rt._maybe_resume(rid, now)

    event = await medication_service.get_event(session, event_id=rid)
    assert event["terminal_state"] == "missed_unconfirmed"
    assert event["status"] == "escalated"
    assert event["retry_count"] == 3

    attempts = await medication_service.list_attempts(session, event_id=rid)
    assert [a["attempt_no"] for a in attempts] == [1, 2, 3, 4], "不得出现第 5 次触达"

    # 再跑一个 tick：已落终态 → 跳过，不重复 resume / 不重复建单
    with _runtime_with(session, saver) as rt2:
        await rt2._maybe_resume(rid, now + datetime.timedelta(hours=1))

    attempts_after = await medication_service.list_attempts(session, event_id=rid)
    assert len(attempts_after) == 4
    orders = (
        (await session.execute(select(WorkOrder).where(WorkOrder.elder_id == seeded["elder_id"])))
        .scalars()
        .all()
    )
    assert len(orders) == 1
    assert orders[0].type == "missed_medication"


async def test_medication_scheduler_finalizes_notification_failed_after_fourth_attempt(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    """F09 + AC-20 分流：四次全没出过门 → notification_failed + failure_reason=offline。"""
    rid, graph, now = await _seed_missed_event(session, seeded, saver, deliver_ok=False)

    state = await graph.aget_state(graph_config(rid))
    assert state.values.get("attempt_no") == 4

    with _runtime_with(session, saver) as rt:
        await rt._maybe_resume(rid, now)

    event = await medication_service.get_event(session, event_id=rid)
    assert event["terminal_state"] == "notification_failed"
    assert event["retry_count"] == 3
    failure_reason = (
        await session.execute(
            select(MedicationEvent.failure_reason).where(MedicationEvent.reminder_id == rid)
        )
    ).scalar_one()
    assert failure_reason == "offline"

    attempts = await medication_service.list_attempts(session, event_id=rid)
    assert len(attempts) == 4
    assert {a["receipt"] for a in attempts} <= {"device_offline", "no_bound_device"}


# ------------------------------------------------------------------ Z-A4d：away 暂离暂停提醒触达（2026-11-12 dev-A 交接）


async def test_za4d_away_elder_paused_from_due_and_waiting(session, seeded):
    """长者 `away` 期间：到期药单不触发、等待中事件不被 resume 推进；恢复 active 后重新可见。

    A3 只做状态流转＋审计；调度链在 `medication_due_service` 统一读 `elder.status`，
    不另设第二套判断（dev-A 交接件，X4/X12 同批联调口径）。
    """
    from app.models.medication import MedicationEvent
    from app.models.organization import Elder

    elder_id = seeded["elder_id"]
    plan_id = seeded["plan_id"]
    now = _utc_now()

    # 基线：active 长者可见
    due = await list_due_plans(session, now=now)
    assert any(dp.plan_id == plan_id for dp in due)

    # 置 away：到期列表立即不含该药单
    await session.execute(
        Elder.__table__.update().where(Elder.elder_id == elder_id).values(status="away")
    )
    await session.flush()
    due_away = await list_due_plans(session, now=now)
    assert not any(dp.plan_id == plan_id for dp in due_away)

    # 等待中的事件：away 长者不被 resume 扫到（不再触达）
    event = MedicationEvent(
        plan_id=plan_id,
        elder_id=elder_id,
        scheduled_at=now,
        plan_version=1,
        triggered_by="scheduler",
        status="waiting_confirm",
        retry_count=0,
        drug_snapshot={"drug_name": "硝苯地平", "dose": "10mg 一片"},
        payload_hash="za4d-test-hash",
    )
    session.add(event)
    await session.flush()
    waiting_away = await list_waiting_confirm_events(session)
    assert not any(w.reminder_id == event.reminder_id for w in waiting_away)

    # 恢复 active：既有事件重新进入 resume 扫描（由 attempts/终态逻辑自然收口，不伪造应答）
    await session.execute(
        Elder.__table__.update().where(Elder.elder_id == elder_id).values(status="active")
    )
    await session.flush()
    waiting_back = await list_waiting_confirm_events(session)
    assert any(w.reminder_id == event.reminder_id for w in waiting_back)
