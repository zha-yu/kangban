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

## 归属校验缺口（登记，未擅自改 service）

`notify_service.acknowledge_notification()` 目前**只按 notification_id 查**，
不校验该通知的 `target_type/target_id` 是否等于当前员工。也就是说：
任何持有效工牌的员工，都能确认**别人**的通知。

本文件**不能**在 api 层补这个校验——`app/api` 不许拼查询，员工侧也没有可用的
读函数。故此处如实登记为缺口，等 A 侧给 `acknowledge_notification` 增加
`target_type` / `target_id` 参数（或提供带归属校验的新函数）。
**在补齐前，本端点不应用于真实环境**，仅教学演示，且已记入计划 §10 与
`R-D10`。

另外：`NotificationError` 同时承载「不存在」与「非法流转」两种语义，只能靠
消息前缀区分（见 `_map_notification_error`）。这是脆的，已登记请 A 侧补
typed exception（`NotificationNotFound` / `IllegalTransition`）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import current_staff
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import notify_service
from app.services.notify_service import NotificationError

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


def _map_notification_error(exc: NotificationError, notification_id: uuid.UUID) -> Any:
    """`NotificationError` → 契约错误体。

    - `illegal notification transition: X → acknowledged` → **409 CONFLICT**（契约 N-3「禁止跳步」）；
    - `notification ... not found` → **404 NOT_FOUND**；
    - 其余（服务层自身异常）原样抛出，不伪装成业务错误。

    靠消息前缀分流是权宜——`notify_service` 目前只抛一个 `NotificationError`。
    已登记请 A 侧拆成两个 typed exception，届时本函数改成 `isinstance` 判断。
    """
    message = str(exc)
    if message.startswith("illegal notification transition"):
        return _error_response(409, "CONFLICT", f"notification {notification_id} 状态不允许确认")
    if "not found" in message:
        return _error_response(404, "NOT_FOUND", "notification not found")
    raise exc


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
    try:
        await notify_service.acknowledge_notification(
            session,
            notification_id=notification_id,
            acknowledger_id=staff.staff_id,
        )
    except NotificationError as exc:
        return _map_notification_error(exc, notification_id)
    return AcknowledgeOut(
        notification_id=notification_id,
        status="acknowledged",
        acknowledged_by=staff.staff_id,
    )
