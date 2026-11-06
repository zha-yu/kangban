"""FastAPI 装配层（技术文档 §2.1：`app/main.py` 承担 FastAPI 装配、`/health`、trace_id 中间件）。

分层约束（AGENTS.md §2）：本文件只做装配，不写业务逻辑；路由在 `app/api/`，
领域逻辑与写库在 `app/services/`。

启动：`uvicorn app.main:app --reload --port 8000`
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

import redis.asyncio as redis_asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import ROUTERS
from app.api.elders import _is_request_format_error
from app.api.schemas.auth import ErrorResponse
from app.audio import signaling  # Day 3 T2：WS 信令路由（/ws/signal）
from app.audio.tts import TTSUnavailable, build_tts
from app.audio.webrtc import (  # Day 3 T3：A3 媒体层接线
    enqueue_tts_for_device,
    install_signaling_handler,
)
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
from app.db import session_factory
from app.services import identity_service, voiceprint_service
from app.services.auth_service import (
    AuthConflictError,
    AuthExpiredError,
    AuthInvalidError,
    AuthRevokedError,
)
from app.services.dialogue_service import (
    DialogueOrchestrator,
    DialogueService,
    IdentityConfirmationRequired,
    IdentityThreadResult,
    RedisTurnStore,
    identity_decision_to_result,
)
from app.services.llm_service import LLMService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程级生命周期：Redis/LLM/Dialogue 运行时与 FINAL seam 的唯一装配点。

    Redis / HTTP 客户端在本进程创建并关闭；两者都是懒连接，外部依赖未起时不阻塞
    启动（`/health` 与 Day2 验收口径不变）。`conversation_threads` 仍由 A1
    `identity_service` 独占创建，A2 只消费其 `IdentityDecision`。
    """
    logger = logging.getLogger("app.main")
    settings: Settings = app.state.settings

    redis_client = redis_asyncio.from_url(
        settings.redis_url, encoding="utf-8", decode_responses=False
    )
    llm_service = LLMService(settings)
    tts_provider = build_tts(settings)
    turn_store = RedisTurnStore(client=redis_client, key_prefix=settings.redis_key_prefix)
    dialogue_service = DialogueService(llm=llm_service, store=turn_store)

    # 每 (device_id, trace_id) 缓存已打开的 thread + 身份：同一 WS 会话内复用，
    # 避免每个 FINAL 都新建 conversation_thread；换连接由新 trace_id 重新首轮校验。
    thread_cache: OrderedDict[tuple[str, str], IdentityThreadResult] = OrderedDict()
    thread_cache_max = 128

    async def _publish_identity(
        device_id: str, decision: identity_service.IdentityDecision
    ) -> None:
        """A1 终态 → C5 identity_result；confirm 分支由 signaling confirm 回调负责。"""
        if decision.source == "voice" and decision.elder_id is not None:
            await signaling.apply_voice_identity(device_id, elder_id=str(decision.elder_id))
        elif decision.source == "generic":
            await signaling.apply_generic_identity(device_id)

    async def _resolve_identity(
        *,
        device_id: str,
        turn_id: str,
        text: str,
        trace_id: str,
        audio_bytes: bytes | None,
    ) -> IdentityThreadResult:
        # A1 identity_service 独占创建 thread；A2 只消费终态 IdentityDecision。
        cache_key = (device_id, trace_id)
        cached = thread_cache.get(cache_key)
        if cached is not None:
            thread_cache.move_to_end(cache_key)
            # 后续 utterance 交给 A1 静默复检；无音频时沿用当前 thread
            if audio_bytes is None:
                return cached
            async with session_factory() as session:
                try:
                    changed = await identity_service.recheck_session_identity(
                        session,
                        device_id=UUID(device_id),
                        current_thread_id=cached.thread_id,
                        audio_bytes=audio_bytes,
                    )
                except voiceprint_service.VoiceprintInvalidAudio:
                    # 语音段过短/格式异常：不打断当前身份，继续沿用当前 thread
                    logger.warning(
                        "voiceprint_recheck_invalid_audio",
                        extra={"trace_id": trace_id, "device_id": device_id},
                    )
                    return cached
            if changed is None:
                return cached
            if changed.thread_id is None:
                # 旧 thread 已在 A1 结束；清缓存，等 confirm/timeout 再建新 thread
                thread_cache.pop(cache_key, None)
            await signaling.begin_identity_recheck(device_id)
            await _publish_identity(device_id, changed)
            result = identity_decision_to_result(changed)
            thread_cache[cache_key] = result
            return result

        async with session_factory() as session:
            try:
                decision = await identity_service.verify_session_identity(
                    session,
                    device_id=UUID(device_id),
                    audio_bytes=audio_bytes,
                )
            except voiceprint_service.VoiceprintInvalidAudio:
                # 声纹容器拒绝该段（过短/格式异常）时降级为无音频主动确认
                logger.warning(
                    "voiceprint_invalid_audio",
                    extra={"trace_id": trace_id, "device_id": device_id},
                )
                decision = await identity_service.verify_session_identity(
                    session,
                    device_id=UUID(device_id),
                    audio_bytes=None,
                )
        await _publish_identity(device_id, decision)
        result = identity_decision_to_result(decision)
        thread_cache[cache_key] = result
        while len(thread_cache) > thread_cache_max:
            thread_cache.popitem(last=False)
        return result

    async def _on_identity_confirm(device_id: str, trace_id: str, elder_id: str) -> None:
        # signaling 已校验 elder_id == 设备绑定候选；A1 事务内开 confirm thread
        async with session_factory() as session:
            decision = await identity_service.open_thread_from_confirm(
                session,
                device_id=UUID(device_id),
                confirmed_elder_id=UUID(elder_id),
                candidate_elder_id=UUID(elder_id),
            )
        result = identity_decision_to_result(decision)
        thread_cache[(device_id, trace_id)] = result
        while len(thread_cache) > thread_cache_max:
            thread_cache.popitem(last=False)

    async def _on_identity_timeout(device_id: str, trace_id: str) -> None:
        # 30s 超时：A1 开 generic thread；signaling 随后下发 identity_result(generic)
        async with session_factory() as session:
            decision = await identity_service.open_thread_as_generic_on_timeout(
                session,
                device_id=UUID(device_id),
            )
        result = identity_decision_to_result(decision)
        thread_cache[(device_id, trace_id)] = result
        while len(thread_cache) > thread_cache_max:
            thread_cache.popitem(last=False)

    orchestrator = DialogueOrchestrator(
        service=dialogue_service,
        identity_thread_resolver=_resolve_identity,
    )

    async def _on_dialogue_final(
        device_id: str,
        turn_id: str,
        trace_id: str,
        text: str,
        audio_bytes: bytes | None,
    ) -> None:
        try:
            reply = await orchestrator.handle_asr_final(
                device_id=device_id,
                trace_id=trace_id,
                turn_id=turn_id,
                text=text,
                audio_bytes=audio_bytes,
            )
        except IdentityConfirmationRequired as exc:
            # A1 reason=no_audio 等不在 C5 两值内时，线上统一按 low_confidence 询问；
            # 原始 reason 只进服务端日志，不改变 C5 契约。
            ask_reason: Literal["low_confidence", "vp_unavailable"] = (
                "vp_unavailable" if exc.reason == "vp_unavailable" else "low_confidence"
            )
            logger.info(
                "identity_confirmation_requested",
                extra={
                    "trace_id": trace_id,
                    "device_id": device_id,
                    "turn_id": turn_id,
                    "reason": ask_reason,
                    "original_reason": exc.reason,
                },
            )
            await signaling.request_identity_confirmation(device_id, reason=ask_reason)
            return
        except Exception:
            logger.exception(
                "dialogue_final_failed",
                extra={"trace_id": trace_id, "device_id": device_id, "turn_id": turn_id},
            )
            return

        if reply is not None:
            delivered = signaling.deliver(
                device_id,
                {"type": "tts_text", "text": reply.text, "degraded": reply.degraded},
            )
            logger.info(
                "dialogue_reply_delivered",
                extra={
                    "trace_id": trace_id,
                    "device_id": device_id,
                    "turn_id": turn_id,
                    "degraded": reply.degraded,
                    "delivered": delivered,
                    "text_len": len(reply.text),
                },
            )
            if not delivered:
                # WS 已断：正文都没送到，不再合成音频
                logger.warning(
                    "dialogue_text_not_delivered",
                    extra={"trace_id": trace_id, "device_id": device_id, "turn_id": turn_id},
                )
                return

            try:
                pcm = await tts_provider.synthesize(reply.text)
            except TTSUnavailable as exc:
                # C1 降级边界：保留大字幕，不因 TTS 失败静默
                logger.warning(
                    "tts_unavailable",
                    extra={
                        "trace_id": trace_id,
                        "device_id": device_id,
                        "turn_id": turn_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return
            except Exception as exc:
                logger.error(
                    "tts_failed",
                    extra={
                        "trace_id": trace_id,
                        "device_id": device_id,
                        "turn_id": turn_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return

            audio_enqueued = await enqueue_tts_for_device(device_id, turn_id, pcm)
            logger.info(
                "dialogue_audio_enqueued",
                extra={
                    "trace_id": trace_id,
                    "device_id": device_id,
                    "turn_id": turn_id,
                    "audio_enqueued": audio_enqueued,
                    "pcm_bytes": len(pcm),
                },
            )

    signaling.set_dialogue_final_handler(_on_dialogue_final)
    signaling.set_identity_confirm_handler(_on_identity_confirm)
    signaling.set_identity_timeout_handler(_on_identity_timeout)
    app.state.redis = redis_client
    app.state.llm_service = llm_service
    app.state.tts_provider = tts_provider
    app.state.dialogue_service = dialogue_service
    app.state.dialogue_orchestrator = orchestrator

    logger.info("application starting", extra={"app_env": settings.app_env})
    try:
        yield
    finally:
        signaling.set_dialogue_final_handler(None)
        signaling.set_identity_confirm_handler(None)
        signaling.set_identity_timeout_handler(None)
        await llm_service.aclose()
        # redis-py 的 asyncio 关闭方法；当前 stubs 尚未同步 aclose
        await redis_client.aclose()  # type: ignore[attr-defined]
        logger.info("application stopped")


def handle_request_validation(req: Request, exc: RequestValidationError) -> JSONResponse:
    """a0-F-01：RequestValidationError 统一错误体（模块级，便于测试 fixture 复用同一实现）。

    elders/devices 走 ContractAPIRoute 已在路由层分流（格式错→400、字段错→422），
    异常到不了这里；本 handler 兜住 audio/auth 等默认路由类的同款异常，避免
    FastAPI 默认裸 422（无 code/trace_id）。§7 分类逻辑单一来源在
    app/api/elders.py::_is_request_format_error，两边不得分叉。
    """
    if _is_request_format_error(exc):
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                detail="malformed request",
                code="BAD_REQUEST",
                trace_id=get_trace_id() or "-",
            ).model_dump(),
        )
    # detail 只取 loc + msg，不带 Pydantic 原始数组与 ctx（不回显内部细节）
    detail = "; ".join(
        "{}: {}".format(".".join(str(p) for p in error.get("loc") or ()), error.get("msg", ""))
        for error in exc.errors()
    )
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            detail=detail or "request validation failed",
            code="VALIDATION_ERROR",
            trace_id=get_trace_id() or "-",
        ).model_dump(),
    )


