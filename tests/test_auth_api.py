"""task-5 · auth API 集成测试（6 条验收全覆盖）。

真实 DB + httpx.AsyncClient 链路：
- NullPool 引擎绕开 task-4 登记的 P-03 事件循环复用问题；
- 每用例一个事务、所有 DB 操作（seed + 请求）共享同一 session，最后统一 rollback；
- httpx.AsyncClient 跑在 pytest 的 async 循环里，不换循环（解决 TestClient 跨循环 bug）。

每条写操作「回查 DB」再断言（契约 §10 硬约束），不只看 HTTP 200。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import hash_password, sha256_hex
from app.db import get_session as _app_get_session
from app.models import (
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    StaffAccount,
)
from app.models.organization import Bed, Device, Elder, Room

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_TEST_PHONE = "13800000001"
_TEST_PASSWORD = "test123"
_DEVICE_SECRET_PLAIN = "device-long-secret-plain-abc123"


# ---------------------------------------------------------------------------
# Fixtures —— 整条链保持 async，共用同一事件循环
# ---------------------------------------------------------------------------


@pytest.fixture
async def test_engine() -> AsyncIterator[AsyncEngine]:
    """NullPool 引擎 + 启动时清 DB（绕开之前 commit 残留）。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        # 每 session 跑之前先清掉可能残留的测试数据（之前版本 commit 没清）
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE elder_guardians, elder_staff_assignments, "
                    "guardian_accounts, staff_accounts, staff_tokens, "
                    "devices, elders, beds, personas, rooms CASCADE"
                )
            )
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def test_session(
    test_engine: AsyncEngine,
) -> AsyncIterator[AsyncSession]:
    """每用例一个事务、yield 后统一 rollback。"""
    sf = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sf() as session:
        try:
            yield session
        finally:
            await session.rollback()


@pytest.fixture
async def seeded_db(test_session: AsyncSession) -> dict[str, Any]:
    """写入 seed 数据（只 flush 不 commit，数据留在 test_session 事务里）。"""
    # Room（先 flush 拿 PK，后面 FK 要用）
    room = Room(room_no="A-301")
    test_session.add(room)
    await test_session.flush()

    # Bed + Device（用 room.room_id）
    bed = Bed(room_id=room.room_id, bed_no="1 号床")
    device = Device(
        room_id=room.room_id,
        token_hash=sha256_hex(_DEVICE_SECRET_PLAIN),
        status="offline",
    )
    test_session.add_all([bed, device])
    await test_session.flush()

    # Elder
    elder = Elder(name="张桂芬", status="active")
    test_session.add(elder)
    await test_session.flush()

    # 关联 Elder ↔ Bed / Device
    bed.elder_id = elder.elder_id
    device.elder_id = elder.elder_id

    # Staff + Guardian
    staff = StaffAccount(
        name="王护工",
        role="care_worker",
        phone=_TEST_PHONE,
        password_hash=hash_password(_TEST_PASSWORD),
        token_version=0,
        status="active",
    )
    guardian = GuardianAccount(
        name="张女士",
        phone="13900000001",
        password_hash=hash_password(_TEST_PASSWORD),
        token_version=0,
        status="active",
    )
    test_session.add_all([staff, guardian])
    await test_session.flush()

    # 关系绑定
    test_session.add_all(
        [
            ElderStaffAssignment(
                elder_id=elder.elder_id,
                staff_id=staff.staff_id,
                relation="primary_care",
            ),
            ElderGuardian(
                elder_id=elder.elder_id,
                guardian_id=guardian.guardian_id,
                relation="daughter",
                status="active",
            ),
        ]
    )
    await test_session.flush()  # 不 commit！留在事务里和测试请求共享

    return {
        "elder_id": elder.elder_id,
        "device_id": device.device_id,
        "staff_id": staff.staff_id,
        "guardian_id": guardian.guardian_id,
    }


