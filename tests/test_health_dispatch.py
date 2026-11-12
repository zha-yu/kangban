"""健康采集面板下发（day08/contracts.md §C-1 + 照护台契约 H-3）测试。

覆盖：

- H-3 `POST /api/health-records/dispatch`：
  - 401：无 token；
  - 403 + audit：care_worker 非名下长者（AC-32 范围过滤）；
  - 200 三态：voice_dispatched（帧形状 + taskId 幂等键断言）/
    device_offline / no_bound_device；
  - 200：primary_care 护工可对自己名下长者补启动。
- 调度 tick `_tick_panel_push`（窗口内补推）：
  - 窗口内：对当日无记录长者逐台补推、有记录长者跳过（幂等键按日固定）；
  - 窗口外：完全不推。

deliver 经 monkeypatch 拦截（task_dispatch_service 延迟 import
`app.audio.signaling.deliver`，属性查找发生在调用期，可安全替换）。
跑法与 test_pad_avatar.py / test_health_api_records.py 同构。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.api import health_records as health_records_api
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_staff_access
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import Bed, Device, Elder, Room
from app.models.audit import AuditLog
from app.models.relation import ElderStaffAssignment
from app.models.staff import StaffAccount
from app.scheduler.health_jobs import HealthSchedulerRuntime
from app.services import health_service

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "task8-test-secret-not-a-real-key-0123456789abcdef"

# ---------------------------------------------------------------- 固定 ID（全部虚构）

ROOM_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-ddddddddddd5")
BED_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-ddddddddddd6")

ELDER_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee4")
DEVICE_ID = uuid.UUID("ffffffff-ffff-4fff-8fff-fffffffffff6")
DEVICE_SECRET = "device-secret-dispatch-not-a-real-value"

CARE_ID = uuid.UUID("22222222-2222-4222-8222-222222222201")
OTHER_CARE_ID = uuid.UUID("22222222-2222-4222-8222-222222222202")
ADMIN_ID = uuid.UUID("22222222-2222-4222-8222-222222222203")


# ---------------------------------------------------------------- 夹具


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
    application.include_router(health_records_api.router)

    # 沿 test_health_api_records `app_with_session` 先例：整文件共用一个会话，
    # 不在异常路径主动 rollback——审计行已 flush 到外层测试事务供断言，
    # 用例结束由外层事务统一回滚，主库零残留。
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
    """一房一床一长者 + 一台绑定设备 + primary_care 护工 / 无绑定护工 / 管理员。"""
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="C-402", status="full")
    )
    await db.execute(
        Elder.__table__.insert().values(
            elder_id=ELDER_ID,
            name="赵德柱",
            birth_date=datetime.date(1944, 1, 18),
            gender="male",
            status="active",
        )
    )
    await db.execute(
        Bed.__table__.insert().values(
            bed_id=BED_ID,
            room_id=ROOM_ID,
            bed_no="2 号床",
            status="occupied",
            elder_id=ELDER_ID,
        )
    )
    await db.execute(
        Device.__table__.insert().values(
            device_id=DEVICE_ID,
            room_id=ROOM_ID,
            elder_id=ELDER_ID,
            token_hash=sha256_hex(DEVICE_SECRET),
            status="online",
        )
    )
    await db.execute(
        StaffAccount.__table__.insert(),
        [
            {
                "staff_id": CARE_ID,
                "name": "护工-补启动",
                "role": "care_worker",
                "password_hash": "x",
            },
            {
                "staff_id": OTHER_CARE_ID,
                "name": "护工-旁人",
                "role": "care_worker",
                "password_hash": "x",
            },
            {"staff_id": ADMIN_ID, "name": "管理员-补启动", "role": "admin", "password_hash": "x"},
        ],
    )
    await db.execute(
        ElderStaffAssignment.__table__.insert().values(
            elder_id=ELDER_ID, staff_id=CARE_ID, relation="primary_care"
        )
    )


# ---------------------------------------------------------------- 工具


def _staff_token(staff_id: uuid.UUID, role: str) -> str:
    return sign_staff_access(kind="staff", staff_id=staff_id, role=role, token_version=0)


def _dispatch_url() -> str:
    return "/api/health-records/dispatch"


def _captured_deliver(monkeypatch: pytest.MonkeyPatch, result: bool) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []

    def fake(device_id: str, message: dict) -> bool:
        captured.append((device_id, message))
        return result

    monkeypatch.setattr("app.audio.signaling.deliver", fake)
    return captured


# ---------------------------------------------------------------- H-3 门禁


@pytest.mark.usefixtures("seeded")
async def test_dispatch_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(_dispatch_url(), json={"elder_id": str(ELDER_ID)})
    assert resp.status_code == 401


@pytest.mark.usefixtures("seeded")
async def test_dispatch_care_worker_out_of_scope_403_audit(
    client: AsyncClient, db: AsyncConnection
) -> None:
    """AC-32：非 primary_care 护工补启动 → 403 + audit_logs 留痕（action=health_dispatch_denied）。"""
    resp = await client.post(
        _dispatch_url(),
        json={"elder_id": str(ELDER_ID)},
        headers={"Authorization": f"Bearer {_staff_token(OTHER_CARE_ID, 'care_worker')}"},
    )
    assert resp.status_code == 403

    session = AsyncSession(db)
    try:
        count = await session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "health_dispatch_denied")
        )
        assert int(count.scalar_one()) == 1
    finally:
        await session.close()


# ---------------------------------------------------------------- H-3 三态


@pytest.mark.usefixtures("seeded")
async def test_dispatch_voice_dispatched_frame_shape(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在线设备：200 voice_dispatched + 帧形状（§C-1）与 taskId 幂等键断言。"""
    captured = _captured_deliver(monkeypatch, result=True)
    resp = await client.post(
        _dispatch_url(),
        json={"elder_id": str(ELDER_ID)},
        headers={"Authorization": f"Bearer {_staff_token(ADMIN_ID, 'admin')}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"status": "voice_dispatched", "device_count": 1}

    assert len(captured) == 1
    device_id, frame = captured[0]
    assert device_id == str(DEVICE_ID)
    assert frame["type"] == "task_command"
    assert frame["kind"] == "health"
    assert frame["source"] == "staff"
    assert frame["hint"]
    assert frame["task_id"] == (
        f"health:{ELDER_ID}:{datetime.datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d')}"
    )
    assert frame["payload"]["kind"] == "health"
    assert frame["payload"]["source"] == "staff_scheduled"
    assert frame["payload"]["startedAt"]


@pytest.mark.usefixtures("seeded")
async def test_dispatch_device_offline(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """设备离线：deliver False → device_offline（不算本轮失败，由调度补推兜底）。"""
    _captured_deliver(monkeypatch, result=False)
    resp = await client.post(
        _dispatch_url(),
        json={"elder_id": str(ELDER_ID)},
        headers={"Authorization": f"Bearer {_staff_token(ADMIN_ID, 'admin')}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "device_offline"


@pytest.mark.usefixtures("seeded")
async def test_dispatch_no_bound_device(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未绑设备：no_bound_device（运维缺口，不抛错）。"""
    _captured_deliver(monkeypatch, result=True)
    resp = await client.post(
        _dispatch_url(),
        json={"elder_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {_staff_token(ADMIN_ID, 'admin')}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "no_bound_device", "device_count": 0}


@pytest.mark.usefixtures("seeded")
async def test_dispatch_primary_care_care_worker_allowed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """primary_care 护工可对自己名下长者补启动（AC-32 正例）。"""
    _captured_deliver(monkeypatch, result=True)
    resp = await client.post(
        _dispatch_url(),
        json={"elder_id": str(ELDER_ID)},
        headers={"Authorization": f"Bearer {_staff_token(CARE_ID, 'care_worker')}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "voice_dispatched"


# ---------------------------------------------------------------- 调度补推 tick


def _session_factory(db: AsyncConnection) -> Callable[[], AsyncSession]:
    return lambda: AsyncSession(
        bind=db, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


def _runtime(db: AsyncConnection, hour: int, minute: int) -> HealthSchedulerRuntime:
    tz = ZoneInfo("Asia/Shanghai")
    today = datetime.datetime.now(tz).date()
    return HealthSchedulerRuntime(
        session_factory=_session_factory(db),
        now_fn=lambda: datetime.datetime(
            today.year, today.month, today.day, hour, minute, tzinfo=tz
        ),
    )


@pytest.mark.usefixtures("seeded")
async def test_panel_push_inside_window_skips_elder_with_record(
    db: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """窗口内：无记录长者逐台补推；已有记录的长者跳过（有记录=已答完，不打扰）。"""
    runtime = _runtime(db, 19, 5)  # 默认窗口 19:00–20:00 内
    captured = _captured_deliver(monkeypatch, result=True)

    await runtime._tick_panel_push()
    assert len(captured) == 1
    today = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
    assert captured[0][1]["task_id"] == f"health:{ELDER_ID}:{today:%Y%m%d}"

    # 补一条当日记录后再 tick：该长者不再被推
    session = AsyncSession(db)
    try:
        await health_service.create_health_record(
            session,
            elder_id=ELDER_ID,
            source="voice_scheduled",
            systolic=120,
            diastolic=78,
            sleep_hours=7.0,
            mood_score=4,
            mood_note=None,
            complaint=None,
            operator_id=None,
        )
        await session.commit()
    finally:
        await session.close()
    captured.clear()
    await runtime._tick_panel_push()
    assert captured == []


@pytest.mark.usefixtures("seeded")
async def test_panel_push_outside_window_noop(
    db: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """窗口外（21:00）：完全不推帧。"""
    runtime = _runtime(db, 21, 0)
    captured = _captured_deliver(monkeypatch, result=True)

    await runtime._tick_panel_push()
    assert captured == []
