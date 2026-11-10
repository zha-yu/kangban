"""Day 7 A3：`decision_logs` 路由决策日志表（ER §4.4.5 / TD §3.4）。

为什么随路由日建表
------------------
它是三级路由的**唯一评测数据源**：意图准确率、敏感召回、四层命中率、P50/P95 全部
从这张表算（AC-03 / AC-31 / AC-36 / AC-37）。没有它，「今天走了哪一层、为什么、多快」
只能靠翻日志文本，无法复跑。

只建表，不动任何历史表
----------------------
`down_revision = "0013"`。0013 及更早的迁移**一行都不改**（AGENTS：主迁移合入后不再改）。

可回滚性
--------
downgrade 先删 3 个索引再删表。表内只有路由日志（可重算的派生数据），
没有其它表的外键指向它，删除不会牵连别的表；但**已写的历史行会丢**，生产降级前应先归档。

两处 CHECK（不用 PG ENUM，见 ER §1 C6）
---------------------------------------
- `route_layer IN ('rule','system_one','agent','fallback','laya')`：`laya` 是 Day 6 及以前的
  历史值，**只读兼容**；新代码只写前四值。去掉它会立刻让历史行违反约束。
- `intent IN (七类)`：意图是产品决策，库层也钉死，避免模型自由生成的新标签悄悄进库。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_decision_logs"
down_revision: str | None = "0013_touch_played_receipt"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

#: 与 `app/models/decision.py` 的 ROUTE_LAYERS / INTENT_VALUES 逐字一致
_ROUTE_LAYERS: tuple[str, ...] = ("rule", "system_one", "agent", "fallback", "laya")
_INTENTS: tuple[str, ...] = (
    "chat",
    "medication",
    "opera",
    "movie",
    "weather",
    "health_checkup",
    "other",
)


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


def upgrade() -> None:
    op.create_table(
        "decision_logs",
        sa.Column(
            "log_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="日志编号",
        ),
        sa.Column(
            "thread_id",
            sa.Text(),
            nullable=False,
            comment="所属会话（串规则→System One→Agent→Tool→TTS）",
        ),
        sa.Column(
            "trace_id",
            sa.Text(),
            nullable=False,
            comment="Langfuse/OTel trace_id，供双向检索",
        ),
        sa.Column(
            "elder_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="长者（generic 轮次无身份，不写本表）",
        ),
        sa.Column(
            "input_text",
            sa.Text(),
            nullable=False,
            comment="用户输入（落库前即脱敏）",
        ),
        sa.Column("intent", sa.Text(), nullable=False, comment="识别意图（七类）"),
        sa.Column(
            "confidence",
            sa.Numeric(4, 3),
            nullable=False,
            comment="置信度（分类器熵归一化值；规则命中为 1.000）",
        ),
        sa.Column(
            "is_sensitive",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="是否命中敏感（G0 或 shadow 信号）",
        ),
        sa.Column(
            "route_layer",
            sa.Text(),
            nullable=False,
            comment="命中层：rule / system_one / agent / fallback（laya 仅历史只读）",
        ),
        sa.Column(
            "escalated_to_s2",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="是否升级大模型（Agent）",
        ),
        sa.Column(
            "latency_ms",
            sa.Integer(),
            nullable=False,
            comment="路由层延迟（P95 统计口径）",
        ),
        sa.Column(
            "asr_confidence",
            sa.Numeric(4, 3),
            nullable=True,
            comment="ASR 置信度（P2 埋点，可空）",
        ),
        sa.Column(
            "model_version",
            sa.Text(),
            nullable=False,
            comment="模型/规则版本（如 laya:multilingual:q1 或 ruleset:r1）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="写入时间（UTC 存储）",
        ),
        sa.PrimaryKeyConstraint("log_id", name="pk_decision_logs"),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["conversation_threads.thread_id"],
            name="fk_decision_logs_thread_id_conversation_threads",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            _in_clause("route_layer", _ROUTE_LAYERS), name="ck_decision_logs_route_layer"
        ),
        sa.CheckConstraint(_in_clause("intent", _INTENTS), name="ck_decision_logs_intent"),
        comment="路由决策日志（只追加；写库唯一入口 decision_log_service.record_decision）",
    )
    # 三个查询面：会话回溯（澄清判据）/ 按层统计命中率 / trace 串全链路
    op.create_index(
        "ix_decision_logs_thread_id_created_at",
        "decision_logs",
        ["thread_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_decision_logs_route_layer_created_at",
        "decision_logs",
        ["route_layer", sa.text("created_at DESC")],
    )
    op.create_index("ix_decision_logs_trace_id", "decision_logs", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_decision_logs_trace_id", table_name="decision_logs")
    op.drop_index("ix_decision_logs_route_layer_created_at", table_name="decision_logs")
    op.drop_index("ix_decision_logs_thread_id_created_at", table_name="decision_logs")
    op.drop_table("decision_logs")
