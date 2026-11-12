"""生命周期 REST（Day 9 A4 / FR-ADM-02 / FR-ADM-09）。

三端点全部 **admin-only**（`require_roles("admin")`），重置与退房需**二次确认
令牌**（字面 `"CONFIRM"`，Z-A4b；缺失/不符 → 409）。薄路由：只校验＋调
`lifecycle_service`＋序列化，不写业务逻辑（AGENTS §2.3）。

## 异常映射（契约 §7 统一错误体 `{detail, code, trace_id}`）

| service 异常 | HTTP | code | 副作用 |
| --- | --- | --- | --- |
| `LifecycleConfirmError` | 409 | `CONFIRM_REQUIRED` | 零副作用 |
| `ElderNotFoundError` | 404 | `NOT_FOUND` | 零副作用 |
| `LifecycleStepError` | 500 | `LIFECYCLE_STEP_FAILED` | **部分完成**：已成功步骤保持（各自已 commit），detail 带 `failed_step` 供单独重跑 |

`LifecycleStepError` 不能映射 4xx：流水线部分完成是服务端状态，重试语义由
幂等编排保证（重跑跳过已成功步骤），客户端拿到 500 后原请求重发即续跑。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import require_roles
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import lifecycle_service
from app.services.lifecycle_service import (
    ElderNotFoundError,
    LifecycleConfirmError,
    LifecycleStepError,
)

router = APIRouter(
    prefix="/api/lifecycle",
    tags=["lifecycle"],
    route_class=ContractAPIRoute,
)


class ConfirmIn(BaseModel):
    """二次确认载荷：重置与退房必填。"""

    confirm_token: str = Field(min_length=1, description="二次确认令牌，字面 CONFIRM")


class AwayIn(BaseModel):
    """暂离/恢复载荷：true=暂离，false=恢复。"""

    away: bool


class StepOut(BaseModel):
    step: str
    skipped: bool
    affected_rows: int
    archived_rows: int
    stub: bool


class LifecycleOut(BaseModel):
    elder_id: uuid.UUID
    reason: str
    success: bool
    steps: list[StepOut]


@router.post(
    "/elders/{elder_id}/checkout",
    response_model=LifecycleOut,
    summary="长者退房（admin-only；五步清理流水线＋每步审计；需二次确认）",
)
async def checkout_elder(
    elder_id: uuid.UUID,
    payload: ConfirmIn,
    admin: Annotated[Identity, Depends(require_roles("admin"))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LifecycleOut | object:
    try:
        report = await lifecycle_service.checkout_elder(
            session, operator=admin, elder_id=elder_id, confirm_token=payload.confirm_token
        )
    except LifecycleConfirmError as exc:
        return _error_response(409, "CONFIRM_REQUIRED", str(exc))
    except ElderNotFoundError as exc:
        return _error_response(404, "NOT_FOUND", str(exc))
    except LifecycleStepError as exc:
        return _error_response(
            500, "LIFECYCLE_STEP_FAILED", f"step {exc.step} failed; pipeline is resumable"
        )
    return _to_out(report)


@router.post(
    "/elders/{elder_id}/teaching-reset",
    response_model=LifecycleOut,
    summary="教学周期重置（admin-only；业务数据清零、账号/配置/审计保留；需二次确认）",
)
async def teaching_reset(
    elder_id: uuid.UUID,
    payload: ConfirmIn,
    admin: Annotated[Identity, Depends(require_roles("admin"))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LifecycleOut | object:
    try:
        report = await lifecycle_service.teaching_reset(
            session, operator=admin, elder_id=elder_id, confirm_token=payload.confirm_token
        )
    except LifecycleConfirmError as exc:
        return _error_response(409, "CONFIRM_REQUIRED", str(exc))
    except ElderNotFoundError as exc:
        return _error_response(404, "NOT_FOUND", str(exc))
    except LifecycleStepError as exc:
        return _error_response(
            500, "LIFECYCLE_STEP_FAILED", f"step {exc.step} failed; pipeline is resumable"
        )
    return _to_out(report)


@router.post(
    "/elders/{elder_id}/away",
    response_model=LifecycleOut,
    summary="暂离/恢复（admin-only；只改状态＋审计；采集与提醒由调度侧读 status 暂停）",
)
async def set_away(
    elder_id: uuid.UUID,
    payload: AwayIn,
    admin: Annotated[Identity, Depends(require_roles("admin"))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LifecycleOut | object:
    try:
        report = await lifecycle_service.set_away(
            session, operator=admin, elder_id=elder_id, away=payload.away
        )
    except ElderNotFoundError as exc:
        return _error_response(404, "NOT_FOUND", str(exc))
    return _to_out(report)


def _to_out(report: object) -> LifecycleOut:
    """`LifecycleReport.as_dict()` → 响应模型（薄序列化，不做判定）。"""
    data = report.as_dict()  # type: ignore[attr-defined]
    return LifecycleOut(
        elder_id=uuid.UUID(data["elder_id"]),
        reason=data["reason"],
        success=data["success"],
        steps=[StepOut(**step) for step in data["steps"]],
    )
