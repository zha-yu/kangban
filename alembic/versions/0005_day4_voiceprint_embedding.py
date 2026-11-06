"""Day 4 · 声纹 embedding 加密列（裁决 B2 · 0005）

**为什么单独一个迁移**：裁决 B2 — embedding 列维度留 spike 结论后一次定准。
CAM++ 192 维已由 spikes/voiceprint/ 确认（2026-11-06），本迁移 ADD COLUMN 定案。

**为什么用 bytea 而非 vector(N)**：embedding 经 Fernet 加密后存 bytes，
进程内 numpy 做 cosine 比对（候选集每长者 ≤3 行，精确扫描）。
不引入 pgvector ANN 索引，避免 pgvector 强依赖 + 加密向量类型兼容问题。

人工审查要点（AGENTS §5）：
- bytea 列存 Fernet(ciphertext)，不是原始 float 数组
- 零默认值 + NOT NULL：voiceprint_service 写库时必须同时给 version 与 embedding
- downgrade 可逆：DROP COLUMN embedding 后，voiceprints 表回退到 0004 骨架

Revision ID: 0005_day4_voiceprint_embedding
Revises: 0004_day4_voice_threads
Create Date: 2026-11-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_day4_voiceprint_embedding"
down_revision: str | None = "0004_day4_voice_threads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CAM++ 192 维，sherpa-onnx 提取 → 归一化 → Fernet 加密 → bytea 存库
    op.add_column(
        "voiceprints",
        sa.Column(
            "embedding",
            sa.LargeBinary(),
            nullable=False,
            comment="声纹向量（Fernet 加密后 bytes，进程内 numpy 解密 + cosine 比对；CAM++ 原始 192 维）",
        ),
    )


def downgrade() -> None:
    op.drop_column("voiceprints", "embedding")
