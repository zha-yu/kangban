"""Day 8 dev-A：health_service 分级引擎单测。

**纯函数部分**（grade_health）：零 DB 依赖、零 mock 依赖，pytest 直接跑。
**写库部分**（create / review / 黄升红 / 越权）：需要独立测试库，走 conftest.db_engine。

AGENTS §8 硬约束：分级纯函数必须能在不 mock session 的情况下跑单测。
"""

from __future__ import annotations

import pytest

from app.services.health_service import (
    RULE_VERSION,
    SEVERITY_GREEN,
    SEVERITY_RED,
    SEVERITY_YELLOW,
    grade_health,
)

# ======================================================================
# 纯函数分级（无 DB 依赖 · 零 mock · AC-09/10/11/21 全覆盖）
# ======================================================================


class TestGradeHealthPure:
    """grade_health 纯函数分级判定。"""

    # --- AC-21 单分量达红 ---

    def test_red_systolic_only_182(self) -> None:
        """AC-21：收缩压 182，舒张压缺失 → red（单分量即判红，不等另一分量）。"""
        r = grade_health(
            systolic=182, diastolic=None, sleep_hours=7.0, mood_score=4, complaint=None
        )
        assert r.severity == SEVERITY_RED
        assert r.rule_version == RULE_VERSION
        assert r.components[0].severity == SEVERITY_RED  # systolic 分量红
        assert r.components[1].severity == SEVERITY_YELLOW  # diastolic 缺失→黄

    def test_red_diastolic_only_125(self) -> None:
        """AC-21：舒张压 125，收缩压正常 → red。"""
        r = grade_health(systolic=130, diastolic=125, sleep_hours=7.0, mood_score=4, complaint=None)
        assert r.severity == SEVERITY_RED

    def test_red_hypotension_systolic_88(self) -> None:
        """低血压：收缩压 ≤90 → red。"""
        r = grade_health(systolic=88, diastolic=55, sleep_hours=7.0, mood_score=4, complaint=None)
        assert r.severity == SEVERITY_RED

    def test_red_hypotension_diastolic_55(self) -> None:
        """低血压：舒张压 ≤60 → red。"""
        r = grade_health(systolic=120, diastolic=55, sleep_hours=7.0, mood_score=4, complaint=None)
        assert r.severity == SEVERITY_RED

    # --- AC-09 黄档 ---

    def test_yellow_systolic_165_85(self) -> None:
        """AC-09：收缩压 165（140-179）、舒张压正常 → yellow。"""
        r = grade_health(systolic=165, diastolic=85, sleep_hours=7.0, mood_score=4, complaint=None)
        assert r.severity == SEVERITY_YELLOW
        assert r.components[0].severity == SEVERITY_YELLOW

    def test_yellow_diastolic_100(self) -> None:
        """AC-09：舒张压 100（90-119）→ yellow。"""
        r = grade_health(systolic=120, diastolic=100, sleep_hours=7.0, mood_score=4, complaint=None)
        assert r.severity == SEVERITY_YELLOW

    def test_yellow_sleep_4h(self) -> None:
        """AC-09：睡眠 <5h → yellow。"""
        r = grade_health(systolic=120, diastolic=80, sleep_hours=4.0, mood_score=5, complaint=None)
        assert r.severity == SEVERITY_YELLOW

    def test_yellow_mood_score_2(self) -> None:
        """AC-09：mood_score ≤2 → yellow。"""
        r = grade_health(systolic=120, diastolic=80, sleep_hours=7.0, mood_score=2, complaint=None)
        assert r.severity == SEVERITY_YELLOW

    def test_yellow_systolic_165_diastolic_105_two_yellow_components(self) -> None:
        """多个黄分量 → 整体仍黄（黄 < 红，不取最高则漏判）。"""
        r = grade_health(systolic=165, diastolic=105, sleep_hours=7.0, mood_score=4, complaint=None)
        assert r.severity == SEVERITY_YELLOW
        assert all(c.severity != SEVERITY_RED for c in r.components)

    # --- 绿档 ---

    def test_green_normal_120_80(self) -> None:
        """正常值 → green。"""
        r = grade_health(systolic=120, diastolic=80, sleep_hours=7.5, mood_score=5, complaint=None)
        assert r.severity == SEVERITY_GREEN
        assert not r.has_missing

    def test_green_boundary_139_89(self) -> None:
        """绿档边界：systolic=139 / diastolic=89 → green。"""
        r = grade_health(systolic=139, diastolic=89, sleep_hours=5.0, mood_score=3, complaint=None)
        assert r.severity == SEVERITY_GREEN

    # --- 缺失数据 ---

    def test_yellow_missing_systolic(self) -> None:
        """任一字段缺失 → yellow。"""
        r = grade_health(systolic=None, diastolic=80, sleep_hours=7.0, mood_score=4, complaint=None)
        assert r.severity == SEVERITY_YELLOW
        assert r.has_missing is True

    def test_yellow_all_missing(self) -> None:
        """全缺失 → yellow（不降级为绿，不抛异常）。"""
        r = grade_health(
            systolic=None, diastolic=None, sleep_hours=None, mood_score=None, complaint=None
        )
        assert r.severity == SEVERITY_YELLOW
        assert r.has_missing is True

    def test_red_takes_priority_over_missing(self) -> None:
        """红色分量 + 其他缺失 → 仍是 red（红 > 黄优先）。"""
        r = grade_health(
            systolic=185, diastolic=None, sleep_hours=None, mood_score=None, complaint=None
        )
        assert r.severity == SEVERITY_RED
        assert r.has_missing is True

    # --- 无效数据（超范围 / 非数字 → 标缺失）---

    def test_invalid_systolic_350(self) -> None:
        """收缩压 350 超范围 → 标缺失 → yellow。"""
        r = grade_health(systolic=350, diastolic=80, sleep_hours=7.0, mood_score=4, complaint=None)
        assert r.severity == SEVERITY_YELLOW
        assert r.components[0].value is None  # 无效 → 置 None

    def test_invalid_sleep_25h(self) -> None:
        """睡眠 25h 超范围 → 标缺失 → yellow。"""
        r = grade_health(systolic=120, diastolic=80, sleep_hours=25.0, mood_score=4, complaint=None)
        assert r.severity == SEVERITY_YELLOW


