"""Day 8 dev-A：HealthRecord ORM（0017 迁移镜像——TD §3.4 + Q-1 裁决补列）。

列注释与 `alembic/versions/0017_day8_health_reports.py` 逐字一致，避免 autogenerate 漂移。

关键口径（与 TD 同步，AGENTS §8 分层硬约束）
----------------------------------------------
1. `superseded_reason`：Q-1 主理人裁决补入列。**服务端固定枚举写入**
   （纠错→review_correction、代填补录→backfill、正常采集或最新版本→NULL），
   前端 review 端点请求体不传该值——纯服务端域。
2. `version` 从 1 起，补录 version+1；`superseded_by` 指向上一版本，
   最新版本 `superseded_by=NULL`。
3. `source` 四值：voice_scheduled/voice_active/staff_scheduled/staff_active
   （裁决 B1：不含旧口径 staff_report/resident）。
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
_SOURCES: tuple[str, ...] = (
    "voice_scheduled",
    "voice_active",
    "staff_scheduled",
    "staff_active",
)
_SEVERITIES: tuple[str, ...] = ("green", "yellow", "red")
_REVIEW_STATUSES: tuple[str, ...] = ("pending", "confirmed", "corrected")
_SUPERSEDED_REASONS: tuple[str, ...] = ("review_correction", "backfill")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class HealthRecord(Base):
    """健康采集记录（19 列：TD 17 列 + review_note + superseded_reason 两裁决补列）。"""

    __tablename__ = "health_records"
    __table_args__ = (
        Index(
            "ix_health_records_elder_id_recorded_at",
            "elder_id",
            text("recorded_at DESC"),
        ),
        # --- 范围 CHECK ---
        CheckConstraint(
            "(systolic BETWEEN 0 AND 300 OR systolic IS NULL)",
            name="ck_health_records_systolic_range",
        ),
        CheckConstraint(
            "(diastolic BETWEEN 0 AND 300 OR diastolic IS NULL)",
            name="ck_health_records_diastolic_range",
        ),
        CheckConstraint(
            "(sleep_hours BETWEEN 0 AND 24 OR sleep_hours IS NULL)",
            name="ck_health_records_sleep_range",
        ),
        CheckConstraint(
            "(mood_score BETWEEN 1 AND 5 OR mood_score IS NULL)",
            name="ck_health_records_mood_range",
        ),
        # --- 枚举 CHECK ---
        CheckConstraint(_in_clause("source", _SOURCES), name="ck_health_records_source"),
        CheckConstraint(
            _in_clause("severity", _SEVERITIES), name="ck_health_records_severity"
        ),
        CheckConstraint(
            _in_clause("review_status", _REVIEW_STATUSES),
            name="ck_health_records_review_status",
        ),
        CheckConstraint(
            f"(superseded_reason IS NULL OR {_in_clause('superseded_reason', _SUPERSEDED_REASONS)})",
            name="ck_health_records_superseded_reason",
        ),
        CheckConstraint("version >= 1", name="ck_health_records_version_positive"),
        {"comment": "健康采集记录（唯一写库入口 health_service.create_health_record）"},
    )

    # --- PK ---
    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="记录编号",
    )

    # --- FK ---
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE"),
        nullable=False,
        comment="长者（唯一外键指向 elders）",
    )
    operator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("staff_accounts.staff_id", ondelete="SET NULL"),
        nullable=True,
        comment="代填护工（staff 外键，voice 采集时为 NULL）",
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("staff_accounts.staff_id", ondelete="SET NULL"),
        nullable=True,
        comment="复核医生（staff 外键；pending 状态为 NULL）",
    )

    # --- 采集时间 ---
    recorded_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="采集/代填时间（UTC 存储）"
    )

    # --- 来源 ---
    source: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="采集来源：voice_scheduled/voice_active/staff_scheduled/staff_active"
    )

    # --- 健康数据字段（可空 = 未采集/无效） ---
    systolic: Mapped[int | None] = mapped_column(Integer(), nullable=True, comment="收缩压（mmHg，0–300）")
    diastolic: Mapped[int | None] = mapped_column(Integer(), nullable=True, comment="舒张压（mmHg，0–300）")
    sleep_hours: Mapped[float | None] = mapped_column(
        Numeric(3, 1), nullable=True, comment="睡眠时长（小时，0.0–24.0；≤1 位小数）"
    )
    mood_score: Mapped[int | None] = mapped_column(Integer(), nullable=True, comment="情绪自评（1–5，5 最好）")
    mood_note: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="情绪备注")
    complaint: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="主诉")

    # --- 分级与复核 ---
    severity: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="分级：green/yellow/red（AC-09/10/21）"
    )
    rule_version: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="分级规则版本（改阈值必须 +1，health_service 常量区）"
    )
    review_status: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="复核状态：pending/confirmed/corrected"
    )

    # --- Q-1 裁决补入 2 列 ---
    review_note: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="复核操作备注（前端 review-api review_note 字段）"
    )
    superseded_reason: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="版本链替换原因：review_correction/backfill（纯服务端写入）",
    )

    # --- 版本链 ---
    version: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        server_default=text("1"),
        comment="版本号（从 1 起，补录 version+1）",
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="被哪个新版本取代（自引用，最新版本为 NULL）",
    )

    # --- LangGraph 链路（可空） ---
    graph_thread_id: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="LangGraph 会话（健康子图写入时回填，独立 thread_id）",
    )
