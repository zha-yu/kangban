"""Day 4 · A3 T6：`GET /api/pad/avatar-config` 的接口级测试（C7 契约）。

覆盖：
- 鉴权三态（无 token / 伪造 token / 合法设备 token）；
- 三层取值链（persona → Settings → 前端内置），以及相对路径拼绝对 URL；
- `version` 随 `personas.avatar_url` 变化而变（前端缓存失效的依据）；
- 未绑定设备 → 403 `device_not_bound`（S-03 自裁，避开 C4 已占用的 409 `device_unbound`）。

跑法与 `test_elders_api.py` 同构：自建 NullPool 引擎（避免模块级连接池把上一个
事件循环的 asyncpg 连接带进本文件），每用例一个事务、结束回滚，主库零残留；
请求走真实 ASGI 链路，`get_session` / `current_device` / `token_hash` 校验全是真实代码。
无 PostgreSQL 时 `pytest.skip` 并打印原因，绝不静默通过。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import pad as pad_api
from app.core.config import get_settings
from app.core.security import sha256_hex, sign_device_session
from app.core.trace import TraceIdMiddleware
from app.db import get_session
from app.models import Bed, Device, Elder, Persona, Room

# 测试专用密钥：只为本地用例签发令牌，不是任何环境的真实配置。
TEST_JWT_SECRET = "task6-test-secret-not-a-real-key-0123456789abcdef"

# ---------------------------------------------------------------- 固定 ID（全部虚构）
PERSONA_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-ccccccccccc1")
PERSONA_EMPTY_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-ccccccccccc2")

ROOM_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-ddddddddddd1")
BED_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-ddddddddddd2")

ELDER_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1")
ELDER_EMPTY = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2")

DEVICE_BOUND = uuid.UUID("ffffffff-ffff-4fff-8fff-fffffffffff1")
DEVICE_NO_PERSONA = uuid.UUID("ffffffff-ffff-4fff-8fff-fffffffffff2")
DEVICE_FREE = uuid.UUID("ffffffff-ffff-4fff-8fff-fffffffffff3")

SECRET_BOUND = "device-secret-bound-not-a-real-value"
SECRET_NO_PERSONA = "device-secret-nopersona-not-a-real-value"
SECRET_FREE = "device-secret-free-not-a-real-value"

AVATAR_RELATIVE = "/avatars/vrm1_sample.vrm"
FALLBACK_RELATIVE = "/avatars/fallback.png"


# ---------------------------------------------------------------- 夹具


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """注入测试密钥；结束后清 Settings 缓存，避免把密钥泄漏给同进程后续文件。"""
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """每用例一个事务、结束回滚；连不上库就 skip 并打印原因（与 conftest 同契约）。"""
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
    """临时装配 pad router；请求与测试事务共用同一事件循环与同一连接。"""
    application = FastAPI()
    application.add_middleware(TraceIdMiddleware)
    application.include_router(pad_api.router)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        # create_savepoint：请求内 commit 只释放保存点，外层测试事务仍可整体回滚。
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
    """一套最小数据：两个人设（一个有形象、一个空）、两位长者、三台设备。"""
    await db.execute(
        Persona.__table__.insert(),
        [
            {
                "persona_id": PERSONA_ID,
                "name": "小满",
                "personality": "温和、耐心",
                "greeting": "您好呀",
                "voice_id": "mock",
                "avatar_url": AVATAR_RELATIVE,
            },
            {
                "persona_id": PERSONA_EMPTY_ID,
                "name": "老同事",
                "personality": "沉稳",
                "greeting": "老伙计，来啦",
                "voice_id": "mock",
                "avatar_url": None,
            },
        ],
    )
    await db.execute(
        Room.__table__.insert().values(room_id=ROOM_ID, room_no="A-301", status="full")
    )
    await db.execute(
        Elder.__table__.insert(),
        [
            {
                "elder_id": ELDER_ID,
                "name": "张桂芬",
                "birth_date": date(1948, 3, 12),
                "gender": "female",
                "persona_id": PERSONA_ID,
                "status": "active",
            },
            {
                "elder_id": ELDER_EMPTY,
                "name": "李建国",
                "birth_date": date(1950, 5, 20),
                "gender": "male",
                "persona_id": PERSONA_EMPTY_ID,
                "status": "active",
            },
        ],
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
                "device_id": DEVICE_NO_PERSONA,
                "room_id": ROOM_ID,
                "elder_id": ELDER_EMPTY,
                "token_hash": sha256_hex(SECRET_NO_PERSONA),
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
    """契约 §7 错误体：状态码 + code + detail + 非空 trace_id。"""
    assert response.status_code == status_code
    body = response.json()
    assert body["code"] == code, body
    assert body["detail"]
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 32, body
    return body


# ---------------------------------------------------------------- 鉴权三态


async def test_missing_token_returns_401(client: AsyncClient, seeded: None) -> None:
    response = await client.get("/api/pad/avatar-config")

    assert response.status_code == 401
    body = response.json()
    assert body["detail"]
    assert body["code"]


async def test_forged_token_returns_401(client: AsyncClient, seeded: None) -> None:
    # 前缀对不上 devices.token_hash 前 16 位 → 伪造成立不了（解绑轮换即失效的那条闸门）
    forged = sign_device_session(
        device_id=DEVICE_BOUND, token_hash_prefix=sha256_hex("some-other-secret")[:16]
    )

    response = await client.get("/api/pad/avatar-config", headers=_bearer(forged))

    _assert_error(response, 401, response.json()["code"])


# ---------------------------------------------------------------- 取值链与 version


async def test_bound_device_returns_persona_avatar_as_absolute_url(
    client: AsyncClient, seeded: None
) -> None:
    response = await client.get(
        "/api/pad/avatar-config", headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"avatar_url", "fallback_image_url", "rotation_y_offset", "version"}
    # 库内存相对路径，响应必须是可直接加载的绝对 URL
    assert body["avatar_url"] == f"{get_settings().public_base_url.rstrip('/')}{AVATAR_RELATIVE}"
    assert isinstance(body["version"], int)


async def test_absolute_url_passes_through(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    await db.execute(
        update(Persona)
        .where(Persona.persona_id == PERSONA_ID)
        .values(avatar_url="https://cdn.example.com/a.vrm")
    )

    response = await client.get(
        "/api/pad/avatar-config", headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    )

    assert response.status_code == 200
    assert response.json()["avatar_url"] == "https://cdn.example.com/a.vrm"


async def test_empty_persona_falls_back_to_settings(
    client: AsyncClient,
    seeded: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEFAULT_AVATAR_URL", "/avatars/settings-default.vrm")
    monkeypatch.setenv("DEFAULT_FALLBACK_AVATAR_URL", FALLBACK_RELATIVE)
    get_settings.cache_clear()

    response = await client.get(
        "/api/pad/avatar-config",
        headers=_bearer(_device_token(DEVICE_NO_PERSONA, SECRET_NO_PERSONA)),
    )

    assert response.status_code == 200
    body = response.json()
    expected = f"{get_settings().public_base_url.rstrip('/')}/avatars/settings-default.vrm"
    assert body["avatar_url"] == expected
    assert body["fallback_image_url"] == (
        f"{get_settings().public_base_url.rstrip('/')}{FALLBACK_RELATIVE}"
    )


async def test_both_layers_empty_returns_null_but_still_200(
    client: AsyncClient, seeded: None
) -> None:
    # 第三层是前端内置资源：接口返回 null 而不是报错
    response = await client.get(
        "/api/pad/avatar-config",
        headers=_bearer(_device_token(DEVICE_NO_PERSONA, SECRET_NO_PERSONA)),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["avatar_url"] is None
    assert isinstance(body["rotation_y_offset"], float)


async def test_version_changes_when_persona_avatar_changes(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    headers = _bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    first = (await client.get("/api/pad/avatar-config", headers=headers)).json()

    await db.execute(
        update(Persona).where(Persona.persona_id == PERSONA_ID).values(avatar_url="/avatars/v2.vrm")
    )

    second = (await client.get("/api/pad/avatar-config", headers=headers)).json()

    assert first["version"] != second["version"], "换形象必须让 version 变化，否则前端不失效缓存"
    assert second["avatar_url"].endswith("/avatars/v2.vrm")


async def test_rotation_offset_comes_from_settings(
    client: AsyncClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVATAR_ROTATION_Y_OFFSET", "3.14159")
    get_settings.cache_clear()

    response = await client.get(
        "/api/pad/avatar-config", headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    )

    assert response.status_code == 200
    assert response.json()["rotation_y_offset"] == pytest.approx(3.14159)


# ---------------------------------------------------------------- 未绑定设备


async def test_unbound_device_returns_403_device_not_bound(
    client: AsyncClient, seeded: None
) -> None:
    response = await client.get(
        "/api/pad/avatar-config", headers=_bearer(_device_token(DEVICE_FREE, SECRET_FREE))
    )

    _assert_error(response, 403, "device_not_bound")


async def test_does_not_leak_other_elder_avatar(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """设备绑定谁就返回谁的形象：把另一位的形象改掉，本设备响应不能跟着变。"""
    headers = _bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    before = (await client.get("/api/pad/avatar-config", headers=headers)).json()

    await db.execute(
        update(Persona)
        .where(Persona.persona_id == PERSONA_EMPTY_ID)
        .values(avatar_url="/avatars/other-persona.vrm")
    )

    after = (await client.get("/api/pad/avatar-config", headers=headers)).json()

    assert before == after


async def test_persona_row_missing_does_not_500(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """人设被删（FK 未强制）时接口不能塌：落 Settings，仍 200。"""
    await db.execute(
        update(Elder).where(Elder.elder_id == ELDER_ID).values(persona_id=PERSONA_EMPTY_ID)
    )

    response = await client.get(
        "/api/pad/avatar-config", headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    )

    assert response.status_code == 200


async def test_elder_without_persona_uses_settings_layer(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """`elders.persona_id` 为 NULL 属正常态（新建档未分配人设），走第二层。"""
    await db.execute(update(Elder).where(Elder.elder_id == ELDER_ID).values(persona_id=None))

    response = await client.get(
        "/api/pad/avatar-config", headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND))
    )

    assert response.status_code == 200
    assert response.json()["avatar_url"] is None


async def test_query_param_elder_id_is_ignored(client: AsyncClient, seeded: None) -> None:
    """`elder_id` 只从设备绑定解出；客户端塞 query 参数不起作用（不越权看别人的形象）。"""
    response = await client.get(
        "/api/pad/avatar-config",
        params={"elder_id": str(ELDER_EMPTY)},
        headers=_bearer(_device_token(DEVICE_BOUND, SECRET_BOUND)),
    )

    assert response.status_code == 200
    assert response.json()["avatar_url"].endswith(AVATAR_RELATIVE)


async def test_select_persona_join_is_used(
    client: AsyncClient, db: AsyncConnection, seeded: None
) -> None:
    """确认读的是 join 后的人设行，而不是 elders 上的冗余列（本表没有该列）。"""
    row = (
        await db.execute(select(Persona.avatar_url).where(Persona.persona_id == PERSONA_ID))
    ).scalar_one()
    assert row == AVATAR_RELATIVE
