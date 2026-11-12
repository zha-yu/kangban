"""Day 5 · metrics 分段耗时打点 ORM（裁决 B6/D2，第 27 张表）。

列注释逐字对齐迁移 0006 与 ER §4.4.7（避免 autogenerate 漂移）。
只写指标不写内容：不含转写全文、不含长者隐私字段；写库唯一入口
`app/services/metrics_service.py`（异步批量缓冲，写失败降级日志不阻塞主链路）。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Metric(Base):
    """分段耗时打点表：一行 = 一次对话链路中一个阶段（stage）的耗时观测。"""

    __tablename__ = "metrics"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('wakeup', 'connection', 'asr', 'route', "
            "'llm_first_token', 'tts_first_packet', 'playback', 'response_segment', 'llm_tokens')",
            name="stage",
        ),
        CheckConstraint("duration_ms >= 0", name="duration_ms"),
        Index("trace_id", "trace_id"),
        Index("stage_created_at", "stage", "created_at"),
        {"comment": "分段耗时打点表（只写指标不写内容；写库唯一入口 metrics_service）"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    trace_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="全链路追踪 ID（与日志 / Langfuse trace 对应；P50/P95 复算的串联键）",
    )
    elder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="长者（可空：唤醒前打点无身份；不建 FK，对齐 decision_logs.elder_id 口径）",
    )
    stage: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="wakeup/connection/asr/route/llm_first_token/tts_first_packet/playback/"
        "response_segment/llm_tokens",
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="该段耗时（毫秒，≥0；llm_tokens 行恒 0，非耗时打点）"
    )
    source: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="口径区分：client（端侧 wake_ts）/ backend（WS accept 兜底）/ non_stream（LLM/TTS 非流式口径）等",
    )
    prompt_tokens: Mapped[int | None] = mapped_column(
        Integer(),
        nullable=True,
        comment="LLM 调用输入 token 数（NULL = 供应商未回 usage，不补零；仅 stage=llm_tokens 有值）",
    )
    completion_tokens: Mapped[int | None] = mapped_column(
        Integer(),
        nullable=True,
        comment="LLM 调用输出 token 数（NULL = 供应商未回 usage，不补零；仅 stage=llm_tokens 有值）",
    )
    total_tokens: Mapped[int | None] = mapped_column(
        Integer(),
        nullable=True,
        comment="LLM 调用总 token 数（仅两侧都已知时非 NULL；仅 stage=llm_tokens 有值）",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="打点入库时间",
    )
