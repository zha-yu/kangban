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
    await notify_service.mark_notification_delivered(session, notification_id=note_id)
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
        await notify_service.mark_notification_delivered(session, notification_id=note_id)


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
    await notify_service.mark_notification_delivered(session, notification_id=note_id)
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
    await notify_service.mark_notification_delivered(session, notification_id=note_id)
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
