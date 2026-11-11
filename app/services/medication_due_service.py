"""用药到点的**只读查询**层（C2 · Day 7）。

**为什么单独一个文件**：scheduler 层按 AGENTS §2 分层硬约束不允许 import models / 直接写 ORM——
它只「知道有个 service 能告诉我现在该触达哪些药单、有哪些等待中的子图」。本文件是那个 service，
把所有 scheduler 需要的**只读查询**封成几口。

三条硬规矩（写在 docstring 里，不是注释）：

1. **绝对只读**。本文件一行 `session.add/commit/flush/execute(UPDATE/INSERT/DELETE)` 都不允许——
   它只回答事实，不写事实。写事实归 medication_service 和 medication_confirmation_graph。
2. **不 import app.scheduler**。依赖方向单向：scheduler → services → models。本文件
   对 scheduler 的存在零感知。
3. **cron 匹配走 APScheduler 的 CronTrigger**，不引入 croniter / crontab 等新依赖——
   APScheduler 已在 pyproject.toml 且 scheduler 层自己也要用，复用同一套解析。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.medication import MedicationEvent, MedicationPlan

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ 返回值


@dataclass(frozen=True, slots=True)
class DuePlan:
    """一条应该被触发的 active 药单。

    字段覆盖 scheduler 驱动链所需全部数据，调用方拿到就可以建事件、起子图，
    不需要再查 plans 表。
    """

    plan_id: uuid.UUID
    elder_id: uuid.UUID
    scheduled_at: datetime.datetime  # UTC，已钉到当前分钟
    drug_name: str
    dose: str
    cron_expr: str
    version: int
    # 构建子图 start_medication_task 时需要；这里算好免去调用方重复逻辑
    deadline: str  # ISO8601，T0 + 15 分钟（四次触达全部用完到达时点）


@dataclass(frozen=True, slots=True)
class WaitingEvent:
    """一条正在 waiting_confirm 的 medication_event（等待中的子图）。"""

    reminder_id: uuid.UUID
    plan_id: uuid.UUID
    scheduled_at: datetime.datetime  # UTC，T0


# ------------------------------------------------------------------ 查询


async def list_due_plans(
    session: AsyncSession,
    *,
    now: datetime.datetime | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> list[DuePlan]:
    """返回当前时刻所有「应该触发」的 active 药单。

    过滤条件（全部命中才返回）：
    - `medication_plans.status = 'active'`
    - 今天（按 timezone_name）在 `[start_date, end_date]` 范围内
      （end_date 可空 = 永久有效）
    - `schedule_cron` 命中当前分钟（cron 表达式也按同一 timezone_name 解释）
    """
    import zoneinfo  # Python 3.12+ 标准库，不引入 pytz

    tz = zoneinfo.ZoneInfo(timezone_name)
    effective_now = now.astimezone(tz) if now is not None else datetime.datetime.now(tz)

    # 钉到分钟，秒/毫秒归零——cron 匹配不关心子分钟
    rounded = effective_now.replace(second=0, microsecond=0)

    # 查 active 计划。一次性把候选集拿回 Python 侧过滤（cron 匹配在 SQL 里不好写）
    stmt = select(MedicationPlan).where(MedicationPlan.status == "active")
    rows: Iterable[MedicationPlan] = (await session.execute(stmt)).scalars().all()

    # 今天日期（按当前时区），用于 start_date/end_date 覆盖过滤
    today_date = effective_now.date()

    due: list[DuePlan] = []
    for plan in rows:
        # 日期覆盖
        if today_date < plan.start_date:
            continue
        if plan.end_date is not None and today_date > plan.end_date:
            continue

        # cron 匹配——CronTrigger 构造时就指定 timezone，from_crontab 解析 expr
        try:
            trigger = CronTrigger.from_crontab(plan.schedule_cron, timezone=timezone_name)
        except Exception:
            logger.warning(
                "medication_due_invalid_cron plan_id=%s cron=%r",
                plan.plan_id,
                plan.schedule_cron,
            )
            continue

        # get_next_fire_time(previous_fire, now) 看下次是否就是 rounded
        # apscheduler 3.11+ 要求 now 参数（3.10 只有 previous_fire）
        next_fire = trigger.get_next_fire_time(rounded - datetime.timedelta(seconds=1), now=rounded)
        if next_fire is None or next_fire.replace(second=0, microsecond=0) != rounded:
            continue

        scheduled_at_utc = rounded.astimezone(datetime.UTC)
        deadline_iso = (rounded + datetime.timedelta(minutes=15)).isoformat()

        due.append(
            DuePlan(
                plan_id=plan.plan_id,
                elder_id=plan.elder_id,
                scheduled_at=scheduled_at_utc,
                drug_name=plan.drug_name,
                dose=plan.dose,
                cron_expr=plan.schedule_cron,
                version=plan.version,
                deadline=deadline_iso,
            )
        )

    logger.info(
        "medication_due_scanned active_plans=%d due_plans=%d",
        len(due) + 0,  # rows 是迭代器，不显式算总数了
        len(due),
    )
    return due


async def list_waiting_confirm_events(session: AsyncSession) -> list[WaitingEvent]:
    """返回所有 status='waiting_confirm' 且 terminal_state IS NULL 的用药事件。

    scheduler 的 resume tick 用：这些子图正在等待长者回答，需要定时推进到下一次触达或 finalize。
    """
    stmt = select(
        MedicationEvent.reminder_id,
        MedicationEvent.plan_id,
        MedicationEvent.scheduled_at,
    ).where(
        MedicationEvent.status == "waiting_confirm",
        MedicationEvent.terminal_state.is_(None),
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [
        WaitingEvent(
            reminder_id=uuid.UUID(str(r["reminder_id"])),
            plan_id=uuid.UUID(str(r["plan_id"])),
            scheduled_at=r["scheduled_at"],
        )
        for r in rows
    ]


async def get_event_scheduled_at(
    session: AsyncSession, reminder_id: uuid.UUID
) -> datetime.datetime | None:
    """取单条 medication_event 的 T0（scheduled_at）——resume 推进时算下次触达时点需要。"""
    return (
        (
            await session.execute(
                select(MedicationEvent.scheduled_at).where(
                    MedicationEvent.reminder_id == reminder_id
                )
            )
        )
        .scalars()
        .first()
    )


__all__ = [
    "DuePlan",
    "WaitingEvent",
    "get_event_scheduled_at",
    "list_due_plans",
    "list_waiting_confirm_events",
]
