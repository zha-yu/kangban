"""app/api/audio.py utterance 端点集成测试。

复用 test_auth_api.py 的 fixture 结构：真实 DB + NullPool 引擎 + 每用例事务 rollback。
核心复用：device-sessions 端点换会话拿 device_token → 调 utterance。

分层验证（AGENTS §2 硬约束）：audio.py **不 import services/models**，
只做参数校验 → Mock ASR/TTS → 序列化；本测试用真实 app + 真实 DB 验证完整链路。
"""

from __future__ import annotations

import io
import struct
import wave
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import audio as audio_api
from app.api import auth as auth_api
from app.audio.asr import MockASRProvider
from app.audio.tts import MockTTSProvider
from app.core.auth import (
    CredentialExpiredError,
    CredentialInvalidError,
    CredentialRevokedError,
)
from app.core.config import get_settings
from app.core.security import TokenExpiredError, sha256_hex
from app.db import get_session as _app_get_session
from app.main import handle_http_exception, handle_request_validation
from app.models.organization import Bed, Device, Elder, Room

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DEVICE_SECRET_PLAIN = "device-long-secret-plain-abc123"
_MAX_AUDIO_SECONDS = 30  # 与 audio.py 硬编码一致


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def audio_test_engine() -> AsyncIterator[AsyncEngine]:
    """NullPool 引擎 + 启动时清 DB。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "TRUNCATE elder_guardians, elder_staff_assignments, "
                    "guardian_accounts, staff_accounts, staff_tokens, "
                    "devices, elders, beds, personas, rooms CASCADE"
                )
            )
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def audio_test_session(audio_test_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """外层事务包住整个用例：service 层（device-sessions 登录）的真实 commit 只释放
    savepoint，结束整体回滚，共享 dev 库零残留（模式同 test_elders_api.client）。

    不能用裸 session：登录 commit 会把种子数据落进共享库，文件组合顺序变化时
    污染 elders 计数用例（2026-11-06 实测 +1 elder +1 device）。
    """
    async with audio_test_engine.connect() as conn:
        trans = await conn.begin()
        sf = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        async with sf() as session:
            try:
                yield session
            finally:
                await session.rollback()
        await trans.rollback()


@pytest.fixture
async def audio_seeded_db(audio_test_session: AsyncSession) -> dict[str, Any]:
    """写入 seed 数据（只 flush 不 commit）。"""
    room = Room(room_no="A-301")
    audio_test_session.add(room)
    await audio_test_session.flush()

    bed = Bed(room_id=room.room_id, bed_no="1 号床")
    device = Device(
        room_id=room.room_id,
        token_hash=sha256_hex(_DEVICE_SECRET_PLAIN),
        status="offline",
    )
    audio_test_session.add_all([bed, device])
    await audio_test_session.flush()

    elder = Elder(name="张桂芬", status="active")
    audio_test_session.add(elder)
    await audio_test_session.flush()

    bed.elder_id = elder.elder_id
    device.elder_id = elder.elder_id
    await audio_test_session.flush()

    return {
        "elder_id": elder.elder_id,
        "device_id": device.device_id,
    }


@pytest.fixture
async def app_for_audio(
    audio_test_session: AsyncSession, audio_seeded_db: dict[str, Any]
) -> FastAPI:
    """audio + auth 路由 + 全局 exception handler（完整复制 auth 层 handler）。"""
    app = FastAPI()
    app.include_router(audio_api.router)
    app.include_router(auth_api.router, prefix="/api")  # a0-F-01：400 分支测试用
    # a0-F-01：裸 app 不走 create_app，手动注册全局 RequestValidationError handler
    app.add_exception_handler(RequestValidationError, handle_request_validation)
    # a0-F-02：HTTPException 兜底（code_map 422→VALIDATION_ERROR），与生产同实现
    app.add_exception_handler(HTTPException, handle_http_exception)

    async def _override_get_session():
        yield audio_test_session

    app.dependency_overrides[_app_get_session] = _override_get_session

    # 测试确定性：显式 pin Mock ASR/TTS，防 .env 的 ASR_PROVIDER/TTS_PROVIDER
    # 渗入 build_asr()/build_tts() 默认依赖（.env 配 funasr 时本文件用例会误连真链路）。
    # 注意必须用零参工厂而非类本身：FastAPI 会把类的 __init__ 参数当可注入查询参数解析
    app.dependency_overrides[audio_api._get_default_asr] = lambda: MockASRProvider()
    app.dependency_overrides[audio_api._get_default_tts] = lambda: MockTTSProvider()

    # 全局 exception handler —— audio.py 鉴权用 Depends(current_device)
    # 会走 get_current_principal → 抛 Credential*Error，必须这里兜
    @app.exception_handler(CredentialInvalidError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_INVALID", "trace_id": "-"},
        )

    @app.exception_handler(CredentialExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    @app.exception_handler(CredentialRevokedError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_REVOKED", "trace_id": "-"},
        )

    @app.exception_handler(TokenExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": "token has expired", "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    return app


@pytest.fixture
async def ac_audio(app_for_audio: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app_for_audio)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def real_device_token(
    audio_test_session: AsyncSession, audio_seeded_db: dict[str, Any]
) -> str:
    """用全量 app 换 device access token（走真实 device-sessions 端点）。"""
    from app.api.auth import router as auth_router
    from app.core.auth import (
        CredentialExpiredError,
        CredentialInvalidError,
        CredentialRevokedError,
    )
    from app.core.security import TokenExpiredError
    from app.services.auth_service import (
        AuthConflictError,
        AuthExpiredError,
        AuthInvalidError,
        AuthRevokedError,
    )

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")

    async def _override_get_session():
        yield audio_test_session

    app.dependency_overrides[_app_get_session] = _override_get_session

    # auth 层 handler（完整抄 test_auth_api.py::app_for_test）
    @app.exception_handler(AuthInvalidError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={
                "detail": str(exc.detail),
                "code": exc.kind or "TOKEN_INVALID",
                "trace_id": "-",
            },
        )

    @app.exception_handler(AuthExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc.detail), "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    @app.exception_handler(AuthRevokedError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc.detail), "code": "TOKEN_REVOKED", "trace_id": "-"},
        )

    @app.exception_handler(AuthConflictError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc.detail), "code": "CONFLICT", "trace_id": "-"},
        )

    @app.exception_handler(CredentialInvalidError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_INVALID", "trace_id": "-"},
        )

    @app.exception_handler(CredentialExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    @app.exception_handler(CredentialRevokedError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "TOKEN_REVOKED", "trace_id": "-"},
        )

    @app.exception_handler(TokenExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": "token has expired", "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/device-sessions",
            json={
                "device_id": str(audio_seeded_db["device_id"]),
                "device_secret": _DEVICE_SECRET_PLAIN,
            },
        )
        assert resp.status_code == 200, f"device-sessions failed: {resp.status_code} {resp.text}"
        return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# 测试辅助：构造最小合法 WAV
# ---------------------------------------------------------------------------


def _make_wav(num_samples: int, sample_rate: int = 16_000) -> bytes:
    """生成全 0 PCM16 WAV（单声道）。"""
    raw = struct.pack(f"<{num_samples}h", *([0] * num_samples))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(raw)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_token_401(ac_audio: httpx.AsyncClient) -> None:
    """伪造 token → 401 + ErrorResponse 包装（含 detail / trace_id）。"""
    wav = _make_wav(16_000)  # 1s
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": "Bearer fake.fake.fake"},
        files={"audio": ("test.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 401, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "detail" in body and "trace_id" in body


@pytest.mark.asyncio
async def test_utterance_success(
    ac_audio: httpx.AsyncClient,
    real_device_token: str,
) -> None:
    """正常路径：鉴权通过 → Mock ASR 成功 → Mock TTS 成功 → 200。"""
    wav = _make_wav(16_000)  # 1s
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {real_device_token}"},
        files={"audio": ("test.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    # 契约 C3 响应形状
    assert "text" in body
    assert "reply" in body and "text" in body["reply"] and "audio_url" in body["reply"]
    # Day 3 写死话术
    assert "您说的是" in body["reply"]["text"]


@pytest.mark.asyncio
async def test_utterance_too_long_413(
    ac_audio: httpx.AsyncClient,
    real_device_token: str,
) -> None:
    """超长音频（>30s）→ 413 拒收。"""
    wav = _make_wav(16_000 * 31)  # 31s
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {real_device_token}"},
        files={"audio": ("long.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 413, f"got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_utterance_empty_file_422(
    ac_audio: httpx.AsyncClient,
    real_device_token: str,
) -> None:
    """空音频文件 → 422。"""
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {real_device_token}"},
        files={"audio": ("empty.wav", b"", "audio/wav")},
    )
    assert resp.status_code == 422, f"got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_client_elder_id_ignored(
    ac_audio: httpx.AsyncClient,
    real_device_token: str,
) -> None:
    """客户端多传 elder_id 不影响（audio.py 根本没这参数，FastAPI 自动忽略）。"""
    wav = _make_wav(16_000)
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {real_device_token}"},
        data={"elder_id": "00000000-0000-0000-0000-000000000000"},
        files={"audio": ("test.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_missing_audio_422_unified_error_body(
    ac_audio: httpx.AsyncClient,
    real_device_token: str,
) -> None:
    """a0-F-01：缺 audio 文件字段 → RequestValidationError → 统一错误体。

    断言 body 是 {detail, code, trace_id} 而非 FastAPI 默认的 Pydantic 裸数组。
    """
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {real_device_token}"},
    )
    assert resp.status_code == 422, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    # trace_id 值由 TraceIdMiddleware 写入 contextvar；本 fixture 是裸 app 没挂中间件，
    # 生产路径由 test_security/test_elders_api 的 TraceIdMiddleware 用例覆盖
    assert "trace_id" in body and isinstance(body["trace_id"], str)
    assert isinstance(body["detail"], str) and body["detail"]
    assert "body.audio" in body["detail"]  # loc 简化拼接，非原始数组


@pytest.mark.asyncio
async def test_garbage_bytes_422_not_503(
    ac_audio: httpx.AsyncClient,
    real_device_token: str,
) -> None:
    """a0-F-02：纯垃圾字节（非 WAV）→ 422 VALIDATION_ERROR，而非漏到 ASR 层炸 503。"""
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {real_device_token}"},
        files={"audio": ("garbage.wav", b"\x00" * 1024, "audio/wav")},
    )
    assert resp.status_code == 422, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"

    text_bytes = "这不是音频，是文本".encode()
    resp = await ac_audio.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {real_device_token}"},
        files={"audio": ("garbage.wav", text_bytes, "audio/wav")},
    )
    assert resp.status_code == 422, f"got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_malformed_json_400_unified_error_body(
    ac_audio: httpx.AsyncClient,
) -> None:
    """a0-F-01：默认路由类（auth，无 ContractAPIRoute）的非法 JSON → §7 分流 400。

    契约 §7：请求体不是合法 JSON 归 400 BAD_REQUEST（elders 的 ContractAPIRoute
    已这么做，这里验证 audio/auth 等默认路由类经全局 handler 后同款行为）；
    body 必须是统一错误体三件套。
    """
    resp = await ac_audio.post(
        "/api/sessions",
        content=b"{",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["code"] == "BAD_REQUEST"
    assert "trace_id" in body
    assert isinstance(body["detail"], str)
