"""admin 房间/床位管理接口测试（计划 v1.5 §7 / §3）。

全部跑在真实 PostgreSQL 上（复用 `tests/conftest.py` 的 `db_conn`，每用例一事务、结束回滚）：

- 被测应用在文件内临时装配（只装 `admin_org.router`），请求走真实 ASGI 链路；
- `get_session` 用 `join_transaction_mode="create_savepoint"` 绑到测试连接，
  请求内 commit 只释放保存点，外层事务仍可整体回滚；
- 房号/床号带随机后缀，`pytest-xdist` 多 worker 并行也不会撞唯一约束；
- 每条写操作回查 `audit_logs`，断言 `detail` 有 before/after 且不含姓名等 PII。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.api import admin_org as admin_org_api
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session, sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import AuditLog, Bed, Device, Elder, GuardianAccount, Room, StaffAccount

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "admin-org-test-secret-not-a-real-key-0123456789"
_PASSWORD_PLACEHOLDER = "admin-org-test-row-not-a-real-hash"
_DEVICE_SECRET = "admin-org-device-secret-not-a-real-value"


# ------------------------------------------------------------------ 夹具


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    get_settings.cache_clear()
    yield None
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(db_conn: AsyncConnection, jwt_env: None) -> AsyncIterator[AsyncClient]:
    """临时装配 admin_org 路由；请求与测试事务共用同一连接与事件循环。"""
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(admin_org_api.router)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=db_conn,
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


@dataclass(frozen=True)
class World:
    suffix: str
    room_main: uuid.UUID
    room_main_no: str
    room_full: uuid.UUID
    room_full_no: str
    room_empty: uuid.UUID
    room_empty_no: str
    bed_main_occupied: uuid.UUID
    bed_main_free: uuid.UUID
    bed_full_occupied: uuid.UUID
    admin_id: uuid.UUID
    admin_headers: dict[str, str]
    care_headers: dict[str, str]
    doctor_headers: dict[str, str]
    guardian_headers: dict[str, str]
    device_headers: dict[str, str]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def world(db_conn: AsyncConnection) -> World:
    """三间房（部分占用 / 满房 / 空房）、三名员工、一位家属、一台设备。"""
    suffix = uuid.uuid4().hex[:8]
    room_main = uuid.uuid4()
    room_full = uuid.uuid4()
    room_empty = uuid.uuid4()
    bed_main_occupied = uuid.uuid4()
    bed_main_free = uuid.uuid4()
    bed_full_occupied = uuid.uuid4()
    elder_one = uuid.uuid4()
    elder_two = uuid.uuid4()

    room_main_no = f"Z-{suffix}-101"
    room_full_no = f"Z-{suffix}-102"
    room_empty_no = f"Z-{suffix}-103"
    bed_main_occupied_no = f"1 号床-{suffix}"
    bed_main_free_no = f"2 号床-{suffix}"
    bed_full_occupied_no = f"1 号床-{suffix}"

    await db_conn.execute(
        Room.__table__.insert(),
        [
            {"room_id": room_main, "room_no": room_main_no, "status": "partial"},
            {"room_id": room_full, "room_no": room_full_no, "status": "full"},
            {"room_id": room_empty, "room_no": room_empty_no, "status": "available"},
        ],
    )
    await db_conn.execute(
        Elder.__table__.insert(),
        [
            {"elder_id": elder_one, "name": "张桂芬", "status": "active"},
            {"elder_id": elder_two, "name": "李建国", "status": "active"},
        ],
    )
    await db_conn.execute(
        Bed.__table__.insert(),
        [
            {
                "bed_id": bed_main_occupied,
                "room_id": room_main,
                "bed_no": bed_main_occupied_no,
                "status": "occupied",
                "elder_id": elder_one,
            },
            {
                "bed_id": bed_main_free,
                "room_id": room_main,
                "bed_no": bed_main_free_no,
                "status": "available",
                "elder_id": None,
            },
            {
                "bed_id": bed_full_occupied,
                "room_id": room_full,
                "bed_no": bed_full_occupied_no,
                "status": "occupied",
                "elder_id": elder_two,
            },
        ],
    )

    admin_id = uuid.uuid4()
    care_id = uuid.uuid4()
    doctor_id = uuid.uuid4()
    guardian_id = uuid.uuid4()
    device_id = uuid.uuid4()

    await db_conn.execute(
        StaffAccount.__table__.insert(),
        [
            {
                "staff_id": admin_id,
                "name": "管理员",
                "role": "admin",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
            },
            {
                "staff_id": care_id,
                "name": "护工",
                "role": "care_worker",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
            },
            {
                "staff_id": doctor_id,
                "name": "医生",
                "role": "doctor",
                "password_hash": _PASSWORD_PLACEHOLDER,
                "token_version": 1,
            },
        ],
    )
    await db_conn.execute(
        GuardianAccount.__table__.insert().values(
            guardian_id=guardian_id,
            name="家属",
            password_hash=_PASSWORD_PLACEHOLDER,
            token_version=1,
        )
    )
    await db_conn.execute(
        Device.__table__.insert().values(
            device_id=device_id,
            room_id=room_main,
            elder_id=None,
            token_hash=sha256_hex(_DEVICE_SECRET),
            status="offline",
        )
    )

    return World(
        suffix=suffix,
        room_main=room_main,
        room_main_no=room_main_no,
        room_full=room_full,
        room_full_no=room_full_no,
        room_empty=room_empty,
        room_empty_no=room_empty_no,
        bed_main_occupied=bed_main_occupied,
        bed_main_free=bed_main_free,
        bed_full_occupied=bed_full_occupied,
        admin_id=admin_id,
        admin_headers=_bearer(
            sign_staff_access(kind="staff", staff_id=admin_id, role="admin", token_version=1)
        ),
        care_headers=_bearer(
            sign_staff_access(kind="staff", staff_id=care_id, role="care_worker", token_version=1)
        ),
        doctor_headers=_bearer(
            sign_staff_access(kind="staff", staff_id=doctor_id, role="doctor", token_version=1)
        ),
        guardian_headers=_bearer(
            sign_staff_access(kind="guardian", staff_id=guardian_id, role="", token_version=1)
        ),
        device_headers=_bearer(
            sign_device_session(
                device_id=device_id, token_hash_prefix=sha256_hex(_DEVICE_SECRET)[:16]
            )
        ),
    )


# ------------------------------------------------------------------ 断言辅助


def _assert_error(response: Response, status_code: int, code: str) -> dict[str, Any]:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["code"] == code, body
    assert body["detail"]
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 32, body
    return body


async def _count(db: AsyncConnection, model: object) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def _audits(
    db: AsyncConnection, action: str, *, resource_id: uuid.UUID | None = None
) -> list[Any]:
    """按动作/资源查审计行；Core 连接不做 ORM 结果处理，这里显式取需要的列。"""
    stmt = select(
        AuditLog.action,
        AuditLog.actor_type,
        AuditLog.actor_id,
        AuditLog.resource_type,
        AuditLog.resource_id,
        AuditLog.detail,
    ).where(AuditLog.action == action)
    if resource_id is not None:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    return list((await db.execute(stmt)).all())


async def _room_row(db: AsyncConnection, room_id: uuid.UUID) -> Any | None:
    return (
        await db.execute(select(Room.room_no, Room.status).where(Room.room_id == room_id))
    ).first()


async def _bed_row(db: AsyncConnection, bed_id: uuid.UUID) -> Any | None:
    return (
        await db.execute(
            select(Bed.bed_no, Bed.status, Bed.elder_id, Bed.room_id).where(Bed.bed_id == bed_id)
        )
    ).first()


async def _create_room(client: AsyncClient, world: World, room_no: str) -> uuid.UUID:
    response = await client.post(
        "/api/admin/rooms", json={"room_no": room_no}, headers=world.admin_headers
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["room_id"])


async def _create_bed(
    client: AsyncClient, world: World, room_id: uuid.UUID, bed_no: str
) -> uuid.UUID:
    response = await client.post(
        "/api/admin/beds",
        json={"room_id": str(room_id), "bed_no": bed_no},
        headers=world.admin_headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["bed_id"])


# ------------------------------------------------------------------ 房间 CRUD


async def test_room_crud_persists_and_audits_before_after(
    client: AsyncClient, db_conn: AsyncConnection, world: World
) -> None:
    room_no = f"T-{world.suffix}-201"
    created = await client.post(
        "/api/admin/rooms", json={"room_no": room_no}, headers=world.admin_headers
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {"room_id", "room_no", "status"}
    assert body["room_no"] == room_no and body["status"] == "available"
    room_id = uuid.UUID(body["room_id"])

    listed = await client.get(
        "/api/admin/rooms", params={"room_no": room_no}, headers=world.admin_headers
    )
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["total"] == 1
    assert payload["page"] == 1 and payload["page_size"] == 20
    assert payload["items"][0]["room_id"] == str(room_id)
    assert payload["items"][0]["beds"] == []

    renamed_no = f"{room_no}-R"
    patched = await client.patch(
        f"/api/admin/rooms/{room_id}",
        json={"room_no": renamed_no},
        headers=world.admin_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["room_no"] == renamed_no

    row = await _room_row(db_conn, room_id)
    assert row is not None and row.room_no == renamed_no

    create_audits = await _audits(db_conn, "room_create", resource_id=room_id)
    assert len(create_audits) == 1
    audit = create_audits[0]
    assert audit.actor_type == "staff" and audit.actor_id == world.admin_id
    assert audit.resource_type == "room"
    assert audit.detail is not None
    assert audit.detail["before"] is None
    assert audit.detail["after"] == {"room_no": room_no, "status": "available"}

    update_audits = await _audits(db_conn, "room_update", resource_id=room_id)
    assert len(update_audits) == 1
    detail = update_audits[0].detail
    assert detail is not None
    assert detail["before"] == {"room_no": room_no, "status": "available"}
    assert detail["after"] == {"room_no": renamed_no, "status": "available"}
    # 审计 detail 只放房号/床号/状态/elder_id，不得出现长者姓名。
    assert "张桂芬" not in str(detail) and "李建国" not in str(detail)

    deleted = await client.delete(f"/api/admin/rooms/{room_id}", headers=world.admin_headers)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert await _room_row(db_conn, room_id) is None

    delete_audits = await _audits(db_conn, "room_delete", resource_id=room_id)
    assert len(delete_audits) == 1
    delete_detail = delete_audits[0].detail
    assert delete_detail is not None
    assert delete_detail["before"] == {"room_no": renamed_no, "status": "available"}
    assert delete_detail["after"] is None


async def test_room_unique_and_blank_validation(
    client: AsyncClient, db_conn: AsyncConnection, world: World
) -> None:
    duplicate = await client.post(
        "/api/admin/rooms", json={"room_no": world.room_main_no}, headers=world.admin_headers
    )
    body = _assert_error(duplicate, 409, "CONFLICT")
    assert "房号" in body["detail"]

    _assert_error(
        await client.post("/api/admin/rooms", json={"room_no": ""}, headers=world.admin_headers),
        422,
        "VALIDATION_ERROR",
    )
    # 全空白串过了 Pydantic 的 min_length，但服务层 trim 后必须 422，不能落库。
    _assert_error(
        await client.post("/api/admin/rooms", json={"room_no": "   "}, headers=world.admin_headers),
        422,
        "VALIDATION_ERROR",
    )

    # PATCH 撞已有房号 → 409，且原值不变。
    conflict = await client.patch(
        f"/api/admin/rooms/{world.room_empty}",
        json={"room_no": world.room_main_no},
        headers=world.admin_headers,
    )
    _assert_error(conflict, 409, "CONFLICT")
    row = await _room_row(db_conn, world.room_empty)
    assert row is not None and row.room_no == world.room_empty_no


async def test_delete_room_with_beds_or_devices_conflicts(
    client: AsyncClient, db_conn: AsyncConnection, world: World
) -> None:
    blocked_by_beds = await client.delete(
        f"/api/admin/rooms/{world.room_main}", headers=world.admin_headers
    )
    body = _assert_error(blocked_by_beds, 409, "CONFLICT")
    assert "床位" in body["detail"]
    assert await _room_row(db_conn, world.room_main) is not None

    # 只被设备引用（无床位）也要 409，不能靠 FK 违约变 500。
    room_id = await _create_room(client, world, f"T-{world.suffix}-dev")
    await db_conn.execute(
        Device.__table__.insert().values(
            device_id=uuid.uuid4(),
            room_id=room_id,
            elder_id=None,
            token_hash=sha256_hex(f"device-{uuid.uuid4().hex}"),
            status="offline",
        )
    )
    blocked_by_device = await client.delete(
        f"/api/admin/rooms/{room_id}", headers=world.admin_headers
    )
    body = _assert_error(blocked_by_device, 409, "CONFLICT")
    assert "设备" in body["detail"]
    assert await _room_row(db_conn, room_id) is not None


# ------------------------------------------------------------------ 床位 CRUD 与房间状态同步


async def test_bed_crud_keeps_room_status_derived(
    client: AsyncClient, db_conn: AsyncConnection, world: World
) -> None:
    room_id = await _create_room(client, world, f"T-{world.suffix}-301")
    bed_one = await _create_bed(client, world, room_id, f"1 号床-{world.suffix}")
    bed_two = await _create_bed(client, world, room_id, f"2 号床-{world.suffix}")
    assert (await _room_row(db_conn, room_id)).status == "available"  # type: ignore[union-attr]

    async def patch_bed(bed_id: uuid.UUID, payload: dict[str, Any]) -> dict[str, Any]:
        response = await client.patch(
            f"/api/admin/beds/{bed_id}", json=payload, headers=world.admin_headers
        )
        assert response.status_code == 200, response.text
        return response.json()

    patched = await patch_bed(bed_one, {"status": "maintenance"})
    assert patched["status"] == "maintenance"
    assert (await _room_row(db_conn, room_id)).status == "partial"  # type: ignore[union-attr]
    assert (await _bed_row(db_conn, bed_one)).status == "maintenance"  # type: ignore[union-attr]

    await patch_bed(bed_one, {"status": "disabled"})
    assert (await _room_row(db_conn, room_id)).status == "partial"  # type: ignore[union-attr]

    await patch_bed(bed_two, {"status": "maintenance"})
    assert (await _room_row(db_conn, room_id)).status == "maintenance"  # type: ignore[union-attr]

    renamed = await patch_bed(bed_one, {"bed_no": f"3 号床-{world.suffix}"})
    assert renamed["bed_no"] == f"3 号床-{world.suffix}"

    deleted = await client.delete(f"/api/admin/beds/{bed_one}", headers=world.admin_headers)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert (await _room_row(db_conn, room_id)).status == "maintenance"  # type: ignore[union-attr]

    await client.delete(f"/api/admin/beds/{bed_two}", headers=world.admin_headers)
    assert (await _room_row(db_conn, room_id)).status == "available"  # type: ignore[union-attr]

    create_audits = await _audits(db_conn, "bed_create")
    ours = [
        audit
        for audit in create_audits
        if audit.detail is not None and audit.detail["after"]["room_no"] == f"T-{world.suffix}-301"
    ]
    assert len(ours) == 2
    first = ours[0].detail
    assert first is not None
    assert first["before"] is None
    assert first["after"]["status"] == "available"
    assert first["after"]["elder_id"] is None

    update_audits = [
        audit
        for audit in await _audits(db_conn, "bed_update")
        if audit.detail is not None and audit.detail["after"]["room_no"] == f"T-{world.suffix}-301"
    ]
    assert update_audits
    # 两次「可用 → 维修」的状态变更都必须留 before/after，且不带长者姓名。
    status_changes = [
        audit.detail
        for audit in update_audits
        if audit.detail["before"]["status"] == "available"
        and audit.detail["after"]["status"] == "maintenance"
    ]
    assert len(status_changes) == 2
    for detail in status_changes:
        assert detail["before"]["elder_id"] is None
        assert detail["after"]["elder_id"] is None

    # 改床号也要留 before/after。
    rename_audits = [
        audit.detail
        for audit in update_audits
        if audit.detail["before"]["bed_no"] != audit.detail["after"]["bed_no"]
    ]
    assert len(rename_audits) == 1
    assert rename_audits[0]["before"]["status"] == "disabled"
    assert rename_audits[0]["after"]["status"] == "disabled"

    delete_audits = await _audits(db_conn, "bed_delete")
    ours = [
        audit
        for audit in delete_audits
        if audit.detail is not None and audit.detail["before"]["room_no"] == f"T-{world.suffix}-301"
    ]
    assert len(ours) == 2
    assert ours[0].detail["after"] is None


async def test_bed_unique_and_validation_rules(
    client: AsyncClient, db_conn: AsyncConnection, world: World
) -> None:
    room_one = await _create_room(client, world, f"T-{world.suffix}-401")
    room_two = await _create_room(client, world, f"T-{world.suffix}-402")
    bed_no = f"1 号床-{world.suffix}"

    await _create_bed(client, world, room_one, bed_no)
    duplicate = await client.post(
        "/api/admin/beds",
        json={"room_id": str(room_one), "bed_no": bed_no},
        headers=world.admin_headers,
    )
    body = _assert_error(duplicate, 409, "CONFLICT")
    assert "床号" in body["detail"]

    # 跨房间同名床号允许（唯一约束是 (room_id, bed_no)）。
    cross_room = await client.post(
        "/api/admin/beds",
        json={"room_id": str(room_two), "bed_no": bed_no},
        headers=world.admin_headers,
    )
    assert cross_room.status_code == 201, cross_room.text

    _assert_error(
        await client.post(
            "/api/admin/beds",
            json={"room_id": str(room_one), "bed_no": "   "},
            headers=world.admin_headers,
        ),
        422,
        "VALIDATION_ERROR",
    )
    _assert_error(
        await client.post(
            "/api/admin/beds",
            json={"room_id": str(uuid.uuid4()), "bed_no": f"9 号床-{world.suffix}"},
            headers=world.admin_headers,
        ),
        404,
        "NOT_FOUND",
    )

    # 显式传 occupied：请求模型层即 422（occupied 只能由 elder_id 派生）。
    _assert_error(
        await client.patch(
            f"/api/admin/beds/{world.bed_main_free}",
            json={"status": "occupied"},
            headers=world.admin_headers,
        ),
        422,
        "VALIDATION_ERROR",
    )
    _assert_error(
        await client.patch(
            f"/api/admin/beds/{world.bed_main_free}",
            json={"bed_no": ""},
            headers=world.admin_headers,
        ),
        422,
        "VALIDATION_ERROR",
    )
    # 显式 null 不能被当成「未传」。
    _assert_error(
        await client.patch(
            f"/api/admin/beds/{world.bed_main_free}",
            json={"status": None},
            headers=world.admin_headers,
        ),
        422,
        "VALIDATION_ERROR",
    )


async def test_occupied_bed_status_and_delete_rules(
    client: AsyncClient, db_conn: AsyncConnection, world: World
) -> None:
    for status_value in ("maintenance", "disabled", "available"):
        response = await client.patch(
            f"/api/admin/beds/{world.bed_main_occupied}",
            json={"status": status_value},
            headers=world.admin_headers,
        )
        _assert_error(response, 409, "CONFLICT")
    occupied = await _bed_row(db_conn, world.bed_main_occupied)
    assert occupied is not None and occupied.status == "occupied"

    _assert_error(
        await client.delete(
            f"/api/admin/beds/{world.bed_main_occupied}", headers=world.admin_headers
        ),
        409,
        "CONFLICT",
    )
    assert await _bed_row(db_conn, world.bed_main_occupied) is not None

    # 占床改床号本身不改变占用语义，允许。
    renamed = await client.patch(
        f"/api/admin/beds/{world.bed_main_occupied}",
        json={"bed_no": f"9 号床-{world.suffix}"},
        headers=world.admin_headers,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["status"] == "occupied"


# ------------------------------------------------------------------ 权限：四类非 admin 一律 403


async def test_all_endpoints_reject_non_admin_with_403(
    client: AsyncClient, db_conn: AsyncConnection, world: World
) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", "/api/admin/rooms", None),
        ("POST", "/api/admin/rooms", {"room_no": f"F-{world.suffix}"}),
        (
            "PATCH",
            f"/api/admin/rooms/{world.room_empty}",
            {"room_no": f"F-{world.suffix}-rename"},
        ),
        ("DELETE", f"/api/admin/rooms/{world.room_empty}", None),
        ("GET", "/api/admin/beds", None),
        (
            "POST",
            "/api/admin/beds",
            {"room_id": str(world.room_empty), "bed_no": f"F-{world.suffix}"},
        ),
        ("PATCH", f"/api/admin/beds/{world.bed_main_free}", {"status": "maintenance"}),
        ("DELETE", f"/api/admin/beds/{world.bed_main_free}", None),
        (
            "POST",
            "/api/admin/beds/batch-status",
            {"bed_ids": [str(world.bed_main_free)], "status": "maintenance"},
        ),
        (
            "POST",
            "/api/admin/beds/batch-delete",
            {"bed_ids": [str(world.bed_main_free)]},
        ),
    ]
    rooms_before = await _count(db_conn, Room)
    beds_before = await _count(db_conn, Bed)
    audits_before = await _count(db_conn, AuditLog)

    for headers in (
        world.care_headers,
        world.doctor_headers,
        world.guardian_headers,
        world.device_headers,
    ):
        for method, path, payload in calls:
            response = await client.request(method, path, json=payload, headers=headers)
            _assert_error(response, 403, "ROLE_FORBIDDEN")

    assert await _count(db_conn, Room) == rooms_before
    assert await _count(db_conn, Bed) == beds_before
    assert await _count(db_conn, AuditLog) == audits_before


# ------------------------------------------------------------------ 分页与筛选


async def test_room_and_bed_pagination_filters(
    client: AsyncClient, db_conn: AsyncConnection, world: World
) -> None:
    await _create_room(client, world, f"P-{world.suffix}-104")

    listed = await client.get(
        "/api/admin/rooms",
        params={"room_no": world.suffix, "page": 2, "page_size": 2},
        headers=world.admin_headers,
    )
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["total"] == 4  # 种子的三间 + 本用例新建的一间
    assert payload["page"] == 2 and payload["page_size"] == 2
    assert len(payload["items"]) == 2

    filtered = await client.get(
        "/api/admin/rooms",
        params={"room_no": world.suffix, "status": "full"},
        headers=world.admin_headers,
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["room_id"] == str(world.room_full)

    # 房间列表必须带全量床位（含空床），状态由床位聚合。
    main_room = await client.get(
        "/api/admin/rooms",
        params={"room_no": world.room_main_no},
        headers=world.admin_headers,
    )
    assert main_room.status_code == 200
    main_item = main_room.json()["items"][0]
    assert main_item["status"] == "partial"
    assert {bed["bed_no"] for bed in main_item["beds"]} == {
        f"1 号床-{world.suffix}",
        f"2 号床-{world.suffix}",
    }
    assert {bed["status"] for bed in main_item["beds"]} == {"available", "occupied"}

    beds = await client.get(
        "/api/admin/beds",
        params={"room_id": str(world.room_main), "page_size": 1, "page": 2},
        headers=world.admin_headers,
    )
    assert beds.status_code == 200, beds.text
    bed_payload = beds.json()
    assert bed_payload["total"] == 2
    assert bed_payload["page"] == 2 and bed_payload["page_size"] == 1
    assert len(bed_payload["items"]) == 1
    assert bed_payload["items"][0]["room_no"] == world.room_main_no

    free = await client.get(
        "/api/admin/beds",
        params={
            "room_id": str(world.room_main),
            "bed_no": f"2 号床-{world.suffix}",
            "status": "available",
        },
        headers=world.admin_headers,
    )
    assert free.status_code == 200
    assert free.json()["total"] == 1
    assert free.json()["items"][0]["bed_id"] == str(world.bed_main_free)

    _assert_error(
        await client.get(
            "/api/admin/rooms", params={"status": "broken"}, headers=world.admin_headers
        ),
        422,
        "VALIDATION_ERROR",
    )
    _assert_error(
        await client.get("/api/admin/beds", params={"page_size": 101}, headers=world.admin_headers),
        422,
        "VALIDATION_ERROR",
    )


# ------------------------------------------------------------------ 批量操作


async def test_batch_status_and_delete_success_and_rollback(
    client: AsyncClient, db_conn: AsyncConnection, world: World
) -> None:
    room_id = await _create_room(client, world, f"T-{world.suffix}-501")
    bed_a = await _create_bed(client, world, room_id, f"1 号床-{world.suffix}")
    bed_b = await _create_bed(client, world, room_id, f"2 号床-{world.suffix}")
    bed_c = await _create_bed(client, world, room_id, f"3 号床-{world.suffix}")

    # 成功：重复 id 去重后实际生效 3 条。
    success = await client.post(
        "/api/admin/beds/batch-status",
        json={
            "bed_ids": [str(bed_a), str(bed_a), str(bed_b), str(bed_c), str(bed_b)],
            "status": "maintenance",
        },
        headers=world.admin_headers,
    )
    assert success.status_code == 200, success.text
    assert success.json()["affected"] == 3
    for bed_id in (bed_a, bed_b, bed_c):
        assert (await _bed_row(db_conn, bed_id)).status == "maintenance"  # type: ignore[union-attr]
    assert (await _room_row(db_conn, room_id)).status == "maintenance"  # type: ignore[union-attr]

    batch_audits = await _audits(db_conn, "bed_batch_status")
    assert len(batch_audits) == 1
    detail = batch_audits[0].detail
    assert detail is not None
    assert sorted(detail["bed_ids"]) == sorted([str(bed_a), str(bed_b), str(bed_c)])
    assert [item["status"] for item in detail["before"]] == ["available"] * 3
    assert [item["status"] for item in detail["after"]] == ["maintenance"] * 3
    assert all(item["room_no"] == f"T-{world.suffix}-501" for item in detail["after"])

    # 含占用床位 → 整批 409，先前成功的三张床保持 maintenance 不变。
    mixed = await client.post(
        "/api/admin/beds/batch-status",
        json={"bed_ids": [str(bed_a), str(world.bed_main_occupied)], "status": "available"},
        headers=world.admin_headers,
    )
    body = _assert_error(mixed, 409, "CONFLICT")
    assert "入住长者" in body["detail"]
    assert (await _bed_row(db_conn, bed_a)).status == "maintenance"  # type: ignore[union-attr]
    assert (await _bed_row(db_conn, world.bed_main_occupied)).status == "occupied"  # type: ignore[union-attr]

    # 不存在 id → 404，且同批存在的床也不改。
    missing = await client.post(
        "/api/admin/beds/batch-status",
        json={"bed_ids": [str(bed_b), str(uuid.uuid4())], "status": "available"},
        headers=world.admin_headers,
    )
    _assert_error(missing, 404, "NOT_FOUND")
    assert (await _bed_row(db_conn, bed_b)).status == "maintenance"  # type: ignore[union-attr]

    # 批量删空床：先证明含占用整批 409 不落任何删除。
    blocked = await client.post(
        "/api/admin/beds/batch-delete",
        json={"bed_ids": [str(bed_a), str(world.bed_full_occupied)]},
        headers=world.admin_headers,
    )
    _assert_error(blocked, 409, "CONFLICT")
    assert await _bed_row(db_conn, bed_a) is not None
    assert await _bed_row(db_conn, world.bed_full_occupied) is not None

    deleted = await client.post(
        "/api/admin/beds/batch-delete",
        json={"bed_ids": [str(bed_a), str(bed_b), str(bed_c)]},
        headers=world.admin_headers,
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["affected"] == 3
    for bed_id in (bed_a, bed_b, bed_c):
        assert await _bed_row(db_conn, bed_id) is None
    assert (await _room_row(db_conn, room_id)).status == "available"  # type: ignore[union-attr]

    delete_audits = await _audits(db_conn, "bed_batch_delete")
    assert len(delete_audits) == 1
    delete_detail = delete_audits[0].detail
    assert delete_detail is not None
    assert sorted(delete_detail["bed_ids"]) == sorted([str(bed_a), str(bed_b), str(bed_c)])
    assert len(delete_detail["before"]) == 3
    assert delete_detail["after"] == []


# ------------------------------------------------------------------ 404


async def test_missing_room_and_bed_return_404(
    client: AsyncClient, db_conn: AsyncConnection, world: World
) -> None:
    missing = uuid.uuid4()
    _assert_error(
        await client.patch(
            f"/api/admin/rooms/{missing}",
            json={"room_no": f"X-{world.suffix}"},
            headers=world.admin_headers,
        ),
        404,
        "NOT_FOUND",
    )
    _assert_error(
        await client.delete(f"/api/admin/rooms/{missing}", headers=world.admin_headers),
        404,
        "NOT_FOUND",
    )
    _assert_error(
        await client.patch(
            f"/api/admin/beds/{missing}",
            json={"bed_no": f"X-{world.suffix}"},
            headers=world.admin_headers,
        ),
        404,
        "NOT_FOUND",
    )
    _assert_error(
        await client.delete(f"/api/admin/beds/{missing}", headers=world.admin_headers),
        404,
        "NOT_FOUND",
    )