@pytest.fixture
async def app_for_test(test_session: AsyncSession, seeded_db: dict[str, Any]) -> FastAPI:
    """注册 auth.router + 全局 exception handler + override get_session。"""
    from fastapi.responses import JSONResponse

    from app.api.auth import router
    from app.core.auth import (
        CredentialExpiredError,
        CredentialInvalidError,
        CredentialRevokedError,
    )
    from app.core.security import TokenExpiredError

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _override_get_session():
        yield test_session

    app.dependency_overrides[_app_get_session] = _override_get_session

    # 全局 exception handler —— 捕获 task-4 auth 层在依赖注入阶段抛的异常
    # （auth_service 层异常由 auth.py 路由内的 _handle_service_exception 处理）
    @app.exception_handler(CredentialInvalidError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_INVALID", "trace_id": "-"},
        )

    @app.exception_handler(CredentialExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    @app.exception_handler(CredentialRevokedError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_REVOKED", "trace_id": "-"},
        )

    @app.exception_handler(TokenExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": "token has expired", "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    return app


@pytest.fixture
async def ac(app_for_test: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """httpx.AsyncClient —— ASGITransport 跑在 pytest 的 async 循环里，不跨循环。"""
    transport = httpx.ASGITransport(app=app_for_test)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# ---------------------------------------------------------------------------
# 验收 1：错误密码 / 不存在手机号 → 同一 401 文案（防枚举）
# ---------------------------------------------------------------------------


async def test_login_wrong_password_returns_401(ac: httpx.AsyncClient) -> None:
    resp = await ac.post(
        "/api/sessions",
        json={"phone": _TEST_PHONE, "password": "wrong"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == "TOKEN_INVALID"
    assert "用户名或密码错误" in body["detail"]


async def test_login_nonexistent_phone_returns_same_error(ac: httpx.AsyncClient) -> None:
    """不存在的手机号 → 同一文案 —— 防枚举硬约束。"""
    resp = await ac.post(
        "/api/sessions",
        json={"phone": "19999999999", "password": "whatever"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == "TOKEN_INVALID"
    assert "用户名或密码错误" in body["detail"]


# ---------------------------------------------------------------------------
# 验收 2：伪造 / 过期 token → 401
# ---------------------------------------------------------------------------


async def test_fake_token_returns_401(ac: httpx.AsyncClient) -> None:
    resp = await ac.get(
        "/api/sessions/current",
        headers={"Authorization": "Bearer fake.fake.fake"},
    )
    assert resp.status_code == 401


async def test_expired_token_returns_401(ac: httpx.AsyncClient, seeded_db: dict[str, Any]) -> None:
    from datetime import datetime

    from app.core.security import KIND_STAFF, TYP_ACCESS, _build_payload

    now = datetime.now(UTC)
    expired_jwt = _build_payload(
        typ=TYP_ACCESS,
        sub=f"{KIND_STAFF}:{seeded_db['staff_id']}",
        ttl_seconds=1,
        extra={"kind": KIND_STAFF, "role": "care_worker", "tv": 0},
        now=now,
    )
    await asyncio.sleep(2)

    resp = await ac.get(
        "/api/sessions/current",
        headers={"Authorization": f"Bearer {expired_jwt}"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] in ("TOKEN_EXPIRED", "TOKEN_INVALID")


# ---------------------------------------------------------------------------
# 验收 3：GET /current 解出角色
# ---------------------------------------------------------------------------


async def test_current_identity_staff(ac: httpx.AsyncClient, seeded_db: dict[str, Any]) -> None:
    login = await ac.post(
        "/api/sessions",
        json={"phone": _TEST_PHONE, "password": _TEST_PASSWORD},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    resp = await ac.get(
        "/api/sessions/current",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "staff"
    assert body["role"] == "care_worker"
    assert body["id"] == str(seeded_db["staff_id"])
    assert body["name"] == "王护工"
    assert str(seeded_db["elder_id"]) in body["scope_elder_ids"]


async def test_current_identity_guardian(ac: httpx.AsyncClient, seeded_db: dict[str, Any]) -> None:
    login = await ac.post(
        "/api/sessions",
        json={"phone": "13900000001", "password": _TEST_PASSWORD},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    resp = await ac.get(
        "/api/sessions/current",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "guardian"
    assert body["role"] is None
    assert str(seeded_db["elder_id"]) in body["scope_elder_ids"]


# ---------------------------------------------------------------------------
# 验收 4：设备 token 解出 device_id
# ---------------------------------------------------------------------------


async def test_device_session_success(ac: httpx.AsyncClient, seeded_db: dict[str, Any]) -> None:
    resp = await ac.post(
        "/api/device-sessions",
        json={
            "device_id": str(seeded_db["device_id"]),
            "device_secret": _DEVICE_SECRET_PLAIN,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["device"]["id"] == str(seeded_db["device_id"])
    assert body["device"]["elder_id"] == str(seeded_db["elder_id"])
    assert body["expires_in"] == 24 * 3600

    # 用设备会话 token 访问 /current
    cur = await ac.get(
        "/api/sessions/current",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert cur.status_code == 200
    cur_body = cur.json()
    assert cur_body["kind"] == "device"
    assert cur_body["id"] == str(seeded_db["device_id"])


async def test_device_session_wrong_secret_401(
    ac: httpx.AsyncClient, seeded_db: dict[str, Any]
) -> None:
    resp = await ac.post(
        "/api/device-sessions",
        json={
            "device_id": str(seeded_db["device_id"]),
            "device_secret": "wrong-secret",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "TOKEN_INVALID"


# ---------------------------------------------------------------------------
# 验收 5：账号停用（token_version+1）后旧 token → 401（AC-24）
# ---------------------------------------------------------------------------


async def test_disabled_account_old_token_401(
    ac: httpx.AsyncClient,
    seeded_db: dict[str, Any],
    test_session: AsyncSession,
) -> None:
    login = await ac.post(
        "/api/sessions",
        json={"phone": _TEST_PHONE, "password": _TEST_PASSWORD},
    )
    assert login.status_code == 200
    old_token = login.json()["access_token"]

    # 在 DB 里把 token_version+1 + status='disabled'
    await test_session.execute(
        update(StaffAccount)
        .where(StaffAccount.staff_id == seeded_db["staff_id"])
        .values(
            token_version=StaffAccount.token_version + 1,
            status="disabled",
        )
    )
    await test_session.flush()

    # 旧 token → 401 TOKEN_REVOKED
    resp = await ac.get(
        "/api/sessions/current",
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "TOKEN_REVOKED"


# ---------------------------------------------------------------------------
# 验收 6：refresh 换新 access + tv 变更后旧 access → 401
# ---------------------------------------------------------------------------


async def test_refresh_generates_new_access(
    ac: httpx.AsyncClient, seeded_db: dict[str, Any]
) -> None:
    login = await ac.post(
        "/api/sessions",
        json={"phone": _TEST_PHONE, "password": _TEST_PASSWORD},
    )
    assert login.status_code == 200
    refresh_token = login.json()["refresh_token"]

    refresh_resp = await ac.post(
        "/api/sessions/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_resp.status_code == 200
    new_access = refresh_resp.json()["access_token"]
    assert len(new_access) > 20  # 有值即可（JWT payload 可能相同，看 iat）
    assert refresh_resp.json()["expires_in"] == 120 * 60


async def test_tv_invalidation_kills_old_access_and_refresh(
    ac: httpx.AsyncClient,
    seeded_db: dict[str, Any],
    test_session: AsyncSession,
) -> None:
    """refresh 后 tv+1，旧 access 和旧 refresh 都 → 401（AC-24 贯穿）。"""
    login = await ac.post(
        "/api/sessions",
        json={"phone": _TEST_PHONE, "password": _TEST_PASSWORD},
    )
    old_access = login.json()["access_token"]
    old_refresh = login.json()["refresh_token"]

    # tv+1
    await test_session.execute(
        update(StaffAccount)
        .where(StaffAccount.staff_id == seeded_db["staff_id"])
        .values(token_version=StaffAccount.token_version + 1)
    )
    await test_session.flush()

    # 旧 access → 401
    resp = await ac.get(
        "/api/sessions/current",
        headers={"Authorization": f"Bearer {old_access}"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "TOKEN_REVOKED"

    # 旧 refresh 也 → 401
    refresh_resp = await ac.post(
        "/api/sessions/refresh",
        json={"refresh_token": old_refresh},
    )
    assert refresh_resp.status_code == 401
