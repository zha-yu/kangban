"""普通「已服自报」撤销测试（Day 8 D3 · FR-UI-08 / AC-26）。

覆盖：
- 10 秒内撤销 → 恢复 waiting_confirm、清空终态/确认时间，并写审计；
- 超过 10 秒 → `MedicationUndoWindowExpired`，事件保持 taken_reported；
- 非「已服自报」终态 → `MedicationUndoNotAllowed`；
- 他人事件 → 一律 `MedicationEventNotFound`（防枚举）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models.audit import AuditLog
from app.models.medication import MedicationEvent, MedicationPlan
from app.models.organization import Elder
from app.models.staff import StaffAccount
from app.services import medication_service

UTC = datetime.UTC


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
    elder = Elder(name="撤销测试长者", status="active")
    doctor = StaffAccount(name="撤销测试医生", role="doctor", password_hash="x" * 32)
    session.add_all([elder, doctor])
    await session.flush()
    plan = MedicationPlan(
        elder_id=elder.elder_id,
        drug_name="氨氯地平",
        dose="5mg 一片",
        schedule_cron="0 8 * * *",
        start_date=datetime.date(2026, 11, 1),
        created_by=doctor.staff_id,
    )
    session.add(plan)
    await session.flush()
    return {"elder_id": elder.elder_id, "plan_id": plan.plan_id}


async def _confirmed_event(session: AsyncSession, seeded: dict[str, Any]) -> uuid.UUID:
    reminder_id = await medication_service.create_event(
        session,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        scheduled_at=datetime.datetime(2026, 11, 11, 8, 0, tzinfo=UTC),
        drug_snapshot={"drug_name": "氨氯地平", "dose": "5mg 一片"},
        triggered_by="scheduler",
    )
    await medication_service.set_terminal(
        session, event_id=reminder_id, terminal_state="taken_reported", confirm_channel="voice"
    )
    return reminder_id


async def test_revoke_within_window_restores_waiting_confirm(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    reminder_id = await _confirmed_event(session, seeded)
    current = (
        await session.execute(
            select(MedicationEvent.confirmed_at, MedicationEvent.terminal_state).where(
                MedicationEvent.reminder_id == reminder_id
            )
        )
    ).first()
    assert current is not None and current.terminal_state == "taken_reported"

    result = await medication_service.revoke_taken_report(
        session,
        elder_id=seeded["elder_id"],
        reminder_id=reminder_id,
        now=current.confirmed_at + datetime.timedelta(seconds=5),
        device_id=str(uuid.uuid4()),
    )
    assert result == {
        "reminder_id": reminder_id,
        "status": "waiting_confirm",
        "terminal_state": None,
    }

    row = (
        await session.execute(
            select(
                MedicationEvent.status,
                MedicationEvent.terminal_state,
                MedicationEvent.confirmed_at,
                MedicationEvent.confirm_channel,
                MedicationEvent.late_taken,
            ).where(MedicationEvent.reminder_id == reminder_id)
        )
    ).one()
    assert row.status == "waiting_confirm"
    assert row.terminal_state is None
    assert row.confirmed_at is None
    assert row.confirm_channel is None
    assert row.late_taken is False

    audits = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "medication_taken_report_revoked",
                    AuditLog.resource_id == reminder_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1, "撤销必须落一条审计"


async def test_revoke_after_window_rejected(session: AsyncSession, seeded: dict[str, Any]) -> None:
    reminder_id = await _confirmed_event(session, seeded)
    confirmed_at = (
        await session.execute(
            select(MedicationEvent.confirmed_at).where(MedicationEvent.reminder_id == reminder_id)
        )
    ).scalar_one()

    with pytest.raises(medication_service.MedicationUndoWindowExpired):
        await medication_service.revoke_taken_report(
            session,
            elder_id=seeded["elder_id"],
            reminder_id=reminder_id,
            now=confirmed_at + datetime.timedelta(seconds=11),
        )

    state = (
        await session.execute(
            select(MedicationEvent.terminal_state, MedicationEvent.confirmed_at).where(
                MedicationEvent.reminder_id == reminder_id
            )
        )
    ).one()
    assert state.terminal_state == "taken_reported", "超窗不得回滚终态"
    assert state.confirmed_at is not None


async def test_revoke_non_taken_terminal_rejected(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    reminder_id = await medication_service.create_event(
        session,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        scheduled_at=datetime.datetime(2026, 11, 11, 12, 0, tzinfo=UTC),
        drug_snapshot={"drug_name": "氨氯地平", "dose": "5mg 一片"},
        triggered_by="scheduler",
    )
    await medication_service.set_terminal(
        session, event_id=reminder_id, terminal_state="refused", confirm_channel="voice"
    )

    with pytest.raises(medication_service.MedicationUndoNotAllowed):
        await medication_service.revoke_taken_report(
            session, elder_id=seeded["elder_id"], reminder_id=reminder_id
        )


async def test_revoke_foreign_event_not_found(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    reminder_id = await _confirmed_event(session, seeded)
    with pytest.raises(medication_service.MedicationEventNotFound):
        await medication_service.revoke_taken_report(
            session, elder_id=uuid.uuid4(), reminder_id=reminder_id
        )
