"""SOS service 骨架（Day 8 D3，FR-ALT-06）：建单/幂等/未分派三件事。

主理人裁决 Day 8 只建骨架：本测试覆盖 service 层，REST/pad 接线归 Day 9/10。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models.medication import RiskEvent, WorkOrder
from app.models.notifications import Notification
from app.models.organization import Elder
from app.models.relation import ElderStaffAssignment
from app.models.staff import StaffAccount
from app.services import sos_service


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
    elder = Elder(name="SOS测试长者", status="active")
    worker = StaffAccount(name="SOS测试护工", role="care_worker", password_hash="x" * 32)
    session.add_all([elder, worker])
    await session.flush()
    session.add(
        ElderStaffAssignment(
            elder_id=elder.elder_id, staff_id=worker.staff_id, relation="primary_care"
        )
    )
    await session.flush()
    return {"elder_id": elder.elder_id, "staff_id": worker.staff_id}


async def test_trigger_sos_creates_event_order_notification(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    result = await sos_service.trigger_sos(session, elder_id=seeded["elder_id"])
    assert result.created is True

    event = (
        await session.execute(select(RiskEvent).where(RiskEvent.event_id == result.event_id))
    ).scalar_one()
    assert event.type == "sos"
    assert event.severity == "red"
    assert event.status == "open"

    order = (
        await session.execute(select(WorkOrder).where(WorkOrder.order_id == result.order_id))
    ).scalar_one()
    assert order.type == "sos"
    assert order.severity == "red"
    assert order.assignee_id == seeded["staff_id"]  # 负责护工自动指派
    assert order.event_id == result.event_id

    notif = (
        await session.execute(
            select(Notification).where(
                Notification.dedupe_key == f"sos:{result.event_id}:{seeded['staff_id']}"
            )
        )
    ).scalar_one()
    assert notif.target_type == "staff"
    assert notif.type == "sos"


async def test_trigger_sos_idempotent_while_open(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    first = await sos_service.trigger_sos(session, elder_id=seeded["elder_id"])
    second = await sos_service.trigger_sos(session, elder_id=seeded["elder_id"])
    assert second.created is False
    assert second.event_id == first.event_id
    assert second.order_id == first.order_id

    events = (
        (
            await session.execute(
                select(RiskEvent).where(
                    RiskEvent.elder_id == seeded["elder_id"], RiskEvent.type == "sos"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1  # 不建第二个事件（AC-12 同精神）
    orders = (
        (await session.execute(select(WorkOrder).where(WorkOrder.elder_id == seeded["elder_id"])))
        .scalars()
        .all()
    )
    assert len(orders) == 1


async def test_trigger_sos_without_primary_care_assignee_none(
    session: AsyncSession,
) -> None:
    elder = Elder(name="SOS无护工长者", status="active")
    session.add(elder)
    await session.flush()
    result = await sos_service.trigger_sos(session, elder_id=elder.elder_id)
    assert result.created is True
    order = (
        await session.execute(select(WorkOrder).where(WorkOrder.order_id == result.order_id))
    ).scalar_one()
    assert order.assignee_id is None  # 未分派是真实状态，不瞎指人
