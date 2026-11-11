"""照护台告警域 N-3（人工确认）接口测试。

覆盖口径（照护台契约 v1.0 §3.2 + AC-23 状态机）：
- 401：无凭证 / 家属主体（种类不符）都不能确认；
- 200：`delivered` → `acknowledged`，`acknowledged_by` 由服务端注入（不信客户端）；
- 409：`generated`/`pending` 等未送达状态**禁止跳步**；
- 404：通知不存在；
- 幂等：已确认再确认一次仍 200，`acknowledged_at` 不被覆盖。

- 403 + 审计：员工 A 确认员工 B 的通知 → `SCOPE_FORBIDDEN`，且 `audit_logs`
  落一条 `notification_acknowledge_forbidden`（R-D10 修复后的回归口）。
"""

from __future__ import annotations

import datetime
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
from app.models.audit import AuditLog
from app.models.guardian import GuardianAccount
from app.models.notifications import Notification
from app.models.staff import StaffAccount

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module")
async def app_with_session(db_conn) -> AsyncIterator[tuple[FastAPI, AsyncSession]]:
    """create_app + 把 `get_session` 覆写为绑定测试事务的 session（与工单测试同款）。"""
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


def _staff_token(staff_id: uuid.UUID, role: str = "care_worker") -> str:
    return sign_staff_access(kind="staff", staff_id=staff_id, role=role, token_version=0)


def _guardian_token(guardian_id: uuid.UUID) -> str:
    return sign_staff_access(kind="guardian", staff_id=guardian_id, role="", token_version=0)


async def _seed_staff(session: AsyncSession, name: str) -> StaffAccount:
    account = StaffAccount(name=name, role="care_worker", password_hash="x")
    session.add(account)
    await session.flush()
    return account


async def _seed_notification(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    status: str = "delivered",
    read: bool = False,
    created_at: datetime.datetime | None = None,
) -> Notification:
    notification = Notification(
        target_type="staff",
        target_id=staff_id,
        type="sensitive",
        payload={"title": "红色事件", "summary": "收缩压 185mmHg"},
        channel="inapp",
        status=status,
        is_mock=True,
        dedupe_key=f"test:{uuid.uuid4()}",
    )
    if read:
        notification.read_at = datetime.datetime.now(datetime.UTC)
    if created_at is not None:
        notification.created_at = created_at
    session.add(notification)
    await session.flush()
    return notification


async def test_acknowledge_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(f"/api/notifications/{uuid.uuid4()}/acknowledge")
    assert resp.status_code == 401


async def test_acknowledge_rejects_guardian_subject(app_with_session, client: AsyncClient) -> None:
    """家属主体调员工端点 → 401（主体种类不符，契约 §1 鉴权口径）。"""
    _, session = app_with_session
    guardian = GuardianAccount(name="家属甲", password_hash="x")
    session.add(guardian)
    await session.flush()
    notification = await _seed_notification(session, staff_id=uuid.uuid4())

    resp = await client.post(
        f"/api/notifications/{notification.notification_id}/acknowledge",
        headers={"Authorization": f"Bearer {_guardian_token(guardian.guardian_id)}"},
    )
    assert resp.status_code == 401


