"""工单建单与流转服务（A12 / 计划 V-A12）。

覆盖七件事，每件对应一个「错了会静默出错」的点：

1. 建单落库，且首条流转记录存在（否则 FR-ALT-02「可查完整流转」从第一笔就断）；
2. **同一来源重复建单只建一张**（确定性单号，靠唯一约束兜底）；
3. 不给 token 时每次都是新单（避免把「允许重复」的场景误做成幂等）；
4. 非法类型 / 非法严重度在写库前就拒掉（不能让 CHECK 报错变成 500）；
5. 合法迁移通过并留痕；**非法迁移抛错而不是静默忽略**；
6. 同状态流转是空操作（重复点「已处理」不该报错、也不该多一条记录）；
7. 两条硬规则：误报必须给原因、红单关闭必须有实际处置时间。

另附一条「未配责任护工时 assignee 为 None」——看板上的「未分派」是真实状态，
不该为了让字段有值而随便指一个人。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models.medication import RiskEvent, WorkOrder, WorkOrderTransition
from app.models.organization import Elder
from app.models.relation import ElderStaffAssignment
from app.models.staff import StaffAccount
from app.services import work_order_service


@pytest_asyncio.fixture
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


@pytest_asyncio.fixture
async def seeded(session: AsyncSession) -> dict[str, Any]:
    """一位长者 + 一位护工（护工用于验证 assignee 写入）。"""
    elder = Elder(name="张桂芬", status="active")
    worker = StaffAccount(name="李护工", role="care_worker", password_hash="x" * 32)
    session.add_all([elder, worker])
    await session.flush()
    return {"elder_id": elder.elder_id, "staff_id": worker.staff_id}


async def _orders(session: AsyncSession, elder_id: uuid.UUID) -> list[WorkOrder]:
    return list(
        (await session.execute(select(WorkOrder).where(WorkOrder.elder_id == elder_id))).scalars()
    )


# ------------------------------------------------------------------ 1. 建单


async def test_open_creates_order_with_first_transition(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    ref = await work_order_service.open_work_order(
        session,
        elder_id=seeded["elder_id"],
        order_type="missed_medication",
        severity="yellow",
        dedupe_token="medication_event:abc",
    )

    assert ref.created is True
    assert ref.status == "new"
    assert ref.severity == "yellow"
    assert ref.order_no.startswith("WO-")
    assert len(ref.order_no.rsplit("-", 1)[-1]) == 8, "随机段固定 8 位，与 C 侧格式一致"

    rows = await _orders(session, seeded["elder_id"])
    assert len(rows) == 1

    transitions = list(
        (
            await session.execute(
                select(WorkOrderTransition).where(WorkOrderTransition.order_id == ref.order_id)
            )
        ).scalars()
    )
    assert len(transitions) == 1, "建单必须留一条流转记录，否则 FR-ALT-02 从第一笔就断"
    assert transitions[0].from_status == "new" and transitions[0].to_status == "new"
    assert transitions[0].note == "created"


async def test_orders_table_has_no_source_column_so_token_is_sha256(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """单号里不得出现原始 token（reminder_id 属敏感标识，不该从工单号反推）。"""
    token = "medication_event:11111111-2222-3333-4444-555555555555"
    ref = await work_order_service.open_work_order(
        session,
        elder_id=seeded["elder_id"],
        order_type="missed_medication",
        severity="yellow",
        dedupe_token=token,
    )
    assert "medication_event" not in ref.order_no
    assert "11111111" not in ref.order_no


# ------------------------------------------------------------------ 2. 幂等


async def test_open_is_idempotent_for_same_token(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """同一提醒事件重复建单只生成一张；不同来源必须各一张。"""
    rid = uuid.uuid4()
    first = await work_order_service.open_missed_medication(
        session, elder_id=seeded["elder_id"], reminder_id=rid
    )
    again = await work_order_service.open_missed_medication(
        session, elder_id=seeded["elder_id"], reminder_id=rid
    )
    assert first.order_id == again.order_id
    assert first.created is True and again.created is False
    assert len(await _orders(session, seeded["elder_id"])) == 1

    other = await work_order_service.open_missed_medication(
        session, elder_id=seeded["elder_id"], reminder_id=uuid.uuid4()
    )
    assert other.order_id != first.order_id
    assert other.created is True
    assert len(await _orders(session, seeded["elder_id"])) == 2


async def test_open_without_token_creates_new_each_time(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    a = await work_order_service.open_work_order(
        session,
        elder_id=seeded["elder_id"],
        order_type="follow_up",
        severity="yellow",
    )
    b = await work_order_service.open_work_order(
        session,
        elder_id=seeded["elder_id"],
        order_type="follow_up",
        severity="yellow",
    )
    assert a.order_no != b.order_no
    assert a.created and b.created


# ------------------------------------------------------------------ 3. 入参校验


@pytest.mark.parametrize(
    ("order_type", "severity"),
    [("not_a_type", "yellow"), ("missed_medication", "blue")],
)
async def test_open_rejects_unknown_enum_before_touching_db(
    session: AsyncSession, seeded: dict[str, Any], order_type: str, severity: str
) -> None:
    with pytest.raises(work_order_service.WorkOrderError):
        await work_order_service.open_work_order(
            session,
            elder_id=seeded["elder_id"],
            order_type=order_type,
            severity=severity,
        )
    assert await _orders(session, seeded["elder_id"]) == []


async def test_missed_medication_is_yellow_and_has_no_event_id(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    ref = await work_order_service.open_missed_medication(
        session, elder_id=seeded["elder_id"], reminder_id=uuid.uuid4()
    )
    order = (
        await session.execute(select(WorkOrder).where(WorkOrder.order_id == ref.order_id))
    ).scalar_one()
    assert order.type == "missed_medication"
    assert order.severity == "yellow", "漏服不是急症；判红会稀释真正需要上门的红单"
    assert order.event_id is None, "漏服不是 risk_events，不得借一个别的 event_id"


# ------------------------------------------------------------------ 4. 流转


async def test_transition_records_history(session: AsyncSession, seeded: dict[str, Any]) -> None:
    ref = await work_order_service.open_missed_medication(
        session, elder_id=seeded["elder_id"], reminder_id=uuid.uuid4()
    )
    await work_order_service.transition(
        session, order_id=ref.order_id, to_status="acknowledged", actor_id=seeded["staff_id"]
    )
    await work_order_service.transition(
        session,
        order_id=ref.order_id,
        to_status="resolved",
        actor_id=seeded["staff_id"],
        note="已电话确认",
    )

    order = (
        await session.execute(select(WorkOrder).where(WorkOrder.order_id == ref.order_id))
    ).scalar_one()
    assert order.status == "resolved"

    rows = list(
        (
            await session.execute(
                select(WorkOrderTransition)
                .where(WorkOrderTransition.order_id == ref.order_id)
                .order_by(WorkOrderTransition.at)
            )
        ).scalars()
    )
    assert [(r.from_status, r.to_status) for r in rows] == [
        ("new", "new"),
        ("new", "acknowledged"),
        ("acknowledged", "resolved"),
    ]


async def test_transition_rejects_illegal_and_keeps_state(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """`new → resolved` 不允许：跳过分诊会让「谁接的单」无从追溯。"""
    ref = await work_order_service.open_missed_medication(
        session, elder_id=seeded["elder_id"], reminder_id=uuid.uuid4()
    )
    with pytest.raises(work_order_service.InvalidTransition):
        await work_order_service.transition(session, order_id=ref.order_id, to_status="resolved")

    order = (
        await session.execute(select(WorkOrder).where(WorkOrder.order_id == ref.order_id))
    ).scalar_one()
    assert order.status == "new", "非法迁移必须不改变任何状态"


async def test_transition_same_status_is_noop(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    ref = await work_order_service.open_missed_medication(
        session, elder_id=seeded["elder_id"], reminder_id=uuid.uuid4()
    )
    await work_order_service.transition(session, order_id=ref.order_id, to_status="new")

    rows = list(
        (
            await session.execute(
                select(WorkOrderTransition).where(WorkOrderTransition.order_id == ref.order_id)
            )
        ).scalars()
    )
    assert len(rows) == 1, "同态流转不得再写一条记录（否则看板的流转条数会虚高）"


async def test_transition_unknown_order_raises(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    with pytest.raises(work_order_service.WorkOrderNotFound):
        await work_order_service.transition(session, order_id=uuid.uuid4(), to_status="closed")


# ------------------------------------------------------------------ 5. 两条硬规则


async def test_false_alarm_requires_reason(session: AsyncSession, seeded: dict[str, Any]) -> None:
    ref = await work_order_service.open_missed_medication(
        session, elder_id=seeded["elder_id"], reminder_id=uuid.uuid4()
    )
    with pytest.raises(work_order_service.WorkOrderError):
        await work_order_service.transition(
            session, order_id=ref.order_id, to_status="closed", false_alarm=True
        )

    await work_order_service.transition(
        session,
        order_id=ref.order_id,
        to_status="closed",
        false_alarm=True,
        false_alarm_reason="长者当天住院，提醒应由住院医嘱覆盖",
    )
    order = (
        await session.execute(select(WorkOrder).where(WorkOrder.order_id == ref.order_id))
    ).scalar_one()
    assert order.false_alarm is True
    assert order.false_alarm_reason is not None
    assert order.closed_at is not None


async def test_red_order_cannot_close_without_handled_at(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    ref = await work_order_service.open_work_order(
        session,
        elder_id=seeded["elder_id"],
        order_type="sos",
        severity="red",
        dedupe_token="sos:1",
    )
    with pytest.raises(work_order_service.WorkOrderError):
        await work_order_service.transition(session, order_id=ref.order_id, to_status="closed")

    await work_order_service.transition(
        session,
        order_id=ref.order_id,
        to_status="closed",
        actual_handled_at=datetime.datetime(2026, 11, 9, 8, 3, tzinfo=datetime.UTC),
    )
    order = (
        await session.execute(select(WorkOrder).where(WorkOrder.order_id == ref.order_id))
    ).scalar_one()
    assert order.actual_handled_at is not None, "AC-22 的红单必须能回溯到处置时间"


# ------------------------------------------------------------------ 5b. F13 关闭联动


async def test_transition_closed_links_risk_event(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """F13：工单 closed → 同事务关闭关联 risk_events（status/closed_at）。"""
    event = RiskEvent(
        event_id=uuid.uuid4(),
        elder_id=seeded["elder_id"],
        type="health_alert",
        severity="red",
        status="open",
    )
    session.add(event)
    await session.flush()

    ref = await work_order_service.open_work_order(
        session,
        elder_id=seeded["elder_id"],
        order_type="health_alert",
        severity="red",
        dedupe_token="link:1",
        event_id=event.event_id,
    )
    await work_order_service.transition(
        session,
        order_id=ref.order_id,
        to_status="closed",
        actual_handled_at=datetime.datetime(2026, 11, 12, 9, 0, tzinfo=datetime.UTC),
    )
    await session.flush()
    updated = (
        await session.execute(select(RiskEvent).where(RiskEvent.event_id == event.event_id))
    ).scalar_one()
    assert updated.status == "closed"
    assert updated.closed_at is not None


async def test_transition_closed_without_event_id_no_linkage(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """F13：无 event_id 的漏服类工单关闭不触发事件联动（幂等空转）。"""
    ref = await work_order_service.open_work_order(
        session,
        elder_id=seeded["elder_id"],
        order_type="drug_question",
        severity="yellow",
        dedupe_token="link:2",
    )
    await work_order_service.transition(session, order_id=ref.order_id, to_status="closed")
    # 只要不抛错即通过：无事件可联动


async def test_work_order_purge_elder_data(session: AsyncSession, seeded: dict[str, Any]) -> None:
    """X12 工单钩子：transitions → orders → risk_events 级联清除，幂等。"""
    event = RiskEvent(
        event_id=uuid.uuid4(),
        elder_id=seeded["elder_id"],
        type="health_alert",
        severity="yellow",
        status="open",
    )
    session.add(event)
    await session.flush()
    ref = await work_order_service.open_work_order(
        session,
        elder_id=seeded["elder_id"],
        order_type="health_alert",
        severity="yellow",
        dedupe_token="purge:1",
        event_id=event.event_id,
    )
    await work_order_service.transition(session, order_id=ref.order_id, to_status="in_progress")

    result = await work_order_service.purge_elder_data(
        session, elder_id=seeded["elder_id"], reason="checkout"
    )
    await session.flush()
    assert result.domain == "work_orders"
    assert result.affected_rows >= 2  # 1 order + ≥1 transition（risk_event 计 1）
    assert (
        await session.execute(
            select(func.count())
            .select_from(WorkOrder)
            .where(WorkOrder.elder_id == seeded["elder_id"])
        )
    ).scalar_one() == 0
    assert (
        await session.execute(
            select(func.count())
            .select_from(WorkOrderTransition)
            .where(WorkOrderTransition.order_id == ref.order_id)
        )
    ).scalar_one() == 0
    assert (
        await session.execute(
            select(func.count())
            .select_from(RiskEvent)
            .where(RiskEvent.elder_id == seeded["elder_id"])
        )
    ).scalar_one() == 0

    # 幂等：第二遍为 0
    again = await work_order_service.purge_elder_data(
        session, elder_id=seeded["elder_id"], reason="teaching_reset"
    )
    assert again.affected_rows == 0

    with pytest.raises(ValueError, match="unknown purge reason"):
        await work_order_service.purge_elder_data(
            session, elder_id=seeded["elder_id"], reason="nope"
        )


# ------------------------------------------------------------------ 6. 责任护工


async def test_resolve_primary_care_returns_none_when_unassigned(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    assert (
        await work_order_service.resolve_primary_care_staff(session, elder_id=seeded["elder_id"])
        is None
    )


async def test_resolve_primary_care_picks_primary(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    doctor = StaffAccount(name="王医生", role="doctor", password_hash="y" * 32)
    session.add(doctor)
    await session.flush()
    session.add_all(
        [
            ElderStaffAssignment(
                elder_id=seeded["elder_id"],
                staff_id=seeded["staff_id"],
                relation="primary_care",
            ),
            # 医生也绑在同一长者身上（`relation` 只有 primary_care / doctor 两值）
            ElderStaffAssignment(
                elder_id=seeded["elder_id"], staff_id=doctor.staff_id, relation="doctor"
            ),
        ]
    )
    await session.flush()

    got = await work_order_service.resolve_primary_care_staff(session, elder_id=seeded["elder_id"])
    assert got == seeded["staff_id"], "只能取 primary_care，不得退化为任意绑定人"
