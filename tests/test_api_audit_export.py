"""Day 9 A4 · 审计日志 CSV 导出端点测试（FR-ADM-05 P2）。

覆盖：admin-only 403、200 CSV 内容与表逐字段一致（抽 3 条）、时间/操作人筛选、
出口脱敏（detail 内手机号在 CSV 中为掩码）。
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.redact import redact_text
from app.core.security import sign_staff_access
from app.db import get_session
from app.main import create_app
from app.models import AuditLog, StaffAccount
from app.services.audit_log_service import record_audit

STAFF_ADMIN = uuid.UUID("dddddddd-0000-0000-0000-000000000021")
STAFF_CARE = uuid.UUID("dddddddd-0000-0000-0000-000000000022")
ACTOR_OTHER = uuid.UUID("dddddddd-0000-0000-0000-000000000023")
TEST_JWT_SECRET = "audit-export-secret-0123456789abcdef"


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with db_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            session.add_all(
                [
                    StaffAccount(
                        staff_id=STAFF_ADMIN,
                        name="管理员",
                        role="admin",
                        phone="13700000021",
                        password_hash="unused-placeholder",
                        token_version=0,
                        status="active",
                    ),
                    StaffAccount(
                        staff_id=STAFF_CARE,
                        name="护工",
                        role="care_worker",
                        phone="13700000022",
                        password_hash="unused-placeholder",
                        token_version=0,
                        status="active",
                    ),
                ]
            )
            await session.flush()
            # 种子审计 3 条：2 条 admin 操作 ＋ 1 条他人工单（含 PII 明文 detail）
            await record_audit(
                session,
                actor_type="staff",
                actor_id=STAFF_ADMIN,
                action="elder.read",
                resource_type="elder",
                resource_id=uuid.UUID("bbbbbbbb-0000-0000-0000-000000000031"),
                detail={"result": "forbidden", "reason": "scope", "phone": "13812345678"},
            )
            await record_audit(
                session,
                actor_type="staff",
                actor_id=STAFF_ADMIN,
                action="elder.assign_bed",
                resource_type="elder",
                resource_id=uuid.UUID("bbbbbbbb-0000-0000-0000-000000000032"),
                detail={"result": "success"},
            )
            await record_audit(
                session,
                actor_type="staff",
                actor_id=ACTOR_OTHER,
                action="work_order.create",
                resource_type="work_order",
                resource_id=uuid.UUID("aaaaaaaa-0000-0000-0000-000000000033"),
                detail={"result": "success"},
            )
            await session.commit()
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    application: FastAPI = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as ac:
        yield ac
    application.dependency_overrides.clear()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _admin_token() -> str:
    return sign_staff_access(kind="staff", staff_id=STAFF_ADMIN, role="admin", token_version=0)


def _care_token() -> str:
    return sign_staff_access(kind="staff", staff_id=STAFF_CARE, role="care_worker", token_version=0)


def _parse_csv(body: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(body))
    return list(reader)


# ------------------------------------------------------------------ 权限


async def test_export_rejects_non_admin(client: AsyncClient) -> None:
    for path in ("/api/audit-logs", "/api/audit-logs/export.csv"):
        resp = await client.get(path, headers=_bearer(_care_token()))
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "ROLE_FORBIDDEN"


# ------------------------------------------------------------------ 列表（json）


async def test_list_returns_items_and_total(client: AsyncClient) -> None:
    resp = await client.get("/api/audit-logs", headers=_bearer(_admin_token()))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    actions = [item["action"] for item in body["items"]]
    assert actions == ["elder.read", "elder.assign_bed", "work_order.create"]
    # 行形态对齐前端 AuditLogItem（可选字段＋透传）
    item = body["items"][0]
    assert set(item) >= {"id", "action", "actor_id", "actor_name", "created_at", "detail"}
    assert item["actor_name"] == "管理员", "actor_name 由账号表联查填充"
    assert item["detail"], "detail 为脱敏后 JSON 文本"


async def test_list_actor_name_fuzzy_filter(client: AsyncClient) -> None:
    """actor 支持姓名模糊匹配（联查 staff/guardian）。"""
    resp = await client.get("/api/audit-logs?actor=管理", headers=_bearer(_admin_token()))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2, "管理员的两条审计"
    assert all(item["action"] != "work_order.create" for item in body["items"])


async def test_list_and_csv_share_filters(client: AsyncClient) -> None:
    """列表与 CSV 走同一筛选条件族（行数一致口径）。"""
    q = "?actor=管理"
    list_resp = await client.get(f"/api/audit-logs{q}", headers=_bearer(_admin_token()))
    csv_resp = await client.get(f"/api/audit-logs/export.csv{q}", headers=_bearer(_admin_token()))
    assert list_resp.status_code == 200 and csv_resp.status_code == 200
    rows = _parse_csv(csv_resp.content.decode("utf-8-sig"))
    assert list_resp.json()["total"] == len(rows) == 2


# ------------------------------------------------------------------ 导出与一致性


async def test_export_rows_match_table(client: AsyncClient, db_session: AsyncSession) -> None:
    resp = await client.get("/api/audit-logs/export.csv", headers=_bearer(_admin_token()))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")

    rows = _parse_csv(resp.content.decode("utf-8-sig"))
    table_rows = (
        (await db_session.execute(select(AuditLog).order_by(AuditLog.created_at.asc())))
        .scalars()
        .all()
    )
    assert len(rows) == len(table_rows), "CSV 行数必须与表一致"

    # 抽 3 条逐字段核对（含表内最后一条）；detail 按「出口脱敏后」口径比较——
    # CSV 的 PII 掩码是既定行为（出口脱敏），一致性核对必须先对 DB 侧做同款脱敏。
    for csv_row, db_row in zip(
        rows[:2] + rows[-1:], table_rows[:2] + table_rows[-1:], strict=False
    ):
        assert csv_row["audit_id"] == str(db_row.audit_id)
        assert csv_row["action"] == db_row.action
        assert csv_row["actor_type"] == db_row.actor_type
        assert csv_row["resource_id"] == (
            "" if db_row.resource_id is None else str(db_row.resource_id)
        )
        expected_detail = (
            ""
            if db_row.detail is None
            else json.loads(redact_text(json.dumps(db_row.detail, ensure_ascii=False)))
        )
        actual_detail = "" if csv_row["detail"] == "" else json.loads(csv_row["detail"])
        assert actual_detail == expected_detail


async def test_export_redacts_pii_in_detail(client: AsyncClient) -> None:
    """detail 里的明文手机号在 CSV 中必须为掩码（验收 grep 零命中）。"""
    resp = await client.get("/api/audit-logs/export.csv", headers=_bearer(_admin_token()))
    assert resp.status_code == 200
    text = resp.content.decode("utf-8-sig")
    assert "13812345678" not in text, "明文手机号不得出现在 CSV"
    assert "138****5678" in text, "掩码形态必须存在（redact.py 出口生效）"


async def test_export_filter_by_actor(client: AsyncClient) -> None:
    """actor 支持 uuid 精确匹配。"""
    resp = await client.get(
        f"/api/audit-logs/export.csv?actor={ACTOR_OTHER}", headers=_bearer(_admin_token())
    )
    assert resp.status_code == 200
    rows = _parse_csv(resp.content.decode("utf-8-sig"))
    assert len(rows) == 1
    assert rows[0]["action"] == "work_order.create"


async def test_export_filter_by_time_window(client: AsyncClient) -> None:
    """时间窗早于所有种子行 → 空导出；start_date/end_date 为 yyyy-mm-dd 口径。"""
    resp = await client.get(
        "/api/audit-logs/export.csv?start_date=2099-01-01", headers=_bearer(_admin_token())
    )
    assert resp.status_code == 200
    rows = _parse_csv(resp.content.decode("utf-8-sig"))
    assert rows == []

    # 覆盖今天整窗 → 全部行
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    resp = await client.get(
        f"/api/audit-logs/export.csv?start_date={today}&end_date={today}",
        headers=_bearer(_admin_token()),
    )
    assert resp.status_code == 200
    assert len(_parse_csv(resp.content.decode("utf-8-sig"))) == 3
