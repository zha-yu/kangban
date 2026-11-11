"""家属端通知 REST（N1 列表 / N4 标记已读，2026-11-10 提前件）。

owner=family 通知接口 · 依据 frontend/src/family/api.ts 注释里的「API 契约_家属数据」
草稿 §4（N1 分页 1/20 上限 100、unread_only 默认 false；N4 只写 read_at 不动 status）；
上游依据 PRD FR-RPT-03 / AC-38（家属通知落库可查）。

分层：只做校验 + 范围过滤（`current_guardian` → `target_id=guardian_id` 天然按人
隔离）+ 调 `app/services/notify_service.py`；不拼查询、不写库。
N4 越权口径按家属端契约 v1.2 冻结收口：不属于当前家属 → 403+审计（同事务
flush、正常 return 让 get_session 提交）；不存在 → 404。

范围：本文件不改 `app/main.py`（写锁 L7-2），经 `app/api/__init__.py` 的
ROUTERS 注册表挂载；`status` 送达状态机归 Day 8 P8 调度器，本接口不碰。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import current_guardian
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import notify_service
from app.services.audit_log_service import record_audit
from app.services.notify_service import (
    GuardianNotificationNotFound,
    NotificationForbidden,
)

router = APIRouter(
    prefix="/api/family",
    tags=["family"],
    route_class=ContractAPIRoute,
)


class NotificationItem(BaseModel):
    """字段 = 前端 `FamilyNotification` 类型（data-source.ts），逐项对齐。"""

    model_config = ConfigDict(extra="forbid")

    notification_id: uuid.UUID
    type: str
    payload: dict[str, Any]
    channel: str
    status: str
    is_mock: bool
    retry_count: int
    created_at: str
    sent_at: str | None
    read_at: str | None


class NotificationReadOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notification_id: uuid.UUID
    read_at: str


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _serialize(notification: Any) -> NotificationItem:
    return NotificationItem(
        notification_id=notification.notification_id,
        type=notification.type,
        payload=dict(notification.payload or {}),
        channel=notification.channel,
        status=notification.status,
        is_mock=bool(notification.is_mock),
        retry_count=int(notification.retry_count or 0),
        created_at=_iso(notification.created_at) or "",
        sent_at=_iso(notification.sent_at),
        read_at=_iso(notification.read_at),
    )


@router.get("/notifications", response_model=dict)
async def list_notifications(
    session: Annotated[AsyncSession, Depends(get_session)],
    guardian: Annotated[Identity, Depends(current_guardian)],
    page: int = Query(default=1, ge=1, description="页码，1 起"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数，上限 100"),
    unread_only: bool = Query(default=False, description="只看未读"),
) -> dict[str, object]:
    """N1：当前家属收到的通知列表（分页，按生成时间倒序）。

    范围过滤：`target_type='guardian' AND target_id=<登录家属>`——查的是
    「发给我」的通知，他人/其他长者的通知在查询条件里就被排除。
    """
    rows, total = await notify_service.list_guardian_notifications(
        session,
        guardian_id=guardian.guardian_id,
        page=page,
        page_size=page_size,
        unread_only=unread_only,
    )
    return {
        "items": [_serialize(n) for n in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/notifications/{notification_id}/read", response_model=NotificationReadOut)
async def mark_notification_read(
    notification_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    guardian: Annotated[Identity, Depends(current_guardian)],
) -> NotificationReadOut:
    """N4：标记已读（幂等：已读返回原 `read_at`；只写 `read_at` 不动 `status`）。

    不属于当前家属 → 403+审计（契约 v1.2 §4 N4）；不存在 → 404。
    """
    try:
        read_at = await notify_service.mark_guardian_notification_read(
            session,
            guardian_id=guardian.guardian_id,
            notification_id=notification_id,
        )
    except NotificationForbidden:
        await record_audit(
            session,
            actor_type="guardian",
            actor_id=uuid.UUID(guardian.guardian_id),
            action="family_notification_read_forbidden",
            resource_type="notification",
            resource_id=notification_id,
            detail={"reason": "not_owned"},
        )
        return _error_response(403, "SCOPE_FORBIDDEN", "notification is outside your scope")
    except GuardianNotificationNotFound as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return NotificationReadOut(notification_id=notification_id, read_at=_iso(read_at) or "")
