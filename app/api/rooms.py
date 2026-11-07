"""房间床位列表 REST 薄层（接口契约 E18，v1.8 新增）。

复用 `app/api/elders.py` 的 `ContractAPIRoute` 做错误体映射（口径见该类 docstring）；
鉴权、范围与组装全部在 `app/services/elder_service.list_rooms_with_beds`，本文件只做
序列化。照护台「注册长者 → 分配床位」向导的第二步用它选房选床。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.core.auth import get_current_principal
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import elder_service

router = APIRouter(prefix="/api/rooms", tags=["rooms"], route_class=ContractAPIRoute)


class BedItemResponse(BaseModel):
    """床位项：`elder_id` 非空即已占用（前端据此禁选）。"""

    bed_id: uuid.UUID
    bed_no: str
    elder_id: uuid.UUID | None


class RoomItemResponse(BaseModel):
    room_id: uuid.UUID
    room_no: str
    status: str
    beds: list[BedItemResponse]


class RoomListResponse(BaseModel):
    items: list[RoomItemResponse]


@router.get(
    "",
    response_model=RoomListResponse,
    summary="房间床位列表（员工全量可读；家属/设备 403）",
)
async def list_rooms(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RoomListResponse:
    rooms = await elder_service.list_rooms_with_beds(session, identity)
    return RoomListResponse(
        items=[
            RoomItemResponse(
                room_id=room.room_id,
                room_no=room.room_no,
                status=room.status,
                beds=[
                    BedItemResponse(bed_id=bed.bed_id, bed_no=bed.bed_no, elder_id=bed.elder_id)
                    for bed in room.beds
                ],
            )
            for room in rooms
        ]
    )


__all__ = [
    "router",
]
