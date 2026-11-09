"""Day 6 · 用药与风险六表：medication_plans / medication_events / medication_attempts /
risk_events / work_orders / work_order_transitions（0010）

**为什么这一个迁移**（计划 `docs/plans/day06/day-06-长者侧Agent闭环.md` §3.1 N09 + 裁决 C-2）：

原排 Day 7 的用药与工单六表，按 2026-11-09 裁决提前到 Day 6——因为用药确认子图
（`medication_confirmation_graph`）本日就要落地，没有业务表它无处落事实：

1. `medication_plans`——药单是**医生专属**维护（PRD §4.5），`created_by` 必填；
2. `medication_events`——幂等键 `UNIQUE(plan_id, scheduled_at)`；`payload_hash` 支撑
   「同键不同内容 → 409」（FR-MED-04）；`terminal_state` 四值互斥，**不得互相混写**；
3. `medication_attempts`——四次触达逐条留痕，`UNIQUE(event_id, attempt_no)` 是触达幂等键；
4. `risk_events` 与 `work_orders` **分表**：事件管分级与升级（2 分钟无人确认 → 原事件标
   `escalated`，不建第二个事件），工单管处置流转；
5. `work_orders.severity` 是**建单时的快照**，不随后同步——否则 AC-22「黄升红」时两表会漂移；
6. `work_order_transitions` 承接「任一工单可查完整状态变更历史」（FR-ALT-02），
   `audit_logs` 只管敏感操作，用它兜会漏。

已合入迁移 0001–0009 一字不改。

人工审查要点（AGENTS §5）：
- 枚举全部 CHECK，不用 PG ENUM（裁决 C6）；
- 外键删除策略：药单/事件/工单之间 `RESTRICT`（不允许静默丢业务事实），员工离职 `SET NULL`；
- `drug_snapshot` 与 `severity` 都是快照语义，**不要**在应用层「顺手同步」；
- downgrade 逆序删表，可回滚到 0009。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_day6_medication"
down_revision: str | None = "0009_day6_memory_chunks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLAN_STATUS = ("active", "stopped")
_TRIGGERED_BY = ("scheduler", "dialog")
_EVENT_STATUS = ("calling", "waiting_confirm", "confirmed", "escalated")
_TERMINAL_STATE = (
    "taken_reported",
    "refused",
    "missed_unconfirmed",
    "notification_failed",
)
_FAILURE_REASON = ("no_answer", "denied", "tts_failed", "offline")
_CONFIRM_CHANNEL = ("voice", "button")
_RECEIPT = ("card_rendered", "tts_finished", "none")
_TTS_RESULT = ("ok", "tts_failed")
_RESPONSE_CHANNEL = ("voice", "button", "none")
_RISK_TYPE = ("red_emergency", "health_alert", "med_risk", "sensitive", "sos")
_RISK_SEVERITY = ("red", "yellow")
_RISK_STATUS = ("open", "escalated", "closed")
_ORDER_TYPE = (
    "missed_medication",
    "sensitive_alert",
    "health_alert",
    "device_fault",
    "sos",
    "collection_missed",
    "drug_question",
    "follow_up",
)
_ORDER_SEVERITY = ("red", "yellow")
_ORDER_STATUS = ("new", "acknowledged", "in_progress", "resolved", "closed", "reopened")


def _in_list(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    # ---------------- 1. medication_plans ----------------
    op.create_table(
        "medication_plans",
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="计划编号",
        ),
        sa.Column("elder_id", postgresql.UUID(as_uuid=True), nullable=False, comment="长者"),
        sa.Column(
            "drug_name", sa.Text(), nullable=False, comment="药品名（教学演示数据，非真实处方）"
        ),
        sa.Column("dose", sa.Text(), nullable=False, comment="剂量（如「5mg 一片」）"),
        sa.Column(
            "schedule_cron", sa.Text(), nullable=False, comment="服用时间规则（如 0 8 * * *）"
        ),
        sa.Column("start_date", sa.Date(), nullable=False, comment="开始日期"),
        sa.Column(
            "end_date", sa.Date(), nullable=True, comment="结束日期（临近时提醒医生复核续方）"
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
            comment="药单版本（修改生成新版本，历史提醒保留旧快照，AC-27）",
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="创建人（仅医生；护工/管理员只读，越权 403 + 审计）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="active / stopped",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="创建时间",
        ),
        sa.PrimaryKeyConstraint("plan_id", name="pk_medication_plans"),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_medication_plans_elder_id_elders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["staff_accounts.staff_id"],
            name="fk_medication_plans_created_by_staff_accounts",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_in_list("status", _PLAN_STATUS), name="status"),
        comment="用药计划（医生专属维护）",
    )
    op.create_index(
        "ix_medication_plans_elder_id_status", "medication_plans", ["elder_id", "status"]
    )

    # ---------------- 2. medication_events ----------------
    op.create_table(
        "medication_events",
        sa.Column(
            "reminder_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="事件编号（PRD 称 reminder；不叫 event_id，避免与 risk_events 同名不同义）",
        ),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False, comment="对应计划"),
        sa.Column(
            "elder_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="冗余存长者（按人查询统计用，不进幂等键）",
        ),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="应服时间 T0",
        ),
        sa.Column(
            "drug_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="药品剂量快照（药单改版不影响历史）",
        ),
        sa.Column(
            "payload_hash",
            sa.Text(),
            nullable=False,
            comment="drug_snapshot 的 hash；同键不同内容 → 409 的判定依据",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="事件生成时间（与 scheduled_at 区分，便于排查）",
        ),
        sa.Column(
            "triggered_by",
            sa.Text(),
            nullable=False,
            comment="scheduler 定时 / dialog 对话内（AC-35 同一张图）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'calling'"),
            nullable=False,
            comment="calling / waiting_confirm / confirmed / escalated",
        ),
        sa.Column(
            "terminal_state",
            sa.Text(),
            nullable=True,
            comment="taken_reported / refused / missed_unconfirmed / notification_failed（四值互斥）",
        ),
        sa.Column(
            "late_taken",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="补确认标记（追加不覆盖原终态、工单不自动关闭，AC-08）",
        ),
        sa.Column(
            "failure_reason",
            sa.Text(),
            nullable=True,
            comment="no_answer / denied / tts_failed / offline",
        ),
        sa.Column(
            "retry_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="已重试次数（≤3，T+5/+10/+15）",
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True, comment="确认时间"),
        sa.Column(
            "confirm_channel",
            sa.Text(),
            nullable=True,
            comment="voice / button（已服自报渠道留痕）",
        ),
        sa.Column(
            "graph_thread_id",
            sa.Text(),
            nullable=True,
            comment="回指 PostgreSQL checkpointer 图状态（可 graph.get_state 复盘）",
        ),
        sa.PrimaryKeyConstraint("reminder_id", name="pk_medication_events"),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["medication_plans.plan_id"],
            name="fk_medication_events_plan_id_medication_plans",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_medication_events_elder_id_elders",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_in_list("triggered_by", _TRIGGERED_BY), name="triggered_by"),
        sa.CheckConstraint(_in_list("status", _EVENT_STATUS), name="status"),
        sa.CheckConstraint(
            "terminal_state IS NULL OR " + _in_list("terminal_state", _TERMINAL_STATE),
            name="terminal_state",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR " + _in_list("failure_reason", _FAILURE_REASON),
            name="failure_reason",
        ),
        sa.CheckConstraint(
            "confirm_channel IS NULL OR " + _in_list("confirm_channel", _CONFIRM_CHANNEL),
            name="confirm_channel",
        ),
        comment="用药事件（用药子图的业务事实落库；图运行时在 langgraph schema）",
    )
    op.create_index(
        "uq_medication_events_plan_id_scheduled_at",
        "medication_events",
        ["plan_id", "scheduled_at"],
        unique=True,
    )
    op.create_index(
        "ix_medication_events_elder_id_scheduled_at",
        "medication_events",
        ["elder_id", "scheduled_at"],
    )

    # ---------------- 3. medication_attempts ----------------
    op.create_table(
        "medication_attempts",
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="记录编号",
        ),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="所属用药事件（medication_events.reminder_id）",
        ),
        sa.Column(
            "attempt_no",
            sa.Integer(),
            nullable=False,
            comment="第几次触达（1=T0，2=T+5，3=T+10，4=T+15+终态分流）",
        ),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="发送时间",
        ),
        sa.Column(
            "receipt",
            sa.Text(),
            nullable=True,
            comment="页面回执 card_rendered / tts_finished / none（「提醒送达」判定依据）",
        ),
        sa.Column(
            "tts_result",
            sa.Text(),
            nullable=True,
            comment="ok / tts_failed（卡片成功但 TTS 失败 = 已送达 + 追加 tts_failed）",
        ),
        sa.Column("response", sa.Text(), nullable=True, comment="长者响应摘要（脱敏文本）"),
        sa.Column(
            "response_channel",
            sa.Text(),
            nullable=True,
            comment="voice / button / none",
        ),
        sa.PrimaryKeyConstraint("attempt_id", name="pk_medication_attempts"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["medication_events.reminder_id"],
            name="fk_medication_attempts_event_id_medication_events",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "receipt IS NULL OR " + _in_list("receipt", _RECEIPT),
            name="receipt",
        ),
        sa.CheckConstraint(
            "tts_result IS NULL OR " + _in_list("tts_result", _TTS_RESULT),
            name="tts_result",
        ),
        sa.CheckConstraint(
            "response_channel IS NULL OR " + _in_list("response_channel", _RESPONSE_CHANNEL),
            name="response_channel",
        ),
        comment="触达记录（四次触达逐条留痕）",
    )
    op.create_index(
        "uq_medication_attempts_event_id_attempt_no",
        "medication_attempts",
        ["event_id", "attempt_no"],
        unique=True,
    )

    # ---------------- 4. risk_events ----------------
    op.create_table(
        "risk_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="事件编号",
        ),
        sa.Column("elder_id", postgresql.UUID(as_uuid=True), nullable=False, comment="长者"),
        sa.Column(
            "type",
            sa.Text(),
            nullable=False,
            comment="red_emergency / health_alert / med_risk / sensitive / sos",
        ),
        sa.Column(
            "severity",
            sa.Text(),
            nullable=False,
            comment="red / yellow（多项并存取最高；黄可升红）",
        ),
        sa.Column(
            "source_type",
            sa.Text(),
            nullable=True,
            comment="来源类型（health_record / medication_event / conversation_thread）",
        ),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True, comment="来源 ID"),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'open'"),
            nullable=False,
            comment="open / escalated / closed",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="生成时间（升级计时起点，不因通知失败暂停）",
        ),
        sa.Column(
            "acknowledged_at", sa.DateTime(timezone=True), nullable=True, comment="首次确认时间"
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True, comment="关闭时间"),
        sa.PrimaryKeyConstraint("event_id", name="pk_risk_events"),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_risk_events_elder_id_elders",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_in_list("type", _RISK_TYPE), name="type"),
        sa.CheckConstraint(_in_list("severity", _RISK_SEVERITY), name="severity"),
        sa.CheckConstraint(_in_list("status", _RISK_STATUS), name="status"),
        comment="风险事件（管分级与升级；2 分钟无人确认 → 原事件标 escalated，不建第二个事件）",
    )
    op.create_index("ix_risk_events_elder_id_created_at", "risk_events", ["elder_id", "created_at"])
    op.create_index("ix_risk_events_status_severity", "risk_events", ["status", "severity"])

    # ---------------- 5. work_orders ----------------
    op.create_table(
        "work_orders",
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="工单编号",
        ),
        sa.Column(
            "order_no",
            sa.Text(),
            nullable=False,
            comment="人类可读编号（业务字段非主键，唯一）",
        ),
        sa.Column("elder_id", postgresql.UUID(as_uuid=True), nullable=False, comment="长者"),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="来源风险事件",
        ),
        sa.Column(
            "type",
            sa.Text(),
            nullable=False,
            comment="missed_medication / sensitive_alert / health_alert / device_fault / "
            "sos / collection_missed / drug_question / follow_up",
        ),
        sa.Column(
            "severity",
            sa.Text(),
            nullable=False,
            comment="严重度（建单时从 risk_events 复制的快照，不随后同步）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'new'"),
            nullable=False,
            comment="new / acknowledged / in_progress / resolved / closed / reopened",
        ),
        sa.Column(
            "assignee_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="当前处理护工",
        ),
        sa.Column(
            "false_alarm",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="误报标记（关误报必填原因并保留原始输入）",
        ),
        sa.Column("false_alarm_reason", sa.Text(), nullable=True, comment="误报原因"),
        sa.Column(
            "actual_handled_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="实际处置时间（红色事件必填）",
        ),
        sa.Column("note", sa.Text(), nullable=True, comment="处置备注（护工自证留痕）"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True, comment="关闭时间"),
        sa.PrimaryKeyConstraint("order_id", name="pk_work_orders"),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_work_orders_elder_id_elders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["risk_events.event_id"],
            name="fk_work_orders_event_id_risk_events",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assignee_id"],
            ["staff_accounts.staff_id"],
            name="fk_work_orders_assignee_id_staff_accounts",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(_in_list("type", _ORDER_TYPE), name="type"),
        sa.CheckConstraint(_in_list("severity", _ORDER_SEVERITY), name="severity"),
        sa.CheckConstraint(_in_list("status", _ORDER_STATUS), name="status"),
        comment="工单（护工看板数据源；severity 为建单快照）",
    )
    op.create_index("uq_work_orders_order_no", "work_orders", ["order_no"], unique=True)
    op.create_index(
        "ix_work_orders_status_severity_created_at",
        "work_orders",
        ["status", "severity", "created_at"],
    )
    op.create_index("ix_work_orders_elder_id_status", "work_orders", ["elder_id", "status"])

    # ---------------- 6. work_order_transitions ----------------
    op.create_table(
        "work_order_transitions",
        sa.Column(
            "transition_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="记录编号",
        ),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False, comment="所属工单"),
        sa.Column("from_status", sa.Text(), nullable=False, comment="变更前状态"),
        sa.Column("to_status", sa.Text(), nullable=False, comment="变更后状态"),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="操作人（系统自动流转可空）",
        ),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="变更时间",
        ),
        sa.Column("note", sa.Text(), nullable=True, comment="备注（如 reopen 原因）"),
        sa.PrimaryKeyConstraint("transition_id", name="pk_work_order_transitions"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["work_orders.order_id"],
            name="fk_work_order_transitions_order_id_work_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["staff_accounts.staff_id"],
            name="fk_work_order_transitions_actor_id_staff_accounts",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(_in_list("from_status", _ORDER_STATUS), name="from_status"),
        sa.CheckConstraint(_in_list("to_status", _ORDER_STATUS), name="to_status"),
        comment="工单状态变更历史（FR-ALT-02：任一工单可查完整流转）",
    )
    op.create_index(
        "ix_work_order_transitions_order_id_at",
        "work_order_transitions",
        ["order_id", "at"],
    )


def downgrade() -> None:
    op.drop_index("ix_work_order_transitions_order_id_at", table_name="work_order_transitions")
    op.drop_table("work_order_transitions")

    op.drop_index("ix_work_orders_elder_id_status", table_name="work_orders")
    op.drop_index("ix_work_orders_status_severity_created_at", table_name="work_orders")
    op.drop_index("uq_work_orders_order_no", table_name="work_orders")
    op.drop_table("work_orders")

    op.drop_index("ix_risk_events_status_severity", table_name="risk_events")
    op.drop_index("ix_risk_events_elder_id_created_at", table_name="risk_events")
    op.drop_table("risk_events")

    op.drop_index("uq_medication_attempts_event_id_attempt_no", table_name="medication_attempts")
    op.drop_table("medication_attempts")

    op.drop_index("ix_medication_events_elder_id_scheduled_at", table_name="medication_events")
    op.drop_index("uq_medication_events_plan_id_scheduled_at", table_name="medication_events")
    op.drop_table("medication_events")

    op.drop_index("ix_medication_plans_elder_id_status", table_name="medication_plans")
    op.drop_table("medication_plans")
