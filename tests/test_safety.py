"""Day 6 N27 · 安全一票否决测试（AC-04 / FR-DLG-01 / FR-DLG-07）。

覆盖四组断言（dev-C 计划 C-4 量化验收）：
    1. 「我胸口疼」→ is_sensitive=true（severity=red）→ 三表各一行（红色事件+工单+
       护工通知，notifications.status='generated'、target_type='staff'、
       dedupe_key 前缀 risk:），一票否决语义以 `SafetyDecision.is_sensitive` 为准
       （True 即 escalated_to_agent=false、不进 Agent，AC-04）；
    2. FR-DLG-07 敏感边界四类冒烟：否定/历史/他人/模糊各 1 条，均不触发红色；
    3. 50 例普通语料（tests/fixtures/safety_normal_corpus.py）误报 ≤2；
    4. dedupe：同一红色事件重复 escalate → notifications 不新增第二行。

DB 用例沿用 tests/test_voiceprint_service.py 的事务回滚模式：conftest 的 module 级
共享引擎 + 每用例一连接一事务，服务层只 flush 不 commit（调用方拥有事务），
夹具统一回滚；库不可达按 conftest 口径 skip 并说明原因，不静默通过。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from tests.fixtures.safety_normal_corpus import NORMAL_CORPUS

from app.models import Elder, ElderStaffAssignment, StaffAccount
from app.router.safety import SafetyDecision, safety_check
from app.services.safety_service import EscalationResult, escalate_safety_event

# 占位 UUID：固定值便于断言，均为虚构（AGENTS §4.3，不写真实长者数据）
ELDER_ID = uuid.UUID("22222222-3333-4444-5555-666666666666")
STAFF_ID = uuid.UUID("33333333-4444-5555-6666-777777777777")

#: 触发样本（AC-04 主样本）
HIT_TEXT = "我胸口疼"


# ============================================================
# 夹具（照 test_voiceprint_service.py 模式）
# ============================================================


@pytest.fixture
async def safety_db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """每用例一个事务、结束回滚；连不上库就 skip 并说明原因。"""
    try:
        conn = await db_engine.connect()
    except Exception as exc:  # 任何连接失败都算「无库」
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")

    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


def _borrow_session(conn: AsyncConnection) -> AsyncSession:
    """把夹具那条未提交的事务借给被测代码（safety_service 只 flush 不 commit）。"""
    return AsyncSession(bind=conn, expire_on_commit=False)


async def _seed_elder_with_primary_care(conn: AsyncConnection) -> None:
    """长者 + 责任护工 + 绑定：notifications.target_id 的取数来源。"""
    await conn.execute(Elder.__table__.insert().values(elder_id=ELDER_ID, name="测试长者"))
    await conn.execute(
        StaffAccount.__table__.insert().values(
            staff_id=STAFF_ID, name="测试护工", role="care_worker", password_hash="test-hash"
        )
    )
    await conn.execute(
        ElderStaffAssignment.__table__.insert().values(
            elder_id=ELDER_ID, staff_id=STAFF_ID, relation="primary_care"
        )
    )


async def _count(conn: AsyncConnection, table: str, where: str = "TRUE") -> int:
    row = await conn.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"))  # noqa: S608
    return int(row.scalar_one())


async def _escalate_chest_pain(
    conn: AsyncConnection, *, existing_event_id: uuid.UUID | None = None
) -> EscalationResult:
    decision = safety_check(HIT_TEXT)
    assert decision.is_sensitive and decision.severity == "red"
    return await escalate_safety_event(
        _borrow_session(conn),
        elder_id=ELDER_ID,
        decision=decision,
        existing_event_id=existing_event_id,
    )


# ============================================================
# 1. 纯判定（无 DB）
# ============================================================


def test_chest_pain_triggers_red() -> None:
    """AC-04 主样本：部位×症状双命中判红。"""
    decision: SafetyDecision = safety_check("我胸口疼")
    assert decision.is_sensitive is True
    assert decision.severity == "red"
    assert decision.category == "chest_pain"
    assert decision.matched_rule is not None and "胸口" in decision.matched_rule
    assert decision.reason is None


def test_critical_phrases_trigger_red() -> None:
    """低歧义短语（呼吸困难/跌倒/轻生）无需部位搭配即判红。"""
    for utterance, category in (
        ("我喘不上气了", "breathing"),
        ("我在厕所摔倒了", "fall"),
        ("我不想活了", "self_harm"),
    ):
        decision = safety_check(utterance)
        assert decision.is_sensitive is True, utterance
        assert decision.severity == "red", utterance
        assert decision.category == category, utterance


def test_fr_dlg_07_boundary_four_types_do_not_trigger_red() -> None:
    """FR-DLG-07 边界四类冒烟（否定/历史/他人/模糊）：均不判红。

    历史/模糊按 PRD 判黄复核，黄档归 Day 9 全量规则集；本日最小一票否决只管红色。
    """
    cases: dict[str, str] = {
        "negation": "没有胸痛",
        "recovery": "昨天胸口疼，现在好了",
        "other_person": "我爸爸胸痛得厉害",
        "hedge": "好像胸口疼",
    }
    for expected_reason, utterance in cases.items():
        decision = safety_check(utterance)
        assert decision.is_sensitive is False, (expected_reason, utterance)
        assert decision.reason == expected_reason, (expected_reason, utterance, decision)


def test_normal_corpus_false_positive_budget() -> None:
    """50 例普通语料误报 ≤2（C-0-4 分母；预期 0，护栏样本必须全放过）。"""
    hits: list[str] = [s for s in NORMAL_CORPUS if safety_check(s).is_sensitive]
    assert len(NORMAL_CORPUS) == 50
    assert len(hits) <= 2, f"普通语料误报超预算：{hits}"


def test_safety_check_is_pure_and_fast() -> None:
    """纯函数口径：空串/None 安全、重复调用结果一致（无状态）。"""
    assert safety_check("").is_sensitive is False
    assert safety_check("   ").is_sensitive is False
    first: SafetyDecision = safety_check("我胸口疼")
    second: SafetyDecision = safety_check("我胸口疼")
    assert first == second


# ============================================================
# 2. 落库（DB 用例，事务回滚）
# ============================================================


async def test_red_event_writes_three_tables_and_vetoes_agent(
    safety_db: AsyncConnection,
) -> None:
    """「我胸口疼」→ 三表各一行；is_sensitive=True 即一票否决（不进 Agent）。"""
    await _seed_elder_with_primary_care(safety_db)

    decision = safety_check(HIT_TEXT)
    # AC-04 一票否决语义：命中即短路，escalated_to_agent=false——以 is_sensitive 为准
    assert decision.is_sensitive is True

    result = await _escalate_chest_pain(safety_db)

    assert result.event_created is True
    assert result.order_created is True
    assert result.notification_created is True
    assert await _count(safety_db, "risk_events", f"event_id = '{result.event_id}'") == 1
    assert await _count(safety_db, "work_orders", f"event_id = '{result.event_id}'") == 1
    assert await _count(safety_db, "notifications", f"dedupe_key = 'risk:{result.event_id}'") == 1

    risk = (
        await safety_db.execute(
            text("SELECT type, severity, status, elder_id FROM risk_events WHERE event_id = :e"),
            {"e": result.event_id},
        )
    ).one()
    assert risk.type == "red_emergency"
    assert risk.severity == "red"
    assert risk.status == "open"
    assert risk.elder_id == ELDER_ID

    order = (
        await safety_db.execute(
            text(
                "SELECT order_no, event_id, type, severity, status, assignee_id "
                "FROM work_orders WHERE event_id = :e"
            ),
            {"e": result.event_id},
        )
    ).one()
    assert order.event_id == result.event_id
    assert order.type == "health_alert"
    assert order.severity == "red"
    assert order.status == "new"
    assert order.assignee_id == STAFF_ID
    assert order.order_no.startswith("WO-")

    notification = (
        await safety_db.execute(
            text(
                "SELECT target_type, target_id, type, channel, status, is_mock, "
                "retry_count, dedupe_key FROM notifications WHERE dedupe_key = :k"
            ),
            {"k": f"risk:{result.event_id}"},
        )
    ).one()
    assert notification.target_type == "staff"
    assert notification.target_id == STAFF_ID
    assert notification.type == "emergency_health"
    assert notification.status == "generated"
    assert notification.is_mock is False
    assert notification.retry_count == 0
    assert notification.dedupe_key == f"risk:{result.event_id}"
    assert notification.dedupe_key.startswith("risk:")


async def test_duplicate_notification_is_deduped(safety_db: AsyncConnection) -> None:
    """同一红色事件重复 escalate → notifications 不新增第二行（dedupe 生效）。"""
    await _seed_elder_with_primary_care(safety_db)

    first = await _escalate_chest_pain(safety_db)
    assert first.notification_created is True

    second = await _escalate_chest_pain(safety_db, existing_event_id=first.event_id)
    # 复用事件重入：事件与工单不重建，通知被 dedupe
    assert second.event_id == first.event_id
    assert second.event_created is False
    assert second.order_created is False
    assert second.order_id == first.order_id
    assert second.notification_created is False
    assert second.notification_id == first.notification_id

    assert await _count(safety_db, "risk_events", f"event_id = '{first.event_id}'") == 1
    assert await _count(safety_db, "work_orders", f"event_id = '{first.event_id}'") == 1
    assert await _count(safety_db, "notifications", f"dedupe_key = 'risk:{first.event_id}'") == 1


async def test_missing_primary_care_skips_notification_but_keeps_event(
    safety_db: AsyncConnection,
) -> None:
    """无责任护工：事件/工单照常落库，通知因 target_id NOT NULL 跳过（自裁口径，见报告）。"""
    await safety_db.execute(Elder.__table__.insert().values(elder_id=ELDER_ID, name="测试长者"))

    result = await _escalate_chest_pain(safety_db)

    assert await _count(safety_db, "risk_events", f"event_id = '{result.event_id}'") == 1
    assert await _count(safety_db, "work_orders", f"event_id = '{result.event_id}'") == 1
    assert await _count(safety_db, "notifications", f"dedupe_key = 'risk:{result.event_id}'") == 0
    assert result.notification_created is False
    assert result.notification_id is None
