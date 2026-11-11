"""照护台告警域 REST：N-3 人工确认（2026-11-11 主理人批准新增，Q-D1）。

## 为什么单独一个文件

`app/api/staff.py` 是员工账号域（建号/改密/停用），`workorders.py` 是工单看板。
告警通知是第三块领域，混进任一个都会让「谁能确认通知」这条权限线埋在别人的
文件里。契约《照护台业务数据》§3.2 的 N-1/N-2/N-3 本来就是一节，故新开
`alerts.py`，前缀 `/api`、路由内写 `/notifications`，编号沿用 N 前缀。

## 分层

只做三件事：校验入参 → 从 JWT 取员工身份 → 调 `notify_service`。
**不拼查询、不写库、不 commit**（`get_session` 收口）。

## 当前范围：N-1 / N-2 / N-3 三条齐

| 编号 | 端点 | 口径 |
| --- | --- | --- |
| N-1 | `GET /api/notifications` | 只出 `target_type='staff' AND target_id=本人`；`created_at` 倒序；`unread_only` 可选 |
| N-2 | `POST /api/notifications/{id}/read` | 只写 `read_at`，**已读≠确认**；幂等返回原时间 |
| N-3 | `POST /api/notifications/{id}/acknowledge` | 仅 `delivered` 可确认，跳步 409，写 `acknowledged_by/at` |

### 一个字段缺口（R-D12，2026-11-11 已收口）

契约 §3.2 的 N-1 里 `acknowledged_by_name` 是 **JOIN 展示字段**。原
`notify_service.list_staff_notifications()` 只返回 `Notification` 行、没有 JOIN，
本文件曾如实返回 null。**收口**：`staff_service.staff_names()` 批量取名（只读），
N-1 序列化时补齐；查不到的 id 落 null 不编。C 的 `alerts-api.ts` 按可空处理，
兼容不变。

## 归属校验（R-D10，2026-11-11 16:20 已由 A 侧修复）

`notify_service.acknowledge_notification()` 现在会校验 `notification.target_id
== acknowledger_id`，不匹配抛 `NotificationForbidden`。本文件据此返回 **403
`SCOPE_FORBIDDEN` 并写一条审计**（照护台契约 §2「服务端兜底 403+审计」）。

审计写法：`record_audit()` 只 `add + flush` 不 commit，由 `get_session` 在请求
成功路径统一提交——所以这里必须**正常 return 403 响应**，不能 raise（raise 会走
异常路径回滚，审计就没了）。

## 异常映射

A 侧已补 typed exception：`NotificationNotFound`（404）／`NotificationForbidden`
（403）／其余 `NotificationError`（本端点内只剩非法流转，409）。不再靠消息前缀
猜语义——R-D11 的脆点已消。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import current_staff
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import notify_service, staff_service
from app.services.audit_log_service import record_audit
from app.services.notify_service import (
    NotificationError,
    NotificationForbidden,
    NotificationNotFound,
    StaffNotificationNotFound,
)

router = APIRouter(
    prefix="/api",
    tags=["alerts"],
    route_class=ContractAPIRoute,
)


class AcknowledgeOut(BaseModel):
    """N-3 响应：契约未定义响应体，这里只回「谁被确认成了什么状态」，不含 payload。"""

    model_config = ConfigDict(extra="forbid")

    notification_id: uuid.UUID
    status: str
    acknowledged_by: uuid.UUID


async def _forbidden_response(
    session: AsyncSession,
    staff_id: uuid.UUID,
    notification_id: uuid.UUID,
    exc: NotificationForbidden,
):
    """403 + 审计。审计与业务同事务，正常 return 让 `get_session` 提交。"""
    await record_audit(
        session,
        actor_type="staff",
        actor_id=staff_id,
        action="notification_acknowledge_forbidden",
        resource_type="notification",
        resource_id=notification_id,
        detail={"reason": "not_target_owner"},
    )
    return _error_response(403, "SCOPE_FORBIDDEN", "notification is not yours")


class StaffNotificationItem(BaseModel):
    """N-1 单条。字段 = 照护台契约 §3.2 表；`acknowledged_by_name` 见模块头 R-D12。"""

    model_config = ConfigDict(extra="forbid")

    notification_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    type: str
    payload: dict
    channel: str
    status: str
    is_mock: bool
    retry_count: int
    dedupe_key: str | None
    created_at: str
    sent_at: str | None
    acknowledged_by: uuid.UUID | None
    acknowledged_by_name: str | None
    acknowledged_at: str | None
    read_at: str | None


class MarkReadOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notification_id: uuid.UUID
    read_at: str


def _iso(value: object) -> str | None:
    return value.isoformat() if value is not None else None  # type: ignore[attr-defined]


def _serialize_staff(
    notification: object, names: dict[uuid.UUID, str] | None = None
) -> StaffNotificationItem:
    return StaffNotificationItem(
        notification_id=notification.notification_id,  # type: ignore[attr-defined]
        target_type=notification.target_type,  # type: ignore[attr-defined]
        target_id=notification.target_id,  # type: ignore[attr-defined]
        type=notification.type,  # type: ignore[attr-defined]
        payload=dict(notification.payload or {}),  # type: ignore[attr-defined]
        channel=notification.channel,  # type: ignore[attr-defined]
        status=notification.status,  # type: ignore[attr-defined]
        is_mock=bool(notification.is_mock),  # type: ignore[attr-defined]
        retry_count=int(notification.retry_count or 0),  # type: ignore[attr-defined]
        dedupe_key=notification.dedupe_key,  # type: ignore[attr-defined]
        created_at=_iso(notification.created_at) or "",  # type: ignore[attr-defined]
        sent_at=_iso(notification.sent_at),  # type: ignore[attr-defined]
        acknowledged_by=notification.acknowledged_by,  # type: ignore[attr-defined]
        # R-D12 收口：service 批量取名（staff_names），查不到落 null 不编
        acknowledged_by_name=(names or {}).get(notification.acknowledged_by),  # type: ignore[attr-defined]
        acknowledged_at=_iso(notification.acknowledged_at),  # type: ignore[attr-defined]
        read_at=_iso(notification.read_at),  # type: ignore[attr-defined]
    )


@router.get("/notifications", response_model=dict)
async def list_staff_notifications(
    session: Annotated[AsyncSession, Depends(get_session)],
    staff: Annotated[Identity, Depends(current_staff)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    unread_only: bool = Query(default=False),
) -> dict[str, object]:
    """N-1：员工收到的通知列表。

    范围过滤在 service 的查询条件里（`target_type='staff' AND target_id=本人`），
    api 不做二次过滤——否则「谁能看什么」会出现第二份定义。
    """
    rows, total = await notify_service.list_staff_notifications(
        session,
        staff_id=uuid.UUID(staff.staff_id),
        page=page,
        page_size=page_size,
        unread_only=unread_only,
    )
    # R-D12 收口：acknowledged_by_name 为契约 JOIN 展示字段，service 批量取名补齐
    names = await staff_service.staff_names(session, [row.acknowledged_by for row in rows])
    return {
        "items": [_serialize_staff(row, names) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/notifications/{notification_id}/read", response_model=MarkReadOut)
async def mark_staff_notification_read(
    notification_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    staff: Annotated[Identity, Depends(current_staff)],
) -> MarkReadOut:
    """N-2：员工标记已读。**已读 ≠ 确认**，不动 `status`（AC-23）。

    不存在 → 404；不属于当前员工 → 403+审计（R-D10 同精神，service 抛
    NotificationForbidden；审计 flush 后正常 return，raise 会回滚审计）。
    """
    try:
        read_at = await notify_service.mark_staff_notification_read(
            session,
            staff_id=uuid.UUID(staff.staff_id),
            notification_id=notification_id,
        )
    except NotificationForbidden:
        await record_audit(
            session,
            actor_type="staff",
            actor_id=uuid.UUID(staff.staff_id),
            action="notification_read_forbidden",
            resource_type="notification",
            resource_id=notification_id,
            detail={"reason": "not_target_owner"},
        )
        return _error_response(403, "SCOPE_FORBIDDEN", "notification is not yours")
    except StaffNotificationNotFound:
        return _error_response(404, "NOT_FOUND", "notification not found")
    return MarkReadOut(notification_id=notification_id, read_at=_iso(read_at) or "")


@router.post("/notifications/{notification_id}/acknowledge", response_model=None)
async def acknowledge_notification(
    notification_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    staff: Annotated[Identity, Depends(current_staff)],
):
    """N-3：员工人工确认（送达 ≠ 确认；只允许 delivered → acknowledged）。

    操作人一律取 `staff.staff_id`，**不接受客户端传 actor**（AGENTS §4 红线 9）。
    幂等：已确认的通知再确认一次返回 200，不重复写审计/时间戳（服务层短路）。
    """
    # `Identity.staff_id` 是字符串，而 `notifications.target_id` 是 uuid 列：
    # service 现在用 `!=` 直接比较两者，不归一化就会「永远不相等」→ 所有员工 403。
    # 这里在 api 边界做一次类型收口（身份解析本来就是 api 的活）。
    acknowledger_id = uuid.UUID(staff.staff_id)
    try:
        await notify_service.acknowledge_notification(
            session,
            notification_id=notification_id,
            acknowledger_id=acknowledger_id,
        )
    except NotificationForbidden as exc:
        return await _forbidden_response(session, acknowledger_id, notification_id, exc)
    except NotificationNotFound:
        return _error_response(404, "NOT_FOUND", "notification not found")
    except NotificationError:
        # 本端点内剩下的只有「非法流转」（generated/pending/failed → acknowledged）
        return _error_response(409, "CONFLICT", f"notification {notification_id} 状态不允许确认")
    return AcknowledgeOut(
        notification_id=notification_id,
        status="acknowledged",
        acknowledged_by=acknowledger_id,
    )
