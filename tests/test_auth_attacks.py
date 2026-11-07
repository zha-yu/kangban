"""task-7 · 三条攻击用例：伪造凭证、越权建档、设备令牌重放。

三层分别验证（契约 §3 的「四类拒绝」）：
1. **签名/类型层**：改 payload、换签名、拿 refresh 当 access —— 401 `TOKEN_INVALID`；
2. **角色层**：护工/家属调 admin 专属的建档接口 —— 403 `ROLE_FORBIDDEN`，且库中零新增；
3. **范围层**：设备令牌跨设备、解绑后重放 —— 403 `SCOPE_FORBIDDEN` / 401 `TOKEN_REVOKED`。

刻意不 mock 鉴权依赖与 service：攻击用例要测的是线上同一条路径（依赖注入 → 服务层 →
写库），mock 掉任何一层都会让「拦下来了」变成「假拦」。
数据库用例沿用 task-3 的事务回滚隔离；连不上库时 skip 并说明原因，不静默通过。
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.logging import JsonFormatter
from app.core.security import (
    sha256_hex,
    sign_staff_access,
    sign_staff_refresh,
)
from app.db import get_session
from app.main import create_app
from app.models import (
    Device,
    Elder,
    GuardianAccount,
    Room,
    StaffAccount,
)

# 固定 UUID：攻击用例要能直接引用主体，不依赖插入顺序或自增
ROOM_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
ELDER_A = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
ELDER_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
DEVICE_A = uuid.UUID("cccccccc-0000-0000-0000-000000000001")
DEVICE_B = uuid.UUID("cccccccc-0000-0000-0000-000000000002")
STAFF_ADMIN = uuid.UUID("dddddddd-0000-0000-0000-000000000001")
STAFF_CARE = uuid.UUID("dddddddd-0000-0000-0000-000000000002")
GUARDIAN_ID = uuid.UUID("eeeeeeee-0000-0000-0000-000000000001")

DEVICE_SECRET_A = "device-a-long-term-secret"
DEVICE_SECRET_B = "device-b-long-term-secret"

TEST_JWT_SECRET = "attack-suite-secret-0123456789abcdef"
PHONE_UNKNOWN = "13900009999"


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """固定 JWT 密钥：攻击用例自己签令牌，密钥必须可预期且与 `.env` 无关。"""
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def engine(jwt_env: None) -> AsyncIterator[AsyncEngine]:
    """独立 engine + NullPool：测试连接不复用应用池，退出即释放。"""
    eng = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with eng.connect():
            pass
    except Exception as exc:  # 任何连接失败（超时/拒绝/口令错/库不存在）都算「无库」
        await eng.dispose()
        pytest.skip(f"PostgreSQL 不可达，跳过攻击用例：{exc!r}")

    yield eng
    await eng.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """每测试一个外层事务；会话内的 commit 只释放 savepoint，最终整体回滚。"""
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            await _seed(session)
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """装配真实的 `create_app()`，只覆盖 `get_session` 以共享测试事务。"""
    application: FastAPI = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as ac:
        yield ac
    application.dependency_overrides.clear()


async def _seed(session: AsyncSession) -> None:
    # 分两批 flush：模型之间没有 relationship()，SQLAlchemy 的表依赖排序在
    # 本地实测不可靠，显式「先房间/长者/账号，再设备」避免 devices 外键踩空
    session.add_all(
        [
            Room(room_id=ROOM_ID, room_no="A-301"),
            Elder(elder_id=ELDER_A, name="张桂芬"),
            Elder(elder_id=ELDER_B, name="李国强"),
            StaffAccount(
                staff_id=STAFF_ADMIN,
                name="管理员",
                role="admin",
                phone="13700000001",
                password_hash="unused-placeholder",
                token_version=0,
                status="active",
            ),
            StaffAccount(
                staff_id=STAFF_CARE,
                name="护工",
                role="care_worker",
                phone="13700000002",
                password_hash="unused-placeholder",
                token_version=0,
                status="active",
            ),
            GuardianAccount(
                guardian_id=GUARDIAN_ID,
                name="家属",
                phone="13700000003",
                password_hash="unused-placeholder",
                token_version=0,
                status="active",
            ),
        ]
    )
    await session.flush()

    session.add_all(
        [
            Device(
                device_id=DEVICE_A,
                room_id=ROOM_ID,
                elder_id=ELDER_A,
                token_hash=sha256_hex(DEVICE_SECRET_A),
                status="online",
            ),
            Device(
                device_id=DEVICE_B,
                room_id=ROOM_ID,
                elder_id=ELDER_B,
                token_hash=sha256_hex(DEVICE_SECRET_B),
                status="online",
            ),
        ]
    )
    await session.commit()


# ------------------------------------------------------------------ helpers


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _care_token() -> str:
    return sign_staff_access(kind="staff", staff_id=STAFF_CARE, role="care_worker", token_version=0)


def _admin_token() -> str:
    return sign_staff_access(kind="staff", staff_id=STAFF_ADMIN, role="admin", token_version=0)


def _guardian_token() -> str:
    # 家属令牌不带 role（契约 §1）；role 传空串才通过签发侧的载荷一致性检查
    return sign_staff_access(kind="guardian", staff_id=GUARDIAN_ID, role="", token_version=0)


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _tamper_payload(token: str, **updates: object) -> str:
    """改 payload 但保留原签名 —— 模拟「只改内容不重签」的攻击者。"""
    header_b64, payload_b64, signature = token.split(".")
    payload = json.loads(_b64url_decode(payload_b64))
    payload.update(updates)
    forged = _b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    return f"{header_b64}.{forged}.{signature}"


def _break_signature(token: str) -> str:
    """改签名段中间一个字符：末位可能只影响 base64 填充位，改中间才必然导致验签失败。"""
    header_b64, payload_b64, signature = token.split(".")
    middle = len(signature) // 2
    replacement = "A" if signature[middle] != "A" else "B"
    broken = signature[:middle] + replacement + signature[middle + 1 :]
    return f"{header_b64}.{payload_b64}.{broken}"


async def _device_token(client: AsyncClient, device_id: uuid.UUID, secret: str) -> str:
    """走真实换会话接口拿设备令牌，不用测试专用签发路径。"""
    resp = await client.post(
        "/api/device-sessions",
        json={"device_id": str(device_id), "device_secret": secret},
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


# ------------------------------------------------------------------ 攻击 A


async def test_forged_tokens_rejected_at_signature_layer(client: AsyncClient) -> None:
    """改 payload / 换签名 / 换 typ：三种伪造都止步于验签与类型校验（401）。"""
    genuine = _care_token()

    # A1 改 payload（role 提权为 admin）但保留旧签名
    escalated = _tamper_payload(genuine, role="admin")
    resp = await client.get("/api/sessions/current", headers=_bearer(escalated))
    assert resp.status_code == 401
    assert resp.json()["code"] == "TOKEN_INVALID"

    # A2 保留 payload、破坏签名
    resp = await client.get("/api/sessions/current", headers=_bearer(_break_signature(genuine)))
    assert resp.status_code == 401
    assert resp.json()["code"] == "TOKEN_INVALID"

    # A3 换 typ：refresh 签名完全合法，但不是可作为请求主体的凭证
    refresh = sign_staff_refresh(kind="staff", staff_id=STAFF_CARE, token_version=0)
    resp = await client.get("/api/sessions/current", headers=_bearer(refresh))
    assert resp.status_code == 401
    assert resp.json()["code"] == "TOKEN_INVALID"


# ------------------------------------------------------------------ 攻击 B


@pytest.mark.parametrize("kind", ["guardian"])
async def test_non_admin_cannot_create_elder(
    client: AsyncClient,
    db_session: AsyncSession,
    kind: str,
) -> None:
    """家属调建档接口：403 + 回查库确认零新增。

    契约 v1.8（2026-11-06 主理人裁决）：E5 已放开到 care_worker，
    这里只保留「非员工主体不可建档」的攻击面。
    """
    token = _care_token() if kind == "care_worker" else _guardian_token()

    before = (await db_session.execute(select(func.count()).select_from(Elder))).scalar_one()

    resp = await client.post("/api/elders", json={"name": "越权建档"}, headers=_bearer(token))
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_FORBIDDEN"

    after = (await db_session.execute(select(func.count()).select_from(Elder))).scalar_one()
    assert after == before, "越权请求必须没有写库，403 不能是「先写后拒」"


# ------------------------------------------------------------------ 攻击 C


async def test_device_token_cannot_access_other_device(client: AsyncClient) -> None:
    """设备 A 的令牌访问设备 B 的候选接口：范围层拒绝（403）。"""
    token_a = await _device_token(client, DEVICE_A, DEVICE_SECRET_A)

    resp = await client.get(f"/api/devices/{DEVICE_B}/elder-candidates", headers=_bearer(token_a))
    assert resp.status_code == 403
    assert resp.json()["code"] == "SCOPE_FORBIDDEN"


async def test_device_token_revoked_after_unbind(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """解绑后旧设备令牌立即失效：token_hash 轮换 → tv 对不上 → 401 TOKEN_REVOKED。"""
    token_a = await _device_token(client, DEVICE_A, DEVICE_SECRET_A)

    resp = await client.post(f"/api/devices/{DEVICE_A}/unbind", headers=_bearer(_admin_token()))
    assert resp.status_code == 200, resp.text
    assert resp.json()["elder_id"] is None

    resp = await client.get(f"/api/devices/{DEVICE_A}/elder-candidates", headers=_bearer(token_a))
    assert resp.status_code == 401
    assert resp.json()["code"] == "TOKEN_REVOKED"

    device = await db_session.get(Device, DEVICE_A)
    assert device is not None
    assert device.token_hash != sha256_hex(DEVICE_SECRET_A), "解绑必须轮换 token_hash"
    assert device.status == "offline"


# ------------------------------------------------------------------ trace_id 贯通


async def test_trace_id_propagates_through_login(client: AsyncClient) -> None:
    """登录请求的响应头、错误体与日志行必须带同一个 trace_id（Day 2 验收口径）。"""
    trace = "attack-suite-trace-0001"
    records: list[dict[str, object]] = []

    def _capture(record: logging.LogRecord) -> None:
        records.append(json.loads(JsonFormatter().format(record)))

    handler = logging.Handler()
    handler.emit = _capture  # type: ignore[method-assign]

    root = logging.getLogger()
    root.addHandler(handler)
    try:
        resp = await client.post(
            "/api/sessions",
            json={"phone": PHONE_UNKNOWN, "password": "whatever"},
            headers={"X-Trace-Id": trace},
        )
    finally:
        root.removeHandler(handler)

    assert resp.status_code == 401
    assert resp.headers["X-Trace-Id"] == trace
    assert resp.json()["trace_id"] == trace
    assert any(item.get("trace_id") == trace for item in records), (
        "日志行必须带同一个 trace_id；JsonFormatter 在请求上下文中取 contextvar"
    )
