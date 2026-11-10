"""console 工单看板 REST（f2-D1，2026-11-10 提前件）。

owner=console 工单接口 · 依据 frontend/src/console/workorders-api.ts（页面层已按
冻结签名开发，本接口字段逐项对齐其 TS 类型 `WorkOrder` / `WorkOrderTransition`）；
上游依据 PRD FR-ALT-01/02（红档置顶、全程留痕）。

只做三件事：Pydantic 校验、接收当前主体（`require_roles`；操作人 `staff_id`
从 JWT 注入，**前端传什么都不采信**——AGENTS §4 红线 9）、调
`app/services/work_order_service.py`。不拼查询、不写库。

权限口径：
- 看板读：care_worker / doctor / admin（交接清单是员工侧视图，不做长者过滤，
  与前端 Mock 版全量展示口径一致）；
- `reopen` 撤销办结：仅 admin（对齐前端 `isReopenAllowed` 的 Mock 口径）。
范围：本文件不改 `app/main.py`（写锁 L7-2），经 `app/api/__init__.py` 的
ROUTERS 注册表挂载。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.core.auth import require_roles
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import work_order_service
from app.services.work_order_service import (
    InvalidTransition,
    WorkOrderConflict,
    WorkOrderError,
    WorkOrderNotFound,
)

router = APIRouter(
    prefix="/api/work-orders",
    tags=["work-orders"],
    route_class=ContractAPIRoute,
)

#: 看板读权限（与 Mock 版口径一致：员工侧全量视图，红档置顶在前端排序）。
_BOARD_ROLES = ("care_worker", "doctor", "admin")

#: TransitionAction → `work_orders.status`（前端 Mock 的六值状态机逐字对齐）。
_ACTION_TO_STATUS: dict[str, str] = {
    "acknowledge": "acknowledged",
    "start": "in_progress",
    "resolve": "resolved",
    "close": "closed",
    "false_alarm": "closed",
    "reopen": "reopened",
}

#: 这些动作必须留备注（resolve 红档另需 actual_handled_at，service 层强制）。
_NOTE_REQUIRED: frozenset[str] = frozenset({"resolve", "close", "false_alarm", "reopen"})


def _require_roles(identity: Identity, roles: tuple[str, ...]) -> None:
    """函数内二次门禁（reopen 专属 admin）；403 语义对齐 RoleForbiddenError。"""
    from fastapi import HTTPException

    role = getattr(identity, "role", None)
    if role not in roles:
        raise HTTPException(status_code=403, detail="该操作仅管理员可用")


# ------------------------------------------------------------------ 响应模型（字段 = 前端 workorders-api.ts 的 TS 类型）


class TransitionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transition_id: uuid.UUID
    order_id: uuid.UUID
    from_status: str
    to_status: str
    actor_id: uuid.UUID | None
    actor_name: str | None
    at: str
    note: str | None


class WorkOrderOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: uuid.UUID
    order_no: str
    elder_id: uuid.UUID
    elder_name: str | None
    room_label: str | None
    type: str
    severity: str
    status: str
    assignee_id: uuid.UUID | None
    assignee_name: str | None
    false_alarm: bool
    false_alarm_reason: str | None
    actual_handled_at: str | None
    note: str | None
    created_at: str
    closed_at: str | None
    transitions: list[TransitionOut]


class TransitionRequest(BaseModel):
    """POST transitions 请求体。

    `actor_id` 仅为兼容前端签名保留（页面透传当前登录人）——**后端一律从
    JWT 注入操作人**，此字段被丢弃，不参与任何写库决策（AGENTS §4 红线 9）。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["acknowledge", "start", "resolve", "close", "false_alarm", "reopen"]
    note: str | None = Field(default=None, max_length=500)
    actual_handled_at: datetime.datetime | None = None
    actor_id: uuid.UUID | None = None


