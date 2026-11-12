"""健康域调度驱动链（B3 · Day 8）测试。

覆盖计划必过 4 用例 + 日报幂等：
1. scheduled_19h_triggers_graph_voice_scheduled：19:00 触发起图（voice_scheduled），
   窗口外不触发，重复 tick 幂等（checkpoint 存在性跳过）
2. window_end_forces_finish_and_missed_order：20:00 先漏采判定建单
   （collection_missed / yellow / assignee=负责护工）再 force_close 收口；重复执行不重复建单
3. red_2min_escalation_scan_idempotent：红事件 2 分钟无人确认 → 原事件标 escalated
   （AC-12，不建第二个事件/工单）；重复扫描不重复发升级通知
4. notification_retry_scan：pending 30s 重试 → delivered；重试耗尽 → failed（AC-23 前半）
5. daily_report_tick_idempotent：日报触发重复执行不重建（C2 generate_daily_report 幂等）

checkpointer / session fixture 与 test_scheduler_medication.py 同形
（真 PG + savepoint 会话；触发时刻经 HealthSchedulerRuntime 的 now_fn 注入，
对应计划待确认项①「Settings 覆盖为可测试时刻」裁决）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.graphs.health_checkup_graph import (
    build_health_checkup_graph,
    default_thread_id,
    graph_config,
)
from app.models.medication import RiskEvent, WorkOrder
from app.models.notifications import Notification
from app.models.organization import Elder
from app.models.relation import ElderStaffAssignment
from app.models.report import DailyReport
from app.models.staff import StaffAccount
from app.scheduler.health_jobs import HealthSchedulerRuntime
from app.services import health_service, notify_service

_TZ_SH = ZoneInfo("Asia/Shanghai")


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


def _settings_hhmm(name: str) -> str:
    """读 Settings 里的 HH:MM 配置（health_checkup_start/end）。

    测试不假设默认值：.env 覆盖成什么就用什么造时钟（待确认项①口径）。
    """
    from app.core.config import get_settings

    return str(getattr(get_settings(), name))


def _shanghai_clock(day: datetime.date, hhmm: str) -> datetime.datetime:
    """给定上海日期 + HH:MM → 对应的 aware UTC 时刻（调度时钟注入值）。"""
    hour, minute = (int(part) for part in hhmm.split(":"))
    local = datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=_TZ_SH)
    return local.astimezone(datetime.UTC)


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
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
    from app.graphs.checkpointer import medication_checkpointer

    if not _test_db_bootstrap:
        pytest.skip("PostgreSQL 不可达")
    async with medication_checkpointer(dsn=_test_psycopg_dsn()) as checkpointer:
        yield checkpointer


@pytest.fixture
async def seeded(session: AsyncSession) -> dict[str, Any]:
    """一位 active 长者 + 一位护工 + primary_care 绑定。"""
    elder = Elder(name="李长生", status="active")
    care_worker = StaffAccount(name="钱护工", role="care_worker", password_hash="x" * 32)
    session.add_all([elder, care_worker])
    await session.flush()
    session.add(
        ElderStaffAssignment(
            elder_id=elder.elder_id,
            staff_id=care_worker.staff_id,
            relation="primary_care",
        )
    )
    await session.flush()
    return {"elder_id": elder.elder_id, "care_worker_id": care_worker.staff_id}


@pytest.fixture
def runtime_factory(session: AsyncSession, saver: AsyncPostgresSaver):
    """构造注入时钟的 HealthSchedulerRuntime（checkpointer 用共享 saver，不 start()）。"""

    def _make(now_fn) -> HealthSchedulerRuntime:
        @asynccontextmanager
        async def _sf():
            yield session

        rt = HealthSchedulerRuntime(
            session_factory=_sf,
            checkpointer_dsn=_test_psycopg_dsn(),
            now_fn=now_fn,
        )
        rt._checkpointer = saver
        return rt

    return _make


# ------------------------------------------------------------------ helpers


def _clock_at(hhmm: str):
    """按真实「今天」的上海日期 + HH:MM 造时钟（窗口守卫按上海时刻判定）。"""
    day = datetime.datetime.now(datetime.UTC).astimezone(_TZ_SH).date()
    return _shanghai_clock(day, hhmm)


# ------------------------------------------------------------------ 用例 ①


async def test_scheduled_19h_triggers_graph_voice_scheduled(
    session, seeded, saver, runtime_factory
):
    """19:00（health_checkup_start）触发起图 source=voice_scheduled；窗口外/重复 tick 不重复起图。"""
    elder_id = seeded["elder_id"]
    day = datetime.datetime.now(datetime.UTC).astimezone(_TZ_SH).date()
    thread_id = default_thread_id(elder_id, "voice_scheduled", on_date=day)
    start_hhmm = _settings_hhmm("health_checkup_start")
    graph = build_health_checkup_graph(session=session, checkpointer=saver)

    # 窗口外（开始时刻前 1 分钟）→ 不触发
    before_hhmm = _hhmm_of(_minutes_of(start_hhmm) - 1)
    rt_before = runtime_factory(lambda: _clock_at(before_hhmm))
    await rt_before._tick_evening_trigger()
    state = await graph.aget_state(graph_config(thread_id))
    assert not state.values  # 窗口外没有起图

    # 窗口内第一次触发 → 起图，source=voice_scheduled，停在 collect 的 interrupt
    rt = runtime_factory(lambda: _clock_at(start_hhmm))
    await rt._tick_evening_trigger()
    state = await graph.aget_state(graph_config(thread_id))
    assert state.values
    assert state.values["source"] == "voice_scheduled"
    assert state.values["elder_id"] == str(elder_id)
    assert state.next  # 停在 collect 节点等回答

    # 重复 tick（时钟不动）→ checkpoint 已存在，不二次起图（无重复 thread）
    await rt._tick_evening_trigger()
    # thread_id 按日幂等派生，天然同一个；状态仍停在第一问
    state2 = await graph.aget_state(graph_config(thread_id))
    assert state2.values["source"] == "voice_scheduled"
    answers = state2.values.get("answers") or {}
    assert not answers  # 没有被二次推进


async def test_window_outside_no_trigger(session, seeded, saver, runtime_factory):
    """开始时刻之外的分钟不触发（负向断言独立成例，避免与用例①互相污染）。"""
    elder_id = seeded["elder_id"]
    day = datetime.datetime.now(datetime.UTC).astimezone(_TZ_SH).date()
    thread_id = default_thread_id(elder_id, "voice_scheduled", on_date=day)
    # 用结束时刻 + 2 分钟（肯定不在触发窗口）跑触发 tick
    end_hhmm = _settings_hhmm("health_checkup_end")
    late = _minutes_of(end_hhmm) + 2
    rt = runtime_factory(lambda: _clock_at(_hhmm_of(late)))
    await rt._tick_evening_trigger()
    graph = build_health_checkup_graph(session=session, checkpointer=saver)
    state = await graph.aget_state(graph_config(thread_id))
    assert not state.values


# ------------------------------------------------------------------ 用例 ②


async def test_window_end_forces_finish_and_missed_order(session, seeded, saver, runtime_factory):
    """20:00（health_checkup_end）：无记录长者建漏采单（assignee=负责护工）+ 挂起线程强制收口。

    重复执行不产生重复工单、重复收口无副作用（幂等）。
    """
    elder_id = seeded["elder_id"]
    care_worker_id = seeded["care_worker_id"]
    day = datetime.datetime.now(datetime.UTC).astimezone(_TZ_SH).date()
    thread_id = default_thread_id(elder_id, "voice_scheduled", on_date=day)

    # 19:00 先起图（停在第一问）
    rt_start = runtime_factory(lambda: _clock_at(_settings_hhmm("health_checkup_start")))
    await rt_start._tick_evening_trigger()

    # 20:00 收口 tick 执行两次（幂等验证）
    rt_close = runtime_factory(lambda: _clock_at(_settings_hhmm("health_checkup_end")))
    await rt_close._tick_window_close()
    await rt_close._tick_window_close()

    # 漏采工单：恰好 1 张、collection_missed / yellow / 指派负责护工。
    # 注：强制收口落的全缺失记录会按 X1 判黄建「黄色复核工单」，属于预期第二张——
    # 漏采单断言必须按 type 过滤，不能数总数。
    orders = (
        (await session.execute(select(WorkOrder).where(WorkOrder.elder_id == elder_id)))
        .scalars()
        .all()
    )
    missed_orders = [o for o in orders if o.type == "collection_missed"]
    assert len(missed_orders) == 1
    order = missed_orders[0]
    assert order.severity == "yellow"
    assert order.assignee_id == care_worker_id

    # 挂起线程被强制收口：图跑完、未答标缺失、全缺失判黄落库
    graph = build_health_checkup_graph(session=session, checkpointer=saver)
    state = await graph.aget_state(graph_config(thread_id))
    assert state.values
    assert not state.next  # 图已到 END
    assert state.values.get("grade_level") == "yellow"  # 全缺失 → 黄（X1 引擎规则）
    record_ref = state.values.get("record_ref") or {}
    assert record_ref.get("record_id")

    # 收口落的记录当日可见（第二次 tick 的漏采判定因此不再建单）
    with_records = await health_service.list_elder_ids_with_records_on(session, day=day)
    assert elder_id in with_records


# ------------------------------------------------------------------ 用例 ③


async def test_red_2min_escalation_scan_idempotent(session, seeded, runtime_factory):
    """红事件 2 分钟无人确认 → 原事件标 escalated（AC-12）；重复扫描不重复发升级通知。"""
    elder_id = seeded["elder_id"]

    # 造红色事件（收缩压 190 ≥180 单分量判红）并把 created_at 回拨 3 分钟
    ref = await health_service.create_health_record(
        session,
        elder_id=elder_id,
        source="voice_active",
        operator_id=None,
        systolic=190,
        diastolic=95,
        sleep_hours=None,
        mood_score=None,
        mood_note=None,
        complaint=None,
    )
    assert ref.risk_event_id is not None
    now = datetime.datetime.now(datetime.UTC)
    await session.execute(
        update(RiskEvent)
        .where(RiskEvent.event_id == ref.risk_event_id)
        .values(created_at=now - datetime.timedelta(minutes=3))
    )
    await session.flush()

    rt = runtime_factory(lambda: datetime.datetime.now(datetime.UTC))
    # F12：升级通知发 admin —— 本测试 seed 一个 admin（不动共享夹具，避免影响其他用例）
    session.add(StaffAccount(name="升级管理员", role="admin", password_hash="x" * 32))
    await session.flush()
    await rt._tick_risk_scan()

    event = (
        await session.execute(select(RiskEvent).where(RiskEvent.event_id == ref.risk_event_id))
    ).scalar_one()
    assert event.status == "escalated"  # 原事件升级，不是新建第二个事件

    # F12（X3 v0.6 期间裁决）：升级通知发 admin，护工不重复收
    admin_id = (
        await session.execute(select(StaffAccount.staff_id).where(StaffAccount.role == "admin"))
    ).scalar_one_or_none()
    assert admin_id is not None, "升级测试需要 seed 一个 admin 角色"
    escalation_notes = (
        (
            await session.execute(
                select(Notification).where(
                    Notification.type == "escalated_red_event",
                    Notification.dedupe_key
                    == f"risk_event:{ref.risk_event_id}:escalated:{admin_id}",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(escalation_notes) == 1
    assert escalation_notes[0].target_id == admin_id

    # 重复扫描：事件已 escalated 不再进扫描面 → 不重复发通知
    await rt._tick_risk_scan()
    escalation_notes_after = (
        (
            await session.execute(
                select(Notification).where(Notification.type == "escalated_red_event")
            )
        )
        .scalars()
        .all()
    )
    assert len(escalation_notes_after) == 1
    assert event.status == "escalated"


async def care_worker_id_of(session: AsyncSession, elder_id: uuid.UUID) -> uuid.UUID:
    return (
        await session.execute(
            select(ElderStaffAssignment.staff_id).where(
                ElderStaffAssignment.elder_id == elder_id,
                ElderStaffAssignment.relation == "primary_care",
            )
        )
    ).scalar_one()


# ------------------------------------------------------------------ 用例 ④


async def test_notification_retry_scan(session, seeded, runtime_factory):
    """F01 送达真链路：重试只记尝试（保持 pending），delivered 仅由 X1 回执驱动；
    重试耗尽 → failed（AC-23 前半）；未到点不抢跑（X11 退避）。"""
    care_worker_id = seeded["care_worker_id"]

    now = datetime.datetime.now(datetime.UTC)
    aged = now - datetime.timedelta(seconds=60)  # 上次尝试 60s 前：30s 退避已到点
    pending = Notification(
        target_type="staff",
        target_id=care_worker_id,
        type="escalated_red_event",
        payload={"event_id": str(uuid.uuid4())},
        channel="inapp",
        status="pending",
        retry_count=0,
        is_mock=True,
        sent_at=aged,
        dedupe_key=f"test_retry_pending:{uuid.uuid4()}",
    )
    fresh = Notification(  # 刚生成未到点：不应被本轮重试（X11 due 门控）
        target_type="staff",
        target_id=care_worker_id,
        type="escalated_red_event",
        payload={"event_id": str(uuid.uuid4())},
        channel="inapp",
        status="pending",
        retry_count=0,
        is_mock=True,
        sent_at=now,
        dedupe_key=f"test_retry_fresh:{uuid.uuid4()}",
    )
    exhausted = Notification(
        target_type="staff",
        target_id=care_worker_id,
        type="escalated_red_event",
        payload={"event_id": str(uuid.uuid4())},
        channel="inapp",
        status="pending",
        retry_count=3,
        is_mock=True,
        dedupe_key=f"test_retry_exhausted:{uuid.uuid4()}",
    )
    session.add_all([pending, fresh, exhausted])
    await session.flush()

    pending_id, fresh_id, exhausted_id = (
        pending.notification_id,
        fresh.notification_id,
        exhausted.notification_id,
    )
    rt = runtime_factory(lambda: now)
    await rt._tick_risk_scan()

    retried_row = (
        await session.execute(
            select(Notification).where(Notification.notification_id == pending_id)
        )
    ).scalar_one()
    fresh_row = (
        await session.execute(select(Notification).where(Notification.notification_id == fresh_id))
    ).scalar_one()
    failed_row = (
        await session.execute(
            select(Notification).where(Notification.notification_id == exhausted_id)
        )
    ).scalar_one()
    # 到点行：记一次尝试，状态保持 pending 等 X1 回执——绝不出现 delivered
    assert retried_row.status == "pending"
    assert retried_row.retry_count == 1
    assert retried_row.sent_at is not None
    # 未到点行：原样跳过
    assert fresh_row.status == "pending"
    assert fresh_row.retry_count == 0
    # 耗尽行：失败终态
    assert failed_row.status == "failed"

    # X1 回执驱动送达：pending → delivered → acknowledged（AC-23 全链路）
    await notify_service.mark_notification_delivered(session, notification_id=pending_id)
    await notify_service.acknowledge_notification(
        session, notification_id=pending_id, acknowledger_id=care_worker_id
    )
    delivered_row = (
        await session.execute(
            select(Notification).where(Notification.notification_id == pending_id)
        )
    ).scalar_one()
    assert delivered_row.status == "acknowledged"

    # 重复执行：failed/acknowledged 均不在扫描面；fresh 仍未到点 → 无变化
    await rt._tick_risk_scan()
    fresh_row2 = (
        await session.execute(select(Notification).where(Notification.notification_id == fresh_id))
    ).scalar_one()
    assert fresh_row2.status == "pending"
    assert fresh_row2.retry_count == 0


# ------------------------------------------------------------------ 用例 ⑤


async def test_daily_report_tick_idempotent(session, seeded, runtime_factory):
    """日报触发重复执行：当日已有版本幂等返回，不重建（AC-25）。"""
    elder_id = seeded["elder_id"]
    from app.scheduler.health_jobs import _DAILY_REPORT_FIRE

    rt = runtime_factory(lambda: _clock_at(_DAILY_REPORT_FIRE))
    await rt._tick_report()
    await rt._tick_report()

    reports = (
        (await session.execute(select(DailyReport).where(DailyReport.elder_id == elder_id)))
        .scalars()
        .all()
    )
    assert len(reports) == 1
    assert reports[0].version == 1


# ------------------------------------------------------------------ helpers（HH:MM 算术）


def _minutes_of(hhmm: str) -> int:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return hour * 60 + minute


def _hhmm_of(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
