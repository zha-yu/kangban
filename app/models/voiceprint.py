"""Day 4 · voiceprints 声纹注册样本 ORM。

列注释逐字对齐迁移 0004 + 0005（避免 autogenerate 漂移）。
embedding 列由 0005 ADD COLUMN 补入（bytea，Fernet 加密后 bytes，进程内 numpy 解密做 cosine 比对）。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Voiceprint(Base):
    """声纹注册样本表：每位长者 3 段（sample_index 1/2/3），原始音频永不入库。"""

    __tablename__ = "voiceprints"
    __table_args__ = (
        UniqueConstraint("elder_id", "version", "sample_index"),
        CheckConstraint("status IN ('active', 'retired')", name="status"),
        {"comment": "声纹注册样本表（原始音频永不入库，embedding 加密存 bytes）"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id", ondelete="CASCADE"),
        nullable=False,
        comment="所属长者（FK→elders）",
    )
    version: Mapped[int] = mapped_column(
        Integer(), server_default=text("1"), nullable=False, comment="声纹版本号（重训 +1）"
    )
    sample_index: Mapped[int] = mapped_column(
        Integer(), server_default=text("1"), nullable=False, comment="样本序号 1/2/3（3 段注册）"
    )
    embedding: Mapped[bytes] = mapped_column(
        LargeBinary(),
        nullable=False,
        comment="声纹向量（Fernet 加密后 bytes，进程内 numpy 解密 + cosine 比对；CAM++ 原始 192 维）",
    )
    status: Mapped[str] = mapped_column(
        Text(), server_default=text("'active'"), nullable=False, comment="active/retired"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
