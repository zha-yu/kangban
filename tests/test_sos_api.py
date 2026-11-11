"""SOS 一键求助 REST 测试（Day 8 · D3，FR-ALT-06）。

覆盖：
- 401：无凭证 / 家属主体调设备端点（凭证体系不串用）；
- 403：设备未绑定长者 → `device_not_bound`（不伪装成已发出）；
- 200：绑定设备点击 → 生成 `sos` 红色事件＋护工工单，响应 `created=true`；
- 幂等：连续两次点击复用同一事件/工单，`created=false` 且库里只一条。

跑法与 `test_pad_avatar.py` 同构：conftest 共享引擎、每用例一个事务、
`get_session` / `current_device` / `token_hash` 全走真实代码，结束整体回滚。
无 PostgreSQL 时 `pytest.skip` 并打印原因，绝不静默通过。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.api import sos as sos_api
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session, sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import Device, Elder, Room
from app.models.audit import AuditLog
from app.models.medication import RiskEvent, WorkOrder

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "sos-api-test-secret-not-a-real-key-0123456789"

ROOM_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddd0001")
ELDER_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01")
DEVICE_BOUND = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffff01")
DEVICE_UNBOUND = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffff02")

SECRET_BOUND = "sos-device-secret-bound-not-a-real-value"
SECRET_UNBOUND = "sos-device-secret-unbound-not-a-real-value"


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
    except Exception as exc:  # 超时/拒绝/口令错一律算「无库」
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
    application.include_router(sos_api.router)

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
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="SOS-101", status="full")
    )
    await db.execute(
        Elder.__table__.insert().values(elder_id=ELDER_ID, name="SOS接口长者", status="active")
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


async def test_sos_requires_auth(client: AsyncClient, seeded: None) -> None:
    response = await client.post("/api/pad/sos")
    assert response.status_code == 401


async def test_sos_rejects_guardian_subject(client: AsyncClient, seeded: None) -> None:
    """家属令牌调设备端点 → 401，不是 403（避免「换个账号就能过」的误导）。"""
    guardian_id = uuid.uuid4()
    token = sign_staff_access(kind="guardian", staff_id=guardian_id, role="", token_version=0)
    response = await client.post("/api/pad/sos", headers=_bearer(token))
    assert response.status_code == 401


async def test_sos_unbound_device_is_403(client: AsyncClient, seeded: None) -> None:
    token = _device_token(DEVICE_UNBOUND, SECRET_UNBOUND)
    response = await client.post("/api/pad/sos", headers=_bearer(token))

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "device_not_bound"
    assert body["detail"]


async def test_sos_creates_event_and_order_then_idempotent(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)

    first = await client.post("/api/pad/sos", headers=_bearer(token))
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] is True
    assert body["order_id"] is not None

    # 连接级执行拿到的是列结果（不是 ORM 实体），按表列断言，避免误读。
    risk_table = RiskEvent.__table__
    event = (
        await db.execute(
            select(
                risk_table.c.event_id,
                risk_table.c.type,
                risk_table.c.severity,
                risk_table.c.status,
                risk_table.c.elder_id,
            ).where(risk_table.c.event_id == uuid.UUID(body["event_id"]))
        )
    ).one()
    assert event.type == "sos"
    assert event.severity == "red"
    assert event.status == "open"
    assert str(event.elder_id) == str(ELDER_ID)

    order_table = WorkOrder.__table__
    order = (
        await db.execute(
            select(
                order_table.c.order_id,
                order_table.c.type,
                order_table.c.severity,
                order_table.c.event_id,
            ).where(order_table.c.order_id == uuid.UUID(body["order_id"]))
        )
    ).one()
    assert order.type == "sos"
    assert order.severity == "red"
    assert order.event_id == event.event_id

    second = await client.post("/api/pad/sos", headers=_bearer(token))
    assert second.status_code == 200, second.text
    again = second.json()
    assert again["created"] is False
    assert again["event_id"] == body["event_id"]
    assert again["order_id"] == body["order_id"]

    events = (
        (
            await db.execute(
                select(risk_table.c.event_id).where(
                    risk_table.c.elder_id == ELDER_ID, risk_table.c.type == "sos"
                )
            )
        )
        .scalars()
        .all()
    )
    orders = (
        (await db.execute(select(order_table.c.order_id).where(order_table.c.elder_id == ELDER_ID)))
        .scalars()
        .all()
    )
    assert len(events) == 1, "连续点击不得重复建事件"
    assert len(orders) == 1, "连续点击不得重复建工单"


async def test_sos_correction_keeps_event_open_and_audited(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """AC-26：红色事件只能追加「说错了」，事件保持 open 并写审计。"""
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)
    created = await client.post("/api/pad/sos", headers=_bearer(token))
    event_id = created.json()["event_id"]

    response = await client.post(f"/api/pad/sos/{event_id}/correction", headers=_bearer(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["event_id"] == event_id
    assert body["status"] == "open", "追加说明不得改变事件状态"
    assert body["note"] == "刚才说错了"

    audits = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "sos_correction_appended",
                    AuditLog.resource_id == uuid.UUID(event_id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1


async def test_sos_correction_unbound_device_is_403(client: AsyncClient, seeded: None) -> None:
    token = _device_token(DEVICE_UNBOUND, SECRET_UNBOUND)
    response = await client.post(f"/api/pad/sos/{uuid.uuid4()}/correction", headers=_bearer(token))
    assert response.status_code == 403
    assert response.json()["code"] == "device_not_bound"


async def test_sos_correction_foreign_event_is_404(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """他人长者的事件：与「不存在」同口径 404，防枚举。"""
    other_elder = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee99")
    await db.execute(
        Elder.__table__.insert().values(elder_id=other_elder, name="SOS他人长者", status="active")
    )
    foreign_event = uuid.uuid4()
    await db.execute(
        RiskEvent.__table__.insert().values(
            event_id=foreign_event,
            elder_id=other_elder,
            type="sos",
            severity="red",
            status="open",
        )
    )

    token = _device_token(DEVICE_BOUND, SECRET_BOUND)
    response = await client.post(f"/api/pad/sos/{foreign_event}/correction", headers=_bearer(token))
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"


async def test_sos_correction_closed_event_is_409(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)
    created = await client.post("/api/pad/sos", headers=_bearer(token))
    event_id = uuid.UUID(created.json()["event_id"])
    await db.execute(
        RiskEvent.__table__.update()
        .where(RiskEvent.__table__.c.event_id == event_id)
        .values(status="closed")
    )

    response = await client.post(f"/api/pad/sos/{event_id}/correction", headers=_bearer(token))
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"