def handle_http_exception(req: Request, exc: HTTPException) -> JSONResponse:
    """兜底：业务代码 raise HTTPException 也包成 ErrorResponse（契约 §7）。

    模块级（同 handle_request_validation），便于测试 fixture 复用同一实现。
    """
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

    # REST 路由统一从 `app/api/__init__.py` 的注册表挂载（T0）：
    # 新增路由只需在那张表里追加一行，不再改动本文件的公共代码块。
    for _router, _prefix in ROUTERS:
        application.include_router(_router, prefix=_prefix)
    application.include_router(signaling.router)  # /ws/signal 无 /api 前缀（契约裁决 A-03）
    install_signaling_handler()  # A3 注册 offer/ice/asr_start/asr_end 上行处理器

    # TTS 落盘目录挂载（dev 兜底）：audio.py 把 TTS WAV 存 `<cwd>/static/mock/` 并返回
    # `/static/mock/*.wav`，nginx（Day 10）上线前由这里保证 URL 可取；
    # check_dir=False——目录由 utterance 写入时惰性创建，启动时不存在不报错。
    application.mount(
        "/static",
        StaticFiles(directory=os.path.join(os.getcwd(), "static"), check_dir=False),
        name="static",
    )

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
    application.add_exception_handler(HTTPException, handle_http_exception)

    # a0-F-01：统一 422 错误体（实现见模块级 handle_request_validation）
    application.add_exception_handler(RequestValidationError, handle_request_validation)

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
