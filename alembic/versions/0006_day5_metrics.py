"""Day 5 · metrics 分段耗时打点表（裁决 B6/D2 · 0006，第 27 张表）

**为什么单独一个迁移**：主理人裁决 D2（docs/plans/day05/day-05-延迟基线与护栏.md S1）——
延迟基线 P50/P95 要求数字可由 SQL 对明细复算（AC-42 / PRD §5.3），日志级打点无法满足，
故追加 `metrics` 表（ER §4.4.7，裁决 B6）。已合入迁移 0001–0005 一字不改。

**设计要点**：
- `elder_id` 可空且**不建 FK**（唤醒前打点无身份；对齐 `decision_logs.elder_id` 口径）；
- `stage` 七值走 CHECK（枚举一律 CHECK，裁决 C6），不建 PG ENUM；
- 两条查询索引：`(trace_id)` 串一次对话各分段、`(stage, created_at)` P50/P95 按段复算；
- 只写指标不写内容：不含转写全文与长者隐私字段。

人工审查要点（AGENTS §5）：
- 零默认值外无业务默认；`downgrade` 为 DROP TABLE，可逆
- 索引命名走 `models/base.py` naming_convention（ix_metrics_trace_id / ix_metrics_stage_created_at）

Revision ID: 0006_day5_metrics
Revises: 0005_day4_voiceprint_embedding
Create Date: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_day5_metrics"
down_revision: str | None = "0005_day4_voiceprint_embedding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metrics",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "trace_id",
            sa.String(length=64),
            nullable=False,
            comment="全链路追踪 ID（与日志 / Langfuse trace 对应；P50/P95 复算的串联键）",
        ),
        sa.Column(
            "elder_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="长者（可空：唤醒前打点无身份；不建 FK，对齐 decision_logs.elder_id 口径）",
        ),
        sa.Column(
            "stage",
            sa.Text(),
            nullable=False,
            comment="wakeup/connection/asr/route/llm_first_token/tts_first_packet/playback",
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=False, comment="该段耗时（毫秒，≥0）"),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=True,
            comment="口径区分：client（端侧 wake_ts）/ backend（WS accept 兜底）/ non_stream（LLM/TTS 非流式口径）等",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="打点入库时间",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_metrics"),
        sa.CheckConstraint(
            "stage IN ('wakeup', 'connection', 'asr', 'route', "
            "'llm_first_token', 'tts_first_packet', 'playback')",
            name="ck_metrics_stage",
        ),
        sa.CheckConstraint("duration_ms >= 0", name="ck_metrics_duration_ms"),
        sa.Index("ix_metrics_trace_id", "trace_id"),
        sa.Index("ix_metrics_stage_created_at", "stage", "created_at"),
        comment="分段耗时打点表（只写指标不写内容；写库唯一入口 metrics_service）",
    )


def downgrade() -> None:
    op.drop_table("metrics")
