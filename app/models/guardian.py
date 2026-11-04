"""监护人账号表：guardian_accounts（ER §4.1.7 / TD §3.2）。

监护关系不在本表，存 `elder_guardians`（一人可绑多位长者，关系随绑定变化）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CheckConstraint, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class GuardianAccount(Base):
    """监护人账号表（一人一账号）；webpush P0 只落库不真推（裁决 B5）。"""

    __tablename__ = "guardian_accounts"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        {"comment": "监护人账号表"},
    )

    guardian_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text(), nullable=False, comment="姓名")
    phone: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="手机号（脱敏）")
    webpush_subscription: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(), nullable=True, comment="浏览器 Web Push 订阅凭证（P0 只存不真推，裁决 B5）"
    )
    password_hash: Mapped[str] = mapped_column(Text(), nullable=False, comment="密码哈希，只存哈希")
    token_version: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        server_default=text("0"),
        comment="同 staff_accounts（解绑即时失效，AC-24）",
    )
    status: Mapped[str] = mapped_column(
        Text(), nullable=False, server_default=text("'active'"), comment="active/disabled"
    )
