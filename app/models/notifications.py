"""Day 6 通知表 notifications（ER §4.4.4 / TD §3.5）——0011 迁移的镜像 ORM。

列注释与 `alembic/versions/0011_day6_notifications.py` 逐字一致，避免 autogenerate 漂移。
三条口径：

1. 多态目标：`target_id` / `acknowledged_by` 可能指向 guardian 或 staff 两张表，
   **故意不建 FK**（TD 原文「polymorphic target」）；
2. 幂等：`dedupe_key` 部分唯一 `WHERE dedupe_key IS NOT NULL`（裁决 P1），
   索引必须显式给名 `uq_notifications_dedupe_key`（uq_ 前缀不经 ix 模板）；
3. `type` 是开放枚举（TD 原文以「…」收尾），ER §5.3 未设 CHECK，这里也不加——
   收口枚举须先改 ER/TD 再新迁移。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Notification(Base):
    """通知表：所有外发通知统一落库（无微信）；N27 红色事件护工通知最小写入（Day 6）。"""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_status_created_at", "status", "created_at"),
        Index(
            "ix_notifications_target_type_target_id_status",
            "target_type",
            "target_id",
            "status",
        ),
        Index(
            "uq_notifications_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
        CheckConstraint("target_type IN ('guardian', 'staff')", name="target_type"),
        CheckConstraint("channel IN ('inapp', 'webpush')", name="channel"),
        CheckConstraint(
            "status IN ('generated', 'pending', 'delivered', 'acknowledged', 'failed')",
            name="status",
        ),
        {"comment": "通知表（所有外发通知统一落库；无微信，多态 target 不建 FK）"},
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="通知编号",
    )
    target_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="guardian 监护人 / staff 员工（多态目标，不建 FK）"
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, comment="目标对象 ID（多态目标，不建 FK）"
    )
    type: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="emergency_health/sensitive/missed_medication/report_ready/sos…"
        "（开放枚举，ER §5.3 未设 CHECK）",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, comment="通知内容（脱敏）"
    )
    channel: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="inapp 站内（保底）/ webpush 浏览器推送（无微信、无邮件）",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="generated/pending/delivered/acknowledged/failed（送达≠确认）",
    )
    is_mock: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        server_default=text("false"),
        comment="是否模拟通知（默认 false；不计入送达率分母）",
    )
    retry_count: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        server_default=text("0"),
        comment="重试次数（pending 每 30s 重试，3 次失败 → failed）",
    )
    dedupe_key: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="幂等键（部分唯一：同一键只发一次，空值不冲突，裁决 P1）",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="生成时间（重试计时起点；与 sent_at 区分：生成后可能延迟发送）",
    )
    sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="发送时间"
    )
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="确认人（staff/guardian_id，多态不建 FK；AC-23 三态严格区分）",
    )
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="确认时间"
    )
    read_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="阅读时间"
    )
