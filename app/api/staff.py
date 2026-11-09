"""员工账号 REST 薄层（契约 E12：建员工 / 改角色与停用）。

owner=建号接口（adhoc-20261106）· 依据 docs/plans/adhoc-20261106-员工与家属建号接口.md

只做四件事：Pydantic 校验、接收当前主体、调 `app/services/staff_service.py`、
把领域异常映射成契约 §7 的错误体（`{detail, code, trace_id}`）。不拼查询、不写库。

权限：仅 `admin`（PRD FR-ADM-01「用户/角色维护，P0 后台」；PRD §3.1 角色矩阵）。
范围：管理员不绑长者（裁决 D-01），建号时不接受 `elder_ids` 里的 admin 分支。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.api.schemas.staff import (
    ElderReassignRequest,
    ElderReassignResponse,
    StaffCreateRequest,
    StaffListItemResponse,
    StaffResponse,
    StaffUpdateRequest,
)
from app.core.auth import require_roles
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import staff_service
from app.services.staff_service import (
    ElderNotFoundError,
    PrimaryCareGapError,
    StaffConflictError,
    StaffNotFoundError,
    StaffRoleConflictError,
)

router = APIRouter(
    prefix="/api/staff",
    tags=["staff"],
    route_class=ContractAPIRoute,
)


def _to_response(row: staff_service.StaffAccount) -> StaffResponse:
    return StaffResponse(
        staff_id=row.staff_id,
        name=row.name,
        role=row.role,
        phone_masked=staff_service.mask_phone(row.phone),
        status=row.status,
    )


@router.get("", response_model=dict)
async def list_staff(
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[Identity, Depends(require_roles("admin"))],
    role: str | None = None,
    status: str | None = None,
) -> dict:
    """列出所有员工（admin 权限）。

    契约 E12 Day 6 补缺口：原契约只冻结 POST/PATCH，Day 6 dev-D N18
    改绑 UI 需要员工列表渲染，因此追加 GET 读入口（主理人裁决，不升级契约版本）。

    Query:
        role=care_worker|doctor|admin   可选过滤
        status=active|disabled           可选过滤
    """
    rows = await staff_service.list_staff_accounts(session, role_filter=role, status_filter=status)
    items: list[StaffListItemResponse] = []
    for row, elder_ids in rows:
        items.append(
            StaffListItemResponse(
                staff_id=row.staff_id,
                name=row.name,
                role=row.role,
                phone_masked=staff_service.mask_phone(row.phone),
                status=row.status,
                bound_elder_ids=elder_ids,
            )
        )
    return {"items": items}


@router.post("", status_code=status.HTTP_201_CREATED, response_model=StaffResponse)
async def create_staff(
    payload: StaffCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[Identity, Depends(require_roles("admin"))],
) -> StaffResponse:
    """建员工账号（admin / care_worker / doctor）。手机号跨表唯一，冲突 409。"""
    try:
        row = await staff_service.create_staff(
            session,
            name=payload.name,
            role=payload.role,
            phone=payload.phone,
            password=payload.password,
            elder_ids=payload.elder_ids,
        )
    except StaffConflictError as exc:
        return _error_response(409, "CONFLICT", exc.message)  # type: ignore[return-value]
    except StaffRoleConflictError as exc:
        return _error_response(403, "ROLE_FORBIDDEN", exc.message)  # type: ignore[return-value]
    except ElderNotFoundError as exc:
        return _error_response(404, "NOT_FOUND", exc.message)  # type: ignore[return-value]
    return _to_response(row)


@router.patch("/{staff_id}", response_model=StaffResponse)
async def update_staff(
    staff_id: uuid.UUID,
    payload: StaffUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[Identity, Depends(require_roles("admin"))],
) -> StaffResponse:
    """改角色或停用；任一变更都会让该账号旧 Token 立即失效（AC-24）。"""
    try:
        row = await staff_service.update_staff(
            session,
            staff_id=staff_id,
            role=payload.role,
            status=payload.status,
        )
    except StaffNotFoundError as exc:
        return _error_response(404, "NOT_FOUND", exc.message)  # type: ignore[return-value]
    return _to_response(row)


@router.put("/{staff_id}/elders", response_model=ElderReassignResponse)
async def reassign_elders(
    staff_id: uuid.UUID,
    payload: ElderReassignRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[Identity, Depends(require_roles("admin"))],
) -> ElderReassignResponse:
    """全量替换式改绑（E20）。

    语义：admin 把目标 staff 当前的长者列表**整体替换**成请求体里的列表。
    空数组表示清空全部绑定。返回差集（added / removed）供前端提示。
    """
    try:
        result = await staff_service.reassign_elders(
            session,
            staff_id=staff_id,
            elder_ids=payload.elder_ids,
            operator_id=admin.staff_id,
        )
    except StaffNotFoundError as exc:
        return _error_response(404, "NOT_FOUND", exc.message)  # type: ignore[return-value]
    except StaffRoleConflictError as exc:
        return _error_response(403, "ROLE_FORBIDDEN", exc.message)  # type: ignore[return-value]
    except ElderNotFoundError as exc:
        return _error_response(404, "NOT_FOUND", exc.message)  # type: ignore[return-value]
    except PrimaryCareGapError as exc:
        return _error_response(409, "CONFLICT", exc.message)  # type: ignore[return-value]
    return ElderReassignResponse(
        staff_id=result["staff_id"],
        added=result["added"],
        removed=result["removed"],
    )


__all__ = ["router"]
