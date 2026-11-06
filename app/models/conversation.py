"""Day 4 · conversation_threads 会话隔离 ORM（闸门①落点）。

列注释逐字对齐 `alembic/versions/0004_day4_voice_threads.py`。
thread_id 由服务端按 (elder_id, session_seq) 生成，前端不可指定——伪造/跨长者访问 403。
generic 模式下 elder_id 为 NULL（FR-VPR-03 约束），CHECK 保证身份与 elder_id 的一致性。
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ConversationThread(Base):
    """会话隔离表：thread_id 闸门①的唯一落点。"""

    __tablename__ = "conversation_threads"
    __table_args__ = (
        CheckConstraint(
            "identity_source IN ('device', 'voice', 'confirm', 'generic')",
            name="identity_source",
        ),
        CheckConstraint(
            "(identity_source = 'generic' AND elder_id IS NULL) "
            "OR (identity_source IN ('device', 'voice', 'confirm') AND elder_id IS NOT NULL)",
            name="identity_source_elder_id",
        ),
        {"comment": "会话隔离表（闸门①：thread_id 服务端生成，elder_id 强制过滤所有下游数据访问）"},
    )

    thread_id: Mapped[str] = mapped_column(
        Text(), primary_key=True, nullable=False,
        comment="会话 ID：服务端按 (elder_id, session_seq) 生成，前端不可指定",
    )
    elder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("elders.elder_id", ondelete="SET NULL"),
        nullable=True,
        comment="识别出的说话人；generic 降级模式为 NULL（FR-VPR-03）",
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.device_id", ondelete="RESTRICT"),
        nullable=False,
        comment="实际使用的设备",
    )
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="会话开始时间",
    )
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="会话结束时间（换人/退出发起新 thread 时回填）",
    )
    identity_source: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="device 设备锁定/voice 声纹/confirm 主动确认/generic 通用模式",
    )
    identity_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=4, scale=3),
        nullable=True,
        comment="声纹置信度（cosine 相似度；generic 模式 NULL）",
    )
