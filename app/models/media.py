"""Day 8 dev-A：MediaLibrary / MediaHistory ORM（0018 迁移镜像——ER §4.4.1/§4.4.2）。

列注释与 `alembic/versions/0018_day8_media.py` 逐字一致，避免 autogenerate 漂移。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# ---------- 与迁移文件同源的枚举常量 ----------
_MEDIA_TYPES: tuple[str, ...] = ("opera", "movie")
_COPYRIGHT_STATUSES: tuple[str, ...] = ("teacher_authorized", "public_domain")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class MediaLibrary(Base):
    """戏曲/老电影媒体库（9 列，ER §4.4.1；唯一写库入口 media_service.seed_media_library）。"""

    __tablename__ = "media_library"
    __table_args__ = (
        Index("ix_media_library_type_genre", "type", "genre"),
        CheckConstraint(
            _in_clause("type", _MEDIA_TYPES),
            name="ck_media_library_type",
        ),
        CheckConstraint(
            _in_clause("copyright_status", _COPYRIGHT_STATUSES),
            name="ck_media_library_copyright_status",
        ),
        CheckConstraint(
            "(duration >= 0 OR duration IS NULL)",
            name="ck_media_library_duration_non_negative",
        ),
        {"comment": "戏曲/老电影媒体库（唯一写库入口 media_service.seed_media_library）"},
    )

    media_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="媒体编号",
    )
    type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="类型：opera 戏曲 / movie 老电影（电影 P2）"
    )
    title: Mapped[str] = mapped_column(Text(), nullable=False, comment="标题")
    genre: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="剧种/类型（如 越剧/京剧/剧情）")
    era: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="年代（如 1950s/文革后/近代）")
    artist: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="演员/主演")
    media_url: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="文件地址（nginx 静态托管，浏览器直连，不经后端转发）",
    )
    duration: Mapped[int | None] = mapped_column(Integer(), nullable=True, comment="时长（秒）")
    copyright_status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="版权状态：teacher_authorized（教师授权样例）/ public_domain（公有领域）",
    )


class MediaHistory(Base):
    """长者点播历史（6 列，ER §4.4.2；唯一写库入口 media_service.record_playback）。"""

    __tablename__ = "media_history"
    __table_args__ = (
        Index(
            "ix_media_history_elder_id_started_at",
            "elder_id",
            text("started_at DESC"),
        ),
        CheckConstraint(
            "(completion_rate BETWEEN 0 AND 1 OR completion_rate IS NULL)",
            name="ck_media_history_completion_rate_range",
        ),
        CheckConstraint(
            "(finished_at IS NULL OR finished_at >= started_at)",
            name="ck_media_history_finished_after_started",
        ),
        {"comment": "长者点播历史（唯一写库入口 media_service.record_playback）"},
    )

    history_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="点播记录编号",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE"),
        nullable=False,
        comment="长者（外键→elders）",
    )
    media_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_library.media_id", ondelete="CASCADE"),
        nullable=False,
        comment="媒体（外键→media_library）",
    )
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="开始时间（UTC）"
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="结束时间（UTC；中途退出为 NULL）"
    )
    completion_rate: Mapped[float | None] = mapped_column(
        Numeric(4, 3),
        nullable=True,
        comment="完播率（0.000–1.000；频繁「换一段」→ 偏好降权信号，FR-CON-06）",
    )
