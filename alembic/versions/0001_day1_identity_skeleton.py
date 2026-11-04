"""Day 1 · 身份与组织骨架（rooms / beds / devices / elders / personas）

对应 docs/ER_数据模型设计.md §4.1.1–§4.1.5 与 §6 的 Day 1 批次（5 张表）。
**人工审查要点**（AGENTS.md §5）：主键全部 UUID（`gen_random_uuid()`，PostgreSQL 13+ 内置）；
`devices(elder_id)` 必须是**部分唯一索引**（`WHERE elder_id IS NOT NULL`），保证设备:长者 1:1
又允许空值的设备先行入库；`beds` 的 `(room_id, bed_no)` 唯一；四张表的 `status` 枚举走 CHECK（不建 PG ENUM）。

Revision ID: 0001_day1
Revises:
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_day1"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- 4.1.5 personas（被 elders 引用，先建） ----
    op.create_table(
        "personas",
        sa.Column("persona_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, comment="人设名（小满 / 孙女 / 老同事…）"),
        sa.Column("personality", sa.Text(), nullable=False, comment="性格描述，拼进 system prompt 固定前缀"),
        sa.Column("greeting", sa.Text(), nullable=False, comment="开场白"),
        sa.Column("tone_examples", postgresql.JSONB(), nullable=True, comment="口吻示例 few-shot，≤3 条，保 prompt cache 稳定"),
        sa.Column("voice_id", sa.Text(), nullable=False, comment="TTS 音色 ID"),
        sa.Column("avatar_url", sa.Text(), nullable=True, comment="VRM 模型地址（nginx 静态托管）"),
        sa.PrimaryKeyConstraint("persona_id", name="pk_personas"),
        comment="数字人人设表",
    )

    # ---- 4.1.1 rooms ----
    op.create_table(
        "rooms",
        sa.Column("room_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("room_no", sa.Text(), nullable=False, comment="房号（如 A-301）"),
        sa.Column("status", sa.Text(), server_default=sa.text("'available'"), nullable=False, comment="由 beds 聚合，服务层维护"),
        sa.PrimaryKeyConstraint("room_id", name="pk_rooms"),
        sa.CheckConstraint(
            "status IN ('available', 'partial', 'full', 'maintenance')",
            name="ck_rooms_status",
        ),
        comment="房间表",
    )

    # ---- 4.1.4 elders ----
    op.create_table(
        "elders",
        sa.Column("elder_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, comment="姓名（演示数据，日志脱敏）"),
        sa.Column("birth_date", sa.Date(), nullable=True, comment="出生日期；用 date 不用 text（可算年龄）"),
        sa.Column("gender", sa.Text(), nullable=True, comment="性别，可选填"),
        sa.Column("persona_id", postgresql.UUID(as_uuid=True), nullable=True, comment="数字人人设分配"),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("session_seq", sa.Integer(), server_default=sa.text("0"), nullable=False, comment="会话计数器，每新会话 +1 → thread_id"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("elder_id", name="pk_elders"),
        sa.ForeignKeyConstraint(["persona_id"], ["personas.persona_id"], name="fk_elders_persona_id_personas"),
        sa.CheckConstraint(
            "status IN ('active', 'away', 'transferred', 'archived')",
            name="ck_elders_status",
        ),
        comment="长者表",
    )

    # ---- 4.1.2 beds ----
    op.create_table(
        "beds",
        sa.Column("bed_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False, comment="所属房间；双人间声纹候选集由设备房间推导"),
        sa.Column("bed_no", sa.Text(), nullable=False, comment="床号（1 号床）"),
        sa.Column("status", sa.Text(), server_default=sa.text("'available'"), nullable=False),
        sa.Column("elder_id", postgresql.UUID(as_uuid=True), nullable=True, comment="当前入住长者，空床为 NULL"),
        sa.PrimaryKeyConstraint("bed_id", name="pk_beds"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.room_id"], name="fk_beds_room_id_rooms"),
        sa.ForeignKeyConstraint(["elder_id"], ["elders.elder_id"], name="fk_beds_elder_id_elders"),
        sa.UniqueConstraint("room_id", "bed_no", name="uq_beds_room_id_bed_no"),
        sa.CheckConstraint(
            "status IN ('available', 'occupied', 'maintenance')",
            name="ck_beds_status",
        ),
        comment="床位表",
    )

    # ---- 4.1.3 devices ----
    op.create_table(
        "devices",
        sa.Column("device_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False, comment="所在房间 → 决定声纹候选范围"),
        sa.Column("elder_id", postgresql.UUID(as_uuid=True), nullable=True, comment="绑定主使用者；设备:长者 1:1，可空"),
        sa.Column("token_hash", sa.Text(), nullable=False, comment="设备长期令牌哈希，解绑即失效"),
        sa.Column("status", sa.Text(), server_default=sa.text("'offline'"), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True, comment="超 2 个心跳周期判掉线"),
        sa.PrimaryKeyConstraint("device_id", name="pk_devices"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.room_id"], name="fk_devices_room_id_rooms"),
        sa.ForeignKeyConstraint(["elder_id"], ["elders.elder_id"], name="fk_devices_elder_id_elders"),
        sa.CheckConstraint(
            "status IN ('online', 'offline', 'locked')",
            name="ck_devices_status",
        ),
        comment="模拟设备表",
    )

    # 设备:长者 1:1 —— 必须部分唯一索引，空值不参与冲突
    op.create_index(
        "uq_devices_elder_id",
        "devices",
        ["elder_id"],
        unique=True,
        postgresql_where=sa.text("elder_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_devices_elder_id", table_name="devices")
    op.drop_table("devices")
    op.drop_table("beds")
    op.drop_table("elders")
    op.drop_table("rooms")
    op.drop_table("personas")
