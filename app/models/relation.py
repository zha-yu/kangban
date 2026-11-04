"""关系绑定表：elder_staff_assignments 与 elder_guardians（ER §4.2.1–§4.2.2 / TD §3.3）。

唯一/查询索引以 ER §5.1/§5.2 为准；「每位长者 active 监护人 ≤2」与
「每位长者 ≥1 责任护工」是服务层校验（裁决 C4），不落 DDL。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ElderStaffAssignment(Base):
    """长者-员工绑定表（护工/医生统一）；范围过滤的唯一依据。"""

    __tablename__ = "elder_staff_assignments"
    __table_args__ = (
        UniqueConstraint("elder_id", "staff_id", "relation"),  # → uq_..._elder_id_staff_id_relation
        CheckConstraint("relation IN ('primary_care', 'doctor')", name="relation"),
        {"comment": "长者-员工绑定表（护工/医生统一；范围过滤的唯一依据）"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    staff_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_accounts.staff_id"), nullable=False, comment="护工或医生"
    )
    relation: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="primary_care 责任护工 / doctor 负责医生（每位长者 ≥1 护工，服务层校验）",
    )
    assigned_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), comment="分配时间"
    )


class ElderGuardian(Base):
    """长者-监护人绑定表（家属侧范围过滤唯一依据）；解绑保痕（status=unbound，AC-24）。"""

    __tablename__ = "elder_guardians"
    __table_args__ = (
        UniqueConstraint("elder_id", "guardian_id"),  # → uq_elder_guardians_elder_id_guardian_id
        CheckConstraint("status IN ('active', 'unbound')", name="status"),
        Index(None, "guardian_id", "status"),  # → ix_elder_guardians_guardian_id_status（家属反查）
        {"comment": "长者-监护人绑定表"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    guardian_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guardian_accounts.guardian_id"), nullable=False
    )
    relation: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="与长者关系（子/女/孙辈/配偶…）；绑定级非账号级，同一人绑两位长者关系可不同",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'active'"),
        comment="active/unbound（解绑保痕，AC-24）",
    )
    assigned_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), comment="绑定时间"
    )
