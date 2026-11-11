"""report_service（C2 · 家属日报）单元测试。

覆盖口径（任务卡 C2 + 计划 S4）：
- AC-13 生成：首版 v1、四类用药统计分列、免责声明拼入 summary_text；
- 幂等：regenerate=False 重复生成返回原版、不重建行、不重发通知（S2 断言：通知数=1）；
- AC-25 版本链：regenerate=True 补录 → version+1 新行、旧版保留、每版通知一次；
- 未答项「未采集」：无健康记录日 summary 声明未采集；无用药记录 total=0；
- dedupe_key 三段式：`daily_report:{report_id}:update`（X2 冻结口径）；
- F1/F2 家属端读侧：绑定范围过滤、每日最大版本去重（S-07）、分页在去重后、
  health_summary/attention_events 查询时组装（S-09）、404/403 分离；
- 红线自检：本服务不读 memory_chunks（grep 断言由任务卡验收命令覆盖）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.guardian import GuardianAccount
from app.models.health import HealthRecord
from app.models.medication import MedicationEvent, MedicationPlan, RiskEvent
from app.models.notifications import Notification
from app.models.organization import Elder
from app.models.relation import ElderGuardian
from app.models.report import DailyReport
from app.models.staff import StaffAccount
from app.services import report_service

_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _drug_snapshot(drug: str = "氨氯地平", dose: str = "5mg 一片") -> dict:
    return {"drug_name": drug, "dose": dose}


def _make_event(
    *,
    elder_id: uuid.UUID,
    plan_id: uuid.UUID,
    scheduled_at: datetime.datetime,
    terminal_state: str | None,
) -> MedicationEvent:
    return MedicationEvent(
        plan_id=plan_id,
        elder_id=elder_id,
        scheduled_at=scheduled_at,
        drug_snapshot=_drug_snapshot(),
        payload_hash="hash-" + terminal_state,
        triggered_by="scheduler",
        terminal_state=terminal_state,
    )


@dataclass(frozen=True)
class ReportWorld:
    elder_id: uuid.UUID
    plan_id: uuid.UUID
    day: datetime.date
    guardian_id: uuid.UUID


@pytest_asyncio.fixture(loop_scope="module")
async def world(db_conn) -> AsyncIterator[ReportWorld]:
    session = AsyncSession(db_conn)
    elder = Elder(name="长者-日报S4", status="active")
    guardian = GuardianAccount(name="家属-日报S4", phone="13800000001", password_hash="x")
    doctor = StaffAccount(name="医生-日报S4", role="doctor", password_hash="x")
    session.add_all([elder, guardian, doctor])
    await session.flush()
    session.add(
        ElderGuardian(
            elder_id=elder.elder_id,
            guardian_id=guardian.guardian_id,
            relation="family",
            status="active",
        )
    )
    plan = MedicationPlan(
        elder_id=elder.elder_id,
        drug_name="氨氯地平",
        dose="5mg 一片",
        schedule_cron="0 8 * * *",
        start_date=datetime.date(2026, 1, 1),
        created_by=doctor.staff_id,
    )
    session.add(plan)
    await session.flush()

    # 当日 4 条用药事件覆盖四类终态（scheduled_at 落在报告日上海时区白天）
    day = datetime.date(2026, 11, 11)
    base = datetime.datetime(day.year, day.month, day.day, 8, 0, tzinfo=_TZ).astimezone(
        datetime.UTC
    )
    offsets = {"taken": 0, "refused": 4, "missed": 8, "failed": 12}
    states = [
        (base + datetime.timedelta(hours=offsets["taken"]), "taken_reported"),
        (base + datetime.timedelta(hours=offsets["refused"]), "refused"),
        (base + datetime.timedelta(hours=offsets["missed"]), "missed_unconfirmed"),
        (base + datetime.timedelta(hours=offsets["failed"]), "notification_failed"),
    ]
    for scheduled_at, state in states:
        session.add(
            _make_event(
                elder_id=elder.elder_id,
                plan_id=plan.plan_id,
                scheduled_at=scheduled_at,
                terminal_state=state,
            )
        )
    # 当日健康采集一条（绿档，已确认）
    session.add(
        HealthRecord(
            elder_id=elder.elder_id,
            recorded_at=base,
            source="staff_scheduled",
            operator_id=None,
            systolic=118,
            diastolic=76,
            sleep_hours=8.0,
            mood_score=4,
            mood_note=None,
            complaint="昨晚睡得不错",
            severity="green",
            rule_version="1",
            review_status="confirmed",
            version=1,
        )
    )
    await session.flush()
    try:
        yield ReportWorld(
            elder_id=elder.elder_id,
            plan_id=plan.plan_id,
            day=day,
            guardian_id=guardian.guardian_id,
        )
    finally:
        await session.close()


async def _notification_count(db_conn, *, prefix: str) -> int:
    session = AsyncSession(db_conn)
    try:
        result = await session.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.dedupe_key.like(f"{prefix}%"))
        )
        return int(result.scalar_one())
    finally:
        await session.close()


async def test_first_version_generates_stats_summary_and_notification(
    world: ReportWorld, db_conn
) -> None:
    """AC-13：首版 v1 + 四类分列 + 免责声明 + 「日报已更新」通知一次。"""
    session = AsyncSession(db_conn)
    ref = await report_service.generate_daily_report(
        session, elder_id=world.elder_id, report_date=world.day
    )
    await session.flush()
    assert ref.created and ref.version == 1 and ref.status == "normal"
    assert ref.notification_created

    report = await session.get(DailyReport, ref.report_id)
    assert report is not None
    stats = report.medication_stats
    assert stats["total"] == 4
    assert stats["taken_reported"] == 1
    assert stats["refused"] == 1
    assert stats["missed_unconfirmed"] == 1
    assert stats["notification_failed"] == 1
    assert "不构成医疗诊断" in report.summary_text
    assert "118" in report.summary_text and "昨晚睡得不错" in report.summary_text
    assert report.pushed_at is not None

    assert await _notification_count(db_conn, prefix=f"daily_report:{ref.report_id}:update") == 1
    await session.rollback()


async def test_regenerate_false_is_idempotent(world: ReportWorld, db_conn) -> None:
    """幂等：重复生成返回原版（不重建行、不重发通知，S2 断言通知数=1）。"""
    session = AsyncSession(db_conn)
    first = await report_service.generate_daily_report(
        session, elder_id=world.elder_id, report_date=world.day
    )
    second = await report_service.generate_daily_report(
        session, elder_id=world.elder_id, report_date=world.day
    )
    assert second.report_id == first.report_id
    assert second.created is False and second.notification_created is False
    assert await _notification_count(db_conn, prefix=f"daily_report:{first.report_id}:update") == 1
    await session.rollback()


async def test_regenerate_creates_new_version_and_keeps_history(
    world: ReportWorld, db_conn
) -> None:
    """AC-25：补录 → v2 新行、v1 保留、每版各通知一次（通知数=2）。"""
    session = AsyncSession(db_conn)
    v1 = await report_service.generate_daily_report(
        session, elder_id=world.elder_id, report_date=world.day
    )
    v2 = await report_service.generate_daily_report(
        session, elder_id=world.elder_id, report_date=world.day, regenerate=True
    )
    assert v2.created and v2.version == 2 and v2.report_id != v1.report_id

    versions = await report_service.list_daily_report_versions(
        session, elder_id=world.elder_id, report_date=world.day
    )
    assert [r.version for r in versions] == [1, 2]
    assert versions[0].report_id == v1.report_id  # 旧版保留

    latest = await report_service.get_daily_report(
        session, elder_id=world.elder_id, report_date=world.day
    )
    assert latest is not None and latest.version == 2

    assert await _notification_count(db_conn, prefix=f"daily_report:{v1.report_id}:update") == 1
    assert await _notification_count(db_conn, prefix=f"daily_report:{v2.report_id}:update") == 1
    await session.rollback()


async def test_empty_day_reports_uncollected(world: ReportWorld, db_conn) -> None:
    """未答项口径：无采集/无用药的日子如实写「未采集」，不编造。"""
    session = AsyncSession(db_conn)
    empty_day = world.day - datetime.timedelta(days=3)
    ref = await report_service.generate_daily_report(
        session, elder_id=world.elder_id, report_date=empty_day
    )
    report = await session.get(DailyReport, ref.report_id)
    assert report is not None
    assert "未采集" in report.summary_text
    assert report.medication_stats is not None
    assert report.medication_stats["total"] == 0
    assert "当日无用药记录" in report.summary_text
    assert "关注事件：无" in report.summary_text
    await session.rollback()


async def test_missing_elder_raises_value_error(world: ReportWorld, db_conn) -> None:
    session = AsyncSession(db_conn)
    with pytest.raises(ValueError, match="not found"):
        await report_service.generate_daily_report(
            session, elder_id=uuid.uuid4(), report_date=world.day
        )
    await session.rollback()


# ---------------- F1/F2 家属端读侧（dev-D D1 依赖，S7 交接补齐）


async def test_list_reports_for_guardian_scope_dedupe_and_paging(
    world: ReportWorld, db_conn
) -> None:
    """F1：绑定范围过滤 + 每日仅最大版本（S-07）+ report_date 倒序 + 分页作用在去重后。"""
    session = AsyncSession(db_conn)
    d1 = world.day - datetime.timedelta(days=1)
    ref_d1 = await report_service.generate_daily_report(
        session, elder_id=world.elder_id, report_date=d1
    )
    await report_service.generate_daily_report(
        session, elder_id=world.elder_id, report_date=world.day
    )
    ref_d2_v2 = await report_service.generate_daily_report(
        session, elder_id=world.elder_id, report_date=world.day, regenerate=True
    )
    # 未绑定长者的日报 → 不出
    other = Elder(name="长者-日报F1未绑", status="active")
    session.add(other)
    await session.flush()
    await report_service.generate_daily_report(
        session, elder_id=other.elder_id, report_date=world.day
    )

    rows, total = await report_service.list_reports_for_guardian(
        session, guardian_id=world.guardian_id
    )
    assert total == 2
    assert [r.report_date for r in rows] == [world.day, d1]  # 倒序
    assert rows[0].report_id == ref_d2_v2.report_id and rows[0].version == 2  # 每日最大版本
    assert rows[1].report_id == ref_d1.report_id and rows[1].version == 1

    # 分页作用在去重后：page_size=1 第 1 页取最新一日、第 2 页取 d1，total 始终=2
    rows_p1, total_p1 = await report_service.list_reports_for_guardian(
        session, guardian_id=world.guardian_id, page=1, page_size=1
    )
    rows_p2, total_p2 = await report_service.list_reports_for_guardian(
        session, guardian_id=world.guardian_id, page=2, page_size=1
    )
    assert total_p1 == total_p2 == 2
    assert [r.report_id for r in rows_p1] == [ref_d2_v2.report_id]
    assert [r.report_id for r in rows_p2] == [ref_d1.report_id]

    # 无绑定家属 → ([], 0)（S-08：200 空列表，不是 403）
    stranger = GuardianAccount(name="家属-F1无绑定", phone="13800000002", password_hash="x")
    session.add(stranger)
    await session.flush()
    assert await report_service.list_reports_for_guardian(
        session, guardian_id=stranger.guardian_id
    ) == ([], 0)
    await session.rollback()


async def test_get_report_detail_for_guardian_assembles_and_gates(
    world: ReportWorld, db_conn
) -> None:
    """F2：health_summary + attention_events 查询时组装（S-09）；404/403 严格分离。"""
    session = AsyncSession(db_conn)
    ref = await report_service.generate_daily_report(
        session, elder_id=world.elder_id, report_date=world.day
    )
    # 当日红档 risk_event（health_record 来源 → brief 取实测血压，不编造）
    recorded_at = datetime.datetime(
        world.day.year, world.day.month, world.day.day, 9, 0, tzinfo=_TZ
    ).astimezone(datetime.UTC)
    record = HealthRecord(
        elder_id=world.elder_id,
        recorded_at=recorded_at,
        source="voice_active",
        operator_id=None,
        systolic=185,
        diastolic=95,
        sleep_hours=None,
        mood_score=None,
        mood_note=None,
        complaint=None,
        severity="red",
        rule_version="1",
        review_status="pending",
        version=1,
    )
    session.add(record)
    await session.flush()
    session.add(
        RiskEvent(
            elder_id=world.elder_id,
            type="red_emergency",
            severity="red",
            source_type="health_record",
            source_id=record.record_id,
            created_at=recorded_at,
        )
    )
    await session.flush()

    report, health_summary, events = await report_service.get_report_detail_for_guardian(
        session, guardian_id=world.guardian_id, report_id=ref.report_id
    )
    assert report.report_id == ref.report_id
    assert "185" in health_summary  # 组装口径=当日窗口最新采集（9:00 红档记录）
    assert len(events) == 1
    assert events[0]["level"] == "red"
    assert events[0]["type"] == "red_emergency"
    assert events[0]["brief"] == "血压 185/95 mmHg"

    # 404：report_id 不存在
    with pytest.raises(report_service.GuardianReportNotFound):
        await report_service.get_report_detail_for_guardian(
            session, guardian_id=world.guardian_id, report_id=uuid.uuid4()
        )
    # 403：存在但长者不在绑定范围（与 404 分离，dev-D 分流 403+审计）
    other = Elder(name="长者-F2未绑", status="active")
    session.add(other)
    await session.flush()
    ref_other = await report_service.generate_daily_report(
        session, elder_id=other.elder_id, report_date=world.day
    )
    with pytest.raises(report_service.GuardianReportForbidden):
        await report_service.get_report_detail_for_guardian(
            session, guardian_id=world.guardian_id, report_id=ref_other.report_id
        )
    await session.rollback()
