"""建号接口 · 家属侧（G1 `POST /api/guardians`、E13 绑定）的接口级测试。

覆盖：
- 鉴权：无 token 401、非 admin 403；
- 建家属账号成功且手机号脱敏；跨表重号 409；
- 绑定成功；长者不存在 / 家属不存在各一条 404；
- 重复绑定 409；第三位 active 监护人 409（≤2 位是服务层校验，不落 DDL）；
- `unbound` 的旧绑定不占名额（能再绑一位）。

跑法与 `test_staff_api.py` 同构：conftest 共享引擎 + 每用例事务回滚 + 真实 ASGI 链路。
无 PostgreSQL 时 `pytest.skip` 并打印原因。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
)

from app.api import guardians as guardians_api
from app.core.config import get_settings
from app.core.security import hash_password, sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import Elder, ElderGuardian, GuardianAccount, StaffAccount

TEST_JWT_SECRET = "guardians-api-test-secret-not-a-real-key-0123456789abcdef"

ADMIN_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
WORKER_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbc")
ELDER_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2")
G1 = uuid.UUID("cccccccc-cccc-4ccc-8ccc-ccccccccccc1")
G2 = uuid.UUID("cccccccc-cccc-4ccc-8ccc-ccccccccccc2")
G3 = uuid.UUID("cccccccc-cccc-4ccc-8ccc-ccccccccccc3")
G4 = uuid.UUID("cccccccc-cccc-4ccc-8ccc-ccccccccccc4")

ADMIN_PHONE = "13800000001"

# 阶段 A 提速：seeded 夹具 function 级、每用例重复 7 次 cost-12 哈希（约 2s/用例），
# 模块级预计算一次全文件复用；bcrypt 随机盐不影响 verify_password 对同一明文的校验。
_HASH_ADMIN = hash_password("Admin@2026")
_HASH_WORKER = hash_password("Care@2026")
_HASH_G1 = hash_password("Fam@2026aa")
_HASH_G2 = hash_password("Fam@2026bb")
_HASH_G3 = hash_password("Fam@2026cc")
_HASH_G4 = hash_password("Fam@2026dd")


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """每用例一个事务、结束回滚（db_engine 为 conftest 的 module 级共享引擎）。"""
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
async def seeded(db: AsyncConnection) -> AsyncConnection:
    """一位 admin、一位护工、一位长者、三位家属（G1/G2 已绑定，G3 未绑定，G4 空闲）。"""
    session = AsyncSession(
        bind=db, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    session.add_all(
        [
            StaffAccount(
                staff_id=ADMIN_ID,
                name="测试管理员",
                role="admin",
                phone=ADMIN_PHONE,
                password_hash=_HASH_ADMIN,
                status="active",
            ),
            StaffAccount(
                staff_id=WORKER_ID,
                name="测试护工",
                role="care_worker",
                phone="13800000002",
                password_hash=_HASH_WORKER,
                status="active",
            ),
            Elder(elder_id=ELDER_ID, name="测试长者", status="active"),
            GuardianAccount(
                guardian_id=G1,
                name="家属一",
                phone="13900000001",
                password_hash=_HASH_G1,
                status="active",
            ),
            GuardianAccount(
                guardian_id=G2,
                name="家属二",
                phone="13900000002",
                password_hash=_HASH_G2,
                status="active",
            ),
            GuardianAccount(
                guardian_id=G3,
                name="家属三",
                phone="13900000003",
                password_hash=_HASH_G3,
                status="active",
            ),
            GuardianAccount(
                guardian_id=G4,
                name="家属四",
                phone="13900000004",
                password_hash=_HASH_G4,
                status="active",
            ),
        ]
    )
    await session.commit()
    return db


@pytest.fixture
async def client(seeded: AsyncConnection, jwt_env: None) -> AsyncIterator[AsyncClient]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(guardians_api.router)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=seeded,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    application.dependency_overrides[get_session] = override_get_session

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _token(role: str, staff_id: uuid.UUID) -> str:
    return sign_staff_access(kind="staff", staff_id=str(staff_id), role=role, token_version=0)


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token('admin', ADMIN_ID)}"}


def _worker_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token('care_worker', WORKER_ID)}"}


async def _bind(client: AsyncClient, guardian_id: uuid.UUID, relation: str = "子") -> int:
    resp = await client.post(
        f"/api/elders/{ELDER_ID}/guardians",
        json={"guardian_id": str(guardian_id), "relation": relation},
        headers=_admin_headers(),
    )
    return resp.status_code


# ---------------------------------------------------------------- 鉴权


async def test_create_guardian_requires_token(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/guardians",
        json={"name": "家属五", "phone": "13900000005", "password": "Fam@2026ee"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "TOKEN_INVALID"


async def test_create_guardian_forbidden_for_non_admin(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/guardians",
        json={"name": "家属五", "phone": "13900000005", "password": "Fam@2026ee"},
        headers=_worker_headers(),
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_FORBIDDEN"


# ---------------------------------------------------------------- 建家属账号


async def test_create_guardian_succeeds(client: AsyncClient, seeded: AsyncConnection) -> None:
    resp = await client.post(
        "/api/guardians",
        json={"name": "家属五", "phone": "13900000005", "password": "Fam@2026ee"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "家属五"
    assert body["status"] == "active"
    assert body["phone_masked"] == "139****0005"
    assert "13900000005" not in resp.text

    session = AsyncSession(bind=seeded, expire_on_commit=False)
    row = (
        await session.execute(select(GuardianAccount).where(GuardianAccount.phone == "13900000005"))
    ).scalar_one()
    assert row.password_hash != "Fam@2026ee"


async def test_create_guardian_duplicate_phone_409(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/guardians",
        json={"name": "撞号", "phone": ADMIN_PHONE, "password": "Fam@2026ff"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"


# ---------------------------------------------------------------- 绑定


async def test_bind_first_guardian_succeeds(client: AsyncClient, seeded: AsyncConnection) -> None:
    resp = await client.post(
        f"/api/elders/{ELDER_ID}/guardians",
        json={"guardian_id": str(G1), "relation": "女儿"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["elder_id"] == str(ELDER_ID)
    assert body["guardian_id"] == str(G1)
    assert body["relation"] == "女儿"
    assert body["status"] == "active"

    session = AsyncSession(bind=seeded, expire_on_commit=False)
    row = (
        await session.execute(select(ElderGuardian).where(ElderGuardian.guardian_id == G1))
    ).scalar_one()
    assert row.elder_id == ELDER_ID


async def test_bind_unknown_elder_404(client: AsyncClient) -> None:
    resp = await client.post(
        f"/api/elders/{uuid.uuid4()}/guardians",
        json={"guardian_id": str(G1), "relation": "女儿"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 404


async def test_bind_unknown_guardian_404(client: AsyncClient) -> None:
    resp = await client.post(
        f"/api/elders/{ELDER_ID}/guardians",
        json={"guardian_id": str(uuid.uuid4()), "relation": "女儿"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 404


async def test_bind_twice_409(client: AsyncClient) -> None:
    assert await _bind(client, G1) == 201
    assert await _bind(client, G1) == 409


async def test_third_active_guardian_409(client: AsyncClient) -> None:
    assert await _bind(client, G1) == 201
    assert await _bind(client, G2) == 201
    assert await _bind(client, G3) == 409


async def test_unbound_does_not_consume_quota(client: AsyncClient, seeded: AsyncConnection) -> None:
    """先绑两位再解绑一位：名额应释放，第三位能绑上。"""
    assert await _bind(client, G1) == 201
    assert await _bind(client, G2) == 201

    session = AsyncSession(
        bind=seeded, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    row = (
        await session.execute(select(ElderGuardian).where(ElderGuardian.guardian_id == G1))
    ).scalar_one()
    row.status = "unbound"
    await session.commit()

    assert await _bind(client, G3) == 201


async def test_bind_requires_token(client: AsyncClient) -> None:
    resp = await client.post(
        f"/api/elders/{ELDER_ID}/guardians",
        json={"guardian_id": str(G1), "relation": "女儿"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------- P1 修复：disabled guardian 不能绑定


async def test_bind_disabled_guardian_returns_409(
    client: AsyncClient, seeded: AsyncConnection
) -> None:
    """disabled 状态的家属账号不能新增绑定。"""
    session = AsyncSession(
        bind=seeded, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    row = (
        await session.execute(
            select(GuardianAccount).where(GuardianAccount.guardian_id == G4)
        )
    ).scalar_one()
    row.status = "disabled"
    await session.commit()

    resp = await client.post(
        f"/api/elders/{ELDER_ID}/guardians",
        json={"guardian_id": str(G4), "relation": "儿子"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"
