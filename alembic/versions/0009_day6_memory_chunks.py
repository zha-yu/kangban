"""Day 6 · 长期记忆表 memory_chunks：结构化字段一次建全（0009）

**为什么这一个迁移**（计划 `docs/plans/day06/day-06-长者侧Agent闭环.md` §9.1 ④，裁决 D-2）：

Day 6 只交付记忆**骨架**（提取 + 召回），但表结构必须**一次定准**——原因有三：

1. 该表此前尚未创建（原排 Day 7），现在建不产生 ALTER 历史；
2. `status` 需要 `disputed` / `pending_review` 两个冲突中间态。若先按 TD 原三值建，
   Day 7 再补就要 `ALTER ... DROP/ADD CONSTRAINT`，而历史迁移一经合入不得改（AGENTS §4.6）；
3. 冲突检测不能靠「相似度 >0.9 扫全表」——需要 `(elder_id, subject, predicate)` 精确检索键，
   这是列级设计，后补等于重扫全表。

TD 原有 10 个字段**全部保留**（`content` 作为自然语言事实给人读），在此基础上增加 13 列作为
机读三元组与有效期，两者**同时写**，不是二选一。

**关于 embedding 用 vector 而非 bytea**：0005 把 `voiceprints.embedding` 建成 bytea，是因为
声纹向量要 Fernet 加密（隐私），加密后无法在库内算余弦。`memory_chunks.embedding` 必须参与
pgvector 召回，加密会让索引与相似度全部失效（TD §3.6 明文），故这里按 TD/ER 用 `vector(1536)`。
镜像为 `pgvector/pgvector:pg16`（docker-compose.yml line 6），扩展可用。

已合入迁移 0001–0008 一字不改。

人工审查要点（AGENTS §5）：
- `source_turn_ids` 为 jsonb 且 NOT NULL——空数组不得入库由服务层校验，DDL 不写触发器；
- `supersedes_id` / `conflicts_with_id` 是**自引用但故意不建 FK**：允许指向已被硬删的历史条目，
  建 FK 会让「删除旧记忆」连带失败；
- downgrade 只删表，**不删 vector 扩展**（其它表/后续迁移可能依赖）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType

revision: str = "0009_day6_memory_chunks"
down_revision: str | None = "0008_day6_conversation_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class Vector1536(UserDefinedType):
    """最小 pgvector 类型声明：只用于渲染 DDL，不引入 `pgvector` Python 包依赖。"""

    cache_ok = True

    def get_col_spec(self, **_: object) -> str:
        return "vector(1536)"


def _ensure_vector_extension() -> None:
    """pgvector 扩展缺失时给可执行的失败信息，而不是让建表报 `type "vector" does not exist`。"""
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as exc:
        raise RuntimeError(
            "无法启用 pgvector 扩展，0009 依赖 vector(1536) 列。"
            "请确认数据库镜像为 pgvector/pgvector:pg16，且有 CREATE EXTENSION 权限。"
            f"原始错误：{exc}"
        ) from exc


def upgrade() -> None:
    _ensure_vector_extension()

    op.create_table(
        "memory_chunks",
        sa.Column(
            "chunk_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="记忆条目编号",
        ),
        sa.Column(
            "elder_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="所属长者——所有查询强制 WHERE elder_id=:runtime.elder_id（闸门②）",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="自然语言事实（人读；仅明确表达的信息入库）",
        ),
        sa.Column(
            "embedding",
            Vector1536(),
            nullable=True,
            comment="语义向量（pgvector 召回；可空以支持仅关键词条目）",
        ),
        sa.Column(
            "category",
            sa.Text(),
            server_default=sa.text("'private'"),
            nullable=False,
            comment="记忆类别；本版仅 private（shared 为非目标）",
        ),
        sa.Column(
            "importance",
            sa.Numeric(precision=3, scale=2),
            server_default=sa.text("1.0"),
            nullable=False,
            comment="重要性分（90 天衰减、每周归并）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="active / superseded / deleted / disputed / pending_review（五值）",
        ),
        sa.Column("source_thread_id", sa.Text(), nullable=True, comment="萃取来源会话"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "last_accessed",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最后召回时间（冷热分层依据）",
        ),
        # ---- 以下为裁决 D-2 增列：机读三元组 + 治理字段 ----
        sa.Column(
            "subject", sa.Text(), nullable=False, comment="结构化主体（固定为 elder_id 字符串）"
        ),
        sa.Column(
            "predicate",
            sa.Text(),
            nullable=False,
            comment="结构化谓词（小写下划线枚举，如 preferred_opera_genre）",
        ),
        sa.Column("object_value", sa.Text(), nullable=False, comment="结构化客体值（如「越剧」）"),
        sa.Column(
            "polarity",
            sa.Text(),
            server_default=sa.text("'positive'"),
            nullable=False,
            comment="positive / negative / neutral",
        ),
        sa.Column(
            "confidence",
            sa.Numeric(precision=3, scale=2),
            nullable=False,
            comment="置信度；行为推断上限 0.70，须低于明确陈述",
        ),
        sa.Column(
            "sensitivity",
            sa.Text(),
            server_default=sa.text("'private'"),
            nullable=False,
            comment="private / health / family（决定可见范围）",
        ),
        sa.Column(
            "source_type",
            sa.Text(),
            nullable=False,
            comment="elder_explicit / staff_note / inferred",
        ),
        sa.Column(
            "source_turn_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="来源轮次数组；空数组不得入库（服务层校验）",
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False, comment="生效时间"),
        sa.Column(
            "valid_to",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="失效时间；召回条件含 valid_to IS NULL OR valid_to > now()",
        ),
        sa.Column(
            "supersedes_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="指向被替代条目（自引用，故意不建 FK）",
        ),
        sa.Column(
            "conflicts_with_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="指向冲突条目（自引用，故意不建 FK）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="状态迁移时间",
        ),
        sa.PrimaryKeyConstraint("chunk_id", name="pk_memory_chunks"),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_memory_chunks_elder_id_elders",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'deleted', 'disputed', 'pending_review')",
            name="status",
        ),
        sa.CheckConstraint("polarity IN ('positive', 'negative', 'neutral')", name="polarity"),
        sa.CheckConstraint(
            "sensitivity IN ('private', 'health', 'family')",
            name="sensitivity",
        ),
        sa.CheckConstraint(
            "source_type IN ('elder_explicit', 'staff_note', 'inferred')",
            name="source_type",
        ),
        sa.CheckConstraint("category = 'private'", name="category"),
        comment="长期记忆（闸门②：所有查询强制 elder_id 过滤；只召回 status='active' 且在有效期）",
    )
    op.create_index("ix_memory_chunks_elder_id_status", "memory_chunks", ["elder_id", "status"])
    op.create_index(
        "ix_memory_chunks_elder_id_subject_predicate",
        "memory_chunks",
        ["elder_id", "subject", "predicate"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_chunks_elder_id_subject_predicate", table_name="memory_chunks")
    op.drop_index("ix_memory_chunks_elder_id_status", table_name="memory_chunks")
    op.drop_table("memory_chunks")
    # 不 DROP EXTENSION vector：扩展可能被其它表/后续迁移使用
