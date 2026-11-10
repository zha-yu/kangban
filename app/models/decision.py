"""路由决策日志 ORM（ER §4.4.5 / TD §3.4，第 31 张表）。

**这张表的唯一用途是回答「这轮为什么走了这一层」**（AC-03/AC-31/AC-37/AC-36 的取证面）：

- 每轮**恰好 1 行**（`turn_service` 在第 3.5 步路由后、同一 turn 事务内写入）；
- **只 INSERT**，不做 UPDATE（`decision_log_service.record_decision` 是唯一写口）；
- `input_text` **落库前即脱敏**（ER §2.4）：手机号/身份证/房间号不落明文；
- `route_layer` 新代码只写 `rule/system_one/agent/fallback`；`laya` 是 Day 6 及以前的历史值，
  只读兼容、**不得新写**（CHECK 里保留它，否则历史行会让迁移失败）。

14 列与 TD §3.4 逐字对齐，不增不减：增列要改 ER/TD 并同步评测脚本；
本地加「方便」的列（如 reason / action）看起来省事，实际会让两份文档与库三处不一致。
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    desc,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: `route_layer` 取值（四值 + 历史只读 `laya`）。服务层校验与 DB CHECK 共用一份。
ROUTE_LAYERS: tuple[str, ...] = ("rule", "system_one", "agent", "fallback", "laya")

#: 意图七类（与 `app/system_one/schemas.INTENTS` 同源；这里写死是为了让 CHECK 可读）
INTENT_VALUES: tuple[str, ...] = (
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


class DecisionLog(Base):
    """一行 = 一次路由决策（只追加）。"""

    __tablename__ = "decision_logs"
    __table_args__ = (
        CheckConstraint(_in_clause("route_layer", ROUTE_LAYERS), name="route_layer"),
        CheckConstraint(_in_clause("intent", INTENT_VALUES), name="intent"),
        # 三个查询面：按会话回溯（澄清判据）、按层统计命中率、按 trace 串全链路
        Index("ix_decision_logs_thread_id_created_at", "thread_id", desc("created_at")),
        Index("ix_decision_logs_route_layer_created_at", "route_layer", desc("created_at")),
        Index("ix_decision_logs_trace_id", "trace_id"),
        {"comment": "路由决策日志（只追加；写库唯一入口 decision_log_service.record_decision）"},
    )

    log_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="日志编号",
    )
    thread_id: Mapped[str] = mapped_column(
        Text(),
        ForeignKey("conversation_threads.thread_id", ondelete="CASCADE"),
        nullable=False,
        comment="所属会话（串规则→System One→Agent→Tool→TTS）",
    )
    trace_id: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="Langfuse/OTel trace_id，供双向检索"
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="长者（generic 轮次无身份，不写本表）",
    )
    input_text: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="用户输入（落库前即脱敏）"
    )
    intent: Mapped[str] = mapped_column(Text(), nullable=False, comment="识别意图（七类）")
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, comment="置信度（分类器熵归一化值；规则命中为 1.000）"
    )
    is_sensitive: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        server_default=text("false"),
        comment="是否命中敏感（G0 或 shadow 信号）",
    )
    route_layer: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="命中层：rule / system_one / agent / fallback（laya 仅历史只读）",
    )
    escalated_to_s2: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        server_default=text("false"),
        comment="是否升级大模型（Agent）",
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="路由层延迟（P95 统计口径）"
    )
    asr_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3), nullable=True, comment="ASR 置信度（P2 埋点，可空）"
    )
    model_version: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="模型/规则版本（如 laya:multilingual:q1 或 ruleset:r1）",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="写入时间（UTC 存储）",
    )