async def test_acknowledge_delivered_then_idempotent(app_with_session, client: AsyncClient) -> None:
    """delivered → acknowledged；再次调用幂等，时间戳不被覆盖。"""
    _, session = app_with_session
    staff = await _seed_staff(session, "护工甲")
    notification = await _seed_notification(session, staff_id=staff.staff_id)
    headers = {"Authorization": f"Bearer {_staff_token(staff.staff_id)}"}

    first = await client.post(
        f"/api/notifications/{notification.notification_id}/acknowledge", headers=headers
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "acknowledged"
    assert first.json()["acknowledged_by"] == str(staff.staff_id)

    await session.refresh(notification)
    assert notification.status == "acknowledged"
    assert notification.acknowledged_by == staff.staff_id
    stamp = notification.acknowledged_at
    assert isinstance(stamp, datetime.datetime)

    second = await client.post(
        f"/api/notifications/{notification.notification_id}/acknowledge", headers=headers
    )
    assert second.status_code == 200, second.text
    await session.refresh(notification)
    assert notification.acknowledged_at == stamp, "幂等：重复确认不得刷新时间戳"


@pytest.mark.parametrize("status", ["generated", "pending", "failed"])
async def test_acknowledge_before_delivered_is_conflict(
    app_with_session, client: AsyncClient, status: str
) -> None:
    """禁止跳步：非 delivered 一律 409 CONFLICT（AC-23）。"""
    _, session = app_with_session
    staff = await _seed_staff(session, f"护工-{status}")
    notification = await _seed_notification(session, staff_id=staff.staff_id, status=status)

    resp = await client.post(
        f"/api/notifications/{notification.notification_id}/acknowledge",
        headers={"Authorization": f"Bearer {_staff_token(staff.staff_id)}"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "CONFLICT"


async def test_acknowledge_unknown_notification_is_404(
    app_with_session, client: AsyncClient
) -> None:
    _, session = app_with_session
    staff = await _seed_staff(session, "护工乙")

    resp = await client.post(
        f"/api/notifications/{uuid.uuid4()}/acknowledge",
        headers={"Authorization": f"Bearer {_staff_token(staff.staff_id)}"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


async def test_acknowledge_bad_uuid_is_422(app_with_session, client: AsyncClient) -> None:
    """path 参数不是 uuid → 422 VALIDATION_ERROR（契约 §1 错误体）。"""
    _, session = app_with_session
    staff = await _seed_staff(session, "护工丙")

    resp = await client.post(
        "/api/notifications/not-a-uuid/acknowledge",
        headers={"Authorization": f"Bearer {_staff_token(staff.staff_id)}"},
    )
    assert resp.status_code in (400, 422), resp.text


async def test_acknowledge_foreign_notification_is_403_and_audited(
    app_with_session, client: AsyncClient
) -> None:
    """R-D10 回归：员工只能确认发给自己的通知；越权 403 且必须留审计。"""
    from sqlalchemy import select

    _, session = app_with_session
    owner = await _seed_staff(session, "护工-收件人")
    intruder = await _seed_staff(session, "护工-路人")
    notification = await _seed_notification(session, staff_id=owner.staff_id)

    resp = await client.post(
        f"/api/notifications/{notification.notification_id}/acknowledge",
        headers={"Authorization": f"Bearer {_staff_token(intruder.staff_id)}"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"

    await session.refresh(notification)
    assert notification.status == "delivered", "越权请求不得改动通知状态"
    assert notification.acknowledged_by is None

    rows = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "notification_acknowledge_forbidden",
                    AuditLog.resource_id == notification.notification_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "403 必须落一条审计（照护台契约 §2）"
    assert rows[0].actor_type == "staff"
    assert rows[0].actor_id == intruder.staff_id


# ------------------------------------------------------------------ N-1 列表


async def test_list_notifications_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/notifications")).status_code == 401


async def test_list_notifications_only_mine_newest_first(
    app_with_session, client: AsyncClient
) -> None:
    """只出「发给我」的通知；他人与家属侧的都不出现；按 created_at 倒序。"""
    _, session = app_with_session
    me = await _seed_staff(session, "护工-我")
    other = await _seed_staff(session, "护工-别人")
    now = datetime.datetime.now(datetime.UTC)
    older = await _seed_notification(
        session, staff_id=me.staff_id, created_at=now - datetime.timedelta(minutes=5)
    )
    newer = await _seed_notification(session, staff_id=me.staff_id, created_at=now)
    await _seed_notification(session, staff_id=other.staff_id, created_at=now)

    resp = await client.get(
        "/api/notifications", headers={"Authorization": f"Bearer {_staff_token(me.staff_id)}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [item["notification_id"] for item in body["items"]]
    assert ids == [str(newer.notification_id), str(older.notification_id)]
    assert body["total"] == 2
    assert body["items"][0]["target_type"] == "staff"
    # R-D12：service 未 JOIN 姓名 → 如实 null，不编
    assert body["items"][0]["acknowledged_by_name"] is None


async def test_list_notifications_unread_only(app_with_session, client: AsyncClient) -> None:
    _, session = app_with_session
    me = await _seed_staff(session, "护工-未读")
    await _seed_notification(session, staff_id=me.staff_id)
    await _seed_notification(session, staff_id=me.staff_id, read=True)

    resp = await client.get(
        "/api/notifications?unread_only=true",
        headers={"Authorization": f"Bearer {_staff_token(me.staff_id)}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 1


async def test_list_notifications_rejects_bad_page_size(
    app_with_session, client: AsyncClient
) -> None:
    _, session = app_with_session
    me = await _seed_staff(session, "护工-分页")
    resp = await client.get(
        "/api/notifications?page_size=101",
        headers={"Authorization": f"Bearer {_staff_token(me.staff_id)}"},
    )
    assert resp.status_code == 422, resp.text


# ------------------------------------------------------------------ N-2 已读


async def test_mark_read_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(f"/api/notifications/{uuid.uuid4()}/read")
    assert resp.status_code == 401


async def test_mark_read_is_idempotent_and_keeps_status(
    app_with_session, client: AsyncClient
) -> None:
    """只写 read_at、不动 status（已读≠确认）；重复调用返回原时间。"""
    _, session = app_with_session
    me = await _seed_staff(session, "护工-已读")
    notification = await _seed_notification(session, staff_id=me.staff_id)
    headers = {"Authorization": f"Bearer {_staff_token(me.staff_id)}"}

    first = await client.post(
        f"/api/notifications/{notification.notification_id}/read", headers=headers
    )
    assert first.status_code == 200, first.text
    await session.refresh(notification)
    assert notification.read_at is not None
    assert notification.status == "delivered", "已读不得推进状态机"
    stamp = notification.read_at

    second = await client.post(
        f"/api/notifications/{notification.notification_id}/read", headers=headers
    )
    assert second.status_code == 200, second.text
    await session.refresh(notification)
    assert notification.read_at == stamp, "幂等：不得刷新 read_at"


async def test_mark_read_foreign_is_404(app_with_session, client: AsyncClient) -> None:
    """不属于自己的通知 → 404（与不存在同口径，不泄露存在性）。"""
    _, session = app_with_session
    owner = await _seed_staff(session, "护工-收件人2")
    intruder = await _seed_staff(session, "护工-路人2")
    notification = await _seed_notification(session, staff_id=owner.staff_id)

    resp = await client.post(
        f"/api/notifications/{notification.notification_id}/read",
        headers={"Authorization": f"Bearer {_staff_token(intruder.staff_id)}"},
    )
    assert resp.status_code == 404, resp.text
    await session.refresh(notification)
    assert notification.read_at is None
