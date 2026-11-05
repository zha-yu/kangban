"""身份认证 HTTP 路由 —— 登录 / 刷新 / 当前身份 / 设备换会话（契约 §4 E1–E4）。

**分层硬约束（AGENTS §2）**：
- 本模块**禁止 import ORM**（StaffAccount / GuardianAccount / Device 等）；
- 本模块**禁止写业务逻辑**，只做三件事：参数校验 → 调 auth_service → 序列化。
- ORM 操作唯一落在 app/services/auth_service.py。

**错误处理**：auth_service 的业务异常在路由函数里捕获 → 映射成 JSONResponse。
task-4 auth 层的异常（Credential* / TokenExpiredError）和其他未预期异常由
**app 级全局 exception handler** 捕获 —— 不在本模块注册，由 app/main.py 和
测试 fixture 负责（见 tests/test_auth_api.py::app_for_test）。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.auth import (
    CurrentIdentityResponse,
    DeviceLoginRequest,
    DeviceLoginResponse,
    ErrorResponse,
    LoginRequest,
    LoginResponse,
    RefreshRequest,
    RefreshResponse,
)
from app.core.auth import get_current_principal
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import auth_service
from app.services.auth_service import (
    AuthConflictError,
    AuthExpiredError,
    AuthInvalidError,
    AuthRevokedError,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# 路由内错误映射（仅 auth_service 层的业务异常；task-4 auth 层异常走全局 handler）
# ---------------------------------------------------------------------------


def _handle_service_exception(exc: Exception) -> JSONResponse:
    trace_id = get_trace_id() or "-"
    if isinstance(exc, AuthInvalidError):
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail), code=exc.kind or "TOKEN_INVALID", trace_id=trace_id
            ).model_dump(),
        )
    if isinstance(exc, AuthExpiredError):
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail), code="TOKEN_EXPIRED", trace_id=trace_id
            ).model_dump(),
        )
    if isinstance(exc, AuthRevokedError):
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail), code="TOKEN_REVOKED", trace_id=trace_id
            ).model_dump(),
        )
    if isinstance(exc, AuthConflictError):
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                detail=str(exc.detail), code="CONFLICT", trace_id=trace_id
            ).model_dump(),
        )
    # 未预期异常 → 500（不回显内部细节）
    logger.exception("unexpected auth error: %s", exc)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            detail="内部错误", code="INTERNAL_ERROR", trace_id=trace_id
        ).model_dump(),
    )


# ---------------------------------------------------------------------------
# E1：POST /api/sessions — 人账号登录
# ---------------------------------------------------------------------------


@router.post(
    "/sessions",
    response_model=LoginResponse,
    summary="人账号登录（staff / guardian 共用）",
)
async def login(
    body: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LoginResponse | JSONResponse:
    try:
        result = await auth_service.login_with_phone(
            session, phone=body.phone, password=body.password
        )
    except Exception as exc:
        return _handle_service_exception(exc)

    return LoginResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        principal={
            "kind": result.principal.kind,
            "id": result.principal.id,
            "role": result.principal.role,
            "name": result.principal.name,
        },
    )


# ---------------------------------------------------------------------------
# E2：POST /api/sessions/refresh
# ---------------------------------------------------------------------------


@router.post(
    "/sessions/refresh",
    response_model=RefreshResponse,
    summary="刷新 Access Token",
)
async def refresh(
    body: RefreshRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RefreshResponse | JSONResponse:
    try:
        result = await auth_service.refresh_access_token(session, refresh_token=body.refresh_token)
    except Exception as exc:
        return _handle_service_exception(exc)

    return RefreshResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
    )


# ---------------------------------------------------------------------------
# E3：GET /api/sessions/current — 需 Bearer token
# ---------------------------------------------------------------------------


@router.get(
    "/sessions/current",
    response_model=CurrentIdentityResponse,
    summary="当前身份详情（需鉴权）",
)
async def current_identity(
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(get_current_principal)],
) -> CurrentIdentityResponse | JSONResponse:
    try:
        result = await auth_service.get_current_identity(session, identity=identity)
    except Exception as exc:
        return _handle_service_exception(exc)

    return CurrentIdentityResponse(
        kind=result.kind,
        id=result.id,
        role=result.role,
        name=result.name,
        scope_elder_ids=result.scope_elder_ids,
    )


# ---------------------------------------------------------------------------
# E4：POST /api/device-sessions — 设备换会话
# ---------------------------------------------------------------------------


@router.post(
    "/device-sessions",
    response_model=DeviceLoginResponse,
    summary="设备长期令牌换 24h 会话",
)
async def device_session(
    body: DeviceLoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviceLoginResponse | JSONResponse:
    try:
        result = await auth_service.exchange_device_token(
            session, device_id=body.device_id, device_secret=body.device_secret
        )
    except Exception as exc:
        return _handle_service_exception(exc)

    return DeviceLoginResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        device={
            "id": result.device.id,
            "room_id": result.device.room_id,
            "elder_id": result.device.elder_id,
        },
    )
