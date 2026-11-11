# owner=D3 · 依据 PRD FR-UI-08 / AC-26
"""pad 普通确认撤销 REST（Day 8 D3 · Q-D7）。

FR-UI-08 的两条边界在这里各走各的：

- **普通「已服自报」** 10 秒内可撤销 → 本文件 `POST /api/pad/medication-events/{id}/revoke`，
  调 `medication_service.revoke_taken_report` 恢复为 `waiting_confirm` 并写审计。
- **红色事件不可一键撤销** → 走 `app/api/sos.py` 的 `POST /api/pad/sos/{id}/correction`，
  只追加「说错了」说明、事件保持 open。

鉴权：`current_device`，`elder_id` 只从设备绑定表读出；事件不属于本设备长者一律按
「不存在」返回 404（防枚举）。分层：只做校验 + 调 service，不拼查询、不写库。
路由经 `app/api/__init__.py` 的 `ROUTERS` 挂载，不改 `main.py`。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import current_device
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import medication_service
from app.services.medication_service import (
    MedicationEventNotFound,
    MedicationUndoNotAllowed,
    MedicationUndoWindowExpired,
)

router = APIRouter(prefix="/api/pad", tags=["pad"], route_class=ContractAPIRoute)


class MedicationUndoResponse(BaseModel):
    """撤销结果：事件回到待确认，终态清空。"""

    model_config = ConfigDict(extra="forbid")

    reminder_id: uuid.UUID
    status: str
    terminal_state: str | None


@router.post(
    "/medication-events/{reminder_id}/revoke",
    response_model=MedicationUndoResponse,
    summary="普通「已服自报」10 秒内撤销（红色事件不走本端点）",
)
async def revoke_taken_report(
    reminder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MedicationUndoResponse | JSONResponse:
    if identity.elder_id is None:
        return _error_response(403, "device_not_bound", "device is not bound to an elder")

    try:
        result = await medication_service.revoke_taken_report(
            session,
            elder_id=uuid.UUID(identity.elder_id),
            reminder_id=reminder_id,
            device_id=identity.device_id,
        )
    except MedicationEventNotFound as exc:
        return _error_response(404, "NOT_FOUND", str(exc))
    except MedicationUndoWindowExpired as exc:
        return _error_response(409, "UNDO_WINDOW_EXPIRED", str(exc))
    except MedicationUndoNotAllowed as exc:
        return _error_response(409, "CONFLICT", str(exc))

    return MedicationUndoResponse.model_validate(result)


__all__ = ["router"]
