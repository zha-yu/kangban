"""健康域 REST（C1 · H-2/R-*）接口测试 —— S1 骨架段。

覆盖口径（任务卡 C1④：核心权限场景 100%，S1 先落门禁与校验层）：

- 401：未带 token 访问任一端点（鉴权前置，不进处理器）；
- 401：家属主体访问员工端点 —— auth.py 既有口径「主体种类错 401 而非 403」
  （CredentialInvalidError，见 app/core/auth.py `_require_kind`）；任务卡
  「越权三例 403」中的家属访问一条按代码现状落为 401，差异已登记计划 §10；
- 422：Pydantic 校验（source 非法值 / 五项指标全空），发生在处理器之前，
  不触碰 NotImplementedError 桩；
- skip 两例：护工代填非负责长者、护工确认红档 —— 依赖 S3 联调段的真实
  health_service 范围过滤，桩阶段无断言意义，按 SKIPS 规范写明原因。

S3 联调段补齐：403+审计断言（越权三例转正）、severity 分级、复核幂等、
版本链留痕、趋势序列窗口。
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
from app.models.staff import StaffAccount

# ------------------------------------------------------------------ 夹具


@pytest_asyncio.fixture(loop_scope="module")
async def app_with_session(db_conn) -> AsyncIterator[tuple[FastAPI, AsyncSession]]:
    """create_app + 把 get_session 覆写为绑定测试事务的 session（沿用 workorders 先例）。"""
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


@pytest_asyncio.fixture(loop_scope="module")
async def staff_account(db_conn) -> AsyncIterator[AsyncSession]:
    session = AsyncSession(db_conn)
    account = StaffAccount(name="护工-健康域", role="care_worker", password_hash="x")
    session.add(account)
    await session.flush()
    try:
        yield account
    finally:
        await session.close()


def _staff_token(staff_id: uuid.UUID, role: str = "care_worker") -> str:
    return sign_staff_access(kind="staff", staff_id=staff_id, role=role, token_version=0)


def _guardian_token(guardian_id: uuid.UUID) -> str:
    return sign_staff_access(kind="guardian", staff_id=guardian_id, role="", token_version=0)


def _submit_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "elder_id": str(uuid.uuid4()),
        "source": "staff_active",
        "systolic": 118,
        "diastolic": 76,
    }
    payload.update(overrides)
    return payload


# ------------------------------------------------------------------ 401 门禁

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_submit_requires_auth(client: AsyncClient) -> None:
    resp = await client.post("/api/health-records", json=_submit_payload())
    assert resp.status_code == 401


async def test_pending_reviews_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/health-records/pending-reviews")
    assert resp.status_code == 401


async def test_trend_requires_auth(client: AsyncClient) -> None:
    resp = await client.get(f"/api/health-records/trend/{uuid.uuid4()}")
    assert resp.status_code == 401


async def test_record_detail_requires_auth(client: AsyncClient) -> None:
    record_id = uuid.uuid4()
    resp = await client.get(f"/api/health-records/{record_id}")
    assert resp.status_code == 401


# ------------------------------------------------------------------ 主体种类错 401


async def test_guardian_accesses_health_endpoints_rejected(
    client: AsyncClient,
    app_with_session: tuple[FastAPI, AsyncSession],
) -> None:
    """家属主体访问员工端点 → 401（auth.py `_require_kind` 既有口径）。

    任务卡表述为 403，按代码现状落 401；审计断言随 S3 转正一并补。
    """
    _, session = app_with_session
    guardian = GuardianAccount(name="家属-健康域", password_hash="x")
    session.add(guardian)
    await session.flush()

    token = _guardian_token(guardian.guardian_id)
    for method, url in (
        ("post", "/api/health-records"),
        ("get", "/api/health-records/pending-reviews"),
    ):
        resp = (
            await client.post(
                url, json=_submit_payload(), headers={"Authorization": f"Bearer {token}"}
            )
            if method == "post"
            else await client.get(url, headers={"Authorization": f"Bearer {token}"})
        )
        assert resp.status_code == 401, f"{method.upper()} {url} 应按主体种类错 401"


# ------------------------------------------------------------------ 422 参数校验


async def test_submit_rejects_invalid_source(
    client: AsyncClient,
    staff_account: StaffAccount,
) -> None:
    token = _staff_token(staff_account.staff_id)
    resp = await client.post(
        "/api/health-records",
        json=_submit_payload(source="voice_auto"),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_submit_rejects_all_empty_metrics(
    client: AsyncClient,
    staff_account: StaffAccount,
) -> None:
    token = _staff_token(staff_account.staff_id)
    resp = await client.post(
        "/api/health-records",
        json=_submit_payload(systolic=None, diastolic=None),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


# ------------------------------------------------------------------ 范围类越权（S3 转正）


@pytest.mark.skip(reason="依赖 S3 联调段真实 health_service 长者范围过滤（护工仅可代填名下长者）")
async def test_care_worker_cannot_submit_for_unassigned_elder() -> None:
    """越权三例之一（403 + audit_logs 断言）：S3 接入真实服务后转正。"""


@pytest.mark.skip(reason="依赖 S3 联调段真实 health_service 红档角色校验（红档仅 doctor/admin）")
async def test_care_worker_cannot_confirm_red_record() -> None:
    """越权三例之二（403 + audit_logs 断言）：S3 接入真实服务后转正。"""
