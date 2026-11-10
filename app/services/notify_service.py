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

import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
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
