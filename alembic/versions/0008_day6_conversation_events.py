"""Day 6 · 会话事件三表：conversation_events / conversation_turns / tool_executions（0008）

**为什么这一个迁移**（计划 `docs/plans/day06/day-06-长者侧Agent闭环.md` §9.1 ①②③，裁决 D-1）：

Day 6 的主线是「长者侧 Agent 完整闭环」，其三条硬需求在现有 schema 上无处安放：

1. **历史要能复盘「改口」**——原 `RedisTurnStore` 只存 `{user, assistant}` 二元组，
   打断、partial、更正全部丢失。`conversation_events` 以**只追加**方式记录 12 类事件，
   更正走新事件 + `ref_event_id` 指向旧事件，不 UPDATE 旧行。
2. **上下文只能拿「已送达」文本**——`conversation_turns.assistant_delivered_text` 是投影，
   生成全文留在事件 `payload_json`；两者混写会让模型以为自己说过没播的话。
3. **副作用幂等**——`tool_executions.idempotency_key` 唯一，同键同参返回上次结果，
   是 `MAX_TOOL_CALLS` 与「重复调用检测」的物理保证。

另外给 `conversation_threads` 增加 `last_event_seq`：`seq` 必须**行锁分配**
（`SELECT … FOR UPDATE` 锁 thread 行再 +1），禁止「先查 max 再 +1」，因此需要一个
可被锁的单调计数器列。

已合入迁移 0001–0007 一字不改（AGENTS §4.6）。

人工审查要点（AGENTS §5）：
- 三表均**只追加**：本迁移不提供任何 UPDATE/DELETE 语义，也不建触发器去模拟；
- `seq` 与 `thread_id` 组合唯一，跨会话互不干扰；
- `conversation_turns.elder_id` 可空（generic 模式），NOT NULL 会让通用模式建不出轮次；
- downgrade 逆序删表并移除 `last_event_seq`，可回滚到 0007。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_day6_conversation_events"
down_revision: str | None = "0007_admin_org_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 事件类型 12 值（与 docs/team_tasks/day06/contracts.md §3 一一对应）
_EVENT_TYPES = (
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


def _in_list(column: str, values: Sequence[str]) -> str:
    """拼 `col IN ('a', 'b')`；值全部来自代码常量，不接受外部输入。"""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    # ---------------- 0. 事件序号计数器（被行锁的列） ----------------
    op.add_column(
        "conversation_threads",
        sa.Column(
            "last_event_seq",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="会话内最后事件序号（seq 分配靠 SELECT … FOR UPDATE 锁本行后 +1）",
        ),
    )

    # ---------------- 1. conversation_events（第 28 张表） ----------------
    op.create_table(
        "conversation_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="事件编号",
        ),
        sa.Column("thread_id", sa.Text(), nullable=False, comment="所属会话（闸门①）"),
        sa.Column(
            "seq",
            sa.BigInteger(),
            nullable=False,
            comment="会话内递增业务顺序号；UNIQUE(thread_id, seq)；行锁分配，不用 id/时间戳",
        ),
        sa.Column(
            "event_type",
            sa.Text(),
            nullable=False,
            comment="事件类型（12 值 CHECK）",
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="事件发生时间（UTC 存储）",
        ),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="事件载荷（生成全文、槽位、工具摘要等；落库前脱敏）",
        ),
        sa.Column("call_id", sa.Text(), nullable=True, comment="一次建连标识"),
        sa.Column("turn_id", sa.Text(), nullable=True, comment="一轮对话标识"),
        sa.Column("response_id", sa.Text(), nullable=True, comment="一次回复标识（多段共用）"),
        sa.Column("task_id", sa.Text(), nullable=True, comment="一个业务任务标识（用药确认等）"),
        sa.Column("trace_id", sa.Text(), nullable=True, comment="串 OTel / 日志"),
        sa.Column(
            "ref_event_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="更正/替代类事件指向旧事件；不覆盖旧行",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_conversation_events"),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["conversation_threads.thread_id"],
            name="fk_conversation_events_thread_id_conversation_threads",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_in_list("event_type", _EVENT_TYPES), name="event_type"),
        comment="会话事件流水（只追加，不改写；复盘与送达判定的唯一事实源）",
    )
    # 唯一性用唯一索引表达：naming_convention 的 uq 模板按列名生成，显式名会被覆盖
    op.create_index(
        "uq_conversation_events_thread_id_seq",
        "conversation_events",
        ["thread_id", "seq"],
        unique=True,
    )
    op.create_index("ix_conversation_events_turn_id", "conversation_events", ["turn_id"])
    op.create_index("ix_conversation_events_response_id", "conversation_events", ["response_id"])

    # ---------------- 2. conversation_turns（第 29 张表） ----------------
    op.create_table(
        "conversation_turns",
        sa.Column("turn_id", sa.Text(), nullable=False, comment="轮次编号（服务端生成）"),
        sa.Column("thread_id", sa.Text(), nullable=False, comment="所属会话"),
        sa.Column(
            "elder_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="说话人；generic 模式为 NULL",
        ),
        sa.Column("call_id", sa.Text(), nullable=True, comment="建连标识"),
        sa.Column(
            "user_text",
            sa.Text(),
            nullable=False,
            comment="只存 ASR final；partial 不入库",
        ),
        sa.Column(
            "assistant_delivered_text",
            sa.Text(),
            nullable=True,
            comment="只存已送达文本；生成全文在 conversation_events.payload_json",
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'open'"),
            nullable=False,
            comment="open / completed / interrupted / failed",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="开始时间",
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True, comment="结束时间"),
        sa.PrimaryKeyConstraint("turn_id", name="pk_conversation_turns"),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["conversation_threads.thread_id"],
            name="fk_conversation_turns_thread_id_conversation_threads",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["elder_id"],
            ["elders.elder_id"],
            name="fk_conversation_turns_elder_id_elders",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'completed', 'interrupted', 'failed')",
            name="status",
        ),
        comment="轮次投影（供上下文读取；只存已送达文本）",
    )
    op.create_index(
        "ix_conversation_turns_thread_id_started_at",
        "conversation_turns",
        ["thread_id", sa.text("started_at DESC")],
    )

    # ---------------- 3. tool_executions（第 30 张表） ----------------
    op.create_table(
        "tool_executions",
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="执行编号",
        ),
        sa.Column("turn_id", sa.Text(), nullable=True, comment="归属轮次"),
        sa.Column("task_id", sa.Text(), nullable=True, comment="归属业务任务"),
        sa.Column("tool_name", sa.Text(), nullable=False, comment="工具名（须在白名单内）"),
        sa.Column(
            "idempotency_key",
            sa.Text(),
            nullable=False,
            comment="幂等键；同键同参返回上次结果",
        ),
        sa.Column(
            "request_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="入参（落库前脱敏）",
        ),
        sa.Column(
            "result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="结果（落库前脱敏）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'running'"),
            nullable=False,
            comment="running / succeeded / failed / denied",
        ),
        sa.Column(
            "error_class",
            sa.Text(),
            nullable=True,
            comment="四类失败：bad_args / not_found / transient / forbidden",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="开始时间",
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True, comment="结束时间"),
        sa.PrimaryKeyConstraint("execution_id", name="pk_tool_executions"),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["conversation_turns.turn_id"],
            name="fk_tool_executions_turn_id_conversation_turns",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'denied')",
            name="status",
        ),
        sa.CheckConstraint(
            "error_class IS NULL "
            "OR error_class IN ('bad_args', 'not_found', 'transient', 'forbidden')",
            name="error_class",
        ),
        comment="工具执行记录（副作用幂等的物理保证）",
    )
    op.create_index(
        "uq_tool_executions_idempotency_key",
        "tool_executions",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index("ix_tool_executions_turn_id", "tool_executions", ["turn_id"])
    op.create_index(
        "ix_tool_executions_tool_name_started_at",
        "tool_executions",
        ["tool_name", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_executions_tool_name_started_at", table_name="tool_executions")
    op.drop_index("ix_tool_executions_turn_id", table_name="tool_executions")
    op.drop_index("uq_tool_executions_idempotency_key", table_name="tool_executions")
    op.drop_table("tool_executions")

    op.drop_index("ix_conversation_turns_thread_id_started_at", table_name="conversation_turns")
    op.drop_table("conversation_turns")

    op.drop_index("ix_conversation_events_response_id", table_name="conversation_events")
    op.drop_index("ix_conversation_events_turn_id", table_name="conversation_events")
    op.drop_index("uq_conversation_events_thread_id_seq", table_name="conversation_events")
    op.drop_table("conversation_events")

    op.drop_column("conversation_threads", "last_event_seq")
