"""照护台告警域 REST：N-3 人工确认（2026-11-11 主理人批准新增，Q-D1）。

## 为什么单独一个文件

`app/api/staff.py` 是员工账号域（建号/改密/停用），`workorders.py` 是工单看板。
告警通知是第三块领域，混进任一个都会让「谁能确认通知」这条权限线埋在别人的
文件里。契约《照护台业务数据》§3.2 的 N-1/N-2/N-3 本来就是一节，故新开
`alerts.py`，前缀 `/api`、路由内写 `/notifications`，编号沿用 N 前缀。

## 分层

只做三件事：校验入参 → 从 JWT 取员工身份 → 调 `notify_service`。
**不拼查询、不写库、不 commit**（`get_session` 收口）。

## 当前范围：只有 N-3

- **N-3 已实现**：`POST /api/notifications/{id}/acknowledge`，
  仅 `status=delivered` 可确认，跳步 409，写 `acknowledged_by/at` 并置 `acknowledged`。
- **N-1 列表 / N-2 已读 未实现**：`notify_service` 只有监护人侧读函数，
  员工侧读/已读函数尚未交付（归 A3，`notify_service` 增量在 A 写域）。
  骨架不先摆出来——摆了就是给人一个「接口在但永远 500」的假象。

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

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import current_staff
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import notify_service
from app.services.audit_log_service import record_audit
from app.services.notify_service import (
    NotificationError,
    NotificationForbidden,
    NotificationNotFound,
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
