"""admin 房间/床位管理 REST 薄层（计划 v1.5 §3）。

只做四件事：Pydantic 校验、角色门禁、调 `app/services/org_service.py`、序列化响应；
错误体复用 `app/api/elders.py` 的 `ContractAPIRoute` / `_error_response`。

权限：全部端点仅 `admin`（PRD §3.1 admin 维护配置）。非员工主体（家属/设备/工牌）
与 care_worker/doctor 一律 403 `ROLE_FORBIDDEN` —— 这里直接用 `check_roles`
（`require_roles("admin")` 的同一判定内核）而不是 `require_roles` 工厂，因为后者经
`current_staff` 会把非员工主体判成 401 凭证不符，与计划 §3「四类非 admin 403」冲突。

E18 `GET /api/rooms` 是冻结基线，本文件不改它、也不复用它的路由。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.api.schemas.org import (
    BedBatchDeleteRequest,
    BedBatchResponse,
    BedBatchStatusRequest,
    BedCreateRequest,
    BedItemResponse,
    BedListResponse,
    BedStatus,
    BedSummaryResponse,
    BedUpdateRequest,
    RoomCreateRequest,
    RoomItemResponse,
    RoomListResponse,
    RoomResponse,
    RoomStatus,
    RoomUpdateRequest,
)
from app.core.auth import check_roles, get_current_principal
from app.core.runtime_context import Identity
from app.db import get_session
from app.models import Room
from app.services import org_service
from app.services.org_service import (
    BedItem,
    OrgConflictError,
    OrgNotFoundError,
    OrgServiceError,
    OrgValidationError,
    RoomItem,
)

router = APIRouter(prefix="/api/admin", tags=["admin-org"], route_class=ContractAPIRoute)


async def _require_admin_identity(
    identity: Annotated[Identity, Depends(get_current_principal)],
) -> Identity:
    """管理员角色门禁：非 admin 一律 403（含非员工主体）。"""
    check_roles(identity, ("admin",))
    return identity


AdminIdentity = Annotated[Identity, Depends(_require_admin_identity)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _org_error(exc: OrgServiceError) -> JSONResponse:
    """领域异常 → 契约 §7 错误体：404 / 409 / 422，detail 直接回可读文案。"""
    if isinstance(exc, OrgNotFoundError):
        return _error_response(404, "NOT_FOUND", exc.message)
    if isinstance(exc, OrgConflictError):
        return _error_response(409, "CONFLICT", exc.message)
    if isinstance(exc, OrgValidationError):
        return _error_response(422, "VALIDATION_ERROR", exc.message)
    # 基类兜底：未知子类按校验失败处理，绝不漏成 500。
    return _error_response(422, "VALIDATION_ERROR", exc.message)  # pragma: no cover


def _room_item(room: RoomItem) -> RoomItemResponse:
    return RoomItemResponse(
        room_id=room.room_id,
        room_no=room.room_no,
        status=room.status,
        beds=[
            BedSummaryResponse(
                bed_id=bed.bed_id,
                bed_no=bed.bed_no,
                status=bed.status,
                elder_id=bed.elder_id,
            )
            for bed in room.beds
        ],
    )


def _bed_item(bed: BedItem) -> BedItemResponse:
    return BedItemResponse(
        bed_id=bed.bed_id,
        room_id=bed.room_id,
        room_no=bed.room_no,
        bed_no=bed.bed_no,
        status=bed.status,
        elder_id=bed.elder_id,
    )


# ------------------------------------------------------------------ 房间


@router.get(
    "/rooms",
    response_model=RoomListResponse,
    summary="房间分页列表（仅 admin；房号模糊、状态过滤，含床位摘要）",
)
async def list_rooms(
    identity: AdminIdentity,
    session: SessionDep,
    page: Annotated[int, Query(ge=1, description="页码，默认 1")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，默认 20，上限 100")] = 20,
    room_no: Annotated[str | None, Query(max_length=64, description="房号模糊匹配")] = None,
    room_status: Annotated[RoomStatus | None, Query(alias="status", description="房间状态")] = None,
) -> RoomListResponse:
    result = await org_service.list_rooms(
        session,
        page=page,
        page_size=page_size,
        room_no=room_no,
        status=room_status,
    )
    return RoomListResponse(
        items=[_room_item(item) for item in result.items],
        total=result.total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/rooms",
    response_model=RoomResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新建房间（仅 admin；房号空值 422、重复 409）",
)
async def create_room(
    payload: RoomCreateRequest,
    identity: AdminIdentity,
    session: SessionDep,
) -> RoomResponse:
    try:
        room = await org_service.create_room(session, identity, room_no=payload.room_no)
    except OrgServiceError as exc:
        return _org_error(exc)  # type: ignore[return-value]
    return RoomResponse(room_id=room.room_id, room_no=room.room_no, status=room.status)


@router.patch(
    "/rooms/{room_id}",
    response_model=RoomResponse,
    summary="修改房号（仅 admin；空值 422、重复 409、不存在 404）",
)
async def update_room(
    room_id: uuid.UUID,
    payload: RoomUpdateRequest,
    identity: AdminIdentity,
    session: SessionDep,
) -> RoomResponse:
    try:
        room = await org_service.update_room(session, identity, room_id, room_no=payload.room_no)
    except OrgServiceError as exc:
        return _org_error(exc)  # type: ignore[return-value]
    return RoomResponse(room_id=room.room_id, room_no=room.room_no, status=room.status)


@router.delete(
    "/rooms/{room_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除房间（仅 admin；有床位/设备引用 409）",
)
async def delete_room(
    room_id: uuid.UUID,
    identity: AdminIdentity,
    session: SessionDep,
) -> None:
    try:
        await org_service.delete_room(session, identity, room_id)
    except OrgServiceError as exc:
        return _org_error(exc)  # type: ignore[return-value]


# ------------------------------------------------------------------ 床位


@router.get(
    "/beds",
    response_model=BedListResponse,
    summary="床位分页列表（仅 admin；房间/床号模糊/状态过滤，含 room_no）",
)
async def list_beds(
    identity: AdminIdentity,
    session: SessionDep,
    page: Annotated[int, Query(ge=1, description="页码，默认 1")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，默认 20，上限 100")] = 20,
    room_id: Annotated[uuid.UUID | None, Query(description="按房间过滤")] = None,
    bed_no: Annotated[str | None, Query(max_length=32, description="床号模糊匹配")] = None,
    bed_status: Annotated[BedStatus | None, Query(alias="status", description="床位状态")] = None,
) -> BedListResponse:
    result = await org_service.list_beds(
        session,
        page=page,
        page_size=page_size,
        room_id=room_id,
        bed_no=bed_no,
        status=bed_status,
    )
    return BedListResponse(
        items=[_bed_item(item) for item in result.items],
        total=result.total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/beds",
    response_model=BedItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新建床位（仅 admin；同房重号 409、空值 422、房间不存在 404）",
)
async def create_bed(
    payload: BedCreateRequest,
    identity: AdminIdentity,
    session: SessionDep,
) -> BedItemResponse:
    try:
        bed = await org_service.create_bed(
            session, identity, room_id=payload.room_id, bed_no=payload.bed_no
        )
    except OrgServiceError as exc:
        return _org_error(exc)  # type: ignore[return-value]
    room_no_row = await _bed_room_no(session, bed.room_id)
    return _bed_item(
        BedItem(
            bed_id=bed.bed_id,
            room_id=bed.room_id,
            room_no=room_no_row,
            bed_no=bed.bed_no,
            status=bed.status,
            elder_id=bed.elder_id,
        )
    )


@router.patch(
    "/beds/{bed_id}",
    response_model=BedItemResponse,
    summary="改床号/状态（仅 admin；occupied 不可手写 422；占床改状态 409）",
)
async def update_bed(
    bed_id: uuid.UUID,
    payload: BedUpdateRequest,
    identity: AdminIdentity,
    session: SessionDep,
) -> BedItemResponse:
    try:
        bed = await org_service.update_bed(
            session,
            identity,
            bed_id,
            bed_no=payload.bed_no,
            status=payload.status,
        )
    except OrgServiceError as exc:
        return _org_error(exc)  # type: ignore[return-value]
    room_no_row = await _bed_room_no(session, bed.room_id)
    return _bed_item(
        BedItem(
            bed_id=bed.bed_id,
            room_id=bed.room_id,
            room_no=room_no_row,
            bed_no=bed.bed_no,
            status=bed.status,
            elder_id=bed.elder_id,
        )
    )


@router.delete(
    "/beds/{bed_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除空床（仅 admin；有入住长者/occupied 409）",
)
async def delete_bed(
    bed_id: uuid.UUID,
    identity: AdminIdentity,
    session: SessionDep,
) -> None:
    try:
        await org_service.delete_bed(session, identity, bed_id)
    except OrgServiceError as exc:
        return _org_error(exc)  # type: ignore[return-value]


@router.post(
    "/beds/batch-status",
    response_model=BedBatchResponse,
    summary="批量改床位状态（仅 admin；含占用/不存在整批拒绝）",
)
async def batch_update_beds_status(
    payload: BedBatchStatusRequest,
    identity: AdminIdentity,
    session: SessionDep,
) -> BedBatchResponse:
    try:
        beds = await org_service.batch_update_beds(
            session, identity, bed_ids=payload.bed_ids, status=payload.status
        )
    except OrgServiceError as exc:
        return _org_error(exc)  # type: ignore[return-value]
    return BedBatchResponse(affected=len(beds))


@router.post(
    "/beds/batch-delete",
    response_model=BedBatchResponse,
    summary="批量删除空床（仅 admin；任一占用/不存在整批拒绝）",
)
async def batch_delete_beds(
    payload: BedBatchDeleteRequest,
    identity: AdminIdentity,
    session: SessionDep,
) -> BedBatchResponse:
    try:
        deleted = await org_service.batch_delete_beds(session, identity, bed_ids=payload.bed_ids)
    except OrgServiceError as exc:
        return _org_error(exc)  # type: ignore[return-value]
    return BedBatchResponse(affected=deleted)


async def _bed_room_no(session: AsyncSession, room_id: uuid.UUID) -> str:
    """写操作响应用的房号；房间刚由服务层校验过，不存在只在数据被并发删时出现。"""
    room_no_row = (
        await session.execute(select(Room.room_no).where(Room.room_id == room_id))
    ).scalar_one_or_none()
    return room_no_row if room_no_row is not None else ""


__all__ = ["router"]
