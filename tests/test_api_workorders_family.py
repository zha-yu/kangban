"""工单看板 API（f2-D1）与家属通知 API（N1/N4）的接口测试。

覆盖口径（FR-TC-06 核心权限场景）：
- 工单：鉴权 401 / 列表 join 冗余字段与流转留痕 / 操作人服务端注入（伪造
  `actor_id` 被丢弃，AGENTS §4 红线 9）/ 备注必填 / 红档办结时间强制 /
  `reopen` 仅 admin（403）；
- 家属通知：主体种类错 401 / 按人隔离（他人的通知不可见）/ 已读幂等 /
  越权与不存在同口径 404（不泄露存在性）。

全部走真库（conftest 事务回滚隔离）；`get_session` 被覆写为绑定测试事务的
session，服务层的 flush 落在 savepoint 上，用例结束统一回滚。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import sign_staff_access
from app.db import get_session
from app.main import create_app
from app.models.guardian import GuardianAccount
from app.models.medication import WorkOrder
from app.models.notifications import Notification
from app.models.organization import Bed, Elder, Room
from app.models.staff import StaffAccount
from app.services import notify_service, work_order_service

# ------------------------------------------------------------------ 夹具


@pytest_asyncio.fixture(loop_scope="module")
async def app_with_session(db_conn) -> AsyncIterator[tuple[FastAPI, AsyncSession]]:
    """create_app + 把 get_session 覆写为绑定测试事务的 session。"""
    session = AsyncSession(db_conn)
    application = create_app()

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = _override
    try:
        yield application, session
    finally:
        application.dependency_overrides.pop(get_session, None)
        await session.close()


@pytest_asyncio.fixture(loop_scope="module")
async def client(app_with_session) -> AsyncIterator[AsyncClient]:
    application, _ = app_with_session
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _staff_token(staff_id: uuid.UUID, role: str) -> str:
    return sign_staff_access(kind="staff", staff_id=staff_id, role=role, token_version=0)


def _guardian_token(guardian_id: uuid.UUID) -> str:
    return sign_staff_access(kind="guardian", staff_id=guardian_id, role="", token_version=0)


async def _seed_staff(session: AsyncSession, role: str = "care_worker") -> StaffAccount:
    account = StaffAccount(name=f"护工-{role}", role=role, password_hash="x")
    session.add(account)
    await session.flush()
    return account


async def _seed_guardian(session: AsyncSession, name: str = "家属甲") -> GuardianAccount:
    account = GuardianAccount(name=name, password_hash="x")
    session.add(account)
    await session.flush()
    return account


async def _seed_work_order(
    session: AsyncSession,
    *,
    severity: str = "yellow",
    status: str = "new",
    order_type: str = "sensitive_alert",
) -> WorkOrder:
    """长者 + 房间 + 床位 + 工单一套种子（room_label / elder_name 链路全通）。"""
    elder = Elder(name="王桂芳")
    session.add(elder)
    await session.flush()
    room = Room(room_no="A-301")
    session.add(room)
    await session.flush()
    session.add(Bed(room_id=room.room_id, bed_no="1", elder_id=elder.elder_id))
    ref = await work_order_service.open_work_order(
        session,
        elder_id=elder.elder_id,
        order_type=order_type,
        severity=severity,
        dedupe_token=f"test:{uuid.uuid4()}",
    )
    if status != "new":
        order = await session.get(WorkOrder, ref.order_id)
        assert order is not None
        order.status = status
        await session.flush()
        return order
    return await session.get(WorkOrder, ref.order_id)


async def _seed_notification(
    session: AsyncSession,
    *,
    guardian_id: uuid.UUID,
    type_: str = "sensitive",
    payload: dict | None = None,
    read: bool = False,
) -> Notification:
    notification = Notification(
        target_type="guardian",
        target_id=guardian_id,
        type=type_,
        payload=payload or {"elder_name": "王桂芳", "event_brief": "测试事件"},
        channel="inapp",
        status="generated",
        is_mock=True,
        dedupe_key=f"test:{uuid.uuid4()}",
    )
    if read:
        import datetime

        notification.read_at = datetime.datetime.now(datetime.UTC)
    session.add(notification)
    await session.flush()
    return notification


# ------------------------------------------------------------------ 工单看板 API


pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_list_work_orders_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/work-orders")
    assert resp.status_code == 401


async def test_list_work_orders_ok(app_with_session, client: AsyncClient) -> None:
    _, session = app_with_session
    account = await _seed_staff(session, role="care_worker")
    order = await _seed_work_order(session, severity="red")
    await work_order_service.transition(
        session,
        order_id=order.order_id,
        to_status="acknowledged",
        actor_id=account.staff_id,
        note="收到",
    )

    resp = await client.get(
        "/api/work-orders",
        headers={"Authorization": f"Bearer {_staff_token(account.staff_id, 'care_worker')}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["order_id"] == str(order.order_id)
    assert row["elder_name"] == "王桂芳"
    assert row["room_label"] == "A-301"
    assert row["severity"] == "red"
    assert row["status"] == "acknowledged"
    ack = next(t for t in row["transitions"] if t["to_status"] == "acknowledged")
    assert ack["actor_name"] == "护工-care_worker"


async def test_transition_injects_actor_and_ignores_forged_id(
    app_with_session, client: AsyncClient
) -> None:
    _, session = app_with_session
    account = await _seed_staff(session, role="care_worker")
    order = await _seed_work_order(session)
    forged = uuid.uuid4()

    resp = await client.post(
        f"/api/work-orders/{order.order_id}/transitions",
        json={"action": "acknowledge", "note": "收到", "actor_id": str(forged)},
        headers={"Authorization": f"Bearer {_staff_token(account.staff_id, 'care_worker')}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "acknowledged"
    ack = next(t for t in body["transitions"] if t["to_status"] == "acknowledged")
    assert ack["actor_id"] == str(account.staff_id)  # 服务端注入
    assert ack["actor_id"] != str(forged)  # 前端伪造值被丢弃


async def test_transition_resolve_requires_note(
    app_with_session, client: AsyncClient
) -> None:
    _, session = app_with_session
    account = await _seed_staff(session)
    order = await _seed_work_order(session)

    resp = await client.post(
        f"/api/work-orders/{order.order_id}/transitions",
        json={"action": "resolve"},
        headers={"Authorization": f"Bearer {_staff_token(account.staff_id, 'care_worker')}"},
    )
    assert resp.status_code == 422


async def test_red_close_requires_actual_handled_at(
    app_with_session, client: AsyncClient
) -> None:
    _, session = app_with_session
    account = await _seed_staff(session)
    order = await _seed_work_order(session, severity="red")

    resp = await client.post(
        f"/api/work-orders/{order.order_id}/transitions",
        json={"action": "resolve", "note": "已上门处置"},
        headers={"Authorization": f"Bearer {_staff_token(account.staff_id, 'care_worker')}"},
    )
    assert resp.status_code == 422  # 红档办结缺 actual_handled_at（service 强制）


async def test_reopen_requires_admin(app_with_session, client: AsyncClient) -> None:
    _, session = app_with_session
    worker = await _seed_staff(session, role="care_worker")
    order = await _seed_work_order(session, status="resolved")

    resp = await client.post(
        f"/api/work-orders/{order.order_id}/transitions",
        json={"action": "reopen", "note": "办结撤销"},
        headers={"Authorization": f"Bearer {_staff_token(worker.staff_id, 'care_worker')}"},
    )
    assert resp.status_code == 403  # reopen 仅 admin

    admin = await _seed_staff(session, role="admin")
    resp = await client.post(
        f"/api/work-orders/{order.order_id}/transitions",
        json={"action": "reopen", "note": "办结撤销"},
        headers={"Authorization": f"Bearer {_staff_token(admin.staff_id, 'admin')}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "reopened"


# ------------------------------------------------------------------ 家属通知 API


async def test_family_notifications_requires_guardian_kind(
    client: AsyncClient,
) -> None:
    resp = await client.get(
        "/api/family/notifications",
        headers={"Authorization": f"Bearer {_staff_token(uuid.uuid4(), 'admin')}"},
    )
    assert resp.status_code == 401  # 拿错凭证体系：kind 不符走 401（auth.py 口径）


async def test_family_notifications_scoped_to_self(
    app_with_session, client: AsyncClient
) -> None:
    _, session = app_with_session
    mine = await _seed_guardian(session)
    other = await _seed_guardian(session, name="家属乙")
    await _seed_notification(session, guardian_id=mine.guardian_id)
    await _seed_notification(session, guardian_id=mine.guardian_id, read=True)
    await _seed_notification(session, guardian_id=other.guardian_id)  # 别人的，必须不可见

    resp = await client.get(
        "/api/family/notifications",
        headers={"Authorization": f"Bearer {_guardian_token(mine.guardian_id)}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert all(item["read_at"] is not None or item["status"] == "generated" for item in body["items"])

    resp = await client.get(
        "/api/family/notifications?unread_only=true",
        headers={"Authorization": f"Bearer {_guardian_token(mine.guardian_id)}"},
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["read_at"] is None


async def test_mark_read_idempotent(app_with_session, client: AsyncClient) -> None:
    _, session = app_with_session
    mine = await _seed_guardian(session)
    notification = await _seed_notification(session, guardian_id=mine.guardian_id)
    headers = {"Authorization": f"Bearer {_guardian_token(mine.guardian_id)}"}

    first = await client.post(
        f"/api/family/notifications/{notification.notification_id}/read", headers=headers
    )
    assert first.status_code == 200
    read_at = first.json()["read_at"]
    assert read_at

    second = await client.post(
        f"/api/family/notifications/{notification.notification_id}/read", headers=headers
    )
    assert second.status_code == 200
    assert second.json()["read_at"] == read_at  # 幂等：返回原 read_at


async def test_mark_read_other_guardian_is_404(
    app_with_session, client: AsyncClient
) -> None:
    _, session = app_with_session
    mine = await _seed_guardian(session)
    other = await _seed_guardian(session, name="家属乙")
    notification = await _seed_notification(session, guardian_id=other.guardian_id)

    resp = await client.post(
        f"/api/family/notifications/{notification.notification_id}/read",
        headers={"Authorization": f"Bearer {_guardian_token(mine.guardian_id)}"},
    )
    assert resp.status_code == 404  # 越权与不存在同口径，不泄露存在性


# 防止 lint 报 unused：notify_service 引用说明通知种子走 service 同源口径
_ = notify_service
