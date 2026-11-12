"""普通确认撤销 REST 测试（Day 8 D3 · FR-UI-08 / AC-26）。

覆盖：401 无凭证／403 未绑定／200 十秒内撤销（状态与审计）／
409 超窗（UNDO_WINDOW_EXPIRED）／409 非已服终态／404 他人事件。

跑法与 `test_sos_api.py` 同构：同一连接、请求走真实 ASGI 链路、
`get_session` / `current_device` / `token_hash` 全真实，结束整体回滚。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.api import pad_undo as pad_undo_api
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import Device, Elder, Room
from app.models.audit import AuditLog
from app.models.medication import MedicationEvent, MedicationPlan
from app.models.staff import StaffAccount

TEST_JWT_SECRET = "pad-undo-test-secret-not-a-real-key-0123456789"
UTC = datetime.UTC

ROOM_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddd0021")
ELDER_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee21")
OTHER_ELDER = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee22")
DEVICE_BOUND = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffff21")
DEVICE_UNBOUND = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffff22")
STAFF_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaa21")
PLAN_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbb21")

SECRET_BOUND = "pad-undo-secret-bound-not-a-real-value"
SECRET_UNBOUND = "pad-undo-secret-unbound-not-a-real-value"


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    try:
        conn = await db_engine.connect()
    except Exception as exc:
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def client(db: AsyncConnection, jwt_env: None) -> AsyncIterator[AsyncClient]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(pad_undo_api.router)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    application.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest.fixture
async def seeded(db: AsyncConnection) -> None:
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="UNDO-101", status="full")
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": ELDER_ID, "name": "撤销接口长者", "status": "active"},
            {"elder_id": OTHER_ELDER, "name": "撤销他人长者", "status": "active"},
        ],
    )
    await db.execute(
        StaffAccount.__table__.insert().values(
            staff_id=STAFF_ID, name="撤销接口医生", role="doctor", password_hash="x" * 32
        )
    )
    await db.execute(
        MedicationPlan.__table__.insert().values(
            plan_id=PLAN_ID,
            elder_id=ELDER_ID,
            drug_name="氨氯地平",
            dose="5mg 一片",
            schedule_cron="0 8 * * *",
            start_date=datetime.date(2026, 11, 1),
            created_by=STAFF_ID,
        )
    )
    await db.execute(
        Device.__table__.insert(),
        [
            {
                "device_id": DEVICE_BOUND,
                "room_id": ROOM_ID,
                "elder_id": ELDER_ID,
                "token_hash": sha256_hex(SECRET_BOUND),
                "status": "online",
            },
            {
                "device_id": DEVICE_UNBOUND,
                "room_id": ROOM_ID,
                "elder_id": None,
                "token_hash": sha256_hex(SECRET_UNBOUND),
                "status": "online",
            },
        ],
    )


def _device_token(device_id: uuid.UUID, secret: str) -> str:
    return sign_device_session(device_id=device_id, token_hash_prefix=sha256_hex(secret)[:16])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _insert_event(
    db: AsyncConnection,
    *,
    reminder_id: uuid.UUID,
    elder_id: uuid.UUID = ELDER_ID,
    terminal_state: str = "taken_reported",
    confirmed_at: datetime.datetime | None = None,
) -> None:
    await db.execute(
        MedicationEvent.__table__.insert().values(
            reminder_id=reminder_id,
            plan_id=PLAN_ID,
            elder_id=elder_id,
            scheduled_at=datetime.datetime(2026, 11, 11, 8, 0, tzinfo=UTC),
            plan_version=1,
            drug_snapshot={"drug_name": "氨氯地平", "dose": "5mg 一片"},
            payload_hash="hash-" + str(reminder_id),
            triggered_by="scheduler",
            status="confirmed",
            terminal_state=terminal_state,
            late_taken=False,
            confirmed_at=confirmed_at or datetime.datetime.now(UTC),
            confirm_channel="voice",
        )
    )


async def test_revoke_requires_auth(client: AsyncClient, seeded: None) -> None:
    response = await client.post(f"/api/pad/medication-events/{uuid.uuid4()}/revoke")
    assert response.status_code == 401


async def test_revoke_unbound_device_is_403(client: AsyncClient, seeded: None) -> None:
    token = _device_token(DEVICE_UNBOUND, SECRET_UNBOUND)
    response = await client.post(
        f"/api/pad/medication-events/{uuid.uuid4()}/revoke", headers=_bearer(token)
    )
    assert response.status_code == 403
    assert response.json()["code"] == "device_not_bound"


async def test_revoke_within_window_ok(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    reminder_id = uuid.uuid4()
    await _insert_event(db, reminder_id=reminder_id)
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)

    response = await client.post(
        f"/api/pad/medication-events/{reminder_id}/revoke", headers=_bearer(token)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "waiting_confirm"
    assert body["terminal_state"] is None

    row = (
        await db.execute(
            select(
                MedicationEvent.__table__.c.status,
                MedicationEvent.__table__.c.terminal_state,
                MedicationEvent.__table__.c.confirmed_at,
            ).where(MedicationEvent.__table__.c.reminder_id == reminder_id)
        )
    ).one()
    assert row.status == "waiting_confirm"
    assert row.terminal_state is None
    assert row.confirmed_at is None

    audits = (
        (
            await db.execute(
                select(AuditLog.__table__.c.audit_id).where(
                    AuditLog.__table__.c.action == "medication_taken_report_revoked",
                    AuditLog.__table__.c.resource_id == reminder_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1


async def test_revoke_after_window_is_409(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    reminder_id = uuid.uuid4()
    await _insert_event(
        db,
        reminder_id=reminder_id,
        confirmed_at=datetime.datetime.now(UTC) - datetime.timedelta(seconds=11),
    )
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)

    response = await client.post(
        f"/api/pad/medication-events/{reminder_id}/revoke", headers=_bearer(token)
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "UNDO_WINDOW_EXPIRED"


async def test_revoke_non_taken_terminal_is_409(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    reminder_id = uuid.uuid4()
    await _insert_event(db, reminder_id=reminder_id, terminal_state="refused")
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)

    response = await client.post(
        f"/api/pad/medication-events/{reminder_id}/revoke", headers=_bearer(token)
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CONFLICT"


async def test_revoke_foreign_event_is_404(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    reminder_id = uuid.uuid4()
    await _insert_event(db, reminder_id=reminder_id, elder_id=OTHER_ELDER)
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)

    response = await client.post(
        f"/api/pad/medication-events/{reminder_id}/revoke", headers=_bearer(token)
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"
