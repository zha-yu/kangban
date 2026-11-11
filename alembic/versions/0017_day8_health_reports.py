"""Day 8 dev-A：`health_records` 健康采集记录 + `daily_reports` 家属日报两表。

为什么今天建
-----------
健康采集（TD §3.4）和家属日报（TD §3.5）是 Day 8 健康业务闭环的核心数据面。
Day 7 只有对话/用药/记忆/通知，**健康域从 Day 8 起才有独立存储**。

与 TD 字段的对齐（Q-1 两列裁决补入，TD 走修订记录）
-------------------------------------------------
health_records TD §3.4 原始 17 列 + 主理人裁决补入 2 列：
  - `review_note text 可空`（复核操作备注，承载前端 review-api `review_note`）
  - `superseded_reason text 可空`（CHECK 枚举 review_correction/backfill，承载前端
    review-api `superseded_reason`；**服务端固定枚举写入，前端请求体不传**）

daily_reports TD §3.5 原始 9 列，无补入。

不碰历史表
----------
down_revision = "0016_decision_logs"。0016 及更早**一行不改**。

CHECK 口径（全部来自 TD §3.6 + AC 编号）
---------------------------------------
血压范围、睡眠范围、情绪范围、来源四值、严重度三值、复核状态三值、
superseded_reason 两值（可空）、日报唯一 + 日报状态。

可回滚性
--------
downgrade 先 drop 4 个索引 → drop health_records → drop daily_reports。
health_records 被 risk_events 引用（FK），但 risk_events 是 0010 已建表，
其 FK 在 health_records 上反向，先删 health_records 没问题。
daily_reports 无外键指向它。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_day8_health_reports"
down_revision: str | None = "0016_decision_logs"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


# ---------- 枚举常量（与 app/models/health.py / report.py 同源） ----------

#: health_records.source 四值（TD §3.6 第 5 条，裁决 B1：不含旧口径 staff_report/resident）
_SOURCES: tuple[str, ...] = (
    "voice_scheduled",
    "voice_active",
    "staff_scheduled",
    "staff_active",
)

#: health_records.severity 三值（TD §3.4）
_SEVERITIES: tuple[str, ...] = ("green", "yellow", "red")

#: health_records.review_status 三值（TD §3.4）
_REVIEW_STATUSES: tuple[str, ...] = ("pending", "confirmed", "corrected")

#: health_records.superseded_reason 两值（Q-1 裁决补入列，CHECK 枚举）
_SUPERSEDED_REASONS: tuple[str, ...] = ("review_correction", "backfill")

#: daily_reports.status 两值（TD §3.5）
_REPORT_STATUSES: tuple[str, ...] = ("normal", "away_notice")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


def upgrade() -> None:
    # ==================== health_records（19 列） ====================
    op.create_table(
        "health_records",
        # --- PK ---
        sa.Column(
            "record_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="记录编号",
        ),
        # --- FK ---
        sa.Column(
            "elder_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="长者（唯一外键指向 elders）",
        ),
        sa.Column(
            "operator_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="代填护工（staff 外键，voice 采集时为 NULL）",
        ),
        sa.Column(
            "reviewed_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="复核医生（staff 外键；pending 状态为 NULL）",
        ),
        # --- 采集时间 ---
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="采集/代填时间（UTC 存储）",
        ),
        # --- 来源 ---
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            comment="采集来源：voice_scheduled/voice_active/staff_scheduled/staff_active",
        ),
        # --- 健康数据字段（可空表示未采集/无效） ---
        sa.Column("systolic", sa.Integer(), nullable=True, comment="收缩压（mmHg，0–300）"),
        sa.Column("diastolic", sa.Integer(), nullable=True, comment="舒张压（mmHg，0–300）"),
        sa.Column(
            "sleep_hours",
            sa.Numeric(3, 1),
            nullable=True,
            comment="睡眠时长（小时，0.0–24.0；≤1 位小数）",
        ),
        sa.Column("mood_score", sa.Integer(), nullable=True, comment="情绪自评（1–5，5 最好）"),
        sa.Column("mood_note", sa.Text(), nullable=True, comment="情绪备注"),
        sa.Column("complaint", sa.Text(), nullable=True, comment="主诉"),
        # --- 分级与复核 ---
        sa.Column(
            "severity",
            sa.Text(),
            nullable=False,
            comment="分级：green/yellow/red（AC-09/10/21）",
        ),
        sa.Column(
            "rule_version",
            sa.Text(),
            nullable=False,
            comment="分级规则版本（改阈值必须 +1，health_service 常量区）",
        ),
        sa.Column(
            "review_status",
            sa.Text(),
            nullable=False,
            comment="复核状态：pending/confirmed/corrected",
        ),
        # --- Q-1 裁决补入 2 列 ---
        sa.Column(
            "review_note",
            sa.Text(),
            nullable=True,
            comment="复核操作备注（前端 review-api review_note 字段）",
        ),
        sa.Column(
            "superseded_reason",
            sa.Text(),
            nullable=True,
            comment="版本链替换原因：review_correction/backfill（纯服务端写入）",
        ),
        # --- 版本链 ---
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="版本号（从 1 起，补录 version+1）",
        ),
        sa.Column(
            "superseded_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="被哪个新版本取代（自引用，最新版本为 NULL）",
        ),
        # --- LangGraph 链路（可空） ---
        sa.Column(
            "graph_thread_id",
            sa.Text(),
            nullable=True,
            comment="LangGraph 会话（健康子图写入时回填，独立 thread_id）",
        ),
        # --- 约束 ---
        sa.PrimaryKeyConstraint("record_id", name="pk_health_records"),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_health_records_elder_id_elders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["staff_accounts.staff_id"],
            name="fk_health_records_operator_id_staff_accounts",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["staff_accounts.staff_id"],
            name="fk_health_records_reviewed_by_staff_accounts",
            ondelete="SET NULL",
        ),
        # 血压范围（TD §3.6 1/2）
        sa.CheckConstraint(
            "(systolic BETWEEN 0 AND 300 OR systolic IS NULL)",
            name="ck_health_records_systolic_range",
        ),
        sa.CheckConstraint(
            "(diastolic BETWEEN 0 AND 300 OR diastolic IS NULL)",
            name="ck_health_records_diastolic_range",
        ),
        # 睡眠范围（TD §3.6 3）
        sa.CheckConstraint(
            "(sleep_hours BETWEEN 0 AND 24 OR sleep_hours IS NULL)",
            name="ck_health_records_sleep_range",
        ),
        # 情绪范围（TD §3.6 4）
        sa.CheckConstraint(
            "(mood_score BETWEEN 1 AND 5 OR mood_score IS NULL)",
            name="ck_health_records_mood_range",
        ),
        # 来源枚举（TD §3.6 5）
        sa.CheckConstraint(
            _in_clause("source", _SOURCES), name="ck_health_records_source"
        ),
        # 严重度三值
        sa.CheckConstraint(
            _in_clause("severity", _SEVERITIES), name="ck_health_records_severity"
        ),
        # 复核状态三值
        sa.CheckConstraint(
            _in_clause("review_status", _REVIEW_STATUSES),
            name="ck_health_records_review_status",
        ),
        # Q-1 superseded_reason 两值（可空）
        sa.CheckConstraint(
            f"(superseded_reason IS NULL OR {_in_clause('superseded_reason', _SUPERSEDED_REASONS)})",
            name="ck_health_records_superseded_reason",
        ),
        sa.CheckConstraint("version >= 1", name="ck_health_records_version_positive"),
        comment="健康采集记录（唯一写库入口 health_service.create_health_record）",
    )
    # 索引：医生健康趋势查询 + 版本链自引用
    op.create_index(
        "ix_health_records_elder_id_recorded_at",
        "health_records",
        ["elder_id", sa.text("recorded_at DESC")],
    )

    # ==================== daily_reports（9 列） ====================
    op.create_table(
        "daily_reports",
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="日报编号",
        ),
        sa.Column(
            "elder_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="长者",
        ),
        sa.Column(
            "report_date",
            sa.Date(),
            nullable=False,
            comment="日报日期（本地 Asia/Shanghai 日期）",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="版本号（日报版本链：家属改了偏好→version+1）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="日报状态：normal/away_notice",
        ),
        sa.Column(
            "summary_text",
            sa.Text(),
            nullable=False,
            comment="日报摘要文本（结构化 JSON 存 medication_stats，本字段是家属可读文本）",
        ),
        sa.Column(
            "medication_stats",
            postgresql.JSONB(),
            nullable=True,
            comment="当日用药结构化统计（四类：按时/漏服/待提醒/未录入）",
        ),
        sa.Column(
            "audio_url",
            sa.Text(),
            nullable=True,
            comment="语音合成日报音频 URL（可空：合成失败降级纯文本）",
        ),
        sa.Column(
            "pushed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="首次推送时间（UTC；未推送为 NULL）",
        ),
        # --- 约束 ---
        sa.PrimaryKeyConstraint("report_id", name="pk_daily_reports"),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_daily_reports_elder_id_elders",
            ondelete="CASCADE",
        ),
        # 日报唯一键：同一长者同一日期同一版本只存一条
        sa.UniqueConstraint(
            "elder_id",
            "report_date",
            "version",
            name="uq_daily_reports_unique",
        ),
        sa.CheckConstraint(
            _in_clause("status", _REPORT_STATUSES),
            name="ck_daily_reports_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_daily_reports_version_positive"),
        comment="家属日报（唯一写库入口 report_service.generate_daily_report）",
    )
    # 索引：家属日报列表查询（按日期倒序）
    op.create_index(
        "ix_daily_reports_elder_id_report_date_version",
        "daily_reports",
        ["elder_id", sa.text("report_date DESC"), sa.text("version DESC")],
    )


def downgrade() -> None:
    # 先 drop 所有索引 → 再 drop 表（顺序：daily_reports 先，health_records 后；
    # health_records 被 risk_events 外键引用，先删 daily_reports 不受影响）
    op.drop_index(
        "ix_daily_reports_elder_id_report_date_version", table_name="daily_reports"
    )
    op.drop_index(
        "ix_health_records_elder_id_recorded_at", table_name="health_records"
    )
    op.drop_table("daily_reports")
    op.drop_table("health_records")
