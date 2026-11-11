"""Day 8 dev-A：DailyReport ORM（0017 迁移镜像——TD §3.5）。

列注释与 `alembic/versions/0017_day8_health_reports.py` 逐字一致，避免 autogenerate 漂移。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: daily_reports.status 两值（TD §3.5）
_REPORT_STATUSES: tuple[str, ...] = ("normal", "away_notice")


class DailyReport(Base):
    """家属日报（9 列，TD §3.5；唯一写库入口 report_service.generate_daily_report）。"""

    __tablename__ = "daily_reports"
    __table_args__ = (
        Index(
            "ix_daily_reports_elder_id_report_date_version",
            "elder_id",
            text("report_date DESC"),
            text("version DESC"),
        ),
        UniqueConstraint(
            "elder_id",
            "report_date",
            "version",
            name="uq_daily_reports_unique",
        ),
        CheckConstraint(
            f"status IN ({', '.join(f'{v!r}' for v in _REPORT_STATUSES)})",
            name="ck_daily_reports_status",
        ),
        CheckConstraint("version >= 1", name="ck_daily_reports_version_positive"),
        {"comment": "家属日报（唯一写库入口 report_service.generate_daily_report）"},
    )

    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="日报编号",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE"),
        nullable=False,
        comment="长者",
    )
    report_date: Mapped[datetime.date] = mapped_column(
        Date(), nullable=False, comment="日报日期（本地 Asia/Shanghai 日期）"
    )
    version: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        server_default=text("1"),
        comment="版本号（日报版本链：家属改了偏好→version+1）",
    )
    status: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="日报状态：normal/away_notice"
    )
    summary_text: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="日报摘要文本（结构化 JSON 存 medication_stats，本字段是家属可读文本）"
    )
    medication_stats: Mapped[dict | None] = mapped_column(
        JSONB(), nullable=True, comment="当日用药结构化统计（四类：按时/漏服/待提醒/未录入）"
    )
    audio_url: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="语音合成日报音频 URL（可空：合成失败降级纯文本）"
    )
    pushed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="首次推送时间（UTC；未推送为 NULL）"
    )
