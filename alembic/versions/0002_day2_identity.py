"""Day 2 · 身份与权限（staff_accounts / guardian_accounts / elder_staff_assignments / elder_guardians / staff_tokens）

对应 docs/ER_数据模型设计.md §4.1.6–§4.1.7、§4.2 与 §6 的 Day 2 批次（5 张表）。
**人工审查要点**（AGENTS.md §5）：
- 枚举列一律 CHECK（裁决 C6），不建 PG ENUM；
- 唯一约束照 ER §5.1：elder_staff_assignments(elder_id, staff_id, relation)、
  elder_guardians(elder_id, guardian_id)；查询索引照 ER §5.2：elder_guardians(guardian_id, status)；
- staff_tokens.token_hash 为 text 主键，只存哈希不存明文；
- 「每位长者 active 监护人 ≤2」是服务层校验（裁决 C4），不落 DDL；
- 约束命名对齐 app/models/base.py 的 naming_convention：target_metadata 挂载后约定会作用于
  迁移内的显式名——因此 CHECK 只给短名（name="role"/"status"/"relation"），由模板拼出
  ck_staff_accounts_role 等全名，与 ORM 侧短名经同一约定渲染的结果严格一致；
  ⚠️ 0001 的 CHECK 当时按全名书写，被同一模板二次前缀（如 ck_rooms_ck_rooms_status），
  属既有瑕疵，0001 已冻结不改，约束功能不受影响。

Revision ID: 0002_day2
Revises: 0001_day1
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_day2"
down_revision: str | None = "0001_day1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- 4.1.6 staff_accounts（机构侧账号：管理员/护工/医生共用） ----
    op.create_table(
        "staff_accounts",
        sa.Column("staff_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, comment="姓名"),
        sa.Column("role", sa.Text(), nullable=False, comment="admin/care_worker/doctor，服务端据此分流权限（AC-18）"),
        sa.Column("phone", sa.Text(), nullable=True, comment="手机号（日志脱敏规则覆盖）"),
        sa.Column("password_hash", sa.Text(), nullable=False, comment="密码哈希，只存哈希"),
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False, comment="停用/解绑/角色变化 +1，旧 Token 立即失效（AC-24）"),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False, comment="active/disabled"),
        sa.PrimaryKeyConstraint("staff_id", name="pk_staff_accounts"),
        sa.CheckConstraint("role IN ('admin', 'care_worker', 'doctor')", name="role"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="status"),
        comment="机构侧账号表（管理员/护工/医生共用）",
    )

    # ---- 4.1.7 guardian_accounts（监护人账号；监护关系存 elder_guardians） ----
    op.create_table(
        "guardian_accounts",
        sa.Column("guardian_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, comment="姓名"),
        sa.Column("phone", sa.Text(), nullable=True, comment="手机号（脱敏）"),
        sa.Column("webpush_subscription", postgresql.JSONB(), nullable=True, comment="浏览器 Web Push 订阅凭证（P0 只存不真推，裁决 B5）"),
        sa.Column("password_hash", sa.Text(), nullable=False, comment="密码哈希，只存哈希"),
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False, comment="同 staff_accounts（解绑即时失效，AC-24）"),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False, comment="active/disabled"),
        sa.PrimaryKeyConstraint("guardian_id", name="pk_guardian_accounts"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="status"),
        comment="监护人账号表",
    )

    # ---- 4.2.1 elder_staff_assignments（长者-员工绑定；范围过滤唯一依据） ----
    op.create_table(
        "elder_staff_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("elder_id", postgresql.UUID(as_uuid=True), nullable=False, comment="长者"),
        sa.Column("staff_id", postgresql.UUID(as_uuid=True), nullable=False, comment="护工或医生"),
        sa.Column("relation", sa.Text(), nullable=False, comment="primary_care 责任护工 / doctor 负责医生（每位长者 ≥1 护工，服务层校验）"),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="分配时间"),
        sa.PrimaryKeyConstraint("id", name="pk_elder_staff_assignments"),
        sa.ForeignKeyConstraint(["elder_id"], ["elders.elder_id"], name="fk_elder_staff_assignments_elder_id_elders"),
        sa.ForeignKeyConstraint(["staff_id"], ["staff_accounts.staff_id"], name="fk_elder_staff_assignments_staff_id_staff_accounts"),
        sa.UniqueConstraint("elder_id", "staff_id", "relation", name="uq_elder_staff_assignments_elder_id_staff_id_relation"),
        sa.CheckConstraint("relation IN ('primary_care', 'doctor')", name="relation"),
        comment="长者-员工绑定表（护工/医生统一；范围过滤的唯一依据）",
    )

    # ---- 4.2.2 elder_guardians（长者-监护人绑定；家属侧范围过滤唯一依据） ----
    op.create_table(
        "elder_guardians",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("elder_id", postgresql.UUID(as_uuid=True), nullable=False, comment="长者"),
        sa.Column("guardian_id", postgresql.UUID(as_uuid=True), nullable=False, comment="监护人"),
        sa.Column("relation", sa.Text(), nullable=False, comment="与长者关系（子/女/孙辈/配偶…）；绑定级非账号级"),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False, comment="active/unbound（解绑保痕，AC-24）"),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="绑定时间"),
        sa.PrimaryKeyConstraint("id", name="pk_elder_guardians"),
        sa.ForeignKeyConstraint(["elder_id"], ["elders.elder_id"], name="fk_elder_guardians_elder_id_elders"),
        sa.ForeignKeyConstraint(["guardian_id"], ["guardian_accounts.guardian_id"], name="fk_elder_guardians_guardian_id_guardian_accounts"),
        sa.UniqueConstraint("elder_id", "guardian_id", name="uq_elder_guardians_elder_id_guardian_id"),
        sa.CheckConstraint("status IN ('active', 'unbound')", name="status"),
        comment="长者-监护人绑定表",
    )

    # 家属登录反查「我能看哪些长者」：WHERE guardian_id=:uid AND status='active'（ER §5.2）
    op.create_index(
        "ix_elder_guardians_guardian_id_status",
        "elder_guardians",
        ["guardian_id", "status"],
    )

    # ---- 4.2.3 staff_tokens（护工工牌临时令牌，FR-ADM-08 P1；主键即哈希） ----
    op.create_table(
        "staff_tokens",
        sa.Column("token_hash", sa.Text(), nullable=False, comment="临时令牌哈希（刷工牌/扫码换取），只存哈希"),
        sa.Column("staff_id", postgresql.UUID(as_uuid=True), nullable=False, comment="所属护工"),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False, comment="在哪台设备上激活；权限范围随该设备绑定长者收敛"),
        sa.Column("scope", sa.Text(), nullable=False, comment="受限操作范围（如 workorder:handle, health:staff_form）"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, comment="短时效，到期自动 401"),
        sa.PrimaryKeyConstraint("token_hash", name="pk_staff_tokens"),
        sa.ForeignKeyConstraint(["staff_id"], ["staff_accounts.staff_id"], name="fk_staff_tokens_staff_id_staff_accounts"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.device_id"], name="fk_staff_tokens_device_id_devices"),
        comment="护工工牌临时令牌表",
    )


def downgrade() -> None:
    op.drop_table("staff_tokens")
    op.drop_index("ix_elder_guardians_guardian_id_status", table_name="elder_guardians")
    op.drop_table("elder_guardians")
    op.drop_table("elder_staff_assignments")
    op.drop_table("guardian_accounts")
    op.drop_table("staff_accounts")
