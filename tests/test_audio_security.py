"""Block 4 · 攻击面增量 + 降级话术三场景触发验证。

复用 test_audio_utterance.py fixture 结构（带 sec_ 前缀防冲突）。
用 `app.dependency_overrides[audio_api._get_default_asr / _get_default_tts]` 控制
ASR/TTS 行为（FastAPI 原生 override，符合项目约定：不用 unittest.mock patch）。
"""

from __future__ import annotations

import io
import struct
import wave
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import audio as audio_api
from app.audio.asr import ASRProvider, ASRUnavailable, MockASRProvider
from app.audio.tts import TTSProvider, TTSUnavailable
from app.core.auth import (
    CredentialExpiredError,
    CredentialInvalidError,
    CredentialRevokedError,
)
from app.core.config import get_settings
from app.core.security import TokenExpiredError, sha256_hex
from app.db import get_session as _app_get_session
from app.models.organization import Bed, Device, Elder, Room

_DEVICE_SECRET_PLAIN = "device-long-secret-plain-abc123"


# ---------------------------------------------------------------------------
# Fixtures（前缀 sec_ 防与 test_audio_utterance.py 冲突）
# ---------------------------------------------------------------------------


@pytest.fixture
async def sec_engine() -> AsyncIterator[AsyncEngine]:
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
async def sec_session(sec_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    sf = async_sessionmaker(sec_engine, expire_on_commit=False)
    async with sf() as session:
        try:
            yield session
        finally:
            await session.rollback()


@pytest.fixture
async def sec_seeded(sec_session: AsyncSession) -> dict[str, Any]:
    room = Room(room_no="A-301")
    sec_session.add(room)
    await sec_session.flush()
    bed = Bed(room_id=room.room_id, bed_no="1 号床")
    device = Device(
        room_id=room.room_id,
        token_hash=sha256_hex(_DEVICE_SECRET_PLAIN),
        status="offline",
    )
    sec_session.add_all([bed, device])
    await sec_session.flush()
    elder = Elder(name="张桂芬", status="active")
    sec_session.add(elder)
    await sec_session.flush()
    bed.elder_id = elder.elder_id
    device.elder_id = elder.elder_id
    await sec_session.flush()
    return {"device_id": device.device_id, "elder_id": elder.elder_id}


@pytest.fixture
async def sec_app(sec_session: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.include_router(audio_api.router)

    async def _override_get_session():
        yield sec_session

    app.dependency_overrides[_app_get_session] = _override_get_session
    # 默认 Mock ASR：本文件默认验「正常路径 + 单点故障」，需要真 ASR 的用例
    # 自己再覆盖 `_get_default_asr`。否则 `.env` 指到 funasr 时，容器状态会让用例红/绿。
    app.dependency_overrides[audio_api._get_default_asr] = lambda: MockASRProvider()

    @app.exception_handler(CredentialInvalidError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401, content={"detail": str(exc), "code": "TOKEN_INVALID", "trace_id": "-"}
        )

    @app.exception_handler(CredentialExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401, content={"detail": str(exc), "code": "TOKEN_EXPIRED", "trace_id": "-"}
        )

    @app.exception_handler(CredentialRevokedError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401, content={"detail": str(exc), "code": "TOKEN_REVOKED", "trace_id": "-"}
        )

    @app.exception_handler(TokenExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401,
            content={"detail": "token has expired", "code": "TOKEN_EXPIRED", "trace_id": "-"},
        )

    return app


@pytest.fixture
async def sec_ac(sec_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=sec_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def sec_token(sec_session: AsyncSession, sec_seeded: dict[str, Any]) -> str:
    """走 device-sessions 端点换 token（真实 DB + 真实鉴权）。"""
    from app.api.auth import router as auth_router
    from app.services.auth_service import (
        AuthConflictError,
        AuthExpiredError,
        AuthInvalidError,
        AuthRevokedError,
    )

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")

    async def _override_get_session():
        yield sec_session

    app.dependency_overrides[_app_get_session] = _override_get_session

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
            status_code=401, content={"detail": str(exc), "code": "TOKEN_INVALID", "trace_id": "-"}
        )

    @app.exception_handler(CredentialExpiredError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401, content={"detail": str(exc), "code": "TOKEN_EXPIRED", "trace_id": "-"}
        )

    @app.exception_handler(CredentialRevokedError)
    async def _(_req, exc):
        return JSONResponse(
            status_code=401, content={"detail": str(exc), "code": "TOKEN_REVOKED", "trace_id": "-"}
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
                "device_id": str(sec_seeded["device_id"]),
                "device_secret": _DEVICE_SECRET_PLAIN,
            },
        )
        assert resp.status_code == 200
        return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _make_wav(num_samples: int = 16_000) -> bytes:
    raw = struct.pack(f"<{num_samples}h", *([0] * num_samples))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16_000)
        wf.writeframes(raw)
    return buf.getvalue()


def _make_corrupt_wav() -> bytes:
    """伪造 WAV 头（wave.open 会抛 wave.Error）。"""
    return b"RIFF" + b"\x00" * 96  # 96 字节全零，wave.open 无法解析


