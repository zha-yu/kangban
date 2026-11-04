"""员工侧身份表：staff_accounts（ER §4.1.6 / TD §3.2）与 staff_tokens（ER §4.2.3 / TD §3.3）。

命名对齐说明：CHECK 约束只给短名（如 name="status"），最终名由 `Base.naming_convention`
拼成 `ck_staff_accounts_status` —— 与 `alembic/versions/0002_day2_identity.py` 里
显式写的全名一致，autogenerate 不会漂移。FK / UNIQUE / PK 不写名，全交约定推导。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StaffAccount(Base):
    """机构侧账号表（管理员 / 护工 / 医生共用），登录后按 role 分流权限（AC-18：服务端为准）。"""

    __tablename__ = "staff_accounts"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'care_worker', 'doctor')", name="role"),
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        {"comment": "机构侧账号表（管理员/护工/医生共用）"},
    )

    staff_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text(), nullable=False, comment="姓名")
    role: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="admin/care_worker/doctor，服务端据此分流权限"
    )
    phone: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="手机号（日志脱敏规则覆盖）"
    )
    password_hash: Mapped[str] = mapped_column(Text(), nullable=False, comment="密码哈希，只存哈希")
    token_version: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        server_default=text("0"),
        comment="停用/解绑/角色变化 +1，旧 Token 立即失效（AC-24）",
    )
    status: Mapped[str] = mapped_column(
        Text(), nullable=False, server_default=text("'active'"), comment="active/disabled"
    )


class StaffToken(Base):
    """护工工牌临时令牌表（FR-ADM-08，P1；设备 JWT + 工牌 JWT 双体系）。

    token_hash 即主键：只存哈希不存明文（task-3 硬约束 3）。
    """

    __tablename__ = "staff_tokens"
    __table_args__ = ({"comment": "护工工牌临时令牌表"},)

    token_hash: Mapped[str] = mapped_column(
        Text(), primary_key=True, comment="临时令牌哈希（刷工牌/扫码换取）"
    )
    staff_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_accounts.staff_id"), nullable=False, comment="所属护工"
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.device_id"),
        nullable=False,
        comment="在哪台设备上激活；权限范围随该设备绑定长者收敛",
    )
    scope: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="受限操作范围（如 workorder:handle, health:staff_form）"
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="短时效，到期自动 401"
    )
