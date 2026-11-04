"""设备绑定 REST 薄层（接口契约 E9–E11）。

鉴权、范围与绑定规则全部在 `app/services/device_service.py`；本文件只做 Pydantic 校验、
接收当前主体、调用服务并复用 `app/api/elders.py` 的 `ContractAPIRoute` 做错误体映射
（本卡不允许新建共享文件，理由见该类的 docstring）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.core.auth import get_current_principal
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import device_service

router = APIRouter(prefix="/api/devices", tags=["devices"], route_class=ContractAPIRoute)


class DeviceBindRequest(BaseModel):
    """E9 请求体。`elder_id` 必须显式给出；显式 `null` 走解绑语义并轮换哈希。"""

    model_config = ConfigDict(extra="forbid")

    room_id: uuid.UUID
    elder_id: uuid.UUID | None


class DeviceBindingResponse(BaseModel):
    """E9/E10 响应：契约给出的三个绑定字段。"""

    device_id: uuid.UUID
    room_id: uuid.UUID
    elder_id: uuid.UUID | None


class ElderCandidateResponse(BaseModel):
    """E11 候选字段严格为三项，姓名只以脱敏形式出现（S-06）。"""

    elder_id: uuid.UUID
    bed_no: str
    display_name: str


class ElderCandidatesResponse(BaseModel):
    room_id: uuid.UUID
    mode: Literal["single", "double"]
    candidates: list[ElderCandidateResponse]


@router.post(
    "/{device_id}/bind",
    response_model=DeviceBindingResponse,
    summary="设备绑定房间与长者（仅 admin）",
)
async def bind_device(
    device_id: uuid.UUID,
    payload: DeviceBindRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviceBindingResponse:
    binding = await device_service.bind_device(
        session,
        identity,
        device_id,
        room_id=payload.room_id,
        elder_id=payload.elder_id,
    )
    return DeviceBindingResponse(
        device_id=binding.device_id,
        room_id=binding.room_id,
        elder_id=binding.elder_id,
    )


@router.post(
    "/{device_id}/unbind",
    response_model=DeviceBindingResponse,
    summary="设备解绑（仅 admin；清长者、置 offline、轮换 token_hash）",
)
async def unbind_device(
    device_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviceBindingResponse:
    binding = await device_service.unbind_device(session, identity, device_id)
    return DeviceBindingResponse(
        device_id=binding.device_id,
        room_id=binding.room_id,
        elder_id=binding.elder_id,
    )


@router.get(
    "/{device_id}/elder-candidates",
    response_model=ElderCandidatesResponse,
    summary="本房间候选长者（admin 或设备本人）",
)
async def list_elder_candidates(
    device_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ElderCandidatesResponse:
    result = await device_service.list_elder_candidates(session, identity, device_id)
    return ElderCandidatesResponse(
        room_id=result.room_id,
        mode=result.mode,
        candidates=[
            ElderCandidateResponse(
                elder_id=item.elder_id,
                bed_no=item.bed_no,
                display_name=item.display_name,
            )
            for item in result.candidates
        ],
    )


__all__ = ["router"]
