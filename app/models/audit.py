"""审计日志 ORM（ER §4.4.6 / TD §3，第 28 张表）。

**为什么提前建**：ER/TD 原排 Day 9，但房间床位管理要求「所有管理操作留痕」
（含操作人、时间、变更前后），结构化日志无法查询也无法与业务写操作同事务回滚。
故本功能随迁移 0007 提前建表，Day 9 不再建第二套 `audit_logs`。

口径（ER §4.4.6）：
- 只增不改：本表不提供 update/delete 语义，查询由审计界面（非本功能）负责；
- `actor_type` 三值 CHECK（`staff` / `guardian` / `system`），枚举一律 CHECK 不建 PG ENUM；
- `detail` jsonb 存操作细节：房间/床位管理固定放 `before` / `after` / `trace_id`；
- 不承担常规状态流转（那是 `work_order_transitions`），越权拒绝也记一条。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditLog(Base):
    """敏感操作审计表：一行 = 一次已发生的写操作或一次越权拒绝。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint("actor_type IN ('staff', 'guardian', 'system')", name="actor_type"),
        Index("resource_type_resource_id_created_at", "resource_type", "resource_id", "created_at"),
        Index("actor_type_actor_id_created_at", "actor_type", "actor_id", "created_at"),
        {"comment": "审计日志表（只增不改；写库唯一入口 audit_log_service）"},
    )

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="审计编号（不叫 log_id，与 decision_logs.log_id 区分）",
    )
    actor_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="操作者类型：staff / guardian / system"
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="操作者 ID（系统动作可空）"
    )
    action: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="动作（create_room / update_bed / batch_delete_beds / unbind_device …）",
    )
    resource_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="操作对象类型（room / bed / device / elder …）"
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="操作对象 ID（批量操作可空，明细见 detail）"
    )
    detail: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(),
        nullable=True,
        comment="操作细节（before/after、trace_id；越权拒绝也记）",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        comment="操作发生时间（UTC 存储）",
    )
