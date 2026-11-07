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


class DeviceCreateRequest(BaseModel):
    """E16 请求体：建档必须先选房间（devices.room_id NOT NULL）。"""

    model_config = ConfigDict(extra="forbid")

    room_id: uuid.UUID


class DeviceCreateResponse(BaseModel):
    """E16 响应：`device_secret` 是**一次性明文**，仅本次响应可见，库内只存 SHA-256。"""

    device_id: uuid.UUID
    room_id: uuid.UUID
    status: str
    device_secret: str


class DeviceListItemResponse(BaseModel):
    """E17 列表项：room_no 由 JOIN rooms 得出。"""

    device_id: uuid.UUID
    room_id: uuid.UUID
    room_no: str
    elder_id: uuid.UUID | None
    status: str


class DeviceListResponse(BaseModel):
    items: list[DeviceListItemResponse]


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
    "",
    response_model=DeviceCreateResponse,
    status_code=201,
    summary="设备建档（admin/care_worker；返回一次性 device_secret）",
)
async def create_device(
    payload: DeviceCreateRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviceCreateResponse:
    created = await device_service.create_device(session, identity, room_id=payload.room_id)
    return DeviceCreateResponse(
        device_id=created.device_id,
        room_id=created.room_id,
        status=created.status,
        device_secret=created.device_secret,
    )


@router.get(
    "",
    response_model=DeviceListResponse,
    summary="设备列表（admin/care_worker；含未绑定设备）",
)
async def list_devices(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviceListResponse:
    items = await device_service.list_devices(session, identity)
    return DeviceListResponse(
        items=[
            DeviceListItemResponse(
                device_id=item.device_id,
                room_id=item.room_id,
                room_no=item.room_no,
                elder_id=item.elder_id,
                status=item.status,
            )
            for item in items
        ]
    )


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
