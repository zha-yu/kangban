"""Day 8 dev-A：`media_library` 媒体库 + `media_history` 点播历史两表。

为什么今天建
-----------
戏曲点播是 PRD FR-CON-03 P0 功能，ER §4.4.1/§4.4.2 已定义两表；
Day 7 只有规则层占位（action="agent" + reason="capability_not_ready"），
**没有戏曲素材存储**——Day 8 A4 建表后 A5 规则直连才能真正执行。

与 ER 的字段对齐
---------------
media_library ER §4.4.1 9 列逐字一致；copyright_status CHECK 枚举
`teacher_authorized` / `public_domain`（大纲 S6/S7 + PRD FR-CON-05）。

media_history ER §4.4.2 6 列逐字一致；completion_rate CHECK 范围
0.000–1.000（频繁「换一段」→ 偏好降权信号，FR-CON-06）。

不碰历史表
----------
down_revision = "0017_day8_health_reports"。health_records / daily_reports /
decision_logs / notifications / work_orders / risk_events 一行不改。

可回滚性
--------
downgrade 顺序：先 drop media_history（FK 引用 media_library）
→ 再 drop media_library → 最后 drop 索引。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_day8_media"
down_revision: str | None = "0017_day8_health_reports"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


# ---------- 枚举常量（与 app/models/media.py 同源） ----------

#: media_library.type 两值（ER §4.4.1）
_MEDIA_TYPES: tuple[str, ...] = ("opera", "movie")

#: media_library.copyright_status 两值（大纲 S6/S7 + PRD FR-CON-05）
_COPYRIGHT_STATUSES: tuple[str, ...] = ("teacher_authorized", "public_domain")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


def upgrade() -> None:
    # ==================== media_library（9 列） ====================
    op.create_table(
        "media_library",
        # --- PK ---
        sa.Column(
            "media_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="媒体编号",
        ),
        # --- 核心字段 ---
        sa.Column(
            "type",
            sa.Text(),
            nullable=False,
            comment="类型：opera 戏曲 / movie 老电影（电影 P2）",
        ),
        sa.Column("title", sa.Text(), nullable=False, comment="标题"),
        sa.Column("genre", sa.Text(), nullable=True, comment="剧种/类型（如 越剧/京剧/剧情）"),
        sa.Column("era", sa.Text(), nullable=True, comment="年代（如 1950s/文革后/近代）"),
        sa.Column("artist", sa.Text(), nullable=True, comment="演员/主演"),
        sa.Column(
            "media_url",
            sa.Text(),
            nullable=False,
            comment="文件地址（nginx 静态托管，浏览器直连，不经后端转发）",
        ),
        sa.Column("duration", sa.Integer(), nullable=True, comment="时长（秒）"),
        sa.Column(
            "copyright_status",
            sa.Text(),
            nullable=False,
            comment="版权状态：teacher_authorized（教师授权样例）/ public_domain（公有领域）",
        ),
        # --- 约束 ---
        sa.PrimaryKeyConstraint("media_id", name="pk_media_library"),
        sa.CheckConstraint(
            _in_clause("type", _MEDIA_TYPES),
            name="ck_media_library_type",
        ),
        sa.CheckConstraint(
            _in_clause("copyright_status", _COPYRIGHT_STATUSES),
            name="ck_media_library_copyright_status",
        ),
        sa.CheckConstraint(
            "(duration >= 0 OR duration IS NULL)",
            name="ck_media_library_duration_non_negative",
        ),
        comment="戏曲/老电影媒体库（唯一写库入口 media_service.seed_media_library）",
    )
    # 索引：剧种搜索（按 type + genre 过滤最频）
    op.create_index(
        "ix_media_library_type_genre",
        "media_library",
        ["type", "genre"],
    )

    # ==================== media_history（6 列） ====================
    op.create_table(
        "media_history",
        # --- PK ---
        sa.Column(
            "history_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="点播记录编号",
        ),
        # --- FK ---
        sa.Column(
            "elder_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="长者（外键→elders）",
        ),
        sa.Column(
            "media_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="媒体（外键→media_library）",
        ),
        # --- 时间 ---
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="开始时间（UTC）",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="结束时间（UTC；中途退出为 NULL）",
        ),
        # --- 偏好信号 ---
        sa.Column(
            "completion_rate",
            sa.Numeric(4, 3),
            nullable=True,
            comment="完播率（0.000–1.000；频繁「换一段」→ 偏好降权信号，FR-CON-06）",
        ),
        # --- 约束 ---
        sa.PrimaryKeyConstraint("history_id", name="pk_media_history"),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_media_history_elder_id_elders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media_library.media_id"],
            name="fk_media_history_media_id_media_library",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(completion_rate BETWEEN 0 AND 1 OR completion_rate IS NULL)",
            name="ck_media_history_completion_rate_range",
        ),
        sa.CheckConstraint(
            "(finished_at IS NULL OR finished_at >= started_at)",
            name="ck_media_history_finished_after_started",
        ),
        comment="长者点播历史（唯一写库入口 media_service.record_playback）",
    )
    # 索引：长者点播历史查询（按时间倒序）
    op.create_index(
        "ix_media_history_elder_id_started_at",
        "media_history",
        ["elder_id", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    # 先 drop media_history（FK 引用 media_library）→ 再 drop media_library
    op.drop_index(
        "ix_media_history_elder_id_started_at", table_name="media_history"
    )
    op.drop_index(
        "ix_media_library_type_genre", table_name="media_library"
    )
    op.drop_table("media_history")
    op.drop_table("media_library")