# ======================================================================
# DB 依赖（需要独立测试库；session fixture 风格照 test_work_order_service.py）
# ======================================================================

pytest.importorskip("sqlalchemy")  # 兜底：纯函数部分不依赖


from collections.abc import AsyncIterator  # noqa: E402
from typing import Any  # noqa: E402

import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker  # noqa: E402

from app.models.health import HealthRecord  # noqa: E402
from app.models.medication import RiskEvent, WorkOrder  # noqa: E402
from app.models.organization import Elder  # noqa: E402
from app.models.relation import ElderStaffAssignment  # noqa: E402
from app.models.staff import StaffAccount  # noqa: E402
from app.services import health_service  # noqa: E402


@pytest_asyncio.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    try:
        conn = await db_engine.connect()
    except Exception as exc:
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")

    trans = await conn.begin()
    sf = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    async with sf() as sess:
        try:
            yield sess
        finally:
            await sess.rollback()
    await trans.rollback()


@pytest_asyncio.fixture
async def seeded(session: AsyncSession) -> dict[str, Any]:
    """一位长者 + 一位 primary_care 护工（代填场景用）。"""
    elder = Elder(name="张桂芬", status="active")
    worker = StaffAccount(name="李护工", role="care_worker", password_hash="x" * 32)
    session.add_all([elder, worker])
    await session.flush()

    binding = ElderStaffAssignment(
        elder_id=elder.elder_id,
        staff_id=worker.staff_id,
        relation="primary_care",
    )
    session.add(binding)
    await session.flush()

    return {"elder_id": elder.elder_id, "staff_id": worker.staff_id}


# ------------------------------------------------------------------ 写库：黄档建单


