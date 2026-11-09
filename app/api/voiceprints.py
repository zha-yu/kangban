"""Day 4 T7 · voiceprint REST 三接口 —— C4 契约原样实现。

契约来源：docs/team_tasks/day04/contracts.md C4（冻结人 A1）。
**分层硬约束（AGENTS §2）**：本模块不 import ORM、不写业务逻辑，
只做：参数校验 → 调 voiceprint_service → 异常映射 → 序列化。
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_device
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.services.voiceprint_service import (
    VoiceprintDuplicateSample,
    VoiceprintExtractUnavailable,
    VoiceprintInvalidAudio,
    VoiceprintStatus,
    delete_all_for_elder,
    get_status,
    register_sample,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voiceprints"])


# ---------------------------------------------------------------------------
# 路由内异常映射（voiceprint_service 层业务异常 → 统一 ErrorResponse）
# ---------------------------------------------------------------------------


def _trace_id() -> str:
    return get_trace_id() or "-"


def _unbound_device_response() -> JSONResponse:
    """C4：status 端点在设备未绑长者时返回 409 device_unbound。

    设计成独立函数是因为 **samples 端点不适用**（注册路径需要先绑长者，但 C4
    明确 device_unbound 只属 status 的分支，samples 端绑长者的前置由 REST 层
    统一拦：unbound 时 status 不可查 → 前端无法显示「去注册」入口，也就到不了
    samples 端点；真调上来的话 service 层 voiceprints 表 FK 约束会拒 ——
    按 C4 契约原样返回 409 更明确）。
    """
    return JSONResponse(
        status_code=409,
        content={
            "detail": "设备尚未绑定长者，请先完成配对",
            "code": "device_unbound",
            "trace_id": _trace_id(),
        },
    )


def _handle_service_exception(exc: Exception) -> JSONResponse:
    """voiceprint_service 层异常的统一映射。"""
    if isinstance(exc, VoiceprintDuplicateSample):
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "code": "duplicate_sample",
                "trace_id": _trace_id(),
            },
        )
    if isinstance(exc, VoiceprintExtractUnavailable):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "声纹识别服务暂时不可用，稍后再试",
                "code": "vp_extract_unavailable",
                "trace_id": _trace_id(),
            },
        )
    if isinstance(exc, VoiceprintInvalidAudio):
        return JSONResponse(
            status_code=422,
            content={
                "detail": str(exc),
                "code": "validation_error",
                "trace_id": _trace_id(),
            },
        )
    # 兜底业务异常（VoiceprintError 其他子类）
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"声纹服务异常：{exc}",
            "code": "voiceprint_error",
            "trace_id": _trace_id(),
        },
    )


# ---------------------------------------------------------------------------
# 参数校验工具
# ---------------------------------------------------------------------------


def _validate_wav_bytes(raw: bytes) -> None:
    """快速校验 WAV 文件头（RIFF/WAVE 标记），并硬卡时长 ≤10s。

    时长上限 16kHz × 16bit × 10s × 1ch = 320KB，给 10% 余量按 350KB 算。
    目的：在推理容器之前就拒掉明显非法的请求，减少容器负载。
    更深层的音频合法性（编码、采样率等）留给推理容器自身判定。
    """
    if len(raw) < 12:
        raise VoiceprintInvalidAudio("音频文件过小（疑似非法 WAV）")
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise VoiceprintInvalidAudio("非 WAV 格式，请上传 16kHz/16bit/单声道 WAV")
    # 350KB ≈ 10s @ 16kHz/16bit/单声道
    if len(raw) > 350_000:
        raise VoiceprintInvalidAudio("音频过长（≤10s）")


def _require_bound_elder(identity: Identity) -> uuid.UUID | None:
    """取 identity.elder_id 并转 UUID。None 表示设备未绑长者。

    Identity.elder_id 存的是 str（令牌文本），服务层签名收 uuid.UUID，在此统一转换。
    """
    return uuid.UUID(identity.elder_id) if identity.elder_id else None


# ---------------------------------------------------------------------------
# C4 · POST /api/voiceprints/samples
# ---------------------------------------------------------------------------


@router.post("/voiceprints/samples")
async def register_voiceprint_sample(
    identity: Annotated[Identity, Depends(current_device)],
    audio: Annotated[UploadFile, File(description="16kHz/16bit/单声道 WAV ≤10s")],
    sample_index: Annotated[int, Form(description="1/2/3 三段注册的样本序号")],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    if sample_index not in (1, 2, 3):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "sample_index 必须是 1、2、3",
                "code": "validation_error",
                "trace_id": _trace_id(),
            },
        )

    elder_id = _require_bound_elder(identity)
    if elder_id is None:
        return _unbound_device_response()

    raw = await audio.read()
    try:
        _validate_wav_bytes(raw)
    except VoiceprintInvalidAudio as exc:
        return JSONResponse(
            status_code=422,
            content={
                "detail": str(exc),
                "code": "validation_error",
                "trace_id": _trace_id(),
            },
        )

    try:
        status: VoiceprintStatus = await register_sample(
            session=session,
            elder_id=elder_id,
            sample_index=sample_index,  # type: ignore[arg-type]  # int → Literal[1,2,3]
            audio_bytes=raw,
        )
    except Exception as exc:
        return _handle_service_exception(exc)

    return JSONResponse(
        status_code=200,
        content={
            "elder_id": str(elder_id),
            "version": status.version,
            "sample_index": sample_index,
            "status": status.status,
        },
    )


# ---------------------------------------------------------------------------
# C4 · GET /api/voiceprints/status
# ---------------------------------------------------------------------------


@router.get("/voiceprints/status")
async def get_voiceprint_status(
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    elder_id = _require_bound_elder(identity)
    if elder_id is None:
        return _unbound_device_response()

    try:
        status = await get_status(session=session, elder_id=elder_id)
    except Exception as exc:
        return _handle_service_exception(exc)

    return JSONResponse(
        status_code=200,
        content={
            "elder_id": str(elder_id),
            "status": status.status,
            "registered": status.registered,
            "version": status.version,
        },
    )


# ---------------------------------------------------------------------------
# C4 · DELETE /api/voiceprints
# ---------------------------------------------------------------------------


@router.delete("/voiceprints", status_code=204)
async def delete_voiceprints(
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    elder_id = _require_bound_elder(identity)
    if elder_id is None:
        return _unbound_device_response()

    try:
        await delete_all_for_elder(
            session=session,
            elder_id=elder_id,
            actor=f"device:{identity.principal_id}",
        )
    except Exception as exc:
        return _handle_service_exception(exc)

    return JSONResponse(status_code=204, content=None)
