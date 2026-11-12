"""Day 9 A4 · 生命周期 REST 端点测试（退房 / 教学重置 / away）。

覆盖：admin-only 403、二次确认 409、200 流水线响应、404、away 幂等；
service 层行为（审计行数/401/403/幂等）见 tests/test_lifecycle.py。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.security import sha256_hex, sign_staff_access
from app.db import get_session
from app.main import create_app
from app.models import Device, Elder, Room, StaffAccount

ROOM_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
ELDER_A = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
DEVICE_A = uuid.UUID("cccccccc-0000-0000-0000-000000000001")
DEVICE_SECRET_A = "device-a-long-term-secret"
CONFIRM_TOKEN = "CONFIRM"
STAFF_ADMIN = uuid.UUID("dddddddd-0000-0000-0000-000000000011")
STAFF_CARE = uuid.UUID("dddddddd-0000-0000-0000-000000000012")
TEST_JWT_SECRET = "lifecycle-api-secret-0123456789abcdef"


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with db_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            session.add_all(
                [
                    Room(room_id=ROOM_ID, room_no="B-101"),
                    Elder(elder_id=ELDER_A, name="张桂芬", status="active"),
                    StaffAccount(
                        staff_id=STAFF_ADMIN,
                        name="管理员",
                        role="admin",
                        phone="13700000011",
                        password_hash="unused-placeholder",
                        token_version=0,
                        status="active",
                    ),
                    StaffAccount(
                        staff_id=STAFF_CARE,
                        name="护工",
                        role="care_worker",
                        phone="13700000012",
                        password_hash="unused-placeholder",
                        token_version=0,
                        status="active",
                    ),
                ]
            )
            await session.flush()
            session.add(
                Device(
                    device_id=DEVICE_A,
                    room_id=ROOM_ID,
                    elder_id=ELDER_A,
                    token_hash=sha256_hex(DEVICE_SECRET_A),
                    status="online",
                )
            )
            await session.commit()
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    application: FastAPI = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as ac:
        yield ac
    application.dependency_overrides.clear()


def _admin_token() -> str:
    return sign_staff_access(kind="staff", staff_id=STAFF_ADMIN, role="admin", token_version=0)


def _care_token() -> str:
    return sign_staff_access(kind="staff", staff_id=STAFF_CARE, role="care_worker", token_version=0)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ 权限


async def test_lifecycle_endpoints_reject_non_admin(client: AsyncClient) -> None:
    """护工调生命周期端点 → 403 ROLE_FORBIDDEN（admin-only）。"""
    for path in (
        f"/api/lifecycle/elders/{ELDER_A}/checkout",
        f"/api/lifecycle/elders/{ELDER_A}/teaching-reset",
    ):
        resp = await client.post(
            path, json={"confirm_token": CONFIRM_TOKEN}, headers=_bearer(_care_token())
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "ROLE_FORBIDDEN"

    resp = await client.post(
        f"/api/lifecycle/elders/{ELDER_A}/away", json={"away": True}, headers=_bearer(_care_token())
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_FORBIDDEN"


# ------------------------------------------------------------------ 409 / 404


async def test_checkout_without_confirm_returns_409(client: AsyncClient) -> None:
    resp = await client.post(
        f"/api/lifecycle/elders/{ELDER_A}/checkout",
        json={"confirm_token": "wrong"},
        headers=_bearer(_admin_token()),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "CONFIRM_REQUIRED"


async def test_unknown_elder_returns_404(client: AsyncClient) -> None:
    resp = await client.post(
        f"/api/lifecycle/elders/{uuid.uuid4()}/teaching-reset",
        json={"confirm_token": CONFIRM_TOKEN},
        headers=_bearer(_admin_token()),
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


# ------------------------------------------------------------------ 200 流水线


async def test_checkout_via_api_returns_step_report(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    resp = await client.post(
        f"/api/lifecycle/elders/{ELDER_A}/checkout",
        json={"confirm_token": CONFIRM_TOKEN},
        headers=_bearer(_admin_token()),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["reason"] == "checkout"
    assert [s["step"] for s in body["steps"]] == [
        "revoke_tokens",
        "purge_voiceprints",
        "purge_memory",
        "archive_health",
        "unbind_devices",
    ]
    assert body["steps"][2]["stub"] is True, "桩步标记透传给前端（联调段后为 false）"

    elder = await db_session.get(Elder, ELDER_A)
    assert elder is not None and elder.status == "transferred"
    device = await db_session.get(Device, DEVICE_A)
    assert device is not None and device.elder_id is None


async def test_away_round_trip_via_api(client: AsyncClient, db_session: AsyncSession) -> None:
    resp = await client.post(
        f"/api/lifecycle/elders/{ELDER_A}/away",
        json={"away": True},
        headers=_bearer(_admin_token()),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["steps"][0]["step"] == "mark_away"

    elder = await db_session.get(Elder, ELDER_A)
    assert elder is not None and elder.status == "away"

    resp = await client.post(
        f"/api/lifecycle/elders/{ELDER_A}/away",
        json={"away": False},
        headers=_bearer(_admin_token()),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["steps"][0]["step"] == "resume"

    elder = await db_session.get(Elder, ELDER_A)
    assert elder is not None and elder.status == "active"
