"""健康域 REST（C1 · H-2/R-*）接口测试。

覆盖口径（任务卡 C1④：核心权限场景 100%）：

- 401：未带 token 访问任一端点（鉴权前置，不进处理器）；
- 401：家属主体访问员工端点 —— auth.py 既有口径「主体种类错 401 而非 403」
  （CredentialInvalidError，见 app/core/auth.py `_require_kind`）；任务卡
  「越权三例 403」中的家属访问一条按代码现状落为 401，差异已登记计划 §10；
- 422：Pydantic 校验（source 非法值 / 五项指标全空），发生在处理器之前。

S3 联调段（真实 health_service 接入后）：
- 越权三例转正：护工代填非负责长者 403+审计、护工确认/纠错红档 403+审计、
  R-1 视图护工看不到红档；
- 正常路径：H-2 冻结五字段、R-3 确认幂等、R-4 纠错新版本+审计+版本链、
  R-4 参数校验、趋势窗口与 404/403。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import sign_staff_access
from app.db import get_session
from app.main import create_app
from app.models.audit import AuditLog
from app.models.guardian import GuardianAccount
from app.models.organization import Bed, Elder, Room
from app.models.relation import ElderStaffAssignment
from app.models.staff import StaffAccount
from app.services import health_service

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


# ------------------------------------------------------------------ S3 联调段：真实服务接线


@dataclass(frozen=True)
class HealthWorld:
    """S3 最小世界：一房一床一长者 + 一名 primary_care 护工 + 一名医生。"""

    elder_id: uuid.UUID
    care_id: uuid.UUID
    other_care_id: uuid.UUID
    doctor_id: uuid.UUID
    room_no: str
    bed_no: str


@pytest_asyncio.fixture(loop_scope="module")
async def world(db_conn) -> AsyncIterator[HealthWorld]:
    session = AsyncSession(db_conn)
    elder = Elder(name="长者-健康域S3", status="active")
    care = StaffAccount(name="护工-S3", role="care_worker", password_hash="x")
    other_care = StaffAccount(name="护工-S3-无绑定", role="care_worker", password_hash="x")
    doctor = StaffAccount(name="医生-S3", role="doctor", password_hash="x")
    session.add_all([elder, care, other_care, doctor])
    await session.flush()

    room = Room(room_no="B-201", status="partial")
    session.add(room)
    await session.flush()
    bed = Bed(room_id=room.room_id, bed_no="1 号床", status="occupied", elder_id=elder.elder_id)
    assignment = ElderStaffAssignment(
        elder_id=elder.elder_id, staff_id=care.staff_id, relation="primary_care"
    )
    session.add_all([bed, assignment])
    await session.flush()

    try:
        yield HealthWorld(
            elder_id=elder.elder_id,
            care_id=care.staff_id,
            other_care_id=other_care.staff_id,
            doctor_id=doctor.staff_id,
            room_no=room.room_no,
            bed_no=bed.bed_no,
        )
    finally:
        await session.close()


async def _audit_count(db_conn, action: str) -> int:
    session = AsyncSession(db_conn)
    try:
        result = await session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
        )
        return int(result.scalar_one())
    finally:
        await session.close()


async def test_submit_green_record_returns_contract_fields(
    client: AsyncClient, world: HealthWorld
) -> None:
    """H-2 正常路径：绿档返回冻结五字段 + follow_up 文案。"""
    token = _staff_token(world.care_id)
    resp = await client.post(
        "/api/health-records",
        json=_submit_payload(
            elder_id=str(world.elder_id),
            systolic=118,
            diastolic=76,
            sleep_hours=8.0,
            mood_score=4,
        ),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["severity"] == "green"
    assert body["rule_version"] == health_service.RULE_VERSION
    assert body["follow_up"] and "健康日报" in body["follow_up"]
    assert set(body) == {"record_id", "severity", "rule_version", "recorded_at", "follow_up"}


async def test_care_worker_cannot_submit_for_unassigned_elder(
    client: AsyncClient, world: HealthWorld, db_conn
) -> None:
    """越权三例之一（转正）：非 primary_care 护工代填 → 403 + audit_logs 留痕。"""
    before = await _audit_count(db_conn, "health_submit_denied")
    token = _staff_token(world.other_care_id)
    resp = await client.post(
        "/api/health-records",
        json=_submit_payload(elder_id=str(world.elder_id), systolic=118, diastolic=76),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert await _audit_count(db_conn, "health_submit_denied") == before + 1


async def test_care_worker_cannot_confirm_red_record(
    client: AsyncClient, world: HealthWorld, db_conn
) -> None:
    """越权三例之二（转正）：红档护工确认 → 403 + audit_logs 留痕；医生可确认。"""
    red = await health_service.create_health_record(
        AsyncSession(db_conn),
        elder_id=world.elder_id,
        source="staff_scheduled",
        systolic=190,
        diastolic=95,
        sleep_hours=7.0,
        mood_score=4,
        mood_note=None,
        complaint=None,
        operator_id=world.care_id,
    )
    assert red.severity == "red"

    before = await _audit_count(db_conn, "health_review_denied")
    care_token = _staff_token(world.care_id)
    resp = await client.post(
        f"/api/health-records/{red.record_id}/confirm",
        json={},
        headers={"Authorization": f"Bearer {care_token}"},
    )
    assert resp.status_code == 403
    assert await _audit_count(db_conn, "health_review_denied") == before + 1

    doctor_token = _staff_token(world.doctor_id, role="doctor")
    resp = await client.post(
        f"/api/health-records/{red.record_id}/confirm",
        json={"note": "已联系医生"},
        headers={"Authorization": f"Bearer {doctor_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_status"] == "confirmed"


async def test_pending_reviews_hides_red_from_care_worker(
    client: AsyncClient, world: HealthWorld, db_conn
) -> None:
    """越权三例之一（R-1 视图）：护工队列看不到红档，医生全可见。"""
    session = AsyncSession(db_conn)
    yellow = await health_service.create_health_record(
        session,
        elder_id=world.elder_id,
        source="staff_scheduled",
        systolic=150,
        diastolic=85,
        sleep_hours=7.0,
        mood_score=4,
        mood_note=None,
        complaint=None,
        operator_id=world.care_id,
    )
    await health_service.create_health_record(
        session,
        elder_id=world.elder_id,
        source="staff_scheduled",
        systolic=200,
        diastolic=95,
        sleep_hours=7.0,
        mood_score=4,
        mood_note=None,
        complaint=None,
        operator_id=world.care_id,
    )
    await session.close()
    assert yellow.severity == "yellow"

    care_view = await client.get(
        "/api/health-records/pending-reviews",
        headers={"Authorization": f"Bearer {_staff_token(world.care_id)}"},
    )
    assert care_view.status_code == 200
    care_ids = {item["record_id"] for item in care_view.json()}
    assert str(yellow.record_id) in care_ids
    assert all(item["severity"] != "red" for item in care_view.json())
    assert all(item["room_no"] == world.room_no for item in care_view.json())

    doctor_view = await client.get(
        "/api/health-records/pending-reviews",
        headers={"Authorization": f"Bearer {_staff_token(world.doctor_id, role='doctor')}"},
    )
    assert doctor_view.status_code == 200
    doctor_severities = {item["severity"] for item in doctor_view.json()}
    assert "red" in doctor_severities


async def test_confirm_is_idempotent_and_corrected_rejects_confirm(
    client: AsyncClient, world: HealthWorld
) -> None:
    """R-3 幂等：重复确认返回原结果；corrected 记录确认 → 409。"""
    token = _staff_token(world.care_id)
    created = await client.post(
        "/api/health-records",
        json=_submit_payload(elder_id=str(world.elder_id), systolic=150, diastolic=85),  # 黄档
        headers={"Authorization": f"Bearer {token}"},
    )
    record_id = created.json()["record_id"]

    first = await client.post(
        f"/api/health-records/{record_id}/confirm",
        json={"note": "无误"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/health-records/{record_id}/confirm",
        json={"note": "无误"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second.status_code == 200
    assert second.json()["record_id"] == record_id
    assert second.json()["review_status"] == "confirmed"


async def test_correct_creates_version_chain_and_audit(
    client: AsyncClient, world: HealthWorld, db_conn
) -> None:
    """R-4：纠错生成新版本（version+1、superseded_reason=review_correction）+ 审计 + 链。"""
    token = _staff_token(world.care_id)
    created = await client.post(
        "/api/health-records",
        json=_submit_payload(elder_id=str(world.elder_id), systolic=130, diastolic=85),
        headers={"Authorization": f"Bearer {token}"},
    )
    old_id = created.json()["record_id"]

    corrected = await client.post(
        f"/api/health-records/{old_id}/correct",
        json={"fields": {"systolic": 118}, "review_note": "录入笔误"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert corrected.status_code == 200, corrected.text
    new_body = corrected.json()
    assert new_body["version"] == 2
    assert new_body["systolic"] == 118
    # A 的口径：superseded_reason 写在新行（「此行因纠错产生」），旧行只有 superseded_by
    assert new_body["superseded_reason"] == "review_correction"

    detail = await client.get(
        f"/api/health-records/{old_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert detail.status_code == 200
    assert detail.json()["review_status"] == "corrected"
    assert detail.json()["superseded_reason"] is None
    assert str(detail.json()["superseded_by"]) == new_body["record_id"]

    chain = await client.get(
        f"/api/health-records/{old_id}/chain",
        headers={"Authorization": f"Bearer {_staff_token(world.doctor_id, role='doctor')}"},
    )
    assert chain.status_code == 200
    versions = [item["version"] for item in chain.json()]
    assert versions == sorted(versions) == [1, 2]

    assert await _audit_count(db_conn, "health_record_correct") >= 1


async def test_correct_rejects_empty_and_unknown_fields(
    client: AsyncClient, world: HealthWorld
) -> None:
    """R-4 参数校验：空 fields 422、白名单外字段 422。"""
    token = _staff_token(world.care_id)
    created = await client.post(
        "/api/health-records",
        json=_submit_payload(elder_id=str(world.elder_id), systolic=150, diastolic=85),
        headers={"Authorization": f"Bearer {token}"},
    )
    record_id = created.json()["record_id"]
    base = {"Authorization": f"Bearer {token}"}

    empty = await client.post(
        f"/api/health-records/{record_id}/correct", json={"fields": {}}, headers=base
    )
    assert empty.status_code == 422

    unknown = await client.post(
        f"/api/health-records/{record_id}/correct",
        json={"fields": {"severity": "green"}},
        headers=base,
    )
    assert unknown.status_code == 422


async def test_trend_returns_window_points_and_404(
    client: AsyncClient, world: HealthWorld, db_conn
) -> None:
    """趋势：有数据返回升序点列；长者不存在 404；护工 403。"""
    session = AsyncSession(db_conn)
    for systolic in (120, 128):
        await health_service.create_health_record(
            session,
            elder_id=world.elder_id,
            source="staff_scheduled",
            systolic=systolic,
            diastolic=76,
            sleep_hours=7.5,
            mood_score=4,
            mood_note=None,
            complaint=None,
            operator_id=world.care_id,
        )
    await session.close()

    doctor_token = {"Authorization": f"Bearer {_staff_token(world.doctor_id, role='doctor')}"}
    trend = await client.get(f"/api/health-records/trend/{world.elder_id}", headers=doctor_token)
    assert trend.status_code == 200
    body = trend.json()
    assert body["window_days"] == 14
    systolic_values = [p["systolic"] for p in body["points"]]
    assert systolic_values == sorted(systolic_values)
    assert set(systolic_values) >= {120, 128}

    missing = await client.get(f"/api/health-records/trend/{uuid.uuid4()}", headers=doctor_token)
    assert missing.status_code == 404

    forbidden = await client.get(
        f"/api/health-records/trend/{world.elder_id}",
        headers={"Authorization": f"Bearer {_staff_token(world.care_id)}"},
    )
    assert forbidden.status_code == 403
