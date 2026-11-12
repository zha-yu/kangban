"""运营统计查询（Day 9 · C4 · FR-ALT-05，PRD line 380「红色事件统计」）。

**只读聚合**，不写任何业务表。三项指标（FR-ALT-05 原文口径）：

1. **本周红色事件数**：``risk_events.severity='red'``、``created_at >= 本周一 00:00``；
2. **平均确认时长**：本周已确认（``acknowledged_at IS NOT NULL``）红色事件的
   ``acknowledged_at - created_at`` 均值（分钟；PRD line 392 升级计时起点＝created_at）；
3. **各楼栋漏采次数**：``work_orders.type='collection_missed'`` 本周计数，按长者当前
   所在床位（``beds.elder_id`` 现住绑定）→ ``rooms.room_no`` 前缀（如 ``A-301`` → ``A``）
   分组；无床位绑定归 ``未分配``。

「本周」按 Asia/Shanghai 自然周（周一 00:00）切分，落库 UTC（AGENTS §8）。
"""

from __future__ import annotations

import datetime
import zoneinfo
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.medication import RiskEvent, WorkOrder
from app.models.organization import Bed, Room

_TZ_SHANGHAI: Final = zoneinfo.ZoneInfo("Asia/Shanghai")

_NO_BUILDING: Final = "未分配"


def week_start_utc(now: datetime.datetime) -> datetime.datetime:
    """本周一 00:00（Asia/Shanghai）对应的 UTC 时刻。"""
    local = now.astimezone(_TZ_SHANGHAI)
    monday = local.date() - datetime.timedelta(days=local.weekday())
    return datetime.datetime(monday.year, monday.month, monday.day, tzinfo=_TZ_SHANGHAI).astimezone(
        datetime.UTC
    )


async def read_weekly_stats(session: AsyncSession, *, now: datetime.datetime) -> dict[str, Any]:
    """FR-ALT-05 三指标聚合（只读；``now`` 由调用方注入，便于测试断言）。"""
    since = week_start_utc(now)

    red_count = (
        await session.execute(
            select(func.count())
            .select_from(RiskEvent)
            .where(RiskEvent.severity == "red", RiskEvent.created_at >= since)
        )
    ).scalar_one()

    avg_ack_seconds = (
        await session.execute(
            select(
                func.avg(func.extract("epoch", RiskEvent.acknowledged_at - RiskEvent.created_at))
            )
            .select_from(RiskEvent)
            .where(
                RiskEvent.severity == "red",
                RiskEvent.created_at >= since,
                RiskEvent.acknowledged_at.is_not(None),
            )
        )
    ).scalar_one()

    # 楼栋 = room_no 前缀（'-' 之前；无 '-' 整串即楼栋）；无床位绑定（split_part(NULL)→NULL）→ 未分配
    building = func.split_part(Room.room_no, "-", 1)
    missed_rows = (
        await session.execute(
            select(building, func.count())
            .select_from(WorkOrder)
            .join(Bed, Bed.elder_id == WorkOrder.elder_id, isouter=True)
            .join(Room, Room.room_id == Bed.room_id, isouter=True)
            .where(
                WorkOrder.type == "collection_missed",
                WorkOrder.created_at >= since,
            )
            .group_by(building)
        )
    ).all()
    missed_by_building = {(b or _NO_BUILDING): int(c) for b, c in missed_rows}

    return {
        "week_start": since.isoformat(),
        "red_events_count": int(red_count),
        "avg_ack_minutes": round(float(avg_ack_seconds) / 60.0, 1)
        if avg_ack_seconds is not None
        else None,
        "missed_by_building": missed_by_building,
    }


__all__ = ["read_weekly_stats", "week_start_utc"]
