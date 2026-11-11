"""建号接口 · 员工侧（E12 `POST/PATCH /api/staff`）的接口级测试。

覆盖：
- 鉴权三态：无 token / 伪造 token / 非 admin 角色的 403；
- 建号成功、手机号跨两表唯一（员工表、家属表各一条冲突用例）；
- 口令只落哈希（`password_hash` 不是明文，且 `verify_password` 能验）；
- 医生建号时同时写 `elder_staff_assignments`；管理员绑长者被拒（裁决 D-01）；
- 长者不存在 → 404；短口令 / 坏手机号 → 422；
- 改角色与停用都让 `token_version + 1`（AC-24 凭证即时失效）；无变更时不加。

跑法与 `test_pad_avatar.py` 同构：conftest 共享引擎、每用例一个事务回滚、
请求走真实 ASGI 链路（`get_session` / `current_staff` / `role` 全真实）。
无 PostgreSQL 时 `pytest.skip` 并打印原因，绝不静默通过。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
)

from app.api import staff as staff_api
from app.core.config import get_settings
from app.core.security import hash_password, sign_staff_access, verify_password
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import Elder, ElderStaffAssignment, GuardianAccount, StaffAccount

TEST_JWT_SECRET = "staff-api-test-secret-not-a-real-key-0123456789abcdef"

ADMIN_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CAREWORKER_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab")
ELDER_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1")

ADMIN_PHONE = "13700000001"
CAREWORKER_PHONE = "13700000002"
NEW_PHONE = "13700000003"

# 阶段 A 提速：seeded 夹具 function 级，每用例重复 cost-12 哈希（约 0.6s/用例），
# 模块级预计算一次全文件复用；bcrypt 随机盐不影响 verify_password 对同一明文的校验。
_HASH_ADMIN = hash_password("Admin@2026")
_HASH_CAREWORKER = hash_password("Care@2026")
_HASH_FAM_XX = hash_password("Fam@2026xx")


# ---------------------------------------------------------------- 夹具


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
    """插一位 admin、一位 care_worker、一位长者（全部随事务回滚）。"""
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
                staff_id=CAREWORKER_ID,
                name="测试护工",
                role="care_worker",
                phone=CAREWORKER_PHONE,
                password_hash=_HASH_CAREWORKER,
                status="active",
            ),
            Elder(elder_id=ELDER_ID, name="测试长者", status="active"),
        ]
    )
    await session.commit()
    return db


@pytest.fixture
async def client(seeded: AsyncConnection, jwt_env: None) -> AsyncIterator[AsyncClient]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(staff_api.router)

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
    return {"Authorization": f"Bearer {_token('care_worker', CAREWORKER_ID)}"}


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "新护工",
        "role": "care_worker",
        "phone": NEW_PHONE,
        "password": "NewCare@2026",
        "elder_ids": [],
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------- 鉴权


async def test_missing_token_returns_401(client: AsyncClient) -> None:
    resp = await client.post("/api/staff", json=_payload())
    assert resp.status_code == 401
    assert resp.json()["code"] == "TOKEN_INVALID"


async def test_forged_token_returns_401(client: AsyncClient) -> None:
    headers = {"Authorization": "Bearer not-a-real-jwt"}
    resp = await client.post("/api/staff", json=_payload(), headers=headers)
    assert resp.status_code == 401


async def test_non_admin_role_returns_403(client: AsyncClient) -> None:
    resp = await client.post("/api/staff", json=_payload(), headers=_worker_headers())
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_FORBIDDEN"


# ---------------------------------------------------------------- 建号


async def test_create_care_worker_succeeds(client: AsyncClient, seeded: AsyncConnection) -> None:
    resp = await client.post("/api/staff", json=_payload(), headers=_admin_headers())
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "care_worker"
    assert body["status"] == "active"
    assert body["name"] == "新护工"
    # 手机号必须脱敏，不能把完整号码回给前端
    assert body["phone_masked"] == "137****0003"
    assert NEW_PHONE not in resp.text

    session = AsyncSession(bind=seeded, expire_on_commit=False)
    row = (
        await session.execute(select(StaffAccount).where(StaffAccount.phone == NEW_PHONE))
    ).scalar_one()
    assert row.password_hash != "NewCare@2026"
    assert verify_password("NewCare@2026", row.password_hash) is True


async def test_duplicate_phone_against_staff_returns_409(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/staff", json=_payload(phone=ADMIN_PHONE), headers=_admin_headers()
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"


async def test_duplicate_phone_against_guardian_returns_409(
    client: AsyncClient, seeded: AsyncConnection
) -> None:
    session = AsyncSession(
        bind=seeded, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    session.add(
        GuardianAccount(
            name="已有家属",
            phone="13700000009",
            password_hash=_HASH_FAM_XX,
            status="active",
        )
    )
    await session.commit()

    resp = await client.post(
        "/api/staff", json=_payload(phone="13700000009"), headers=_admin_headers()
    )
    assert resp.status_code == 409


async def test_create_doctor_assigns_elder(client: AsyncClient, seeded: AsyncConnection) -> None:
    resp = await client.post(
        "/api/staff",
        json=_payload(role="doctor", phone="13700000004", elder_ids=[str(ELDER_ID)]),
        headers=_admin_headers(),
    )
    assert resp.status_code == 201
    staff_id = resp.json()["staff_id"]

    session = AsyncSession(bind=seeded, expire_on_commit=False)
    rows = list(
        (
            await session.execute(
                select(ElderStaffAssignment).where(
                    ElderStaffAssignment.staff_id == uuid.UUID(staff_id)
                )
            )
        ).scalars()
    )
    assert len(rows) == 1
    assert rows[0].elder_id == ELDER_ID
    assert rows[0].relation == "doctor"


async def test_admin_cannot_bind_elder(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/staff",
        json=_payload(role="admin", phone="13700000005", elder_ids=[str(ELDER_ID)]),
        headers=_admin_headers(),
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_FORBIDDEN"


async def test_unknown_elder_returns_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/staff",
        json=_payload(role="doctor", phone="13700000006", elder_ids=[str(uuid.uuid4())]),
        headers=_admin_headers(),
    )
    assert resp.status_code == 404


async def test_short_password_returns_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/staff", json=_payload(password="short"), headers=_admin_headers()
    )
    assert resp.status_code == 422


async def test_bad_phone_returns_422(client: AsyncClient) -> None:
    resp = await client.post("/api/staff", json=_payload(phone="123"), headers=_admin_headers())
    assert resp.status_code == 422


# ---------------------------------------------------------------- 改角色 / 停用


async def test_update_role_bumps_token_version(
    client: AsyncClient, seeded: AsyncConnection
) -> None:
    resp = await client.patch(
        f"/api/staff/{CAREWORKER_ID}", json={"role": "doctor"}, headers=_admin_headers()
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "doctor"

    session = AsyncSession(bind=seeded, expire_on_commit=False)
    row = (
        await session.execute(select(StaffAccount).where(StaffAccount.staff_id == CAREWORKER_ID))
    ).scalar_one()
    assert row.token_version == 1


async def test_disable_bumps_token_version(client: AsyncClient, seeded: AsyncConnection) -> None:
    resp = await client.patch(
        f"/api/staff/{CAREWORKER_ID}", json={"status": "disabled"}, headers=_admin_headers()
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"

    session = AsyncSession(bind=seeded, expire_on_commit=False)
    row = (
        await session.execute(select(StaffAccount).where(StaffAccount.staff_id == CAREWORKER_ID))
    ).scalar_one()
    assert row.token_version == 1


async def test_noop_update_does_not_bump(client: AsyncClient, seeded: AsyncConnection) -> None:
    resp = await client.patch(f"/api/staff/{CAREWORKER_ID}", json={}, headers=_admin_headers())
    assert resp.status_code == 200

    session = AsyncSession(bind=seeded, expire_on_commit=False)
    row = (
        await session.execute(select(StaffAccount).where(StaffAccount.staff_id == CAREWORKER_ID))
    ).scalar_one()
    assert row.token_version == 0


async def test_update_unknown_staff_returns_404(client: AsyncClient) -> None:
    resp = await client.patch(
        f"/api/staff/{uuid.uuid4()}", json={"status": "disabled"}, headers=_admin_headers()
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------- P1 修复：角色变更联动 assignment.relation


async def test_update_role_syncs_assignment_relation_doctor_to_worker(
    client: AsyncClient, seeded: AsyncConnection
) -> None:
    """doctor 改 care_worker → ElderStaffAssignment.relation 从 'doctor' 变 'primary_care'。"""
    # 先建一个有 assignment 的 doctor
    create_resp = await client.post(
        "/api/staff",
        json=_payload(role="doctor", phone="13700000010", elder_ids=[str(ELDER_ID)]),
        headers=_admin_headers(),
    )
    assert create_resp.status_code == 201
    new_staff_id = uuid.UUID(create_resp.json()["staff_id"])

    # 改角色
    resp = await client.patch(
        f"/api/staff/{new_staff_id}", json={"role": "care_worker"}, headers=_admin_headers()
    )
    assert resp.status_code == 200

    # 验证 assignment.relation 同步
    session = AsyncSession(bind=seeded, expire_on_commit=False)
    row = (
        await session.execute(
            select(ElderStaffAssignment).where(ElderStaffAssignment.staff_id == new_staff_id)
        )
    ).scalar_one()
    assert row.relation == "primary_care"


async def test_update_role_syncs_assignment_relation_worker_to_doctor(
    client: AsyncClient, seeded: AsyncConnection
) -> None:
    """care_worker 改 doctor → relation 从 'primary_care' 变 'doctor'。"""
    create_resp = await client.post(
        "/api/staff",
        json=_payload(role="care_worker", phone="13700000011", elder_ids=[str(ELDER_ID)]),
        headers=_admin_headers(),
    )
    assert create_resp.status_code == 201
    new_staff_id = uuid.UUID(create_resp.json()["staff_id"])

    resp = await client.patch(
        f"/api/staff/{new_staff_id}", json={"role": "doctor"}, headers=_admin_headers()
    )
    assert resp.status_code == 200

    session = AsyncSession(bind=seeded, expire_on_commit=False)
    row = (
        await session.execute(
            select(ElderStaffAssignment).where(ElderStaffAssignment.staff_id == new_staff_id)
        )
    ).scalar_one()
    assert row.relation == "doctor"


async def test_update_role_noop_no_sync(client: AsyncClient, seeded: AsyncConnection) -> None:
    """同值 PATCH 不加 token_version 也不碰 assignment（幂等分支）。"""
    create_resp = await client.post(
        "/api/staff",
        json=_payload(role="doctor", phone="13700000012", elder_ids=[str(ELDER_ID)]),
        headers=_admin_headers(),
    )
    new_staff_id = uuid.UUID(create_resp.json()["staff_id"])

    # 传同值
    resp = await client.patch(
        f"/api/staff/{new_staff_id}", json={"role": "doctor"}, headers=_admin_headers()
    )
    assert resp.status_code == 200

    session = AsyncSession(bind=seeded, expire_on_commit=False)
    staff = (
        await session.execute(select(StaffAccount).where(StaffAccount.staff_id == new_staff_id))
    ).scalar_one()
    assert staff.token_version == 0


# ---------------------------------------------------------------- 改绑 PUT /api/staff/{staff_id}/elders（E20 · N19）


async def test_reassign_no_token_401(client: AsyncClient) -> None:
    """无凭证 → 401 TOKEN_INVALID。"""
    resp = await client.put(
        f"/api/staff/{CAREWORKER_ID}/elders", json={"elder_ids": [str(ELDER_ID)]}
    )
    assert resp.status_code == 401


async def test_reassign_non_admin_403(client: AsyncClient) -> None:
    """护工 token 调改绑 → 403 ROLE_FORBIDDEN（admin only）。"""
    resp = await client.put(
        f"/api/staff/{CAREWORKER_ID}/elders",
        json={"elder_ids": [str(ELDER_ID)]},
        headers=_worker_headers(),
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_FORBIDDEN"


async def test_reassign_staff_not_found_404(client: AsyncClient) -> None:
    """目标 staff 不存在 → 404 NOT_FOUND。"""
    resp = await client.put(
        f"/api/staff/{uuid.uuid4()}/elders",
        json={"elder_ids": []},
        headers=_admin_headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_reassign_admin_target_403(client: AsyncClient) -> None:
    """目标 staff 是 admin → 403 ROLE_FORBIDDEN（裁决 D-01）。"""
    resp = await client.put(
        f"/api/staff/{ADMIN_ID}/elders",
        json={"elder_ids": [str(ELDER_ID)]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_FORBIDDEN"


async def test_reassign_unknown_elder_404(client: AsyncClient) -> None:
    """请求体里有不存在的 elder → 404 NOT_FOUND。"""
    ghost = uuid.uuid4()
    resp = await client.put(
        f"/api/staff/{CAREWORKER_ID}/elders",
        json={"elder_ids": [str(ghost)]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_reassign_success_add_new(client: AsyncClient, seeded: AsyncConnection) -> None:
    """CAREWORKER_ID 当前无 assignment → PUT [ELDER_ID] → added=[ELDER_ID]。"""
    resp = await client.put(
        f"/api/staff/{CAREWORKER_ID}/elders",
        json={"elder_ids": [str(ELDER_ID)]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["staff_id"] == str(CAREWORKER_ID)
    assert body["added"] == [str(ELDER_ID)]
    assert body["removed"] == []
    # trace_id 在响应 header 里（成功响应体是 Pydantic 模型，不带 trace_id 字段）
    assert "x-trace-id" in {k.lower() for k in resp.headers}

    # DB 验证：确实写入了 assignment
    session = AsyncSession(bind=seeded, expire_on_commit=False)
    rows = (
        (
            await session.execute(
                select(ElderStaffAssignment).where(ElderStaffAssignment.staff_id == CAREWORKER_ID)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].elder_id == ELDER_ID
    assert rows[0].relation == "primary_care"


async def test_reassign_success_diff_remove(client: AsyncClient, seeded: AsyncConnection) -> None:
    """已有 [ELDER_A, ELDER_B] → 只留 [ELDER_B] → removed=[ELDER_A]。

    关键前置：ELDER_A 必须有至少一位其他 primary_care，否则移 CAREWORKER_ID 会触发 409。
    """
    elder_b = Elder(
        elder_id=uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"),
        name="长者乙",
        status="active",
    )
    # 再造一个 staff 当 ELDER_ID 的兜底 primary_care
    backup = StaffAccount(
        staff_id=uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"),
        name="备用护工",
        role="care_worker",
        phone="13700000020",
        password_hash="$2b$12$placeholderplaceholderplaceholderplaceholder",
        status="active",
        token_version=0,
    )
    sess = AsyncSession(
        bind=seeded, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    # 父表先 flush，再插子表（AGENTS：多表带外键必须父表先 flush）
    sess.add_all([elder_b, backup])
    await sess.flush()
    sess.add_all(
        [
            ElderStaffAssignment(
                elder_id=ELDER_ID, staff_id=CAREWORKER_ID, relation="primary_care"
            ),
            ElderStaffAssignment(
                elder_id=elder_b.elder_id, staff_id=CAREWORKER_ID, relation="primary_care"
            ),
            # 兜底：backup 也是 ELDER_ID 的 primary_care，这样 CAREWORKER_ID 移除后不触发 gap
            ElderStaffAssignment(
                elder_id=ELDER_ID, staff_id=backup.staff_id, relation="primary_care"
            ),
        ]
    )
    await sess.commit()
    await sess.close()

    # 只保留 ELDER_B → ELDER_ID 应出现在 removed 里
    resp = await client.put(
        f"/api/staff/{CAREWORKER_ID}/elders",
        json={"elder_ids": [str(elder_b.elder_id)]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["staff_id"] == str(CAREWORKER_ID)
    assert body["added"] == []
    assert body["removed"] == [str(ELDER_ID)]

    # DB 验证：CAREWORKER_ID 只剩 ELDER_B
    session = AsyncSession(bind=seeded, expire_on_commit=False)
    rows = (
        (
            await session.execute(
                select(ElderStaffAssignment.elder_id).where(
                    ElderStaffAssignment.staff_id == CAREWORKER_ID
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == [elder_b.elder_id]


async def test_reassign_primary_care_gap_409(client: AsyncClient, seeded: AsyncConnection) -> None:
    """CAREWORKER_ID 是 ELDER_ID 唯一的 primary_care → 清空绑定 → 409 CONFLICT。"""
    sess = AsyncSession(
        bind=seeded, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    sess.add(
        ElderStaffAssignment(elder_id=ELDER_ID, staff_id=CAREWORKER_ID, relation="primary_care")
    )
    await sess.commit()
    await sess.close()

    resp = await client.put(
        f"/api/staff/{CAREWORKER_ID}/elders",
        json={"elder_ids": []},
        headers=_admin_headers(),
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "CONFLICT"
    assert body["trace_id"] != "-"
    # 错误体是契约格式，不是 FastAPI 默认的 `{"detail": "..."}`
    assert "detail" in body

    # 原绑定不应受影响
    session = AsyncSession(bind=seeded, expire_on_commit=False)
    count = (
        await session.execute(
            select(func.count())
            .select_from(ElderStaffAssignment)
            .where(ElderStaffAssignment.staff_id == CAREWORKER_ID)
        )
    ).scalar_one()
    assert count == 1


async def test_reassign_bad_uuid_path_400(client: AsyncClient) -> None:
    """路径参数 staff_id 不是合法 UUID → 400 BAD_REQUEST（契约 §7：路径格式错归 BAD_REQUEST）。"""
    resp = await client.put(
        "/api/staff/not-a-valid-uuid/elders",
        json={"elder_ids": []},
        headers=_admin_headers(),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "BAD_REQUEST"
