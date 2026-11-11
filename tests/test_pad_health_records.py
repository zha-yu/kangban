"""pad 健康采集答案回传 §P-1（`POST /api/pad/health-records`）接口测试。

覆盖口径（day08/contracts.md §P-1，参照 test_pad_avatar.py / test_health_api_records.py）：

- 401 三态：无 token / 伪造 token（口径同 C7 avatar-config：只认设备凭证）；
- 403 `device_not_bound`：设备未绑定长者；
- 200 正常落库：elder_id 服务端解出（客户端传了也不认）、source 按采集窗口判定
  （monkeypatch `HEALTH_CHECKUP_START/END` 两个窗口夹逼当前时刻，确定性断言）、
  响应恰为冻结五字段；
- 分级联动：红档写 risk_events（黄红档自动进复核队列属 health_service 既有行为，
  不在本文件重复）；
- 422：五项指标全空。

跑法与 test_pad_avatar.py 同构：conftest module 级共享引擎，每用例一个事务、
结束回滚；请求走真实 ASGI 链路。无 PostgreSQL 时 pytest.skip。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.api import pad as pad_api
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import Bed, Device, Elder, Room
from app.models.health import HealthRecord
from app.models.medication import RiskEvent

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "task8-test-secret-not-a-real-key-0123456789abcdef"

# ---------------------------------------------------------------- 固定 ID（全部虚构）

ROOM_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-ddddddddddd3")
BED_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-ddddddddddd4")

ELDER_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee3")

DEVICE_BOUND = uuid.UUID("ffffffff-ffff-4fff-8fff-fffffffffff4")
DEVICE_FREE = uuid.UUID("ffffffff-ffff-4fff-8fff-fffffffffff5")

SECRET_BOUND = "device-secret-bound-not-a-real-value"
SECRET_FREE = "device-secret-free-not-a-real-value"


# ---------------------------------------------------------------- 夹具


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """注入测试密钥；结束后清 Settings 缓存，避免把密钥泄漏给同进程后续文件。"""
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def window_inside(jwt_env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """采集窗口覆盖任意时刻（00:00–23:59）→ source 必判 voice_scheduled。"""
    monkeypatch.setenv("HEALTH_CHECKUP_START", "00:00")
    monkeypatch.setenv("HEALTH_CHECKUP_END", "23:59")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def window_outside(jwt_env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """窗口倒挂（23:59–23:58，恒为空）→ source 必判 voice_active。"""
    monkeypatch.setenv("HEALTH_CHECKUP_START", "23:59")
    monkeypatch.setenv("HEALTH_CHECKUP_END", "23:58")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """每用例一个事务、结束回滚（db_engine 为 conftest 的 module 级共享引擎）。"""
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
async def client(db: AsyncConnection) -> AsyncIterator[AsyncClient]:
    """临时装配 pad router；请求与测试事务共用同一事件循环与同一连接。

    沿 test_health_api_records `app_with_session` 先例：整文件共用一个会话、
    不在请求路径 commit/rollback——服务层只 flush，落库行对外层事务可见，
    用例结束由外层事务统一回滚，主库零残留。
    """
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(pad_api.router)

    session = AsyncSession(db)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    await session.close()


@pytest.fixture
async def seeded(db: AsyncConnection) -> None:
    """最小世界：一房一床一位长者 + 一台绑定设备 + 一台未绑定设备。"""
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="C-401", status="full")
    )
    await db.execute(
        Elder.__table__.insert().values(
            elder_id=ELDER_ID,
            name="王秀兰",
            birth_date=date(1946, 7, 2),
            gender="female",
            status="active",
        )
    )
    await db.execute(
        Bed.__table__.insert().values(
            bed_id=BED_ID,
            room_id=ROOM_ID,
            bed_no="1 号床",
            status="occupied",
            elder_id=ELDER_ID,
        )
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
                "device_id": DEVICE_FREE,
                "room_id": ROOM_ID,
                "elder_id": None,
                "token_hash": sha256_hex(SECRET_FREE),
                "status": "offline",
            },
        ],
    )


# ---------------------------------------------------------------- 工具


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _device_token(device_id: uuid.UUID, secret: str) -> str:
    return sign_device_session(device_id=device_id, token_hash_prefix=sha256_hex(secret)[:16])


def _assert_error(response: Response, status_code: int, code: str) -> dict[str, object]:
    assert response.status_code == status_code
    body = response.json()
    assert body["code"] == code, body
    assert body["detail"]
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 32, body
    return body


# ---------------------------------------------------------------- 鉴权三态


@pytest.mark.usefixtures("seeded")
async def test_missing_token_returns_401(client: AsyncClient) -> None:
    resp = await client.post("/api/pad/health-records", json={"systolic": 120})
    assert resp.status_code == 401


@pytest.mark.usefixtures("seeded")
async def test_forged_token_returns_401(client: AsyncClient) -> None:
    forged = sign_device_session(
        device_id=DEVICE_BOUND, token_hash_prefix=sha256_hex("some-other-secret")[:16]
    )
    resp = await client.post(
        "/api/pad/health-records", json={"systolic": 120}, headers=_bearer(forged)
    )
    assert resp.status_code == 401


@pytest.mark.usefixtures("seeded")
async def test_unbound_device_returns_403_device_not_bound(client: AsyncClient) -> None:
    token = _device_token(DEVICE_FREE, SECRET_FREE)
    resp = await client.post(
        "/api/pad/health-records", json={"systolic": 120}, headers=_bearer(token)
    )
    _assert_error(resp, 403, "device_not_bound")


# ---------------------------------------------------------------- 正常落库


@pytest.mark.usefixtures("seeded", "window_inside")
async def test_submit_green_inside_window_voice_scheduled(
    client: AsyncClient, db: AsyncConnection
) -> None:
    """窗口内提交：绿档五字段齐全，source=voice_scheduled，elder_id 服务端解出。"""
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)
    resp = await client.post(
        "/api/pad/health-records",
        json={"systolic": 118, "diastolic": 76, "sleep_hours": 7.5, "mood_score": 4},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"record_id", "severity", "rule_version", "recorded_at", "follow_up"}
    assert body["severity"] == "green"
    assert body["follow_up"]

    session = AsyncSession(db)
    try:
        row = (
            await session.execute(select(HealthRecord).where(HealthRecord.elder_id == ELDER_ID))
        ).scalar_one()
        assert row.source == "voice_scheduled"
        assert row.operator_id is None
    finally:
        await session.close()


@pytest.mark.usefixtures("seeded", "window_outside")
async def test_submit_outside_window_voice_active(client: AsyncClient, db: AsyncConnection) -> None:
    """窗口外提交：source 判 voice_active；elder_id / source 客户端不可传。"""
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)
    resp = await client.post(
        "/api/pad/health-records",
        json={"systolic": 118, "diastolic": 76},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text

    session = AsyncSession(db)
    try:
        row = (
            await session.execute(select(HealthRecord).where(HealthRecord.elder_id == ELDER_ID))
        ).scalar_one()
        assert row.source == "voice_active"
    finally:
        await session.close()


@pytest.mark.usefixtures("seeded")
async def test_submit_rejects_client_elder_id(client: AsyncClient) -> None:
    """§P-1 安全口径：elder_id 只能服务端解出，请求体传入即 422（extra=forbid）。"""
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)
    resp = await client.post(
        "/api/pad/health-records",
        json={"elder_id": str(uuid.uuid4()), "systolic": 118, "diastolic": 76},
        headers=_bearer(token),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]


# ---------------------------------------------------------------- 分级与值域


@pytest.mark.usefixtures("seeded", "window_inside")
async def test_submit_red_creates_risk_event(client: AsyncClient, db: AsyncConnection) -> None:
    """红档联动（AC-21）：pad 回传与 H-2 代填同走 create_health_record。"""
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)
    resp = await client.post(
        "/api/pad/health-records",
        json={"systolic": 185, "diastolic": 95, "sleep_hours": 7.0, "mood_score": 4},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["severity"] == "red"

    session = AsyncSession(db)
    try:
        count = await session.execute(
            select(func.count()).select_from(RiskEvent).where(RiskEvent.elder_id == ELDER_ID)
        )
        assert int(count.scalar_one()) >= 1
    finally:
        await session.close()


@pytest.mark.usefixtures("seeded", "window_inside")
async def test_submit_complaint_only_is_yellow(client: AsyncClient, db: AsyncConnection) -> None:
    """只有主诉：缺指标字段 → 黄档（字段缺失判黄口径）；「没有」解析为 null。"""
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)
    resp = await client.post(
        "/api/pad/health-records",
        json={"complaint": "有点不舒服"},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["severity"] == "yellow"

    session = AsyncSession(db)
    try:
        row = (
            await session.execute(select(HealthRecord).where(HealthRecord.elder_id == ELDER_ID))
        ).scalar_one()
        assert row.complaint == "有点不舒服"
        assert row.systolic is None
    finally:
        await session.close()


@pytest.mark.usefixtures("seeded")
async def test_submit_all_empty_returns_422(client: AsyncClient) -> None:
    """五项全空 → 422（至少一项非空校验，发生在处理器之前）。"""
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)
    resp = await client.post("/api/pad/health-records", json={}, headers=_bearer(token))
    assert resp.status_code == 422


@pytest.mark.usefixtures("seeded")
async def test_submit_out_of_range_returns_422(client: AsyncClient) -> None:
    """值域越界（systolic 500 > 300）→ 422（H-2 同口径）。"""
    token = _device_token(DEVICE_BOUND, SECRET_BOUND)
    resp = await client.post(
        "/api/pad/health-records", json={"systolic": 500}, headers=_bearer(token)
    )
    assert resp.status_code == 422
