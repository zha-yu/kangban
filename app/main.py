"""FastAPI 装配层（技术文档 §2.1：`app/main.py` 承担 FastAPI 装配、`/health`、trace_id 中间件）。

分层约束（AGENTS.md §2）：本文件只做装配，不写业务逻辑；路由在 `app/api/`，
领域逻辑与写库在 `app/services/`。

启动：`uvicorn app.main:app --reload --port 8000`
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import audio as audio_api
from app.api import auth as auth_api
from app.api import devices, elders, health
from app.api.schemas.auth import ErrorResponse
from app.audio import signaling  # Day 3 T2：WS 信令路由（/ws/signal）
from app.audio.webrtc import install_signaling_handler  # Day 3 T3：A3 媒体层接线
from app.core.auth import (
    CredentialExpiredError,
    CredentialInvalidError,
    CredentialMissingError,
    CredentialRevokedError,
)
from app.core.config import Settings, get_settings
from app.core.context import TRACE_HEADER, get_trace_id
from app.core.logging import configure_logging
from app.core.security import TokenExpiredError
from app.core.trace import TraceIdMiddleware
from app.services.auth_service import (
    AuthConflictError,
    AuthExpiredError,
    AuthInvalidError,
    AuthRevokedError,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程级生命周期。Day 2 起在这里挂：DB 连接池、Redis、APScheduler。

    现在只记日志 —— 不建连接，保证没有 PG/Redis 也能启动（1.4 验收口径）。
    """
    logger = logging.getLogger("app.main")
    logger.info("application starting", extra={"app_env": app.state.settings.app_env})
    yield
    logger.info("application stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂：便于测试注入不同配置（`create_app(Settings(app_env="test"))`）。"""
    settings = settings or get_settings()
    configure_logging(settings.log_level, service=settings.otel_service_name)

    application = FastAPI(
        title=f"{settings.app_name} backend",
        version=__version__,
        description="康伴（SilverMate）3D 数字人陪伴系统后端",
        lifespan=lifespan,
    )
    application.state.settings = settings

    # 中间件：后添加者在外层，因此 CORS 包住 TraceId，异常响应也带链路头
    application.add_middleware(TraceIdMiddleware)
    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[TRACE_HEADER],
        )

    application.include_router(health.router)
    application.include_router(auth_api.router, prefix="/api")
    application.include_router(elders.router)
    application.include_router(devices.router)
    application.include_router(audio_api.router)  # /api/audio 降级上传（C3，路由自带前缀）
    application.include_router(signaling.router)  # /ws/signal 无 /api 前缀（契约裁决 A-03）
    install_signaling_handler()  # A3 注册 offer/ice/asr_start/asr_end 上行处理器

    # ---------- 全局 exception handler（契约 §7） ----------
    # auth_service 层异常（双重保险：auth.py 路由已捕获，这里兜底）
    @application.exception_handler(AuthInvalidError)
    async def _handle_auth_invalid(req: Request, exc: AuthInvalidError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail),
                code=exc.kind or "TOKEN_INVALID",
                trace_id=get_trace_id() or "-",
            ).model_dump(),
        )

    @application.exception_handler(AuthExpiredError)
    async def _handle_auth_expired(req: Request, exc: AuthExpiredError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail), code="TOKEN_EXPIRED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(AuthRevokedError)
    async def _handle_auth_revoked(req: Request, exc: AuthRevokedError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail), code="TOKEN_REVOKED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(AuthConflictError)
    async def _handle_auth_conflict(req: Request, exc: AuthConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                detail=str(exc.detail), code="CONFLICT", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    # task-4 auth 层异常（依赖注入阶段抛出，auth_service 还没机会参与）
    @application.exception_handler(CredentialInvalidError)
    async def _handle_cred_invalid(req: Request, exc: CredentialInvalidError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc), code="TOKEN_INVALID", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(CredentialMissingError)
    async def _handle_cred_missing(req: Request, exc: CredentialMissingError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc), code="TOKEN_INVALID", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(CredentialExpiredError)
    async def _handle_cred_expired(req: Request, exc: CredentialExpiredError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc), code="TOKEN_EXPIRED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(CredentialRevokedError)
    async def _handle_cred_revoked(req: Request, exc: CredentialRevokedError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc), code="TOKEN_REVOKED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(TokenExpiredError)
    async def _handle_token_expired(req: Request, exc: TokenExpiredError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail="token has expired", code="TOKEN_EXPIRED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    # 兜底：业务代码 raise HTTPException 也包成 ErrorResponse（契约 §7）
    @application.exception_handler(HTTPException)
    async def _handle_http_exception(req: Request, exc: HTTPException) -> JSONResponse:
        code_map = {
            400: "BAD_REQUEST",
            401: "TOKEN_INVALID",
            403: "ROLE_FORBIDDEN",
            404: "NOT_FOUND",
            413: "PAYLOAD_TOO_LARGE",
            422: "VALIDATION_ERROR",
            503: "SERVICE_UNAVAILABLE",
        }
        code = code_map.get(exc.status_code, f"HTTP_{exc.status_code}")
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                detail=str(exc.detail),
                code=code,
                trace_id=get_trace_id() or "-",
            ).model_dump(),
        )

    # 未预期异常 → 500，不回显内部细节（AGENTS §8 失败分类）
    @application.exception_handler(Exception)
    async def _handle_unknown(req: Request, exc: Exception) -> JSONResponse:
        logging.getLogger("app.main").exception("unhandled exception")
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                detail="内部错误", code="INTERNAL_ERROR", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    return application


app = create_app()