# ------------------------------------------------------------------ 序列化


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _serialize(row: work_order_service.WorkOrderRow) -> WorkOrderOut:
    order = row.order
    return WorkOrderOut(
        order_id=order.order_id,
        order_no=order.order_no,
        elder_id=order.elder_id,
        elder_name=row.elder_name,
        room_label=row.room_label,
        type=order.type,
        severity=order.severity,
        status=order.status,
        assignee_id=order.assignee_id,
        assignee_name=row.assignee_name,
        false_alarm=bool(order.false_alarm),
        false_alarm_reason=order.false_alarm_reason,
        actual_handled_at=_iso(order.actual_handled_at),
        note=order.note,
        created_at=_iso(order.created_at) or "",
        closed_at=_iso(order.closed_at),
        transitions=[
            TransitionOut(
                transition_id=t.transition.transition_id,
                order_id=t.transition.order_id,
                from_status=t.transition.from_status,
                to_status=t.transition.to_status,
                actor_id=t.transition.actor_id,
                actor_name=t.actor_name,
                at=_iso(t.transition.at) or "",
                note=t.transition.note,
            )
            for t in row.transitions
        ],
    )


def _map_service_error(exc: Exception) -> None:
    """服务层领域异常 → 契约错误体（与 elders.py 的映射风格一致）。"""
    if isinstance(exc, WorkOrderNotFound):
        raise _not_found() from exc
    if isinstance(exc, InvalidTransition | WorkOrderConflict | WorkOrderError):
        raise _invalid_transition(str(exc)) from exc
    raise exc


def _not_found():
    from fastapi import HTTPException

    return HTTPException(status_code=404, detail="工单不存在")


def _invalid_transition(detail: str):
    from fastapi import HTTPException

    return HTTPException(status_code=422, detail=detail)


# ------------------------------------------------------------------ 路由


@router.get("", response_model=list[WorkOrderOut])
async def list_work_orders(
    session: Annotated[AsyncSession, Depends(get_session)],
    _staff: Annotated[Identity, Depends(require_roles(*_BOARD_ROLES))],
    limit: int = Query(default=500, ge=1, le=1000),
) -> list[WorkOrderOut]:
    """看板全量列表（含全程流转内嵌；筛选与红档置顶在页面层做，Mock 口径不变）。"""
    rows = await work_order_service.list_work_orders(session, limit=limit)
    return [_serialize(row) for row in rows]


@router.post("/{order_id}/transitions", response_model=WorkOrderOut)
async def transition_work_order(
    order_id: uuid.UUID,
    body: TransitionRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_roles(*_BOARD_ROLES))],
) -> WorkOrderOut:
    """工单流转（确认/开始/办结/关闭/误报/撤销办结）。

    操作人从 JWT 注入；`reopen` 仅 admin；流转后返回整张工单（含新增流转），
    前端以返回值整行替换（与 Mock 版 `transitionWorkOrder` 签名一致）。
    """
    if body.action == "reopen":
        _require_roles(identity, ("admin",))
    if body.action in _NOTE_REQUIRED and not (body.note and body.note.strip()):
        raise _invalid_transition(f"{body.action} 必须填写备注")

    to_status = _ACTION_TO_STATUS[body.action]
    actor_id = getattr(identity, "staff_id", None)
    try:
        await work_order_service.transition(
            session,
            order_id=order_id,
            to_status=to_status,
            actor_id=actor_id,
            note=body.note,
            false_alarm=body.action == "false_alarm",
            false_alarm_reason=body.note if body.action == "false_alarm" else None,
            actual_handled_at=body.actual_handled_at,
        )
        row = await work_order_service.get_work_order(session, order_id=order_id)
    except (WorkOrderNotFound, InvalidTransition, WorkOrderConflict, WorkOrderError) as exc:
        _map_service_error(exc)
        raise  # pragma: no cover — _map_service_error 必抛
    if row is None:
        raise _not_found()
    return _serialize(row)
