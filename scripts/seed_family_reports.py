"""家属端日报/通知联调 mock 数据。

前置（一次性）：

    docker compose up -d postgres redis
    uv run alembic upgrade head
    uv run python scripts/seed_test_data.py

本脚本给指定家属绑定的每位长者生成最近 N 天的：

- ``health_records``（含 1 天红档）与 ``risk_events``（红/黄各 1 起）；
- ``medication_events``（每日 4 条，四类终态各 1，不合并）；
- ``daily_reports``（经 ``report_service.generate_daily_report`` 生成，其中 1 天再生成 v2）；
- 家属通知（report_ready 五种状态循环 + 1 条 emergency_health 演练通知）。

重复执行会先清理窗口内本脚本产生的日报/健康/用药事件/风险事件/通知，再重建。

用法：

    uv run python scripts/seed_family_reports.py
    uv run python scripts/seed_family_reports.py --phone 13900000000 --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import sha256_hex
from app.db import session_factory
from app.models import Elder, ElderGuardian, GuardianAccount, StaffAccount
from app.models.health import HealthRecord
from app.models.medication import MedicationEvent, MedicationPlan, RiskEvent
from app.models.notifications import Notification
from app.models.report import DailyReport
from app.services import health_service, report_service

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed_family_reports")

_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MEAL_HOURS: tuple[int, ...] = (8, 12, 18, 20)
_TERMINAL_STATES: tuple[str, ...] = (
    "taken_reported",
    "refused",
    "missed_unconfirmed",
    "notification_failed",
)
_FAILURE_REASONS: dict[str, str] = {
    "refused": "denied",
    "missed_unconfirmed": "no_answer",
    "notification_failed": "offline",
}
_NOTIFY_STATUSES: tuple[str, ...] = (
    "delivered",
    "acknowledged",
    "pending",
    "failed",
    "generated",
)


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    """上海本地时刻 → UTC（库里统一存 UTC）。"""
    local = datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=_TZ_SHANGHAI)
    return local.astimezone(datetime.UTC)


async def _resolve_scope(
    session: AsyncSession, phone: str
) -> tuple[GuardianAccount, list[tuple[Elder, str]]]:
    guardian = (
        await session.execute(select(GuardianAccount).where(GuardianAccount.phone == phone))
    ).scalar_one_or_none()
    if guardian is None:
        raise SystemExit(f"未找到家属账号 {phone}，请先运行 scripts/seed_test_data.py")
    rows = (
        await session.execute(
            select(Elder, ElderGuardian.relation)
            .join(ElderGuardian, ElderGuardian.elder_id == Elder.elder_id)
            .where(
                ElderGuardian.guardian_id == guardian.guardian_id,
                ElderGuardian.status == "active",
            )
            .order_by(Elder.name.asc())
        )
    ).all()
    if not rows:
        raise SystemExit(f"家属 {phone} 没有 active 绑定长者，请先运行 scripts/seed_test_data.py")
    return guardian, [(row[0], row[1]) for row in rows]


async def _doctor_id(session: AsyncSession) -> uuid.UUID:
    doctor_id = (
        await session.execute(
            select(StaffAccount.staff_id).where(StaffAccount.role == "doctor").limit(1)
        )
    ).scalar_one_or_none()
    if doctor_id is None:
        raise SystemExit("未找到医生账号，请先运行 scripts/seed_test_data.py")
    return doctor_id


async def _ensure_plan(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    doctor_id: uuid.UUID,
    start_day: datetime.date,
) -> MedicationPlan:
    plan = (
        await session.execute(
            select(MedicationPlan)
            .where(
                MedicationPlan.elder_id == elder_id,
                MedicationPlan.drug_name == "苯磺酸氨氯地平片",
                MedicationPlan.status == "active",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if plan is not None:
        return plan
    plan = MedicationPlan(
        elder_id=elder_id,
        drug_name="苯磺酸氨氯地平片",
        dose="5mg 一片",
        schedule_cron="0 8,12,18,20 * * *",
        start_date=start_day,
        created_by=doctor_id,
        status="active",
    )
    session.add(plan)
    await session.flush()
    return plan


async def _reset_window(
    session: AsyncSession,
    *,
    elder_ids: list[uuid.UUID],
    guardian_ids: list[uuid.UUID],
    report_start: datetime.date,
    report_end: datetime.date,
    window: tuple[datetime.datetime, datetime.datetime],
) -> None:
    """清掉窗口内本脚本产出的数据，保证可重复执行。"""
    report_ids = (
        (
            await session.execute(
                select(DailyReport.report_id).where(
                    DailyReport.elder_id.in_(elder_ids),
                    DailyReport.report_date >= report_start,
                    DailyReport.report_date <= report_end,
                )
            )
        )
        .scalars()
        .all()
    )
    report_keys = [
        Notification.dedupe_key.like(f"daily_report:{rid}:update%") for rid in report_ids
    ]
    await session.execute(
        delete(Notification).where(
            Notification.target_type == "guardian",
            Notification.target_id.in_(guardian_ids),
            or_(*report_keys, Notification.dedupe_key.like("demo:%")),
        )
    )
    await session.execute(
        delete(DailyReport).where(
            DailyReport.elder_id.in_(elder_ids),
            DailyReport.report_date >= report_start,
            DailyReport.report_date <= report_end,
        )
    )
    await session.execute(
        delete(RiskEvent).where(
            RiskEvent.elder_id.in_(elder_ids),
            RiskEvent.created_at >= window[0],
            RiskEvent.created_at < window[1],
        )
    )
    await session.execute(
        delete(HealthRecord).where(
            HealthRecord.elder_id.in_(elder_ids),
            HealthRecord.recorded_at >= window[0],
            HealthRecord.recorded_at < window[1],
        )
    )
    await session.execute(
        delete(MedicationEvent).where(
            MedicationEvent.elder_id.in_(elder_ids),
            MedicationEvent.scheduled_at >= window[0],
            MedicationEvent.scheduled_at < window[1],
        )
    )


async def _seed_elder(
    session: AsyncSession,
    *,
    elder: Elder,
    days: list[datetime.date],
    plan: MedicationPlan,
) -> list[report_service.DailyReportRef]:
    """按天造健康/用药/风险事实，再走日报生成入口（不手写日报行）。"""
    refs: list[report_service.DailyReportRef] = []
    red_index = 2  # 倒数第 3 天造红档，保证最近 3 天都能看到关注事件
    regenerate_index = len(days) - 2  # 昨天补录一版，覆盖 F1 去重与版本链

    for index, day in enumerate(days):
        first_event_id: uuid.UUID | None = None
        for slot, hour in enumerate(_MEAL_HOURS):
            state = _TERMINAL_STATES[(index + slot) % len(_TERMINAL_STATES)]
            scheduled_at = _at(day, hour)
            confirmed = state in ("taken_reported", "refused")
            reminder_id = uuid.uuid4()
            if first_event_id is None:
                first_event_id = reminder_id
            session.add(
                MedicationEvent(
                    reminder_id=reminder_id,
                    plan_id=plan.plan_id,
                    elder_id=elder.elder_id,
                    scheduled_at=scheduled_at,
                    plan_version=plan.version,
                    drug_snapshot={"drug_name": plan.drug_name, "dose": plan.dose},
                    payload_hash=sha256_hex(f"{plan.plan_id}:{scheduled_at.isoformat()}"),
                    triggered_by="scheduler",
                    status="confirmed" if confirmed else "escalated",
                    terminal_state=state,
                    late_taken=False,
                    failure_reason=_FAILURE_REASONS.get(state),
                    confirmed_at=(
                        scheduled_at + datetime.timedelta(minutes=5) if confirmed else None
                    ),
                    confirm_channel="voice" if confirmed else None,
                )
            )

        systolic = 118 + (index % 3) * 4
        diastolic = 76 + (index % 2) * 3
        sleep_hours = round(7.5 - (index % 3) * 0.4, 1)
        mood_score = 4 if index % 4 else 3
        complaint: str | None = None
        if index == red_index:
            systolic, diastolic = 182, 108
            complaint = "有点头晕，已建议问医生"

        grade = health_service.grade_health(
            systolic=systolic,
            diastolic=diastolic,
            sleep_hours=sleep_hours,
            mood_score=mood_score,
            complaint=complaint,
        )
        record_id = uuid.uuid4()
        recorded_at = _at(day, 19, 30)
        session.add(
            HealthRecord(
                record_id=record_id,
                elder_id=elder.elder_id,
                recorded_at=recorded_at,
                source="voice_scheduled",
                systolic=systolic,
                diastolic=diastolic,
                sleep_hours=sleep_hours,
                mood_score=mood_score,
                complaint=complaint,
                severity=grade.severity,
                rule_version=grade.rule_version,
                review_status="confirmed",
                version=1,
            )
        )

        if index == red_index:
            session.add(
                RiskEvent(
                    elder_id=elder.elder_id,
                    type="red_emergency",
                    severity="red",
                    source_type="health_record",
                    source_id=record_id,
                    status="closed",
                    created_at=recorded_at,
                    acknowledged_at=recorded_at + datetime.timedelta(minutes=1),
                    closed_at=recorded_at + datetime.timedelta(minutes=3),
                )
            )
        elif index == red_index - 2 and first_event_id is not None:
            session.add(
                RiskEvent(
                    elder_id=elder.elder_id,
                    type="med_risk",
                    severity="yellow",
                    source_type="medication_event",
                    source_id=first_event_id,
                    status="closed",
                    created_at=_at(day, 9),
                    closed_at=_at(day, 10),
                )
            )

        await session.flush()
        refs.append(
            await report_service.generate_daily_report(
                session, elder_id=elder.elder_id, report_date=day
            )
        )
        if index == regenerate_index:
            refs.append(
                await report_service.generate_daily_report(
                    session, elder_id=elder.elder_id, report_date=day, regenerate=True
                )
            )
    return refs


async def _decorate_notifications(
    session: AsyncSession, *, refs: list[report_service.DailyReportRef]
) -> None:
    """让 report_ready 通知覆盖五种状态，供 N1 状态展示联调。"""
    now = datetime.datetime.now(datetime.UTC)
    for index, ref in enumerate(refs):
        status = _NOTIFY_STATUSES[index % len(_NOTIFY_STATUSES)]
        rows = (
            await session.execute(
                select(Notification).where(
                    Notification.dedupe_key.like(f"daily_report:{ref.report_id}:update%")
                )
            )
        ).scalars()
        for row in rows:
            row.is_mock = True
            row.status = status
            if status in ("delivered", "acknowledged", "failed"):
                row.sent_at = now
            if status == "acknowledged":
                row.read_at = now
                row.acknowledged_at = now
                row.acknowledged_by = row.target_id
            if status == "failed":
                row.retry_count = 3


async def _run(phone: str, days_count: int) -> None:
    today = datetime.datetime.now(_TZ_SHANGHAI).date()
    days = [today - datetime.timedelta(days=offset) for offset in range(days_count - 1, -1, -1)]

    async with session_factory() as session:
        guardian, elders = await _resolve_scope(session, phone)
        elder_ids = [elder.elder_id for elder, _relation in elders]
        guardian_ids = list(
            dict.fromkeys(
                (
                    await session.execute(
                        select(ElderGuardian.guardian_id).where(
                            ElderGuardian.elder_id.in_(elder_ids),
                            ElderGuardian.status == "active",
                        )
                    )
                )
                .scalars()
                .all()
            )
        )
        doctor_id = await _doctor_id(session)
        window = (_at(days[0], 0), _at(days[-1] + datetime.timedelta(days=1), 0))

        await _reset_window(
            session,
            elder_ids=elder_ids,
            guardian_ids=guardian_ids,
            report_start=days[0],
            report_end=days[-1],
            window=window,
        )

        all_refs: list[report_service.DailyReportRef] = []
        for elder, _relation in elders:
            plan = await _ensure_plan(
                session, elder_id=elder.elder_id, doctor_id=doctor_id, start_day=days[0]
            )
            refs = await _seed_elder(session, elder=elder, days=days, plan=plan)
            all_refs.extend(refs)
            for guardian_id in guardian_ids:
                session.add(
                    Notification(
                        target_type="guardian",
                        target_id=guardian_id,
                        type="emergency_health",
                        payload={
                            "elder_name": elder.name,
                            "event_brief": "模拟红色事件：长者血压异常，已通知护工处理",
                            "report_id": None,
                        },
                        channel="inapp",
                        status="delivered",
                        is_mock=True,
                        retry_count=0,
                        dedupe_key=f"demo:emergency:{elder.elder_id}:{guardian_id}",
                        sent_at=datetime.datetime.now(datetime.UTC),
                    )
                )

        await _decorate_notifications(session, refs=all_refs)
        await session.commit()

        print()
        print("=" * 64)
        print(f"家属登录：{guardian.phone} / 123456（{guardian.name}）")
        for elder, relation in elders:
            print(f"  长者：{elder.name}（{relation}） elder_id={elder.elder_id}")
        print(f"日报：{len(days)} 天，库内版本 {len(all_refs)} 条（含 1 天 v2，F1 只出最新版）")
        print("通知：report_ready 五状态循环 + emergency_health 演练各 1 条/人")
        print("=" * 64)
        print("自检：POST /api/sessions 登录拿 guardian token，再依次请求")
        print("  GET /api/family/elders")
        print("  GET /api/family/elders/<elder_id>/reports")
        print("  GET /api/family/notifications?page=1&page_size=100")
        print("=" * 64)


def main() -> None:
    parser = argparse.ArgumentParser(description="家属端日报/通知联调 mock 数据")
    parser.add_argument("--phone", default="13900000000", help="家属手机号（默认种子账号）")
    parser.add_argument("--days", type=int, default=7, help="生成最近 N 天（默认 7，至少 3）")
    args = parser.parse_args()
    if args.days < 3:
        parser.error("--days 至少 3（需要留出红档与补录版本）")
    asyncio.run(_run(args.phone, args.days))


if __name__ == "__main__":
    main()
