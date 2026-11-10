"""B3 · E20 改绑并发与 API 边界（P2-19/20、P3-33/34）。

覆盖四条判据：
1. 并发摘护工：两个事务同时移除同一位长者的不同 primary_care，恰好 1 成功 1 409，
   长者至少保留 1 位责任护工（P2-19 行锁 + 锁后重读 diff）；
2. 乐观 CAS（P2-20）：`expected_updated_at` 过期 → `StaleWriteConflictError`（API 层
   409 STALE_WRITE）；不传 → 只走行锁（D2 未接入版本前不被阻塞）；成功后基准推进；
3. 失败路径审计：409/403/404 都在 `audit_logs` 留行（操作者、原因、差集）；
4. API 边界：`elder_ids` 201 个 → 422（P3-34）；`GET /api/staff?role=superman` → 422
   而非 200 空列表（P3-33）；admin 自我保护 403（P2-20）。

跑法：服务级用例走 conftest 共享引擎 + 每用例一事务回滚；并发用例是例外——
行锁要靠真实提交才能验证，自带建行与清理（同 test_history_store 的并发用例）；
API 级用例走真实 ASGI 链路（同 test_staff_api）。无 PostgreSQL 时 skip 不静默通过。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.api import staff as staff_api
from app.api.schemas.staff import ElderReassignRequest
from app.core.config import get_settings
from app.core.security import hash_password, sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import AuditLog, Elder, StaffAccount
from app.models.relation import ElderStaffAssignment
from app.services.staff_service import (
    PrimaryCareGapError,
    StaffNotFoundError,
    StaffProtectedError,
    StaffRoleConflictError,
    StaleWriteConflictError,
    reassign_elders,
    update_staff,
)

TEST_JWT_SECRET = "staff-reassign-test-secret-not-a-real-key-0f3e5a7b9c1d2e4f"

_ADMIN_ID = uuid.uuid4()
_ADMIN_PHONE = "139" + uuid.uuid4().hex[:8]
_HASH = hash_password("Reassign@2026")


# ---------------------------------------------------------------- 服务级夹具


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """每用例一事务，结束回滚（同 test_history_store.session）。"""
    try:
        conn = await db_engine.connect()
    except Exception as exc:  # 任何连接失败都算「无库」
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


@pytest.fixture
async def seeded(session: AsyncSession) -> dict[str, object]:
    """1 admin + 2 care_worker + 2 长者；elder_a 同时绑 W1/W2 两位 primary_care。"""
    admin = StaffAccount(
        staff_id=_ADMIN_ID,
        name="管理员",
        role="admin",
        phone=_ADMIN_PHONE,
        password_hash=_HASH,
        status="active",
    )
    w1 = StaffAccount(
        name="护工一",
        role="care_worker",
        phone="138" + uuid.uuid4().hex[:8],
        password_hash=_HASH,
        status="active",
    )
    w2 = StaffAccount(
        name="护工二",
        role="care_worker",
        phone="138" + uuid.uuid4().hex[:8],
        password_hash=_HASH,
        status="active",
    )
    elder_a = Elder(name="长者甲", status="active")
    elder_b = Elder(name="长者乙", status="active")
    session.add_all([admin, w1, w2, elder_a, elder_b])
    await session.flush()
    session.add_all(
        [
            ElderStaffAssignment(
                elder_id=elder_a.elder_id, staff_id=w1.staff_id, relation="primary_care"
            ),
            ElderStaffAssignment(
                elder_id=elder_a.elder_id, staff_id=w2.staff_id, relation="primary_care"
            ),
            ElderStaffAssignment(
                elder_id=elder_b.elder_id, staff_id=w1.staff_id, relation="primary_care"
            ),
        ]
    )
    await session.flush()
    return {
        "admin_id": admin.staff_id,
        "w1_id": w1.staff_id,
        "w2_id": w2.staff_id,
        "elder_a": elder_a.elder_id,
        "elder_b": elder_b.elder_id,
    }


async def _primary_care_count(session: AsyncSession, elder_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(ElderStaffAssignment)
                .where(
                    ElderStaffAssignment.elder_id == elder_id,
                    ElderStaffAssignment.relation == "primary_care",
                )
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------- 1. 并发摘护工（真实并发）


async def test_concurrent_reassign_one_success_one_409(db_engine: AsyncEngine) -> None:
    """两个事务同时移除同一位长者的不同护工：行锁串行化 → 恰好 1 成功 1 409。

    故意绕开 savepoint 夹具：行锁要靠真实提交才能释放、锁等待后才能看到对方
    已提交的删除。自带建行与收尾清理。
    """
    elder = Elder(name="并发长者", status="active")
    w1 = StaffAccount(
        name="并发护工一",
        role="care_worker",
        phone="138" + uuid.uuid4().hex[:8],
        password_hash=_HASH,
        status="active",
    )
    w2 = StaffAccount(
        name="并发护工二",
        role="care_worker",
        phone="138" + uuid.uuid4().hex[:8],
        password_hash=_HASH,
        status="active",
    )
    async with AsyncSession(bind=db_engine, expire_on_commit=False) as setup, setup.begin():
        setup.add_all([elder, w1, w2])
        await setup.flush()
        setup.add_all(
            [
                ElderStaffAssignment(
                    elder_id=elder.elder_id, staff_id=w1.staff_id, relation="primary_care"
                ),
                ElderStaffAssignment(
                    elder_id=elder.elder_id, staff_id=w2.staff_id, relation="primary_care"
                ),
            ]
        )

    results: list[dict[str, object]] = []

    async def _remove(staff_id: uuid.UUID) -> None:
        sf = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with sf() as sess, sess.begin():
            try:
                await reassign_elders(
                    sess,
                    staff_id=staff_id,
                    elder_ids=[],  # 清空 = 移除自己在该长者的绑定
                    operator_id=_ADMIN_ID,
                )
                results.append({"staff_id": staff_id, "outcome": "ok"})
            except PrimaryCareGapError:
                results.append({"staff_id": staff_id, "outcome": "gap_409"})

    try:
        import asyncio

        await asyncio.gather(_remove(w1.staff_id), _remove(w2.staff_id))

        outcomes = sorted(r["outcome"] for r in results)
        assert outcomes == ["gap_409", "ok"], f"期望 1 成功 1 409，实际 {results}"
        # 失败方事务已回滚，成功方提交 → 长者仍保留 1 位责任护工
        async with AsyncSession(bind=db_engine) as check:
            remaining = int(
                (
                    await check.execute(
                        select(func.count())
                        .select_from(ElderStaffAssignment)
                        .where(
                            ElderStaffAssignment.elder_id == elder.elder_id,
                            ElderStaffAssignment.relation == "primary_care",
                        )
                    )
                ).scalar_one()
            )
        assert remaining == 1, "并发摘护工后该长者至少保留 1 位责任护工"
    finally:
        async with (
            AsyncSession(bind=db_engine, expire_on_commit=False) as cleanup,
            cleanup.begin(),
        ):
            # 失败分支在事务内捕获异常（镜像 API 层：错误响应体走正常返回 → commit），
            # 被拒审计会随提交落库——两种 action 都要清理
            await cleanup.execute(
                delete(AuditLog).where(
                    AuditLog.action.in_(["reassign_elders", "reassign_elders_rejected"])
                )
            )
            await cleanup.execute(
                delete(ElderStaffAssignment).where(
                    ElderStaffAssignment.staff_id.in_([w1.staff_id, w2.staff_id])
                )
            )
            await cleanup.execute(
                delete(StaffAccount).where(StaffAccount.staff_id.in_([w1.staff_id, w2.staff_id]))
            )
            await cleanup.execute(delete(Elder).where(Elder.elder_id == elder.elder_id))


# ---------------------------------------------------------------- 2. 乐观 CAS


async def test_stale_write_conflict_on_outdated_baseline(
    session: AsyncSession, seeded: dict[str, object]
) -> None:
    """P2-20：expected_updated_at 过期 → StaleWriteConflictError；成功后基准推进。"""
    w1_id: uuid.UUID = seeded["w1_id"]  # type: ignore[assignment]
    elder_a: uuid.UUID = seeded["elder_a"]  # type: ignore[assignment]
    elder_b: uuid.UUID = seeded["elder_b"]  # type: ignore[assignment]

    baseline = (
        await session.execute(select(Elder.updated_at).where(Elder.elder_id == elder_a))
    ).scalar_one()

    # 第一次：基准新鲜 → 成功（w1 从长者甲移除；乙本就绑定、无 diff）
    result = await reassign_elders(
        session,
        staff_id=w1_id,
        elder_ids=[elder_b],
        operator_id=_ADMIN_ID,
        expected_updated_at=baseline,
    )
    assert result["removed"] == [elder_a], "第一次改绑应把 w1 从长者甲移除"

    # 基准已推进（涉改动长者 = removed ∪ added = {elder_a}）
    new_ts = (
        await session.execute(select(Elder.updated_at).where(Elder.elder_id == elder_a))
    ).scalar_one()
    assert new_ts > baseline, "改绑成功必须推进涉改动长者的 updated_at"

    # 第二次：仍用旧基准，构造涉及 elder_a 的 diff（把它加回）→ stale 409
    with pytest.raises(StaleWriteConflictError):
        await reassign_elders(
            session,
            staff_id=w1_id,
            elder_ids=[elder_a, elder_b],
            operator_id=_ADMIN_ID,
            expected_updated_at=baseline,
        )

    # 失败路径审计有行
    rejected = (
        await session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "reassign_elders_rejected")
        )
    ).scalar_one()
    assert rejected >= 1, "409 失败路径必须写 audit_logs"


async def test_missing_expected_updated_at_skips_cas(
    session: AsyncSession, seeded: dict[str, object]
) -> None:
    """不传 expected_updated_at → 只走行锁（D2 未接入版本前不被阻塞）。"""
    w1_id: uuid.UUID = seeded["w1_id"]  # type: ignore[assignment]
    elder_a: uuid.UUID = seeded["elder_a"]  # type: ignore[assignment]
    elder_b: uuid.UUID = seeded["elder_b"]  # type: ignore[assignment]

    await reassign_elders(session, staff_id=w1_id, elder_ids=[elder_b], operator_id=_ADMIN_ID)
    # 第二次：不传基准，构造纯新增 diff（把 w1 加回长者甲）→ 不因 CAS 受阻
    result = await reassign_elders(
        session, staff_id=w1_id, elder_ids=[elder_a, elder_b], operator_id=_ADMIN_ID
    )
    assert result["added"] == [elder_a], "不传 expected_updated_at 时只走行锁，改绑照常生效"


# ---------------------------------------------------------------- 3. 失败路径审计


async def test_role_forbidden_and_not_found_writes_audit(
    session: AsyncSession, seeded: dict[str, object]
) -> None:
    """403（admin 改绑）/ 404（staff 不存在）失败路径写 audit_logs 后原样重抛。"""
    admin_id: uuid.UUID = seeded["admin_id"]  # type: ignore[assignment]
    elder_a: uuid.UUID = seeded["elder_a"]  # type: ignore[assignment]

    missing_id = uuid.uuid4()
    with pytest.raises(StaffRoleConflictError):
        await reassign_elders(
            session, staff_id=admin_id, elder_ids=[elder_a], operator_id=_ADMIN_ID
        )
    with pytest.raises(StaffNotFoundError):
        await reassign_elders(
            session,
            staff_id=missing_id,
            elder_ids=[elder_a],
            operator_id=_ADMIN_ID,
        )

    # 按 resource_id 过滤：测试库可能残留其他用例提交的同 action 审计行
    rows = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "reassign_elders_rejected",
                    AuditLog.resource_id.in_([admin_id, missing_id]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2, "两次失败各留一条审计"
    assert {r.detail["reason"] for r in rows} == {"StaffRoleConflictError", "StaffNotFoundError"}


# ---------------------------------------------------------------- 4. admin 自我保护


async def test_admin_cannot_disable_self(session: AsyncSession, seeded: dict[str, object]) -> None:
    admin_id: uuid.UUID = seeded["admin_id"]  # type: ignore[assignment]
    with pytest.raises(StaffProtectedError):
        await update_staff(session, staff_id=admin_id, status="disabled", operator_id=admin_id)


async def test_admin_cannot_disable_last_admin(
    session: AsyncSession, seeded: dict[str, object]
) -> None:
    """计数分支：active admin 只剩 1 位、且目标不是操作者本人 → 仍被拒。

    构造：三位 admin → 服务层合法停用一位（3→2）→ SQL 绕过服务层再停一位（2→1）
    → 此时服务层停用最后一位（operator ≠ target，绕开 self 分支）→ 计数分支拦截。
    """
    admin_id: uuid.UUID = seeded["admin_id"]  # type: ignore[assignment]
    other = StaffAccount(
        name="第二管理员",
        role="admin",
        phone="139" + uuid.uuid4().hex[:8],
        password_hash=_HASH,
        status="active",
    )
    third = StaffAccount(
        name="第三管理员",
        role="admin",
        phone="139" + uuid.uuid4().hex[:8],
        password_hash=_HASH,
        status="active",
    )
    session.add_all([other, third])
    await session.flush()

    # 3 位 active：服务层互停允许（保留 2 位，不触发保护）
    await update_staff(session, staff_id=other.staff_id, status="disabled", operator_id=admin_id)
    # 绕过服务层把 third 也置 disabled → active 只剩 admin_id 一位
    await session.execute(
        update(StaffAccount)
        .where(StaffAccount.staff_id == third.staff_id)
        .values(status="disabled")
    )
    # target=admin_id（唯一 active），operator=third（≠ target）→ 计数分支拦截
    with pytest.raises(StaffProtectedError):
        await update_staff(
            session, staff_id=admin_id, status="disabled", operator_id=third.staff_id
        )


# ---------------------------------------------------------------- 5. API 边界（422/409/403）


async def test_reassign_201_elder_ids_returns_422() -> None:
    """P3-34：elder_ids 上限 200，201 个 → pydantic 422。"""
    import pydantic

    ids = [str(uuid.uuid4()) for _ in range(201)]
    with pytest.raises(pydantic.ValidationError):
        ElderReassignRequest.model_validate({"elder_ids": ids})
    # 边界：恰好 200 合法
    req = ElderReassignRequest.model_validate({"elder_ids": ids[:200]})
    assert len(req.elder_ids) == 200


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def api_db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
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
async def api_seeded(api_db: AsyncConnection) -> AsyncConnection:
    """admin + 2 care_worker + 长者（各绑一条 primary_care），供 API 级用例。

    长者必须绑**两位**护工：stale 用例第一次 PUT 清空单一护工会触发
    PrimaryCareGapError（409），无法走到 CAS 分支。
    """
    session = AsyncSession(
        bind=api_db, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    admin = StaffAccount(
        staff_id=_ADMIN_ID,
        name="接口管理员",
        role="admin",
        phone=_ADMIN_PHONE,
        password_hash=_HASH,
        status="active",
    )
    worker = StaffAccount(
        name="接口护工",
        role="care_worker",
        phone="138" + uuid.uuid4().hex[:8],
        password_hash=_HASH,
        status="active",
    )
    worker2 = StaffAccount(
        name="接口护工二",
        role="care_worker",
        phone="138" + uuid.uuid4().hex[:8],
        password_hash=_HASH,
        status="active",
    )
    elder = Elder(name="接口长者", status="active")
    session.add_all([admin, worker, worker2, elder])
    await session.flush()
    session.add_all(
        [
            ElderStaffAssignment(
                elder_id=elder.elder_id, staff_id=worker.staff_id, relation="primary_care"
            ),
            ElderStaffAssignment(
                elder_id=elder.elder_id, staff_id=worker2.staff_id, relation="primary_care"
            ),
        ]
    )
    await session.commit()
    return api_db


@pytest.fixture
async def client(api_seeded: AsyncConnection, jwt_env: None) -> AsyncIterator[AsyncClient]:
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(staff_api.router)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=api_seeded,
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


def _admin_headers() -> dict[str, str]:
    token = sign_staff_access(kind="staff", staff_id=str(_ADMIN_ID), role="admin", token_version=0)
    return {"Authorization": f"Bearer {token}"}


async def test_get_staff_invalid_role_returns_422(client: AsyncClient) -> None:
    """P3-33：role=superman → 422，不是 200 空列表掩盖拼写错误。"""
    resp = await client.get("/api/staff", params={"role": "superman"}, headers=_admin_headers())
    assert resp.status_code == 422


async def test_get_staff_invalid_status_returns_422(client: AsyncClient) -> None:
    resp = await client.get("/api/staff", params={"status": "gone"}, headers=_admin_headers())
    assert resp.status_code == 422


async def test_get_staff_valid_role_returns_200(client: AsyncClient) -> None:
    resp = await client.get("/api/staff", params={"role": "care_worker"}, headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["items"]


async def test_put_elders_201_ids_returns_422(
    client: AsyncClient, api_seeded: AsyncConnection
) -> None:
    """P3-34 接口级：201 个 elder_ids → 422。"""
    session = AsyncSession(bind=api_seeded, expire_on_commit=False)
    worker = (
        await session.execute(select(StaffAccount).where(StaffAccount.name == "接口护工"))
    ).scalar_one()
    ids = [str(uuid.uuid4()) for _ in range(201)]
    resp = await client.put(
        f"/api/staff/{worker.staff_id}/elders",
        json={"elder_ids": ids},
        headers=_admin_headers(),
    )
    assert resp.status_code == 422


async def test_put_elders_stale_write_returns_409(
    client: AsyncClient, api_seeded: AsyncConnection
) -> None:
    """接口级：过期 expected_updated_at → 409 STALE_WRITE；成功后基准推进。"""
    session = AsyncSession(
        bind=api_seeded, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    worker = (
        await session.execute(select(StaffAccount).where(StaffAccount.name == "接口护工"))
    ).scalar_one()
    elder = (await session.execute(select(Elder).limit(1))).scalar_one()
    baseline = elder.updated_at

    # 第一次：新鲜基准 → 200（清空绑定，涉改动长者 updated_at 被推进）
    resp = await client.put(
        f"/api/staff/{worker.staff_id}/elders",
        json={"elder_ids": [], "expected_updated_at": baseline.isoformat()},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["removed"]

    # 第二次：旧基准 + 构造涉及该长者的 diff（把它加回）→ 409 STALE_WRITE
    resp = await client.put(
        f"/api/staff/{worker.staff_id}/elders",
        json={
            "elder_ids": [str(elder.elder_id)],
            "expected_updated_at": baseline.isoformat(),
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "STALE_WRITE"


async def test_patch_disable_self_returns_403(
    client: AsyncClient, api_seeded: AsyncConnection
) -> None:
    """P2-20 自我保护接口级：admin 停用自己 → 403 PROTECTED_ADMIN。"""
    resp = await client.patch(
        f"/api/staff/{_ADMIN_ID}",
        json={"status": "disabled"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "PROTECTED_ADMIN"