class _ASRUnavailableProvider(ASRProvider):
    """模拟 ASR 不可用：stream() 直接抛 ASRUnavailable。"""

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator:
        raise ASRUnavailable("test: asr unavailable")


class _TTSUnavailableProvider(TTSProvider):
    """模拟 TTS 不可用：synthesize() 直接抛 TTSUnavailable。"""

    async def synthesize(self, text: str) -> bytes:
        raise TTSUnavailable("test: tts unavailable")


# ---------------------------------------------------------------------------
# D1 · ASR 不可用 → 503 + 降级话术
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asr_unavailable_503(
    sec_app: FastAPI, sec_ac: httpx.AsyncClient, sec_token: str
) -> None:
    """override _get_default_asr → unavailable=True → 期望 503。"""

    async def _asr_unavailable():
        return _ASRUnavailableProvider()

    sec_app.dependency_overrides[audio_api._get_default_asr] = _asr_unavailable

    wav = _make_wav()
    resp = await sec_ac.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {sec_token}"},
        files={"audio": ("test.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 503, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    # 降级话术必须出现
    assert "抱歉" in body.get("detail", "") or "不可用" in body.get("detail", "")
    # 清理 override（避免影响其他用例，pytest function 级 fixture 每个用例一个新 app，
    # 理论上不需要清理，但显式清理更保险）
    sec_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# D2 · TTS 不可用 → 200 + audio_url=null + 话术前缀
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tts_unavailable_200(
    sec_app: FastAPI, sec_ac: httpx.AsyncClient, sec_token: str
) -> None:
    """override _get_default_tts → unavailable=True → 期望 200 + audio_url=null。"""

    async def _tts_unavailable():
        return _TTSUnavailableProvider()

    sec_app.dependency_overrides[audio_api._get_default_tts] = _tts_unavailable

    wav = _make_wav()
    resp = await sec_ac.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {sec_token}"},
        files={"audio": ("test.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["reply"]["audio_url"] is None, "TTS 不可用时 audio_url 应为 null"
    assert "语音播报暂时不可用" in body["reply"]["text"], (
        "TTS 不可用时 reply.text 应以降级话术开头，实际: " + body["reply"]["text"]
    )
    sec_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# D3 · ASR+TTS 同时不可用 → 先抛 ASR → 503（TTS 不会被调到）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asr_then_tts_both_unavailable_asr_wins(
    sec_app: FastAPI, sec_ac: httpx.AsyncClient, sec_token: str
) -> None:
    """ASR 先调、先抛 → 直接 503；TTS 根本不会被调。"""

    async def _asr_down():
        return _ASRUnavailableProvider()

    async def _tts_down():
        return _TTSUnavailableProvider()

    sec_app.dependency_overrides[audio_api._get_default_asr] = _asr_down
    sec_app.dependency_overrides[audio_api._get_default_tts] = _tts_down

    resp = await sec_ac.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {sec_token}"},
        files={"audio": ("test.wav", _make_wav(), "audio/wav")},
    )
    assert resp.status_code == 503, f"got {resp.status_code}: {resp.text}"
    assert "语音识别" in resp.json().get("detail", "")


# ---------------------------------------------------------------------------
# A3 · 损坏 WAV 头 → 不 crash + 正常继续（duration_ms=0 不触发 413）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrupt_wav_header_not_crash(sec_ac: httpx.AsyncClient, sec_token: str) -> None:
    """伪造 WAV 头 → _wav_duration_ms 内部 catch wave.Error 返回 0，不触发 413，
    Mock ASR/TTS 继续工作 → 200。
    """
    corrupt = _make_corrupt_wav()
    resp = await sec_ac.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {sec_token}"},
        files={"audio": ("corrupt.wav", corrupt, "audio/wav")},
    )
    # 必须不是 500（不 crash）；实际会走 Mock ASR/TTS 正常处理 → 200
    assert resp.status_code != 500, f"corrupt WAV caused 500: {resp.text}"
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# A2 · 被吊销 token（token_hash 轮换）→ 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_device_token_401(
    sec_session: AsyncSession,
    sec_ac: httpx.AsyncClient,
    sec_token: str,
    sec_seeded: dict[str, Any],
) -> None:
    """token_hash 轮换 → 旧 token 的 tv 前缀对不上 → 401 revoked。"""
    from sqlalchemy import update as sa_update

    # 改 DB：模拟设备解绑 → token_hash 轮换
    await sec_session.execute(
        sa_update(Device)
        .where(Device.device_id == sec_seeded["device_id"])
        .values(token_hash=sha256_hex("NEW-secret-" + _DEVICE_SECRET_PLAIN))
    )
    await sec_session.commit()

    # 旧 token 再打 utterance → 401
    resp = await sec_ac.post(
        "/api/audio/utterance",
        headers={"Authorization": f"Bearer {sec_token}"},
        files={"audio": ("test.wav", _make_wav(), "audio/wav")},
    )
    assert resp.status_code == 401, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("code") in ("TOKEN_REVOKED", "TOKEN_INVALID"), (
        f"revoked token 应返回 revoked/invalid，实际 code={body.get('code')}: {resp.text}"
    )
    # （TokenRevokedError 的 detail 由 auth.py 决定，不硬编码具体文案，
    # 只断言 401 且不是 400/500）
