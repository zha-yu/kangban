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

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.notifications import Notification
from app.models.relation import ElderGuardian

logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 4096

_TYPE_GUARDIAN = "guardian"
_STATUS_GENERATED = "generated"


class NotificationError(Exception):
    """家属通知落库失败基类（失败四分类的上游语义）。"""


class NotificationPayloadError(NotificationError):
    """payload 非法（JSON 对象、可序列化、≤4KB）→ 参数错。"""


class NotificationTargetNotFound(NotificationError):
    """长者没有 active 家属绑定 → 目标不存在，如实告知不编造。"""


class GuardianNotificationNotFound(NotificationError):
    """通知不存在或不属于当前监护人 → 404，不泄露存在性（越权同口径）。"""


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
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
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
