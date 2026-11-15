"""A5 设备掉线检测（Day 10 dev-A · FR-ADM-10）测试。

覆盖任务书 A5 判据四条：
1. 心跳超 2 周期 → status 置 offline 且 notifications 有行
   （收件人＝管理员 + 负责护工；无关护工不收）；重复检测幂等不重复通知。
2. 恢复心跳回 online（仅绑定设备；解绑设备不因心跳回线上，AC-24）。
3. 掉线窗口与用药 T0 重叠 → 立即生成人工介入工单（device_fault/yellow），
   反复检测同一 T0 不多建单。
4. 非 admin/非负责护工：掉线通知按既有权限矩阵隔离
   （列表范围过滤看不到 + mark-read 抛 NotificationForbidden → API 层映射 403）。

session fixture 与 test_notification_service.py 同形（独立测试库 + 事务回滚）。
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

from app.models.medication import MedicationEvent, MedicationPlan
from app.models.notifications import Notification
from app.models.organization import Device, Elder, Room
from app.models.relation import ElderStaffAssignment
from app.models.staff import StaffAccount
from app.services import device_service, notify_service, work_order_service

_PERIOD = 60  # 与 config.device_heartbeat_period_seconds 默认一致
_THRESHOLD = datetime.timedelta(seconds=2 * _PERIOD)  # 2 个周期


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
    """一长者 + 一房 + 管理员 + 负责护工（primary_care）+ 无关护工 + 在线设备。"""
    elder = Elder(name="张桂芬", status="active")
    room = Room(room_no="101")
    admin = StaffAccount(name="王管理", role="admin", password_hash="x" * 32)
    worker = StaffAccount(name="李护工", role="care_worker", password_hash="x" * 32)
    outsider = StaffAccount(name="赵无关", role="care_worker", password_hash="x" * 32)
    session.add_all([elder, room, admin, worker, outsider])
    await session.flush()

    session.add(
        ElderStaffAssignment(
            elder_id=elder.elder_id, staff_id=worker.staff_id, relation="primary_care"
        )
    )
    device_id = uuid.uuid4()
    session.add(
        Device(
            device_id=device_id,
            room_id=room.room_id,
            elder_id=elder.elder_id,
            token_hash="x" * 32,
            status="online",
        )
    )
    await session.flush()
    return {
        "elder_id": elder.elder_id,
        "device_id": device_id,
        "admin_id": admin.staff_id,
        "worker_id": worker.staff_id,
        "outsider_id": outsider.staff_id,
    }


async def _staff_notifications(session: AsyncSession) -> list[Notification]:
    return list(
        (
            await session.execute(
                select(Notification).where(Notification.target_type == "staff")
            )
        )
        .scalars()
        .all()
    )


# ======================================================================
# ① 掉线判定 + 通知（admin + 负责护工）+ 幂等
# ======================================================================


async def test_stale_heartbeat_marks_offline_and_notifies(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """心跳超 2 周期 → offline；通知恰好发给管理员与负责护工；重复检测不重复通知。"""
    now = datetime.datetime.now(datetime.UTC)
    device = await session.get(Device, seeded["device_id"])
    assert device is not None
    device.last_heartbeat = now - _THRESHOLD - datetime.timedelta(seconds=60)

    outcomes = await device_service.detect_offline_devices(session, now=now)
    await session.flush()

    assert len(outcomes) == 1
    assert outcomes[0].device_id == seeded["device_id"]
    assert device.status == "offline"

    notes = await _staff_notifications(session)
    recipients = {n.target_id for n in notes}
    assert recipients == {seeded["admin_id"], seeded["worker_id"]}
    assert all(n.type == "device_offline" for n in notes)
    # 无关护工（无 primary_care 绑定、非 admin）不在收件人之列
    assert seeded["outsider_id"] not in recipients

    # 幂等：同日重新上线又掉线（flap），重复检测不重复通知
    device.status = "online"
    await session.flush()
    outcomes2 = await device_service.detect_offline_devices(session, now=now)
    await session.flush()
    assert len(outcomes2) == 1
    assert len(await _staff_notifications(session)) == 2


async def test_fresh_heartbeat_not_marked_offline(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """心跳新鲜（阈值内）的在线设备不掉线、零通知。"""
    now = datetime.datetime.now(datetime.UTC)
    device = await session.get(Device, seeded["device_id"])
    assert device is not None
    device.last_heartbeat = now - datetime.timedelta(seconds=30)

    outcomes = await device_service.detect_offline_devices(session, now=now)
    await session.flush()

    assert outcomes == []
    assert device.status == "online"
    assert await _staff_notifications(session) == []


# ======================================================================
# ② 恢复心跳回 online
# ======================================================================


async def test_recovered_heartbeat_back_online(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """离线设备重新出现新鲜心跳 → online；解绑设备不回线上（AC-24）；陈旧心跳不恢复。"""
    now = datetime.datetime.now(datetime.UTC)
    device = await session.get(Device, seeded["device_id"])
    assert device is not None
    device.status = "offline"
    device.last_heartbeat = now - datetime.timedelta(seconds=30)
    await session.flush()

    recovered = await device_service.recover_online_devices(session, now=now)
    await session.flush()
    assert recovered == [seeded["device_id"]]
    assert device.status == "online"

    # 负例 1：解绑设备（elder_id=NULL）即使心跳新鲜也不恢复
    device.elder_id = None
    device.status = "offline"
    await session.flush()
    assert await device_service.recover_online_devices(session, now=now) == []
    assert device.status == "offline"

    # 负例 2：心跳陈旧（超 2 周期）不恢复
    device.elder_id = seeded["elder_id"]
    device.last_heartbeat = now - _THRESHOLD - datetime.timedelta(seconds=1)
    await session.flush()
    assert await device_service.recover_online_devices(session, now=now) == []
    assert device.status == "offline"


# ======================================================================
# ③ 掉线窗口与用药 T0 重叠 → 人工介入工单
# ======================================================================


async def _seed_pending_t0(
    session: AsyncSession, seeded: dict[str, Any], *, scheduled_at: datetime.datetime
) -> uuid.UUID:
    doctor = StaffAccount(name="钱医生", role="doctor", password_hash="x" * 32)
    session.add(doctor)
    await session.flush()
    plan = MedicationPlan(
        elder_id=seeded["elder_id"],
        drug_name="硝苯地平",
        dose="10mg 一片",
        schedule_cron="0 8 * * *",
        start_date=scheduled_at.date(),
        end_date=None,
        created_by=doctor.staff_id,
        status="active",
    )
    session.add(plan)
    await session.flush()
    event = MedicationEvent(
        plan_id=plan.plan_id,
        elder_id=seeded["elder_id"],
        scheduled_at=scheduled_at,
        drug_snapshot={"drug_name": "硝苯地平", "dose": "10mg 一片"},
        payload_hash="x" * 32,
        triggered_by="scheduler",
        status="calling",
    )
    session.add(event)
    await session.flush()
    return event.reminder_id


async def test_t0_overlap_opens_device_fault_order(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """掉线窗口覆盖 pending T0 → 立即建 device_fault/yellow 工单；flap 重查不多建。"""
    now = datetime.datetime.now(datetime.UTC)
    device = await session.get(Device, seeded["device_id"])
    assert device is not None
    device.last_heartbeat = now - _THRESHOLD - datetime.timedelta(seconds=30)
    # T0 落在掉线窗口 [last_heartbeat, now] 内、且仍待确认
    await _seed_pending_t0(session, seeded, scheduled_at=now - datetime.timedelta(seconds=30))

    outcomes = await device_service.detect_offline_devices(session, now=now)
    await session.flush()

    assert len(outcomes) == 1
    assert len(outcomes[0].t0_work_order_ids) == 1
    orders = list(
        (
            await session.execute(select(work_order_service.WorkOrder))
        )
        .scalars()
        .all()
    )
    assert len(orders) == 1
    assert orders[0].type == "device_fault"
    assert orders[0].severity == "yellow"
    assert orders[0].elder_id == seeded["elder_id"]

    # flap：重新上线再掉线，同一 pending T0 不多建单
    device.status = "online"
    await session.flush()
    outcomes2 = await device_service.detect_offline_devices(session, now=now)
    await session.flush()
    assert len(outcomes2) == 1
    assert outcomes2[0].t0_work_order_ids == []  # 幂等命中，created=False
    assert len(list((await session.execute(select(work_order_service.WorkOrder))).scalars())) == 1


async def test_no_t0_overlap_no_order(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """掉线窗口内无 pending T0（T0 在窗口外 / 事件已终态）→ 不建单。"""
    now = datetime.datetime.now(datetime.UTC)
    device = await session.get(Device, seeded["device_id"])
    assert device is not None
    device.last_heartbeat = now - _THRESHOLD - datetime.timedelta(seconds=30)
    # T0 在窗口开始之前（上个周期的事），不算重叠
    await _seed_pending_t0(
        session, seeded, scheduled_at=now - _THRESHOLD - datetime.timedelta(minutes=5)
    )

    outcomes = await device_service.detect_offline_devices(session, now=now)
    await session.flush()

    assert len(outcomes) == 1
    assert outcomes[0].t0_work_order_ids == []
    assert list((await session.execute(select(work_order_service.WorkOrder))).scalars()) == []


# ======================================================================
# ④ 既有权限矩阵：无关员工看不到、动不了
# ======================================================================


async def test_offline_notification_permission_matrix(
    session: AsyncSession, seeded: dict[str, Any]
) -> None:
    """非 admin/非负责护工：列表范围过滤 0 行；mark-read 越权抛 NotificationForbidden（API 映射 403）。"""
    now = datetime.datetime.now(datetime.UTC)
    device = await session.get(Device, seeded["device_id"])
    assert device is not None
    device.last_heartbeat = now - _THRESHOLD - datetime.timedelta(seconds=60)
    await device_service.detect_offline_devices(session, now=now)
    await session.flush()

    # 范围过滤：无关护工的收件列表为空（查询条件层面排除，无泄漏）
    rows, total = await notify_service.list_staff_notifications(
        session, staff_id=seeded["outsider_id"], page=1, page_size=50
    )
    assert rows == []
    assert total == 0

    # 越权操作：把管理员收到的掉线通知标记已读 → NotificationForbidden（403）
    admin_note = (
        await session.execute(
            select(Notification).where(Notification.target_id == seeded["admin_id"])
        )
    ).scalar_one()
    with pytest.raises(notify_service.NotificationForbidden):
        await notify_service.mark_staff_notification_read(
            session, staff_id=seeded["outsider_id"], notification_id=admin_note.notification_id
        )
