"""家属（监护人）通知落库服务 —— `send_family_notification` 的唯一写库口。

对应 notifications 表（技术设计文档 §3.3.4，迁移 0011）：
- `notifications` 落库即视为「通知已产生」（Mock 口径，AC-38）；
- 监护人列表从 `elder_guardians(status='active')` 解析，调用方不得指定通知对象；
- 幂等：`dedupe_key` 部分唯一索引兜底（AC-25），逐监护人的键为
  `<dedupe_key>:<guardian_id>`（同一事件不同家属各自只收一条）；
- `is_mock=true` 的行不计入送达率统计分母（FR-RPT-03 口径）。

分层约束：本文件属于 `app/services/**`（唯一写库层）；`mcp-servers/mock-operations`
进程通过本服务落库，自身不写 SQL。
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.notifications import Notification
from app.models.relation import ElderGuardian

logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 4096

_TYPE_GUARDIAN = "guardian"
_TYPE_STAFF = "staff"
_STATUS_GENERATED = "generated"
_STATUS_PENDING = "pending"
_STATUS_DELIVERED = "delivered"
_STATUS_ACKNOWLEDGED = "acknowledged"
_STATUS_FAILED = "failed"

#: pending 重试间隔（秒）：30s 一次，3 次失败 → failed（0011 迁移 retry_count 字段）
RETRY_INTERVAL_S: Final[int] = 30
MAX_RETRIES: Final[int] = 3

#: 红色事件升级窗口（分钟）：2 分钟无人确认 → status=escalated（AC-12）
ESCALATION_WINDOW_MIN: Final[int] = 2


class NotificationError(Exception):
    """家属通知落库失败基类（失败四分类的上游语义）。"""


class NotificationPayloadError(NotificationError):
    """payload 非法（JSON 对象、可序列化、≤4KB）→ 参数错。"""


class NotificationTargetNotFound(NotificationError):
    """长者没有 active 家属绑定 → 目标不存在，如实告知不编造。"""


class GuardianNotificationNotFound(NotificationError):
    """通知不存在或不属于当前监护人 → 404，不泄露存在性（越权同口径）。"""


class NotificationNotFound(NotificationError):
    """通知不存在（按 id 查不到）→ 404。"""


class NotificationIllegalTransition(NotificationError):
    """非法状态迁移（如 generated → delivered 跳过 pending）→ 409。"""


class NotificationForbidden(NotificationError):
    """归属校验失败（acknowler 不是通知 target_id）→ 403。"""


@dataclass(frozen=True, slots=True)
class NotificationRef:
    """一次通知落库的结果引用（created=False 表示幂等复用既有记录）。"""

    notification_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    type: str
    channel: str
    status: str
    is_mock: bool
    created: bool


def validate_payload(payload: object) -> dict:
    """校验通知 payload：必须是 JSON 对象、可序列化且 ≤4KB，原样返回。"""
    if not isinstance(payload, dict):
        raise NotificationPayloadError("payload 必须是 JSON 对象")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        raise NotificationPayloadError("payload 无法序列化为 JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise NotificationPayloadError(f"payload 超过 {MAX_PAYLOAD_BYTES} 字节上限")
    return payload


async def send_guardian_notifications(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    ntype: str,
    payload: dict,
    dedupe_key: str | None = None,
    channel: str | None = None,
    is_mock: bool = True,
) -> list[NotificationRef]:
    """按 active 家属绑定逐人落通知记录，返回每条记录的引用。

    - `dedupe_key` 提供时：逐监护人键 `<dedupe_key>:<guardian_id>`，重复调用
      返回原记录（created=False），不重复落库；
    - `channel` 缺省取 `settings.notify_channel`；
    - 长者无 active 绑定 → `NotificationTargetNotFound`。
    """
    validate_payload(payload)

    guardian_ids: list[uuid.UUID] = list(
        (
            await session.execute(
                select(ElderGuardian.guardian_id).where(
                    ElderGuardian.elder_id == elder_id,
                    ElderGuardian.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    if not guardian_ids:
        raise NotificationTargetNotFound(f"elder {elder_id} 没有可通知的家属绑定")

    resolved_channel = channel or get_settings().notify_channel
    refs: list[NotificationRef] = []

    for guardian_id in guardian_ids:
        key = f"{dedupe_key}:{guardian_id}" if dedupe_key else None
        if key is not None:
            existing = (
                await session.execute(select(Notification).where(Notification.dedupe_key == key))
            ).scalar_one_or_none()
            if existing is not None:
                refs.append(
                    NotificationRef(
                        notification_id=existing.notification_id,
                        target_type=existing.target_type,
                        target_id=existing.target_id,
                        type=existing.type,
                        channel=existing.channel,
                        status=existing.status,
                        is_mock=existing.is_mock,
                        created=False,
                    )
                )
                continue

        notification = Notification(
            target_type=_TYPE_GUARDIAN,
            target_id=guardian_id,
            type=ntype,
            payload=dict(payload),
            channel=resolved_channel,
            status=_STATUS_GENERATED,
            is_mock=is_mock,
            dedupe_key=key,
        )
        session.add(notification)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            # 并发同 key 抢插 → 部分唯一索引兜底，回查按幂等返回
            if key is None:
                raise
            existing = (
                await session.execute(select(Notification).where(Notification.dedupe_key == key))
            ).scalar_one_or_none()
            if existing is None:
                raise
            refs.append(
                NotificationRef(
                    notification_id=existing.notification_id,
                    target_type=existing.target_type,
                    target_id=existing.target_id,
                    type=existing.type,
                    channel=existing.channel,
                    status=existing.status,
                    is_mock=existing.is_mock,
                    created=False,
                )
            )
            continue

        refs.append(
            NotificationRef(
                notification_id=notification.notification_id,
                target_type=_TYPE_GUARDIAN,
                target_id=guardian_id,
                type=ntype,
                channel=resolved_channel,
                status=_STATUS_GENERATED,
                is_mock=is_mock,
                created=True,
            )
        )

    logger.debug(
        "guardian notifications resolved: elder=%s count=%d created=%d",
        elder_id,
        len(refs),
        sum(1 for r in refs if r.created),
    )
    return refs


# ------------------------------------------------------------------ 家属端查询（N1/N4，family 通知中心，2026-11-10 提前件）
#
# 只读 + read_at 单列写：target_id 即登录家属的 guardian_id，天然按人隔离；
# `status` 送达状态机归 Day 8 调度器，本文件不碰。


async def list_guardian_notifications(
    session: AsyncSession,
    *,
    guardian_id: uuid.UUID,
    page: int,
    page_size: int,
    unread_only: bool = False,
) -> tuple[list[Notification], int]:
    """某监护人收到的通知列表（N1：分页 1 起、page_size ≤100、默认不筛已读）。

    范围过滤：`target_type='guardian' AND target_id=guardian_id`——家属只能
    看到**发给自己**的通知，长者维度越权在查询条件里就被排除。
    返回 `(rows, total)`，`total` 为过滤后总数（分页元数据）。
    """
    base = select(Notification).where(
        Notification.target_type == _TYPE_GUARDIAN,
        Notification.target_id == guardian_id,
    )
    if unread_only:
        base = base.where(Notification.read_at.is_(None))
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = list(
        (
            await session.execute(
                base.order_by(Notification.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars()
    )
    return rows, total


async def mark_guardian_notification_read(
    session: AsyncSession,
    *,
    guardian_id: uuid.UUID,
    notification_id: uuid.UUID,
) -> datetime.datetime:
    """标记已读（N4：只写 `read_at`，不动 `status`）。

    幂等：已读直接返回原 `read_at`。找不到或**不属于当前监护人**一律
    抛 `GuardianNotificationNotFound`（越权与不存在同口径，不泄露存在性）。
    """
    notification = (
        await session.execute(
            select(Notification).where(
                Notification.notification_id == notification_id,
                Notification.target_type == _TYPE_GUARDIAN,
                Notification.target_id == guardian_id,
            )
        )
    ).scalar_one_or_none()
    if notification is None:
        raise GuardianNotificationNotFound("通知不存在")
    if notification.read_at is not None:
        return notification.read_at
    notification.read_at = datetime.datetime.now(datetime.UTC)
    await session.flush()
    logger.debug(
        "guardian notification marked read: notification=%s guardian=%s",
        notification_id,
        guardian_id,
    )
    return notification.read_at


# ======================================================================
# Day 8 dev-A 增量：staff 侧 + 状态机五态驱动 + 2 分钟升级扫描
# ======================================================================


# ------------------------------------------------------------------ staff 侧入口


async def send_staff_notifications(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    ntype: str,
    payload: dict,
    dedupe_key: str | None = None,
    channel: str | None = None,
    is_mock: bool = True,
) -> list[NotificationRef]:
    """给负责护工（primary_care relation）发通知；找不到护工返回空列表不报错。

    AC-38 双通知链路：红色事件 → 本函数（护工侧）+ send_guardian_notifications（家属侧）各一条。

    - 幂等：dedupe_key 部分唯一索引兜底，重复调用返回既有记录（created=False）；
    - channel 缺省取 settings.notify_channel；
    - 无 active 绑定 → 返回空列表（与 guardian 侧抛错不同：护工查不到是常态，不应阻断主流程）。
    """
    validate_payload(payload)
    # 循环 import：work_order_service.resolve_primary_care_staff 不反向依赖本文件，安全
    from app.models.relation import ElderStaffAssignment

    staff_ids: list[uuid.UUID] = list(
        (
            await session.execute(
                select(ElderStaffAssignment.staff_id).where(
                    ElderStaffAssignment.elder_id == elder_id,
                    ElderStaffAssignment.relation == "primary_care",
                )
            )
        )
        .scalars()
        .all()
    )
    if not staff_ids:
        logger.info("no primary_care staff for elder %s — staff notification skipped", elder_id)
        return []

    resolved_channel = channel or get_settings().notify_channel
    refs: list[NotificationRef] = []

    for staff_id in staff_ids:
        key = f"{dedupe_key}:{staff_id}" if dedupe_key else None
        if key is not None:
            existing = (
                await session.execute(select(Notification).where(Notification.dedupe_key == key))
            ).scalar_one_or_none()
            if existing is not None:
                refs.append(
                    NotificationRef(
                        notification_id=existing.notification_id,
                        target_type=existing.target_type,
                        target_id=existing.target_id,
                        type=existing.type,
                        channel=existing.channel,
                        status=existing.status,
                        is_mock=existing.is_mock,
                        created=False,
                    )
                )
                continue

        notification = Notification(
            target_type=_TYPE_STAFF,
            target_id=staff_id,
            type=ntype,
            payload=dict(payload),
            channel=resolved_channel,
            status=_STATUS_GENERATED,
            is_mock=is_mock,
            dedupe_key=key,
        )
        session.add(notification)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            if key is None:
                raise
            existing = (
                await session.execute(select(Notification).where(Notification.dedupe_key == key))
            ).scalar_one_or_none()
            if existing is None:
                raise
            refs.append(
                NotificationRef(
                    notification_id=existing.notification_id,
                    target_type=existing.target_type,
                    target_id=existing.target_id,
                    type=existing.type,
                    channel=existing.channel,
                    status=existing.status,
                    is_mock=existing.is_mock,
                    created=False,
                )
            )
            continue

        refs.append(
            NotificationRef(
                notification_id=notification.notification_id,
                target_type=_TYPE_STAFF,
                target_id=staff_id,
                type=ntype,
                channel=resolved_channel,
                status=_STATUS_GENERATED,
                is_mock=is_mock,
                created=True,
            )
        )

    logger.debug(
        "staff notifications resolved: elder=%s count=%d created=%d",
        elder_id,
        len(refs),
        sum(1 for r in refs if r.created),
    )
    return refs


# ------------------------------------------------------------------ 状态机驱动（五态）

# 合法迁移（刻意不含 delivered→generated 这种反向跳）
_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    _STATUS_GENERATED: frozenset({_STATUS_PENDING}),
    _STATUS_PENDING: frozenset({_STATUS_DELIVERED, _STATUS_FAILED}),
    _STATUS_DELIVERED: frozenset({_STATUS_ACKNOWLEDGED}),
    _STATUS_ACKNOWLEDGED: frozenset(),   # 终态
    _STATUS_FAILED: frozenset(),          # 失败终态
}


async def mark_notification_sent(
    session: AsyncSession, *, notification_id: uuid.UUID
) -> None:
    """generated → pending（客户端已收到推送、等回执）。"""
    notification = (
        await session.execute(
            select(Notification).where(Notification.notification_id == notification_id)
        )
    ).scalar_one_or_none()
    if notification is None:
        raise NotificationNotFound(f"notification {notification_id} not found")
    _require_transition(notification.status, _STATUS_PENDING)
    notification.status = _STATUS_PENDING
    notification.sent_at = datetime.datetime.now(datetime.UTC)
    await session.flush()


async def mark_notification_delivered(
    session: AsyncSession, *, notification_id: uuid.UUID
) -> None:
    """pending → delivered（客户端回执，AC-23）。"""
    notification = (
        await session.execute(
            select(Notification).where(Notification.notification_id == notification_id)
        )
    ).scalar_one_or_none()
    if notification is None:
        raise NotificationNotFound(f"notification {notification_id} not found")
    _require_transition(notification.status, _STATUS_DELIVERED)
    notification.status = _STATUS_DELIVERED
    await session.flush()
    logger.info(
        "notification_delivered notification_id=%s", notification_id,
    )


async def acknowledge_notification(
    session: AsyncSession,
    *,
    notification_id: uuid.UUID,
    acknowledger_id: uuid.UUID,
) -> None:
    """delivered → acknowledged（人工确认，AC-23）。

    **归属校验**：acknowledger_id 必须等于 notification.target_id。
    任何持有效工牌的人不能确认别人的通知（R-D10 安全修复）。

    幂等：已确认直接返回，不报错。
    """
    notification = (
        await session.execute(
            select(Notification).where(Notification.notification_id == notification_id)
        )
    ).scalar_one_or_none()
    if notification is None:
        raise NotificationNotFound(f"notification {notification_id} not found")
    if notification.target_id != acknowledger_id:
        raise NotificationForbidden(
            f"acknowledger {acknowledger_id} does not own notification {notification_id} "
            f"(target_type={notification.target_type}, target_id={notification.target_id})"
        )
    if notification.status == _STATUS_ACKNOWLEDGED:
        return  # 幂等
    _require_transition(notification.status, _STATUS_ACKNOWLEDGED)
    notification.status = _STATUS_ACKNOWLEDGED
    notification.acknowledged_by = acknowledger_id
    notification.acknowledged_at = datetime.datetime.now(datetime.UTC)
    await session.flush()
    logger.info(
        "notification_acknowledged notification_id=%s acknowledger=%s",
        notification_id, acknowledger_id,
    )


def _require_transition(current: str, target: str) -> None:
    allowed = _STATUS_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise NotificationIllegalTransition(
            f"illegal notification transition: {current} → {target}"
        )


# ------------------------------------------------------------------ 扫描函数（APScheduler 驱动）


async def scan_pending_notifications(
    session: AsyncSession, *, now: datetime.datetime
) -> list[Notification]:
    """pending 每 30s 重试；retry_count ≥ MAX_RETRIES → failed（失败终态）。

    scheduler 每次调本函数，返回需重试的通知列表由调用方执行实际推送。
    """
    pending = list(
        (
            await session.execute(
                select(Notification).where(
                    Notification.status == _STATUS_PENDING,
                    Notification.retry_count < MAX_RETRIES,
                )
            )
        ).scalars()
    )

    # 超过重试上限 → failed（终态）
    exhausted = list(
        (
            await session.execute(
                select(Notification).where(
                    Notification.status == _STATUS_PENDING,
                    Notification.retry_count >= MAX_RETRIES,
                )
            )
        ).scalars()
    )
    for n in exhausted:
        n.status = _STATUS_FAILED
        logger.warning(
            "notification_failed_exhausted notification_id=%s target_type=%s type=%s retries=%d",
            n.notification_id, n.target_type, n.type, n.retry_count,
        )
    if exhausted:
        await session.flush()

    return pending


async def scan_escalations(
    session: AsyncSession,
    *,
    now: datetime.datetime,
    window_minutes: int = ESCALATION_WINDOW_MIN,
) -> list[uuid.UUID]:
    """扫描 open 且 created_at < now-2min 且无 acknowledged_at 的红色 risk_events。

    返回需升级的 event_id 列表供 scheduler 驱动 escalate_red_event。
    不直接调用 escalate_red_event 避免在扫描里做写操作（由 scheduler 批处理）。
    """
    from app.models.medication import RiskEvent

    cutoff = now - datetime.timedelta(minutes=window_minutes)
    rows = list(
        (
            await session.execute(
                select(RiskEvent).where(
                    RiskEvent.status == "open",
                    RiskEvent.severity == "red",
                    RiskEvent.created_at < cutoff,
                )
            )
        ).scalars()
    )
    return [r.event_id for r in rows]


async def escalate_red_event(
    session: AsyncSession, *, event_id: uuid.UUID
) -> None:
    """红色事件 2 分钟无人确认 → risk_events.status=escalated + 升级通知。

    AC-12：只改原事件 status，不建第二个 risk_event / work_order。
    升级通知是**第三条** escalation 类型通知（dedupe_key=risk_event:<id>:escalated），
    与最初的 staff + guardian 两条不同。
    """
    from app.models.medication import RiskEvent

    event = (
        await session.execute(
            select(RiskEvent).where(RiskEvent.event_id == event_id)
        )
    ).scalar_one_or_none()
    if event is None:
        raise NotificationNotFound(f"risk_event {event_id} not found")
    if event.severity != "red":
        logger.info("escalation skipped: event %s severity=%s", event_id, event.severity)
        return
    if event.status != "open":
        logger.info("escalation skipped: event %s already %s", event_id, event.status)
        return

    event.status = "escalated"
    await session.flush()
    logger.warning(
        "red_event_escalated event_id=%s elder_id=%s source_type=%s source_id=%s",
        event_id, event.elder_id, event.source_type, event.source_id,
    )

    # 升级通知：给负责护工（已生成过 staff 通知，但这是 escalation 专属）
    escalation_key = f"risk_event:{event_id}:escalated"
    escalation_payload = {
        "event_id": str(event_id),
        "severity": "red",
        "reason": "no_acknowledgement_within_2min",
        "escalated_at": now_iso_utc(),
    }
    # staff 侧 escalation 通知
    try:
        await send_staff_notifications(
            session,
            elder_id=event.elder_id,
            ntype="escalated_red_event",
            payload=escalation_payload,
            dedupe_key=escalation_key,
            is_mock=True,
        )
        await session.flush()
    except Exception:
        logger.exception("escalation staff notification failed for event %s", event_id)


def now_iso_utc() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


__all__ = [
    "ESCALATION_WINDOW_MIN",
    "MAX_RETRIES",
    "RETRY_INTERVAL_S",
    "GuardianNotificationNotFound",
    "NotificationError",
    "NotificationForbidden",
    "NotificationIllegalTransition",
    "NotificationNotFound",
    "NotificationPayloadError",
    "NotificationRef",
    "NotificationTargetNotFound",
    "acknowledge_notification",
    "escalate_red_event",
    "list_guardian_notifications",
    "mark_guardian_notification_read",
    "mark_notification_delivered",
    "mark_notification_sent",
    "scan_escalations",
    "scan_pending_notifications",
    "send_guardian_notifications",
    "send_staff_notifications",
    "validate_payload",
]
