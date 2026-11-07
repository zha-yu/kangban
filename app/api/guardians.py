"""家属账号与家属绑定 REST 薄层（G1 建家属账号 / E13 绑定）。

owner=建号接口（adhoc-20261106）· 依据 docs/plans/adhoc-20261106-员工与家属建号接口.md

契约只定义了 E13「绑定已有家属账号」，**没有创建家属账号的端点**（缺口 G1）；
本模块补上 `POST /api/guardians`，与 E13 同属「管理员建号 + 绑定」这一条能力。

权限：仅 `admin`。
范围：绑定的写入侧同时补齐缺口 G2 —— `elder_guardians` 是家属侧范围过滤的唯一依据，
此前没有任何写入口，导致新建家属永远看不到长者。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.api.schemas.staff import (
    GuardianBindRequest,
    GuardianBindResponse,
    GuardianCreateRequest,
    GuardianCreateResponse,
)
from app.core.auth import require_roles
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import staff_service
from app.services.staff_service import (
    ElderNotFoundError,
    GuardianAlreadyBoundError,
    GuardianDisabledError,
    GuardianLimitExceededError,
    GuardianNotFoundError,
    StaffConflictError,
)

router = APIRouter(tags=["guardians"], route_class=ContractAPIRoute)


@router.post(
    "/api/guardians",
    status_code=status.HTTP_201_CREATED,
    response_model=GuardianCreateResponse,
)
async def create_guardian(
    payload: GuardianCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[Identity, Depends(require_roles("admin"))],
) -> GuardianCreateResponse:
    """建家属账号（契约缺口 G1）。手机号跨表唯一，冲突 409。"""
    try:
        row = await staff_service.create_guardian(
            session,
            name=payload.name,
            phone=payload.phone,
            password=payload.password,
        )
    except StaffConflictError as exc:
        return _error_response(409, "CONFLICT", exc.message)  # type: ignore[return-value]
    return GuardianCreateResponse(
        guardian_id=row.guardian_id,
        name=row.name,
        phone_masked=staff_service.mask_phone(row.phone),
        status=row.status,
    )


@router.post(
    "/api/elders/{elder_id}/guardians",
    status_code=status.HTTP_201_CREATED,
    response_model=GuardianBindResponse,
)
async def bind_guardian(
    elder_id: uuid.UUID,
    payload: GuardianBindRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _admin: Annotated[Identity, Depends(require_roles("admin"))],
) -> GuardianBindResponse:
    """把已有家属账号绑定到长者（E13）。每位长者 active 监护人最多 2 位。"""
    try:
        binding = await staff_service.bind_guardian(
            session,
            elder_id=elder_id,
            guardian_id=payload.guardian_id,
            relation=payload.relation,
        )
    except ElderNotFoundError as exc:
        return _error_response(404, "NOT_FOUND", exc.message)  # type: ignore[return-value]
    except GuardianNotFoundError as exc:
        return _error_response(404, "NOT_FOUND", exc.message)  # type: ignore[return-value]
    except GuardianAlreadyBoundError as exc:
        return _error_response(409, "CONFLICT", exc.message)  # type: ignore[return-value]
    except GuardianDisabledError as exc:
        # 存量修复（2026-11-07）：disabled 家属绑定应返 409 而非漏捕 500（A-1 全量回归发现）
        return _error_response(409, "CONFLICT", exc.message)  # type: ignore[return-value]
    except GuardianLimitExceededError as exc:
        return _error_response(409, "CONFLICT", exc.message)  # type: ignore[return-value]
    return GuardianBindResponse(
        elder_id=binding.elder_id,
        guardian_id=binding.guardian_id,
        relation=binding.relation,
        status=binding.status,
    )


__all__ = ["router"]
