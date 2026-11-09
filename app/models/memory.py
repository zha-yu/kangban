"""长期记忆 ORM（TD line 549 / 迁移 0009，第 29 张表）。

口径（迁移 0009 逐行对照，AGENTS §4.6：迁移已合入不得改，ORM 必须与 DDL 对齐）：

- 闸门②：所有查询强制 `WHERE elder_id = :runtime.elder_id`（FR-MEM-05）。
- TD 原 10 字段 + 裁决 D-2 增 13 列 = 23 列（自然语言 content + 机读三元组 subject/predicate/object_value 同时写）。
- `embedding` 用最小 pgvector 声明（`Vector1536` UserDefinedType），**不引** pgvector Python 包依赖（与迁移 0009 口径一致）。
- `supersedes_id` / `conflicts_with_id` 是自引用 UUID 但**故意不建 FK**：允许指向已硬删条目。
- 枚举一律 CHECK 不建 PG ENUM（与全项目保持一致）。
- `category` CHECK 固定为 `'private'`（本版不做 shared）。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from app.models.base import Base


class Vector1536(UserDefinedType):
    """最小 pgvector 类型声明：ORM 对齐迁移 0009 的 DDL，不引 pgvector 包。"""

    cache_ok = True

    def get_col_spec(self, **_: object) -> str:
        return "vector(1536)"


class MemoryChunk(Base):
    """长期记忆条目：一行 = 一条被 Agent 记住的事实（偏好 / 习惯 / 关系）。"""

    __tablename__ = "memory_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_memory_chunks_elder_id_elders",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'deleted', 'disputed', 'pending_review')",
            name="status",
        ),
        CheckConstraint("polarity IN ('positive', 'negative', 'neutral')", name="polarity"),
        CheckConstraint(
            "sensitivity IN ('private', 'health', 'family')",
            name="sensitivity",
        ),
        CheckConstraint(
            "source_type IN ('elder_explicit', 'staff_note', 'inferred')",
            name="source_type",
        ),
        CheckConstraint("category = 'private'", name="category"),
        Index("ix_memory_chunks_elder_id_status", "elder_id", "status"),
        Index(
            "ix_memory_chunks_elder_id_subject_predicate",
            "elder_id",
            "subject",
            "predicate",
        ),
        {
            "comment": "长期记忆（闸门②：所有查询强制 elder_id 过滤；只召回 status='active' 且在有效期）"
        },
    )

    # --- 主键 ---
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="记忆条目编号",
    )

    # --- 核心 4 列（TD 原） ---
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id"),
        nullable=False,
        comment="所属长者——所有查询强制 WHERE elder_id=:runtime.elder_id（闸门②）",
    )
    content: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="自然语言事实（人读；仅明确表达的信息入库）"
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector1536(), nullable=True, comment="语义向量（pgvector 召回；可空以支持仅关键词条目）"
    )
    category: Mapped[str] = mapped_column(
        Text(),
        server_default=text("'private'"),
        nullable=False,
        comment="记忆类别；本版仅 private（shared 为非目标）",
    )

    # --- 治理列（TD 原） ---
    importance: Mapped[float] = mapped_column(
        Numeric(precision=3, scale=2),
        server_default=text("1.0"),
        nullable=False,
        comment="重要性分（90 天衰减、每周归并）",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        server_default=text("'active'"),
        nullable=False,
        comment="active / superseded / deleted / disputed / pending_review（五值）",
    )
    source_thread_id: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="萃取来源会话"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, comment="创建时间"
    )
    last_accessed: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最后召回时间（冷热分层依据）"
    )

    # --- 裁决 D-2 增列：机读三元组 + 治理字段 ---
    subject: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="结构化主体（固定为 elder_id 字符串）"
    )
    predicate: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="结构化谓词（小写下划线枚举，如 preferred_opera_genre）"
    )
    object_value: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="结构化客体值（如「越剧」）"
    )
    polarity: Mapped[str] = mapped_column(
        Text(),
        server_default=text("'positive'"),
        nullable=False,
        comment="positive / negative / neutral",
    )
    confidence: Mapped[float] = mapped_column(
        Numeric(precision=3, scale=2),
        nullable=False,
        comment="置信度；行为推断上限 0.70，须低于明确陈述",
    )
    sensitivity: Mapped[str] = mapped_column(
        Text(),
        server_default=text("'private'"),
        nullable=False,
        comment="private / health / family（决定可见范围）",
    )
    source_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="elder_explicit / staff_note / inferred"
    )
    source_turn_ids: Mapped[list[Any]] = mapped_column(
        JSONB(), nullable=False, comment="来源轮次数组；空数组不得入库（服务层校验）"
    )
    valid_from: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="生效时间"
    )
    valid_to: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="失效时间；召回条件含 valid_to IS NULL OR valid_to > now()",
    )
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="指向被替代条目（自引用，故意不建 FK）"
    )
    conflicts_with_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="指向冲突条目（自引用，故意不建 FK）"
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="状态迁移时间",
    )


__all__ = ["MemoryChunk", "Vector1536"]
