"""家属日报 F2 详情接口测试（Day 8 · D1）。

覆盖口径（家属端契约 v1.2 §4 F2 / §5）：
- 401：无凭证 / 员工主体（种类不符）；
- 200：四键 `medication_stats` 分列、`summary_text` 含固定声明、`attention_events` 为数组；
- 404：report_id 不存在；
- 403 + 审计：日报所属长者不在绑定范围。

**F1 列表暂不在本文件**：契约路径是 `/api/family/elders/{elder_id}/reports`，
而 `report_service.list_reports_for_guardian()` 只提供跨长者全量分页、没有 elder
过滤参数，api 里过滤会把分页算错——已提 J-C2c，参数到位后补 `test_list_*`。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import sign_staff_access
from app.db import get_session
from app.main import create_app
from app.models.audit import AuditLog
from app.models.guardian import GuardianAccount
from app.models.organization import Elder
from app.models.relation import ElderGuardian
from app.services import report_service

pytestmark = pytest.mark.asyncio(loop_scope="module")

DISCLAIMER = "教学模拟信息，不构成医疗诊断"


@pytest_asyncio.fixture(loop_scope="module")
async def app_with_session(db_conn) -> AsyncIterator[tuple[FastAPI, AsyncSession]]:
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


def _guardian_token(guardian_id: uuid.UUID) -> str:
    return sign_staff_access(kind="guardian", staff_id=guardian_id, role="", token_version=0)


def _staff_token(staff_id: uuid.UUID) -> str:
    return sign_staff_access(kind="staff", staff_id=staff_id, role="care_worker", token_version=0)


async def _seed_elder(session: AsyncSession, name: str) -> Elder:
    elder = Elder(name=name, status="active")
    session.add(elder)
    await session.flush()
    return elder


async def _seed_guardian(session: AsyncSession, name: str) -> GuardianAccount:
    guardian = GuardianAccount(name=name, password_hash="x")
    session.add(guardian)
    await session.flush()
    return guardian


async def test_report_detail_requires_auth(client: AsyncClient) -> None:
    assert (await client.get(f"/api/family/reports/{uuid.uuid4()}")).status_code == 401


async def test_report_detail_rejects_staff_subject(app_with_session, client: AsyncClient) -> None:
    """员工主体调家属端 → 401（主体种类不符）。"""
    _, session = app_with_session
    elder = await _seed_elder(session, "长者-员工主体")
    ref = await report_service.generate_daily_report(session, elder_id=elder.elder_id)

    resp = await client.get(
        f"/api/family/reports/{ref.report_id}",
        headers={"Authorization": f"Bearer {_staff_token(uuid.uuid4())}"},
    )
    assert resp.status_code == 401


async def test_report_detail_ok(app_with_session, client: AsyncClient) -> None:
    """绑定长者 → 200，四键分列、含固定声明、事件为数组。"""
    _, session = app_with_session
    elder = await _seed_elder(session, "长者-张桂芬")
    guardian = await _seed_guardian(session, "家属-女儿")
    session.add(
        ElderGuardian(
            elder_id=elder.elder_id,
            guardian_id=guardian.guardian_id,
            relation="女儿",
            status="active",
        )
    )
    await session.flush()
    ref = await report_service.generate_daily_report(session, elder_id=elder.elder_id)

    resp = await client.get(
        f"/api/family/reports/{ref.report_id}",
        headers={"Authorization": f"Bearer {_guardian_token(guardian.guardian_id)}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["report_id"] == str(ref.report_id)
    assert body["elder_id"] == str(elder.elder_id)
    assert body["version"] == 1
    assert DISCLAIMER in body["summary_text"], "日报必须带固定声明"
    assert set(body["medication_stats"]) == {
        "taken_reported",
        "refused",
        "missed_unconfirmed",
        "notification_failed",
    }, "四类终态必须分列，不得合并"
    assert isinstance(body["attention_events"], list)
    assert body["audio_url"] is None, "P0 阶段 audio_url 恒 null（FR-RPT-04）"


async def test_report_detail_not_found(app_with_session, client: AsyncClient) -> None:
    _, session = app_with_session
    guardian = await _seed_guardian(session, "家属-404")
    resp = await client.get(
        f"/api/family/reports/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {_guardian_token(guardian.guardian_id)}"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


async def test_report_detail_out_of_scope_is_403_and_audited(
    app_with_session, client: AsyncClient
) -> None:
    """长者不在绑定范围 → 403 + 审计（契约 §4 F2 括注）。"""
    _, session = app_with_session
    mine = await _seed_elder(session, "长者-我家")
    other = await _seed_elder(session, "长者-别家")
    guardian = await _seed_guardian(session, "家属-越权")
    session.add(
        ElderGuardian(
            elder_id=mine.elder_id,
            guardian_id=guardian.guardian_id,
            relation="女儿",
            status="active",
        )
    )
    await session.flush()
    ref = await report_service.generate_daily_report(session, elder_id=other.elder_id)

    resp = await client.get(
        f"/api/family/reports/{ref.report_id}",
        headers={"Authorization": f"Bearer {_guardian_token(guardian.guardian_id)}"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"

    rows = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "daily_report_read_forbidden",
                    AuditLog.resource_id == ref.report_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "403 必须落一条审计"
    assert rows[0].actor_type == "guardian"
    assert rows[0].actor_id == guardian.guardian_id
