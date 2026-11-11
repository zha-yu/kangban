"""SOS 一键求助 service 骨架（Day 8 · D3，FR-ALT-06）。

主理人裁决（2026-11-11）：Day 8 建 service 骨架，前端按钮接线与 30 秒联演
顺延 Day 9/10。点击即生成敏感事件＋护工工单，**不经语音路由**；本骨架不建
REST 端点（Day 9 接线时加），不写 pad。

幂等：长者存在未关闭（open/escalated）sos 事件时复用事件/工单/通知，不重复
建单（dedupe_key=sos:<event_id>）。0010 迁移两表枚举已含 sos（2026-11-11
实测核对），**零迁移改动**——A 卡「枚举没定 sos」说法不成立，留痕见计划。

分层：services 唯一写库层；建单/通知复用 work_order_service/notify_service
单一写口，不另造第二套。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.medication import RiskEvent, WorkOrder
from app.services import notify_service, work_order_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SosResult:
    event_id: uuid.UUID
    order_id: uuid.UUID | None
    created: bool


async def trigger_sos(session: AsyncSession, *, elder_id: uuid.UUID) -> SosResult:
    """生成 sos 红色事件＋护工工单＋护工通知；未关闭事件存在时幂等复用。

    不 commit：事务归调用方（与 record_audit/escalate 同纪律）。
    """
    existing = (
        (
            await session.execute(
                select(RiskEvent).where(
                    RiskEvent.elder_id == elder_id,
                    RiskEvent.type == "sos",
                    RiskEvent.status.in_(["open", "escalated"]),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        order = (
            (
                await session.execute(
                    select(WorkOrder).where(WorkOrder.event_id == existing.event_id)
                )
            )
            .scalars()
            .first()
        )
        logger.info("sos_idempotent_reuse event_id=%s elder_id=%s", existing.event_id, elder_id)
        return SosResult(existing.event_id, order.order_id if order else None, False)

    event = RiskEvent(
        event_id=uuid.uuid4(),
        elder_id=elder_id,
        type="sos",
        severity="red",
        status="open",
    )
    session.add(event)
    await session.flush()

    assignee = await work_order_service.resolve_primary_care_staff(session, elder_id=elder_id)
    ref = await work_order_service.open_work_order(
        session,
        elder_id=elder_id,
        order_type="sos",
        severity="red",
        dedupe_token=f"sos:{event.event_id}",
        event_id=event.event_id,
        assignee_id=assignee,
    )
    await notify_service.send_staff_notifications(
        session,
        elder_id=elder_id,
        ntype="sos",
        payload={"title": "一键求助", "summary": "长者按下 SOS 按钮，请立即响应"},
        dedupe_key=f"sos:{event.event_id}",
    )
    logger.warning(
        "sos_triggered event_id=%s elder_id=%s order_id=%s", event.event_id, elder_id, ref.order_id
    )
    return SosResult(event.event_id, ref.order_id, True)
