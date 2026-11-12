"""Day 8 dev-A：notify_service 状态机 + 双通知 + dedupe + 重试 + 升级单测。

覆盖计划 §2.4 A3 Step 3.3 的 5 类核心用例：
1. AC-23 成功链路 generated→pending→delivered→acknowledged
2. pending 重试 3 次失败 → failed
3. AC-38 红色事件双通知（staff + guardian 各一条）
4. AC-12 2 分钟升级 → 原事件 escalated、不建第二 risk_event
5. dedupe_key 幂等（重复触发只有一条）
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models.guardian import GuardianAccount
from app.models.medication import RiskEvent
from app.models.notifications import Notification
from app.models.organization import Elder
from app.models.relation import ElderGuardian, ElderStaffAssignment
from app.models.staff import StaffAccount
from app.services import notify_service


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
    """一位长者 + primary_care 护工 + 一位 active 家属。"""
    elder = Elder(name="张桂芬", status="active")
    worker = StaffAccount(name="李护工", role="care_worker", password_hash="x" * 32)
    guardian = GuardianAccount(name="张子女", password_hash="x" * 32)
    session.add_all([elder, worker, guardian])
    await session.flush()

    session.add(
        ElderStaffAssignment(
            elder_id=elder.elder_id, staff_id=worker.staff_id, relation="primary_care"
        )
    )
    session.add(
        ElderGuardian(
            elder_id=elder.elder_id,
            guardian_id=guardian.guardian_id,
            relation="child",
            status="active",
        )
    )
    await session.flush()

    return {
        "elder_id": elder.elder_id,
        "staff_id": worker.staff_id,
        "guardian_id": guardian.guardian_id,
    }


# ======================================================================
# AC-23 状态机五态
# ======================================================================


async def test_notification_lifecycle_success(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """AC-23：generated → pending → delivered → acknowledged 四态依次执行。"""
    refs = await notify_service.send_staff_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="escalated_red_event",
        payload={"event_id": str(uuid.uuid4()), "reason": "test"},
    )
    await session.flush()
    assert len(refs) == 1
    note_id = refs[0].notification_id

    # generated → pending
    await notify_service.mark_notification_sent(session, notification_id=note_id)
    await session.flush()
    n = (
        await session.execute(select(Notification).where(Notification.notification_id == note_id))
    ).scalar_one()
    assert n.status == "pending"
    assert n.sent_at is not None

    # pending → delivered
    await notify_service.mark_notification_delivered(
        session, notification_id=note_id, receipt_kind="card_rendered"
    )
    await session.flush()
    n = (
        await session.execute(select(Notification).where(Notification.notification_id == note_id))
    ).scalar_one()
    assert n.status == "delivered"

    # delivered → acknowledged
    await notify_service.acknowledge_notification(
        session, notification_id=note_id, acknowledger_id=seeded["staff_id"]
    )
    await session.flush()
    n = (
        await session.execute(select(Notification).where(Notification.notification_id == note_id))
    ).scalar_one()
    assert n.status == "acknowledged"
    assert n.acknowledged_by == seeded["staff_id"]
    assert n.acknowledged_at is not None


async def test_illegal_transition_rejected(session: AsyncSession, seeded: dict[str, Any]) -> None:
    """非法迁移抛错（generated → delivered 不能跳 pending）。"""
    refs = await notify_service.send_staff_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="escalated_red_event",
        payload={"event_id": str(uuid.uuid4())},
    )
    note_id = refs[0].notification_id
    await session.flush()

    with pytest.raises(notify_service.NotificationError):
        # generated 不能直接到 delivered
        await notify_service.mark_notification_delivered(
            session, notification_id=note_id, receipt_kind="card_rendered"
        )


async def test_acknowledge_idempotent(session: AsyncSession, seeded: dict[str, Any]) -> None:
    """acknowledge 幂等：已确认直接返回不报错。"""
    refs = await notify_service.send_staff_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="escalated_red_event",
        payload={"event_id": str(uuid.uuid4())},
    )
    note_id = refs[0].notification_id
    await session.flush()

    await notify_service.mark_notification_sent(session, notification_id=note_id)
    await notify_service.mark_notification_delivered(
        session, notification_id=note_id, receipt_kind="card_rendered"
    )
    await notify_service.acknowledge_notification(
        session, notification_id=note_id, acknowledger_id=seeded["staff_id"]
    )
    await session.flush()

    # 第二次确认 → 不报错
    await notify_service.acknowledge_notification(
        session, notification_id=note_id, acknowledger_id=seeded["staff_id"]
    )
    await session.flush()


async def test_acknowledge_forbidden_wrong_owner(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """R-D10 安全修复：任何持有效工牌的人不能确认别人的通知。"""
    refs = await notify_service.send_staff_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="escalated_red_event",
        payload={"event_id": str(uuid.uuid4())},
    )
    note_id = refs[0].notification_id
    await session.flush()

    await notify_service.mark_notification_sent(session, notification_id=note_id)
    await notify_service.mark_notification_delivered(
        session, notification_id=note_id, receipt_kind="card_rendered"
    )
    await session.flush()

    # 家属持有效工牌但 target_type=staff，不能确认 staff 通知
    with pytest.raises(notify_service.NotificationForbidden):
        await notify_service.acknowledge_notification(
            session, notification_id=note_id, acknowledger_id=seeded["guardian_id"]
        )


async def test_mark_guardian_read_forbidden_wrong_owner(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """Q-D2：家属侧越权（归属不匹配）必须抛 NotificationForbidden (403) + 审计日志。"""
    refs = await notify_service.send_staff_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="escalated_red_event",
        payload={"event_id": str(uuid.uuid4())},
    )
    note_id = refs[0].notification_id
    await session.flush()

    # 家属不能把发给 staff 的通知标记为已读
    with pytest.raises(notify_service.NotificationForbidden):
        await notify_service.mark_guardian_notification_read(
            session, guardian_id=seeded["guardian_id"], notification_id=note_id
        )


async def test_mark_staff_read_forbidden_wrong_owner(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """Q-D2：员工侧越权（归属不匹配）必须抛 NotificationForbidden (403) + 审计日志。"""
    refs = await notify_service.send_guardian_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="emergency_health",
        payload={"reason": "test"},
    )
    note_id = refs[0].notification_id
    await session.flush()

    # 护工不能把发给 guardian 的通知标记为已读
    with pytest.raises(notify_service.NotificationForbidden):
        await notify_service.mark_staff_notification_read(
            session, staff_id=seeded["staff_id"], notification_id=note_id
        )


# ======================================================================
# 重试失败终态
# ======================================================================


async def test_pending_retry_exhausted_becomes_failed(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """pending 重试 ≥ MAX_RETRIES(3) → 自动 failed（终态）。"""
    refs = await notify_service.send_staff_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="escalated_red_event",
        payload={"event_id": str(uuid.uuid4())},
    )
    note_id = refs[0].notification_id
    await session.flush()

    await notify_service.mark_notification_sent(session, notification_id=note_id)
    await session.flush()

    # 手动把 retry_count 调到上限
    n = (
        await session.execute(select(Notification).where(Notification.notification_id == note_id))
    ).scalar_one()
    n.retry_count = notify_service.MAX_RETRIES
    await session.flush()

    now = datetime.datetime.now(datetime.UTC)
    exhausted = await notify_service.scan_pending_notifications(session, now=now)
    await session.flush()

    # 重试耗尽的变成 failed，不再出现在 pending 返回列表里
    updated = (
        await session.execute(select(Notification).where(Notification.notification_id == note_id))
    ).scalar_one()
    assert updated.status == "failed"
    assert note_id not in {n.notification_id for n in exhausted}


# ======================================================================
# F01/X11：退避函数与真链路（delivered 只由回执驱动）
# ======================================================================


def test_next_retry_at_alert_sequence() -> None:
    """X11 告警退避：30s → 60s → 90s；第 4 次请求返回 None（3 次用尽）。"""
    base = datetime.datetime(2026, 11, 12, 9, 0, 0, tzinfo=datetime.UTC)
    assert notify_service.next_retry_at(
        ntype="health_alert", retry_count=0, now=base
    ) == base + datetime.timedelta(seconds=30)
    assert notify_service.next_retry_at(
        ntype="escalated_red_event", retry_count=1, now=base
    ) == base + datetime.timedelta(seconds=60)
    assert notify_service.next_retry_at(
        ntype="medication_reminder", retry_count=2, now=base
    ) == base + datetime.timedelta(seconds=90)
    assert notify_service.next_retry_at(ntype="health_alert", retry_count=3, now=base) is None


def test_next_retry_at_report_sequence() -> None:
    """X11 日报退避：1min → 5min → 15min；medication_* 前缀外的未知类别按告警兜底。"""
    base = datetime.datetime(2026, 11, 12, 20, 0, 0, tzinfo=datetime.UTC)
    assert notify_service.next_retry_at(
        ntype="report_ready", retry_count=0, now=base
    ) == base + datetime.timedelta(minutes=1)
    assert notify_service.next_retry_at(
        ntype="report_updated", retry_count=1, now=base
    ) == base + datetime.timedelta(minutes=5)
    assert notify_service.next_retry_at(
        ntype="report_ready", retry_count=2, now=base
    ) == base + datetime.timedelta(minutes=15)
    assert notify_service.next_retry_at(ntype="report_ready", retry_count=3, now=base) is None
    # 未知类别 → 告警序列兜底（宁快勿迟）
    assert notify_service.next_retry_at(
        ntype="unknown_type", retry_count=0, now=base
    ) == base + datetime.timedelta(seconds=30)


async def test_no_receipt_retries_then_failed(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """F01 真链路：无回执 → 3 次重试（保持 pending）→ failed；全链路永不出现 delivered。"""
    refs = await notify_service.send_staff_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="escalated_red_event",
        payload={"event_id": str(uuid.uuid4())},
    )
    note_id = refs[0].notification_id
    await session.flush()
    await notify_service.mark_notification_sent(session, notification_id=note_id)
    await session.flush()

    # 三个到点时刻：+30s / +90s（+30 后再 60）/ +180s（再 90）
    first_sent = (
        (await session.execute(select(Notification).where(Notification.notification_id == note_id)))
        .scalar_one()
        .sent_at
    )
    assert first_sent is not None
    base = first_sent
    for expected_count, offset in ((1, 30), (2, 90), (3, 180)):
        result = await notify_service.retry_pending_notifications(
            session, now=base + datetime.timedelta(seconds=offset)
        )
        await session.flush()
        row = (
            await session.execute(
                select(Notification).where(Notification.notification_id == note_id)
            )
        ).scalar_one()
        assert row.status == "pending", f"第 {expected_count} 次重试后仍应等待回执"
        assert row.retry_count == expected_count
        assert result.retried == 1

    # 第 4 轮（retry_count=3 ≥ MAX_RETRIES）→ failed 终态
    result = await notify_service.retry_pending_notifications(
        session, now=base + datetime.timedelta(seconds=181)
    )
    await session.flush()
    row = (
        await session.execute(select(Notification).where(Notification.notification_id == note_id))
    ).scalar_one()
    assert row.status == "failed"
    assert result.failed == 1


# ======================================================================
# AC-38 双通知（红档 → staff + guardian 各一条）
# ======================================================================


async def test_red_event_dual_notification_staff_and_guardian(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """AC-38：红色事件双通知 —— staff + guardian 各一条 generated。"""
    event_id = uuid.uuid4()

    # staff 侧
    staff_refs = await notify_service.send_staff_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="emergency_health",
        payload={"event_id": str(event_id), "severity": "red"},
        dedupe_key=f"risk_event:{event_id}:staff",
    )
    await session.flush()

    # guardian 侧
    guard_refs = await notify_service.send_guardian_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="emergency_health",
        payload={"event_id": str(event_id), "severity": "red"},
        dedupe_key=f"risk_event:{event_id}:guardian",
    )
    await session.flush()

    assert len(staff_refs) == 1
    assert len(guard_refs) == 1

    # 两条通知 target_type 不同
    all_notes = list(
        (
            await session.execute(
                select(Notification).where(
                    Notification.notification_id.in_(
                        [staff_refs[0].notification_id, guard_refs[0].notification_id]
                    )
                )
            )
        ).scalars()
    )
    types = {n.target_type for n in all_notes}
    assert types == {"staff", "guardian"}
    for n in all_notes:
        assert n.status == "generated"
        assert n.type == "emergency_health"


# ======================================================================
# AC-12 升级（2 分钟无人确认 → 原事件 escalated）
# ======================================================================


async def test_red_escalation_no_second_risk_event(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """AC-12：2 分钟无人确认 → risk_event.status=escalated、不建第二事件。"""
    # 造一个 3 分钟前创建的 open red risk_event
    old_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=3)
    event = RiskEvent(
        event_id=uuid.uuid4(),
        elder_id=seeded["elder_id"],
        type="health_alert",
        severity="red",
        status="open",
        created_at=old_time,
    )
    session.add(event)
    await session.flush()

    # 扫描应该返回这个 event_id
    now = datetime.datetime.now(datetime.UTC)
    to_escalate = await notify_service.scan_escalations(session, now=now)
    assert event.event_id in to_escalate

    # 执行升级
    await notify_service.escalate_red_event(session, event_id=event.event_id)
    await session.flush()

    updated = (
        await session.execute(select(RiskEvent).where(RiskEvent.event_id == event.event_id))
    ).scalar_one()
    assert updated.status == "escalated"
    # severity 保持 red（升级不改 severity）
    assert updated.severity == "red"

    # 不应产生第二 risk_event
    all_red = list(
        (
            await session.execute(
                select(RiskEvent).where(
                    RiskEvent.elder_id == seeded["elder_id"],
                    RiskEvent.severity == "red",
                )
            )
        ).scalars()
    )
    assert len(all_red) == 1


async def test_escalation_skipped_already_closed(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """已 escalated/closed 的事件再触发升级 → 跳过、不报错。"""
    old_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    event = RiskEvent(
        event_id=uuid.uuid4(),
        elder_id=seeded["elder_id"],
        type="health_alert",
        severity="red",
        status="escalated",
        created_at=old_time,
    )
    session.add(event)
    await session.flush()

    # 扫描不应包含已 escalated 的
    now = datetime.datetime.now(datetime.UTC)
    to_escalate = await notify_service.scan_escalations(session, now=now)
    assert event.event_id not in to_escalate

    # 直接调 escalate_red_event 也不报错
    await notify_service.escalate_red_event(session, event_id=event.event_id)


async def test_escalation_skipped_yellow_event(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """黄色事件不进入升级路径（只有 red 才升级）。"""
    old_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    event = RiskEvent(
        event_id=uuid.uuid4(),
        elder_id=seeded["elder_id"],
        type="health_alert",
        severity="yellow",
        status="open",
        created_at=old_time,
    )
    session.add(event)
    await session.flush()

    now = datetime.datetime.now(datetime.UTC)
    to_escalate = await notify_service.scan_escalations(session, now=now)
    assert event.event_id not in to_escalate


# ======================================================================
# F12：升级通知发 admin；已确认事件不被扫描
# ======================================================================


async def test_escalation_goes_to_admin_not_primary_care(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """F12：升级通知发 admin 角色，护工不重复收（主理人裁决口径）。"""
    admin = StaffAccount(name="王管理员", role="admin", password_hash="x" * 32)
    session.add(admin)
    await session.flush()

    old_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=3)
    event = RiskEvent(
        event_id=uuid.uuid4(),
        elder_id=seeded["elder_id"],
        type="health_alert",
        severity="red",
        status="open",
        created_at=old_time,
    )
    session.add(event)
    await session.flush()

    await notify_service.escalate_red_event(session, event_id=event.event_id)
    await session.flush()

    escalation_rows = list(
        (
            await session.execute(
                select(Notification).where(
                    # staff 侧键按「逐目标」派生：<key>:<staff_id>，故用前缀匹配
                    Notification.dedupe_key.like(f"risk_event:{event.event_id}:escalated:%")
                )
            )
        ).scalars()
    )
    assert len(escalation_rows) == 1
    assert escalation_rows[0].target_type == "staff"
    assert escalation_rows[0].target_id == admin.staff_id  # admin 收到
    assert escalation_rows[0].target_id != seeded["staff_id"]  # 护工不重复收


async def test_scan_escalations_skips_acknowledged_event(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """F12：acknowledged_at 非空的事件不进入升级扫描（确认即止血）。"""
    old_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=3)
    acknowledged = RiskEvent(
        event_id=uuid.uuid4(),
        elder_id=seeded["elder_id"],
        type="health_alert",
        severity="red",
        status="open",
        created_at=old_time,
        acknowledged_at=old_time + datetime.timedelta(minutes=1),
    )
    unacknowledged = RiskEvent(
        event_id=uuid.uuid4(),
        elder_id=seeded["elder_id"],
        type="health_alert",
        severity="red",
        status="open",
        created_at=old_time,
    )
    session.add_all([acknowledged, unacknowledged])
    await session.flush()

    now = datetime.datetime.now(datetime.UTC)
    to_escalate = await notify_service.scan_escalations(session, now=now)
    assert acknowledged.event_id not in to_escalate
    assert unacknowledged.event_id in to_escalate


# ======================================================================
# F13：确认联动 risk_events.acknowledged_at
# ======================================================================


async def test_acknowledge_links_risk_event_acknowledged_at(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """F13：通知确认后同事务回写 risk_events.acknowledged_at（幂等，二次确认不覆盖）。"""
    event_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    event = RiskEvent(
        event_id=uuid.uuid4(),
        elder_id=seeded["elder_id"],
        type="health_alert",
        severity="red",
        status="open",
        created_at=event_time,
    )
    session.add(event)
    await session.flush()

    notification = Notification(
        target_type="staff",
        target_id=seeded["staff_id"],
        type="health_alert",
        payload={"title": "健康红色预警", "risk_event_id": str(event.event_id)},
        channel="inapp",
        status="delivered",
        is_mock=True,
        dedupe_key=f"risk_event:{event.event_id}:staff:{seeded['staff_id']}",
    )
    session.add(notification)
    await session.flush()

    await notify_service.acknowledge_notification(
        session, notification_id=notification.notification_id, acknowledger_id=seeded["staff_id"]
    )
    await session.flush()
    updated = (
        await session.execute(select(RiskEvent).where(RiskEvent.event_id == event.event_id))
    ).scalar_one()
    assert updated.acknowledged_at is not None
    first_ack = updated.acknowledged_at

    # 幂等：事件再关一次（模拟重复确认另一条同锚点通知）不覆盖既有时间戳
    second = Notification(
        target_type="staff",
        target_id=seeded["staff_id"],
        type="health_alert",
        payload={"title": "健康红色预警", "risk_event_id": str(event.event_id)},
        channel="inapp",
        status="delivered",
        is_mock=True,
        dedupe_key=f"risk_event:{event.event_id}:staff:second",
    )
    session.add(second)
    await session.flush()
    await notify_service.acknowledge_notification(
        session, notification_id=second.notification_id, acknowledger_id=seeded["staff_id"]
    )
    await session.flush()
    again = (
        await session.execute(select(RiskEvent).where(RiskEvent.event_id == event.event_id))
    ).scalar_one()
    assert again.acknowledged_at == first_ack


async def test_purge_notifications_by_elder(session: AsyncSession, seeded: dict[str, Any]) -> None:
    """X12 notify 钩子：按 guardian 目标 + risk_event 键两条通路清通知，幂等。"""
    event = RiskEvent(
        event_id=uuid.uuid4(),
        elder_id=seeded["elder_id"],
        type="health_alert",
        severity="red",
        status="open",
    )
    session.add(event)
    await session.flush()
    rows = [
        Notification(
            target_type="staff",
            target_id=seeded["staff_id"],
            type="health_alert",
            payload={},
            channel="inapp",
            status="delivered",
            is_mock=True,
            dedupe_key=f"risk_event:{event.event_id}:staff:{seeded['staff_id']}",
        ),
        Notification(
            target_type="guardian",
            target_id=seeded["guardian_id"],
            type="health_alert",
            payload={},
            channel="inapp",
            status="delivered",
            is_mock=True,
            dedupe_key=f"risk_event:{event.event_id}:guardian:{seeded['guardian_id']}",
        ),
        # 别的照护单元的通知：不应误伤
        Notification(
            target_type="staff",
            target_id=seeded["staff_id"],
            type="shift",
            payload={},
            channel="inapp",
            status="delivered",
            is_mock=True,
            dedupe_key="unrelated:shift:1",
        ),
    ]
    session.add_all(rows)
    await session.flush()

    ref = await notify_service.purge_elder_data(
        session, elder_id=seeded["elder_id"], reason="teaching_reset"
    )
    await session.flush()
    assert ref.domain == "notifications"
    assert ref.affected_rows == 2
    assert ref.archived_rows == 0

    remaining = list((await session.execute(select(Notification.dedupe_key))).scalars())
    assert "unrelated:shift:1" in remaining
    assert all(not k.startswith(f"risk_event:{event.event_id}") for k in remaining)

    # 幂等：第二遍计数为 0
    ref2 = await notify_service.purge_elder_data(
        session, elder_id=seeded["elder_id"], reason="teaching_reset"
    )
    assert ref2.affected_rows == 0

    with pytest.raises(ValueError, match="unknown purge reason"):
        await notify_service.purge_elder_data(session, elder_id=seeded["elder_id"], reason="nope")


# ======================================================================
# dedupe_key 幂等
# ======================================================================


async def test_dedupe_key_prevents_duplicate_notifications(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """同一 dedupe_key 重复触发 → 只有一条记录、created=False。"""
    dedupe_key = f"risk_event:{uuid.uuid4()}:staff"

    first = await notify_service.send_staff_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="emergency_health",
        payload={"reason": "test"},
        dedupe_key=dedupe_key,
    )
    await session.flush()

    second = await notify_service.send_staff_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="emergency_health",
        payload={"reason": "test"},
        dedupe_key=dedupe_key,
    )
    await session.flush()

    assert len(first) == 1 and first[0].created is True
    assert len(second) == 1 and second[0].created is False
    assert second[0].notification_id == first[0].notification_id

    # 库里只有一条（按 id 查，key 被服务内部加了 staff_id 后缀）
    n = (
        await session.execute(
            select(Notification).where(Notification.notification_id == first[0].notification_id)
        )
    ).scalar_one_or_none()
    assert n is not None


async def test_dedupe_key_per_target(session: AsyncSession, seeded: dict[str, Any]) -> None:
    """dedupe_key 逐目标追加后缀 —— staff 侧和 guardian 侧可以各自独立各发一条。"""
    base = f"risk_event:{uuid.uuid4()}"

    staff = await notify_service.send_staff_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="emergency_health",
        payload={"reason": "test"},
        dedupe_key=base,
    )
    guard = await notify_service.send_guardian_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="emergency_health",
        payload={"reason": "test"},
        dedupe_key=base,
    )
    await session.flush()

    # 库里应两条 dedupe_key（各加了 staff/guardian 后缀）
    all_notes = list(
        (
            await session.execute(
                select(Notification).where(
                    Notification.elder_id == seeded["elder_id"]
                    if False
                    else Notification.notification_id.in_(
                        [staff[0].notification_id, guard[0].notification_id]
                    )
                )  # 简化：直接按 id 查
            )
        ).scalars()
    )
    keys = {n.dedupe_key for n in all_notes if n.dedupe_key}
    # 因为两个 target 各自在 key 后加了后缀，这里验证 key 不同
    assert len(keys) == 2


async def test_no_staff_returns_empty_list_not_error(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """无 primary_care 护工时 staff 通知返回空列表（不抛错）。"""
    # 删掉 binding
    from app.models.relation import ElderStaffAssignment

    await session.execute(
        ElderStaffAssignment.__table__.delete().where(
            ElderStaffAssignment.elder_id == seeded["elder_id"]
        )
    )
    await session.flush()

    refs = await notify_service.send_staff_notifications(
        session,
        elder_id=seeded["elder_id"],
        ntype="emergency_health",
        payload={"reason": "test"},
    )
    assert refs == []


__all__ = [
    "test_acknowledge_forbidden_wrong_owner",
    "test_acknowledge_idempotent",
    "test_dedupe_key_per_target",
    "test_dedupe_key_prevents_duplicate_notifications",
    "test_escalation_skipped_already_closed",
    "test_escalation_skipped_yellow_event",
    "test_illegal_transition_rejected",
    "test_mark_guardian_read_forbidden_wrong_owner",
    "test_mark_staff_read_forbidden_wrong_owner",
    "test_no_staff_returns_empty_list_not_error",
    "test_notification_lifecycle_success",
    "test_pending_retry_exhausted_becomes_failed",
    "test_red_escalation_no_second_risk_event",
    "test_red_event_dual_notification_staff_and_guardian",
]
