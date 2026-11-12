"""用药 / 风险 / 工单 ORM 六表（迁移 0010，TD line 422-534，第 30-35 张表）。

口径（迁移 0010 逐行对照，AGENTS §4.6：迁移已合入不得改，ORM 必须与 DDL 对齐）：

1. 外键删除策略：业务事实之间（plan→event→attempt、risk→order、order→transition）
   一律 RESTRICT / CASCADE，员工离职（created_by / assignee_id / actor_id）SET NULL。
2. `severity`（work_orders）与 `drug_snapshot`（medication_events）是**建单快照**，
   不随后同步——应用层别"顺手更新"。
3. 枚举全部 CHECK，不用 PG ENUM（裁决 C6）。
4. 幂等键：`UNIQUE(plan_id, scheduled_at)`（medication_events）与
   `UNIQUE(event_id, attempt_no)`（medication_attempts）——支撑 scheduler/Graph 多次触发
   不重复入库，`payload_hash` 支撑同键异内容 → 409。
5. `work_order_transitions` 独立承接工单完整流转历史（FR-ALT-02），与 `audit_logs` 互补。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# ------------------------------------------------------------------ medication_plans


class MedicationPlan(Base):
    """用药计划（医生专属维护）。"""

    __tablename__ = "medication_plans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_medication_plans_elder_id_elders",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["staff_accounts.staff_id"],
            name="fk_medication_plans_created_by_staff_accounts",
            ondelete="RESTRICT",
        ),
        CheckConstraint("status IN ('active', 'stopped')", name="status"),
        Index("ix_medication_plans_elder_id_status", "elder_id", "status"),
        {"comment": "用药计划（医生专属维护）"},
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="计划编号",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id"), nullable=False, comment="长者"
    )
    drug_name: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="药品名（教学演示数据，非真实处方）"
    )
    dose: Mapped[str] = mapped_column(Text(), nullable=False, comment="剂量（如「5mg 一片」）")
    schedule_cron: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="服用时间规则（如 0 8 * * *）"
    )
    start_date: Mapped[datetime.date] = mapped_column(Date(), nullable=False, comment="开始日期")
    end_date: Mapped[datetime.date | None] = mapped_column(
        Date(), nullable=True, comment="结束日期（临近时提醒医生复核续方）"
    )
    version: Mapped[int] = mapped_column(
        Integer(),
        server_default=text("1"),
        nullable=False,
        comment="药单版本（修改生成新版本，历史提醒保留旧快照，AC-27）",
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_accounts.staff_id"),
        nullable=False,
        comment="创建人（仅医生；护工/管理员只读，越权 403 + 审计）",
    )
    status: Mapped[str] = mapped_column(
        Text(), server_default=text("'active'"), nullable=False, comment="active / stopped"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, comment="创建时间"
    )


# ------------------------------------------------------------------ medication_events


class MedicationEvent(Base):
    """用药事件（用药子图的业务事实落库；图运行时在 langgraph schema）。"""

    __tablename__ = "medication_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id"],
            ["medication_plans.plan_id"],
            name="fk_medication_events_plan_id_medication_plans",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_medication_events_elder_id_elders",
            ondelete="CASCADE",
        ),
        CheckConstraint("triggered_by IN ('scheduler', 'dialog')", name="triggered_by"),
        CheckConstraint(
            "status IN ('calling', 'waiting_confirm', 'confirmed', 'escalated')",
            name="status",
        ),
        CheckConstraint(
            "terminal_state IS NULL OR terminal_state IN ('taken_reported', 'refused', "
            "'missed_unconfirmed', 'notification_failed', 'late_taken_reported')",
            name="terminal_state",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN ('no_answer', 'denied', 'tts_failed', "
            "'offline', 'rtc_failed')",
            name="failure_reason",
        ),
        CheckConstraint(
            "confirm_channel IS NULL OR confirm_channel IN ('voice', 'button')",
            name="confirm_channel",
        ),
        UniqueConstraint(
            "plan_id",
            "scheduled_at",
            "plan_version",
            name="uq_medication_events_plan_id_scheduled_at_plan_version",
        ),
        Index(
            "ix_medication_events_elder_id_scheduled_at",
            "elder_id",
            "scheduled_at",
        ),
        {"comment": "用药事件（用药子图的业务事实落库；图运行时在 langgraph schema）"},
    )

    reminder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="事件编号（PRD 称 reminder；不叫 event_id，避免与 risk_events 同名不同义）",
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medication_plans.plan_id"), nullable=False, comment="对应计划"
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id"),
        nullable=False,
        comment="冗余存长者（按人查询统计用，不进幂等键）",
    )
    scheduled_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="应服时间 T0"
    )
    plan_version: Mapped[int] = mapped_column(
        Integer(),
        server_default=text("1"),
        nullable=False,
        comment="药单版本（与 medication_plans.version 联动；改药单后新事件自动升级到新版本）",
    )
    drug_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, comment="药品剂量快照（药单改版不影响历史）"
    )
    payload_hash: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="drug_snapshot 的 hash；同键不同内容 → 409 的判定依据"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="事件生成时间（与 scheduled_at 区分，便于排查）",
    )
    triggered_by: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="scheduler 定时 / dialog 对话内（AC-35 同一张图）"
    )
    status: Mapped[str] = mapped_column(
        Text(),
        server_default=text("'calling'"),
        nullable=False,
        comment="calling / waiting_confirm / confirmed / escalated",
    )
    terminal_state: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="taken_reported / refused / missed_unconfirmed / notification_failed（四值互斥）",
    )
    late_taken: Mapped[bool] = mapped_column(
        Boolean(),
        server_default=text("false"),
        nullable=False,
        comment="补确认标记（追加不覆盖原终态、工单不自动关闭，AC-08）",
    )
    failure_reason: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="no_answer / denied / tts_failed / offline"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer(), server_default=text("0"), nullable=False, comment="已重试次数（≤3，T+5/+10/+15）"
    )
    confirmed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="确认时间"
    )
    confirm_channel: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="voice / button（已服自报渠道留痕）"
    )
    graph_thread_id: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="回指 PostgreSQL checkpointer 图状态（可 graph.get_state 复盘）",
    )


# ------------------------------------------------------------------ medication_attempts


class MedicationAttempt(Base):
    """触达记录（四次触达逐条留痕）。"""

    __tablename__ = "medication_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id"],
            ["medication_events.reminder_id"],
            name="fk_medication_attempts_event_id_medication_events",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            # 白名单含三态投递回执（E2，0012）与 played 留痕（TD-2，0013）：
            # voice_dispatched=任一设备收下、device_offline=有绑定但全离线、
            # no_bound_device=没绑设备（运维缺口）、played=触达语音真被听到
            # （只允许从 voice_dispatched 升级）。前三值为 0010 占位口径，保留兼容已有行。
            "receipt IS NULL OR receipt IN ('card_rendered', 'tts_finished', 'none', "
            "'voice_dispatched', 'device_offline', 'no_bound_device', 'played')",
            name="receipt",
        ),
        CheckConstraint(
            "tts_result IS NULL OR tts_result IN ('ok', 'tts_failed')", name="tts_result"
        ),
        CheckConstraint(
            "response_channel IS NULL OR response_channel IN ('voice', 'button', 'none')",
            name="response_channel",
        ),
        UniqueConstraint(
            "event_id",
            "attempt_no",
            name="uq_medication_attempts_event_id_attempt_no",
        ),
        {"comment": "触达记录（四次触达逐条留痕）"},
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="记录编号",
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medication_events.reminder_id"),
        nullable=False,
        comment="所属用药事件（medication_events.reminder_id）",
    )
    attempt_no: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="第几次触达（1=T0，2=T+5，3=T+10，4=T+15+终态分流）"
    )
    sent_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, comment="发送时间"
    )
    receipt: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="页面回执 card_rendered / tts_finished / none（「提醒送达」判定依据）；"
        "投递三态 voice_dispatched / device_offline / no_bound_device（E2）；"
        "played = 触达语音真被听到（TD-2，仅从 voice_dispatched 升级）",
    )
    tts_result: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="ok / tts_failed（卡片成功但 TTS 失败 = 已送达 + 追加 tts_failed）",
    )
    response: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="长者响应摘要（脱敏文本）"
    )
    response_channel: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="voice / button / none"
    )


# ------------------------------------------------------------------ risk_events


class RiskEvent(Base):
    """风险事件（管分级与升级；2 分钟无人确认 → 原事件标 escalated，不建第二个事件）。"""

    __tablename__ = "risk_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_risk_events_elder_id_elders",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "type IN ('red_emergency', 'health_alert', 'med_risk', 'sensitive', 'sos')",
            name="type",
        ),
        CheckConstraint("severity IN ('red', 'yellow')", name="severity"),
        CheckConstraint("status IN ('open', 'escalated', 'closed')", name="status"),
        Index("ix_risk_events_elder_id_created_at", "elder_id", "created_at"),
        Index("ix_risk_events_status_severity", "status", "severity"),
        {
            "comment": "风险事件（管分级与升级；2 分钟无人确认 → 原事件标 escalated，不建第二个事件）"
        },
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="事件编号",
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id"), nullable=False, comment="长者"
    )
    type: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="red_emergency / health_alert / med_risk / sensitive / sos",
    )
    severity: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="red / yellow（多项并存取最高；黄可升红）"
    )
    source_type: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="来源类型（health_record / medication_event / conversation_thread）",
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="来源 ID"
    )
    status: Mapped[str] = mapped_column(
        Text(), server_default=text("'open'"), nullable=False, comment="open / escalated / closed"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="生成时间（升级计时起点，不因通知失败暂停）",
    )
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="首次确认时间"
    )
    closed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="关闭时间"
    )


# ------------------------------------------------------------------ work_orders


class WorkOrder(Base):
    """工单（护工看板数据源；severity 为建单快照）。"""

    __tablename__ = "work_orders"
    __table_args__ = (
        ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_work_orders_elder_id_elders",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["event_id"],
            ["risk_events.event_id"],
            name="fk_work_orders_event_id_risk_events",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["assignee_id"],
            ["staff_accounts.staff_id"],
            name="fk_work_orders_assignee_id_staff_accounts",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "type IN ('missed_medication', 'sensitive_alert', 'health_alert', 'device_fault', "
            "'sos', 'collection_missed', 'drug_question', 'follow_up')",
            name="type",
        ),
        CheckConstraint("severity IN ('red', 'yellow')", name="severity"),
        CheckConstraint(
            "status IN ('new', 'acknowledged', 'in_progress', 'resolved', 'closed', 'reopened')",
            name="status",
        ),
        UniqueConstraint("order_no", name="uq_work_orders_order_no"),
        Index(
            "ix_work_orders_status_severity_created_at",
            "status",
            "severity",
            "created_at",
        ),
        Index("ix_work_orders_elder_id_status", "elder_id", "status"),
        {"comment": "工单（护工看板数据源；severity 为建单快照）"},
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="工单编号",
    )
    order_no: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="人类可读编号（业务字段非主键，唯一）"
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id"), nullable=False, comment="长者"
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_events.event_id"), nullable=True, comment="来源风险事件"
    )
    type: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="missed_medication / sensitive_alert / health_alert / device_fault / sos / collection_missed / drug_question / follow_up",
    )
    severity: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="严重度（建单时从 risk_events 复制的快照，不随后同步）",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        server_default=text("'new'"),
        nullable=False,
        comment="new / acknowledged / in_progress / resolved / closed / reopened",
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("staff_accounts.staff_id"), nullable=True, comment="当前处理护工"
    )
    false_alarm: Mapped[bool] = mapped_column(
        Boolean(),
        server_default=text("false"),
        nullable=False,
        comment="误报标记（关误报必填原因并保留原始输入）",
    )
    false_alarm_reason: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="误报原因"
    )
    actual_handled_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="实际处置时间（红色事件必填）"
    )
    note: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="处置备注（护工自证留痕）"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="创建时间",
    )
    closed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="关闭时间"
    )


# ------------------------------------------------------------------ work_order_transitions


class WorkOrderTransition(Base):
    """工单状态变更历史（FR-ALT-02：任一工单可查完整流转）。"""

    __tablename__ = "work_order_transitions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id"],
            ["work_orders.order_id"],
            name="fk_work_order_transitions_order_id_work_orders",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["actor_id"],
            ["staff_accounts.staff_id"],
            name="fk_work_order_transitions_actor_id_staff_accounts",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "from_status IN ('new', 'acknowledged', 'in_progress', 'resolved', 'closed', 'reopened')",
            name="from_status",
        ),
        CheckConstraint(
            "to_status IN ('new', 'acknowledged', 'in_progress', 'resolved', 'closed', 'reopened')",
            name="to_status",
        ),
        Index("ix_work_order_transitions_order_id_at", "order_id", "at"),
        {"comment": "工单状态变更历史（FR-ALT-02：任一工单可查完整流转）"},
    )

    transition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="记录编号",
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_orders.order_id"), nullable=False, comment="所属工单"
    )
    from_status: Mapped[str] = mapped_column(Text(), nullable=False, comment="变更前状态")
    to_status: Mapped[str] = mapped_column(Text(), nullable=False, comment="变更后状态")
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("staff_accounts.staff_id"),
        nullable=True,
        comment="操作人（系统自动流转可空）",
    )
    at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, comment="变更时间"
    )
    note: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="备注（如 reopen 原因）"
    )


__all__ = [
    "MedicationAttempt",
    "MedicationEvent",
    "MedicationPlan",
    "RiskEvent",
    "WorkOrder",
    "WorkOrderTransition",
]
