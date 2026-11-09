"""会话域 ORM：`conversation_threads`（Day 4）+ 事件三表（Day 6，第 28–30 张）。

列注释逐字对齐迁移：
- `alembic/versions/0004_day4_voice_threads.py`（会话隔离，闸门①）
- `alembic/versions/0008_day6_conversation_events.py`（事件流水 / 轮次投影 / 工具执行）

口径要点（缺一条就会在复盘时失真）：

1. **事件只追加**：`conversation_events` 没有任何 UPDATE 语义，更正走新事件 + `ref_event_id`
   指向旧行。ORM 层也不提供 `update()` 便捷方法 —— 想改就得显式写 SQL，写之前会先看到本注释。
2. **`seq` 靠行锁分配**：计数器列 `conversation_threads.last_event_seq` 的存在就是为了被
   `SELECT … FOR UPDATE` 锁住再 +1。**禁止** `SELECT max(seq)+1`：两个并发写会拿到同一个号，
   然后撞 `uq_conversation_events_thread_id_seq`（撞了还算幸运，最坏是重排）。
3. **`assistant_delivered_text` 只存已送达**：生成全文在 `conversation_events.payload_json`。
   两者混写，模型会以为自己说过「被打断时其实没播」的那半句。
4. **`conversation_turns.elder_id` 可空**：generic 模式（身份未确认）没有 elder_id，NOT NULL
   会让通用模式建不出轮次。
5. 索引与约束名**逐一写死**，与 0008 里的显式名一致；否则 `alembic check` 每次都会冒出
   「drop index + create index」的假 diff。
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: 事件类型 12 值（与 `contracts.md` §3、0008 迁移的 CHECK 同源）
#: 服务层校验与 DB CHECK 共用一份常量，避免「代码放行、库拒绝」的错位。
EVENT_TYPES: tuple[str, ...] = (
    "turn_started",
    "asr_final_committed",
    "response_started",
    "response_segment_delivered",
    "response_interrupted",
    "response_completed",
    "tool_started",
    "tool_succeeded",
    "tool_failed",
    "task_waiting",
    "task_resumed",
    "turn_completed",
)

#: 轮次状态四值
TURN_STATUSES: tuple[str, ...] = ("open", "completed", "interrupted", "failed")

#: 工具执行状态四值
TOOL_STATUSES: tuple[str, ...] = ("running", "succeeded", "failed", "denied")

#: 工具失败四分类（contracts.md §5.3）
TOOL_ERROR_CLASSES: tuple[str, ...] = ("bad_args", "not_found", "transient", "forbidden")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """拼 `col IN ('a', 'b')`。值来自本模块常量，不接受外部输入。"""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ConversationThread(Base):
    """会话隔离表：thread_id 闸门①的唯一落点。"""

    __tablename__ = "conversation_threads"
    __table_args__ = (
        CheckConstraint(
            "identity_source IN ('device', 'voice', 'confirm', 'generic')",
            name="identity_source",
        ),
        CheckConstraint(
            "(identity_source = 'generic' AND elder_id IS NULL) "
            "OR (identity_source IN ('device', 'voice', 'confirm') AND elder_id IS NOT NULL)",
            name="identity_source_elder_id",
        ),
        {"comment": "会话隔离表（闸门①：thread_id 服务端生成，elder_id 强制过滤所有下游数据访问）"},
    )

    thread_id: Mapped[str] = mapped_column(
        Text(),
        primary_key=True,
        nullable=False,
        comment="会话 ID：服务端按 (elder_id, session_seq) 生成，前端不可指定",
    )
    elder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("elders.elder_id", ondelete="SET NULL"),
        nullable=True,
        comment="识别出的说话人；generic 降级模式为 NULL（FR-VPR-03）",
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.device_id", ondelete="RESTRICT"),
        nullable=False,
        comment="实际使用的设备",
    )
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="会话开始时间",
    )
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="会话结束时间（换人/退出发起新 thread 时回填）",
    )
    identity_source: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="device 设备锁定/voice 声纹/confirm 主动确认/generic 通用模式",
    )
    identity_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=4, scale=3),
        nullable=True,
        comment="声纹置信度（cosine 相似度；generic 模式 NULL）",
    )
    last_event_seq: Mapped[int] = mapped_column(
        BigInteger(),
        server_default=text("0"),
        nullable=False,
        comment="会话内最后事件序号（seq 分配靠 SELECT … FOR UPDATE 锁本行后 +1）",
    )


class ConversationEvent(Base):
    """事件流水（只追加）。

    `seq` 是**会话内**业务顺序号，不是全局序号；跨会话不比较大小。
    更正类事件（`*_corrected` 等）带 `ref_event_id` 指向原事件，原事件永不修改。
    """

    __tablename__ = "conversation_events"
    __table_args__ = (
        Index("uq_conversation_events_thread_id_seq", "thread_id", "seq", unique=True),
        Index("ix_conversation_events_turn_id", "turn_id"),
        Index("ix_conversation_events_response_id", "response_id"),
        CheckConstraint(_check_in("event_type", EVENT_TYPES), name="event_type"),
        {"comment": "会话事件流水（只追加，不改写；复盘与送达判定的唯一事实源）"},
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
        comment="事件编号",
    )
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_threads.thread_id", ondelete="CASCADE"),
        nullable=False,
        comment="所属会话（闸门①）",
    )
    seq: Mapped[int] = mapped_column(
        BigInteger(),
        nullable=False,
        comment="会话内递增业务顺序号；UNIQUE(thread_id, seq)；行锁分配，不用 id/时间戳",
    )
    event_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="事件类型（12 值 CHECK）"
    )
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="事件发生时间（UTC 存储）",
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        server_default=text("'{}'::jsonb"),
        nullable=False,
        comment="事件载荷（生成全文、槽位、工具摘要等；落库前脱敏）",
    )
    call_id: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="一次建连标识")
    turn_id: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="一轮对话标识")
    response_id: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="一次回复标识（多段共用）"
    )
    task_id: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="一个业务任务标识（用药确认等）"
    )
    trace_id: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="串 OTel / 日志")
    ref_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="更正/替代类事件指向旧事件；不覆盖旧行",
    )


class ConversationTurn(Base):
    """轮次投影（供上下文读取，不承担审计职责）。

    `assistant_delivered_text` 只存**已送达**文本；同一轮多次回复（`response_id` 不同）
    以最后一次 `commit_response` 覆盖投影，事件流水里仍保留每次的生成全文。
    """

    __tablename__ = "conversation_turns"
    __table_args__ = (
        Index(
            "ix_conversation_turns_thread_id_started_at",
            "thread_id",
            text("started_at DESC"),
        ),
        CheckConstraint(_check_in("status", TURN_STATUSES), name="status"),
        {"comment": "轮次投影（供上下文读取；只存已送达文本）"},
    )

    turn_id: Mapped[str] = mapped_column(
        Text(), primary_key=True, nullable=False, comment="轮次编号（服务端生成）"
    )
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_threads.thread_id", ondelete="CASCADE"),
        nullable=False,
        comment="所属会话",
    )
    elder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("elders.elder_id", ondelete="SET NULL"),
        nullable=True,
        comment="说话人；generic 模式为 NULL",
    )
    call_id: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="建连标识")
    user_text: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="只存 ASR final；partial 不入库"
    )
    assistant_delivered_text: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="只存已送达文本；生成全文在 conversation_events.payload_json",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        server_default=text("'open'"),
        nullable=False,
        comment="open / completed / interrupted / failed",
    )
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="开始时间",
    )
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="结束时间"
    )


class ToolExecution(Base):
    """工具执行记录（副作用幂等的物理保证）。

    `idempotency_key` 唯一：同键**同参**返回上次结果（`idempotent_replay=True`）；
    同键**异参**视为调用方拼键错误，直接拒绝（否则一次误配就会静默吞掉新请求）。
    """

    __tablename__ = "tool_executions"
    __table_args__ = (
        Index("uq_tool_executions_idempotency_key", "idempotency_key", unique=True),
        Index("ix_tool_executions_turn_id", "turn_id"),
        Index(
            "ix_tool_executions_tool_name_started_at",
            "tool_name",
            text("started_at DESC"),
        ),
        CheckConstraint(_check_in("status", TOOL_STATUSES), name="status"),
        CheckConstraint(
            "error_class IS NULL " + f"OR {_check_in('error_class', TOOL_ERROR_CLASSES)}",
            name="error_class",
        ),
        {"comment": "工具执行记录（副作用幂等的物理保证）"},
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
        comment="执行编号",
    )
    turn_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_turns.turn_id", ondelete="SET NULL"),
        nullable=True,
        comment="归属轮次",
    )
    task_id: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="归属业务任务")
    tool_name: Mapped[str] = mapped_column(Text(), nullable=False, comment="工具名（须在白名单内）")
    idempotency_key: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="幂等键；同键同参返回上次结果"
    )
    request_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()), nullable=False, comment="入参（落库前脱敏）"
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=Text()), nullable=True, comment="结果（落库前脱敏）"
    )
    status: Mapped[str] = mapped_column(
        Text(),
        server_default=text("'running'"),
        nullable=False,
        comment="running / succeeded / failed / denied",
    )
    error_class: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="四类失败：bad_args / not_found / transient / forbidden",
    )
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="开始时间",
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="结束时间"
    )


__all__ = [
    "EVENT_TYPES",
    "TOOL_ERROR_CLASSES",
    "TOOL_STATUSES",
    "TURN_STATUSES",
    "ConversationEvent",
    "ConversationThread",
    "ConversationTurn",
    "ToolExecution",
]