async def test_create_yellow_health_record_builds_work_order_and_risk_event(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """AC-09：黄档采集 → risk_events(severity=yellow) + work_order(severity=yellow) 各一条。"""
    ref = await health_service.create_health_record(
        session,
        elder_id=seeded["elder_id"],
        source="voice_active",
        systolic=165,
        diastolic=85,
        sleep_hours=7.0,
        mood_score=4,
        mood_note=None,
        complaint=None,
        operator_id=None,
    )
    await session.flush()

    assert ref.severity == SEVERITY_YELLOW
    assert ref.version == 1
    assert ref.risk_event_id is not None
    assert ref.work_order_id is not None

    # 查 risk_event
    event = (
        await session.execute(select(RiskEvent).where(RiskEvent.event_id == ref.risk_event_id))
    ).scalar_one_or_none()
    assert event is not None
    assert event.severity == "yellow"
    assert event.type == "health_alert"
    assert event.status == "open"

    # 查 work_order
    order = (
        await session.execute(select(WorkOrder).where(WorkOrder.order_id == ref.work_order_id))
    ).scalar_one_or_none()
    assert order is not None
    assert order.severity == "yellow"
    assert order.type == "health_alert"


async def test_create_green_health_record_no_risk_event(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """绿档 → 不建 risk_event / work_order。"""
    ref = await health_service.create_health_record(
        session,
        elder_id=seeded["elder_id"],
        source="voice_active",
        systolic=120,
        diastolic=80,
        sleep_hours=7.0,
        mood_score=5,
        mood_note=None,
        complaint=None,
        operator_id=None,
    )
    await session.flush()

    assert ref.severity == SEVERITY_GREEN
    assert ref.risk_event_id is None
    assert ref.work_order_id is None


async def test_create_red_health_record_risk_event_not_work_order(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """红档 → risk_event 建 red；工单**不建**（红档走 safety_service/dev-C）。"""
    ref = await health_service.create_health_record(
        session,
        elder_id=seeded["elder_id"],
        source="voice_active",
        systolic=185,
        diastolic=90,
        sleep_hours=7.0,
        mood_score=4,
        mood_note=None,
        complaint=None,
        operator_id=None,
    )
    await session.flush()

    assert ref.severity == SEVERITY_RED
    assert ref.risk_event_id is not None
    assert ref.work_order_id is None  # 红档不建工单（AGENTS §2 分层约束）


# ------------------------------------------------------------------ AC-22 黄升红


async def test_yellow_to_red_upgrade_no_second_risk_event(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """AC-22：当日已有黄档 risk_event → 补录红档 → 原事件升级 red、不建第二事件。"""
    # 第一步：建黄档
    first = await health_service.create_health_record(
        session,
        elder_id=seeded["elder_id"],
        source="voice_active",
        systolic=165,
        diastolic=85,
        sleep_hours=7.0,
        mood_score=4,
        mood_note=None,
        complaint=None,
        operator_id=None,
    )
    await session.flush()
    first_event_id = first.risk_event_id
    assert first_event_id is not None

    # 第二步：补录红档
    second = await health_service.create_health_record(
        session,
        elder_id=seeded["elder_id"],
        source="voice_active",
        systolic=185,
        diastolic=90,
        sleep_hours=7.0,
        mood_score=4,
        mood_note=None,
        complaint=None,
        operator_id=None,
    )
    await session.flush()

    assert second.severity == SEVERITY_RED
    assert second.risk_event_id == first_event_id  # 同一个 risk_event，不新建
    assert second.work_order_id is None  # 工单不重复建

    # 验证原事件已升级
    event = (
        await session.execute(select(RiskEvent).where(RiskEvent.event_id == first_event_id))
    ).scalar_one_or_none()
    assert event is not None
    assert event.severity == "red"
    assert event.status == "open"  # 保持 open，不 escalate


# ------------------------------------------------------------------ AC-32 代填越权


async def test_staff_proxy_unauthorized_403(session: AsyncSession, seeded: dict[str, Any]) -> None:
    """AC-32：非负责护工代填 → PermissionError。"""
    # 造一个不是 primary_care 的护工
    other_worker = StaffAccount(name="王护工", role="care_worker", password_hash="x" * 32)
    session.add(other_worker)
    await session.flush()

    with pytest.raises(PermissionError):
        await health_service.create_health_record(
            session,
            elder_id=seeded["elder_id"],
            source="staff_active",
            systolic=120,
            diastolic=80,
            sleep_hours=7.0,
            mood_score=5,
            mood_note=None,
            complaint=None,
            operator_id=other_worker.staff_id,  # 不是 primary_care
        )


async def test_staff_proxy_authorized(session: AsyncSession, seeded: dict[str, Any]) -> None:
    """AC-32：负责护工代填 → 正常通过。"""
    ref = await health_service.create_health_record(
        session,
        elder_id=seeded["elder_id"],
        source="staff_active",
        systolic=120,
        diastolic=80,
        sleep_hours=7.0,
        mood_score=5,
        mood_note=None,
        complaint=None,
        operator_id=seeded["staff_id"],  # primary_care
    )
    assert ref.severity == SEVERITY_GREEN


async def test_staff_proxy_missing_operator_id(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """AC-32：source=staff_* 但 operator_id=None → ValueError。"""
    with pytest.raises(ValueError):
        await health_service.create_health_record(
            session,
            elder_id=seeded["elder_id"],
            source="staff_scheduled",
            systolic=120,
            diastolic=80,
            sleep_hours=7.0,
            mood_score=5,
            mood_note=None,
            complaint=None,
            operator_id=None,
        )


# ------------------------------------------------------------------ 版本链


async def test_review_confirm_does_not_create_new_version(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """confirm → 只改 review_status，不创建新版本。"""
    first = await health_service.create_health_record(
        session,
        elder_id=seeded["elder_id"],
        source="voice_active",
        systolic=120,
        diastolic=80,
        sleep_hours=7.0,
        mood_score=5,
        mood_note=None,
        complaint=None,
        operator_id=None,
    )
    await session.flush()

    reviewed = await health_service.review_health_record(
        session,
        record_id=first.record_id,
        reviewer_id=seeded["staff_id"],
        action="confirm",
        review_note="数据确认无误",
    )
    await session.flush()

    assert reviewed.record_id == first.record_id  # 同一个 record
    assert reviewed.version == 1

    rec = (
        await session.execute(select(HealthRecord).where(HealthRecord.record_id == first.record_id))
    ).scalar_one_or_none()
    assert rec is not None
    assert rec.review_status == "confirmed"
    assert rec.reviewed_by == seeded["staff_id"]
    assert rec.review_note == "数据确认无误"


async def test_review_correct_creates_new_version_chain(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """correct → 新版本 version+1、原记录 superseded_by 指向新记录、superseded_reason='review_correction'。"""
    first = await health_service.create_health_record(
        session,
        elder_id=seeded["elder_id"],
        source="voice_active",
        systolic=165,
        diastolic=85,
        sleep_hours=7.0,
        mood_score=4,
        mood_note=None,
        complaint=None,
        operator_id=None,
    )
    await session.flush()

    corrected = await health_service.review_health_record(
        session,
        record_id=first.record_id,
        reviewer_id=seeded["staff_id"],
        action="correct",
        corrected_values={"systolic": 125, "diastolic": 82},
        review_note="录音解析有误，实际 125/82",
    )
    await session.flush()

    assert corrected.record_id != first.record_id  # 新记录
    assert corrected.version == 2
    assert corrected.severity == SEVERITY_GREEN  # 纠错后分级变绿

    # 原记录标记为被取代
    old = (
        await session.execute(select(HealthRecord).where(HealthRecord.record_id == first.record_id))
    ).scalar_one_or_none()
    assert old is not None
    assert old.review_status == "corrected"
    assert old.superseded_by == corrected.record_id

    # 新记录 superseded_reason = 'review_correction'（纯服务端写入）
    new = (
        await session.execute(
            select(HealthRecord).where(HealthRecord.record_id == corrected.record_id)
        )
    ).scalar_one_or_none()
    assert new is not None
    assert new.superseded_by is None  # 最新版本指向 None
    assert new.superseded_reason == "review_correction"
    assert new.systolic == 125
    assert new.diastolic == 82


async def test_create_health_record_version_defaults_to_1(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """首次采集 → version=1、superseded_by=NULL、superseded_reason=NULL。"""
    ref = await health_service.create_health_record(
        session,
        elder_id=seeded["elder_id"],
        source="voice_active",
        systolic=120,
        diastolic=80,
        sleep_hours=7.0,
        mood_score=5,
        mood_note=None,
        complaint=None,
        operator_id=None,
    )
    await session.flush()

    rec = (
        await session.execute(select(HealthRecord).where(HealthRecord.record_id == ref.record_id))
    ).scalar_one_or_none()
    assert rec is not None
    assert rec.version == 1
    assert rec.superseded_by is None
    assert rec.superseded_reason is None
    assert rec.review_status == "pending"


async def test_rule_version_always_written(session: AsyncSession, seeded: dict[str, Any]) -> None:
    """rule_version 恒写（改阈值必 +1 可追溯）。"""
    ref = await health_service.create_health_record(
        session,
        elder_id=seeded["elder_id"],
        source="voice_active",
        systolic=120,
        diastolic=80,
        sleep_hours=7.0,
        mood_score=5,
        mood_note=None,
        complaint=None,
        operator_id=None,
    )
    await session.flush()

    rec = (
        await session.execute(select(HealthRecord).where(HealthRecord.record_id == ref.record_id))
    ).scalar_one_or_none()
    assert rec is not None
    assert rec.rule_version == RULE_VERSION


__all__ = [
    "TestGradeHealthPure",
    "test_create_green_health_record_no_risk_event",
    "test_create_health_record_version_defaults_to_1",
    "test_create_red_health_record_risk_event_not_work_order",
    "test_create_yellow_health_record_builds_work_order_and_risk_event",
    "test_review_confirm_does_not_create_new_version",
    "test_review_correct_creates_new_version_chain",
    "test_rule_version_always_written",
    "test_staff_proxy_authorized",
    "test_staff_proxy_missing_operator_id",
    "test_staff_proxy_unauthorized_403",
    "test_yellow_to_red_upgrade_no_second_risk_event",
]
