"""Day 4 · 声纹身份闭环（voiceprints / conversation_threads）

Day 4 批次两张新表（技术文档 §3.6，裁决 C1/C2）。**embedding 列不在此迁移**
（裁决 B2：维度 + 类型留 0005，本迁移先建不含 embedding 的骨架）。

人工审查要点（AGENTS §5）：
- voiceprints 唯一约束 UNIQUE(elder_id, version, sample_index) 裁决 C1
- conversation_threads 跨列 CHECK：
  (identity_source='generic' AND elder_id IS NULL)
  OR (identity_source IN ('device','voice','confirm') AND elder_id IS NOT NULL)
  —— generic 通用模式无确定 elder_id，非 generic 必须有（FR-VPR-03）
- identity_source 枚举值：device/voice/confirm/generic（技术文档 §3.6）
- FK 外键列类型对齐父表（elders.elder_id / devices.device_id 均 UUID）
- 约束命名按 naming_convention：CHECK 给短名，PK/FK 模板推导

Revision ID: 0004_day4_voice_threads
Revises: 0003_fix_day1_ck_names
Create Date: 2026-11-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_day4_voice_threads"
down_revision: str | None = "0003_fix_day1_ck_names"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- voiceprints（Day 4 批次 · 声纹注册样本表） ----
    # embedding 列留 0005 迁移（裁决 B2：维度 + 类型待定）
    op.create_table(
        "voiceprints",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("elder_id", postgresql.UUID(as_uuid=True), nullable=False, comment="所属长者（FK→elders）"),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False, comment="声纹版本号（重训 +1）"),
        sa.Column("sample_index", sa.Integer(), server_default=sa.text("1"), nullable=False, comment="样本序号 1/2/3（3 段注册）"),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="active/retired",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_voiceprints"),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_voiceprints_elder_id_elders",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "elder_id", "version", "sample_index",
            name="uq_voiceprints_elder_id_version_sample_index",
        ),
        sa.CheckConstraint("status IN ('active', 'retired')", name="status"),
        comment="声纹注册样本表（原始音频永不入库，embedding 加密存 bytes）",
    )

    # ---- conversation_threads（Day 4 批次 · 会话隔离表 · 闸门①落点） ----
    op.create_table(
        "conversation_threads",
        sa.Column("thread_id", sa.Text(), nullable=False, comment="会话 ID：服务端按 (elder_id, session_seq) 生成，前端不可指定"),
        sa.Column(
            "elder_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="识别出的说话人；generic 降级模式为 NULL（FR-VPR-03）",
        ),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="实际使用的设备",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="会话开始时间",
        ),
        sa.Column(
            "ended_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="会话结束时间（换人/退出发起新 thread 时回填）",
        ),
        sa.Column(
            "identity_source",
            sa.Text(),
            nullable=False,
            comment="device 设备锁定/voice 声纹/confirm 主动确认/generic 通用模式",
        ),
        sa.Column(
            "identity_confidence",
            sa.Numeric(precision=4, scale=3),
            nullable=True,
            comment="声纹置信度（cosine 相似度；generic 模式 NULL）",
        ),
        sa.PrimaryKeyConstraint("thread_id", name="pk_conversation_threads"),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_conversation_threads_elder_id_elders",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.device_id"],
            name="fk_conversation_threads_device_id_devices",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "identity_source IN ('device', 'voice', 'confirm', 'generic')",
            name="identity_source",
        ),
        sa.CheckConstraint(
            "(identity_source = 'generic' AND elder_id IS NULL) "
            "OR (identity_source IN ('device', 'voice', 'confirm') AND elder_id IS NOT NULL)",
            name="identity_source_elder_id",
        ),
        comment="会话隔离表（闸门①：thread_id 服务端生成，elder_id 强制过滤所有下游数据访问）",
    )


def downgrade() -> None:
    op.drop_table("conversation_threads")
    op.drop_table("voiceprints")
