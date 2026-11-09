"""审计日志的**唯一写库入口**（ER §4.4.6 / AC-18 / AC-24 / AC-29）。

三条口径落在这里，而不是各个业务服务里：

1. **与业务同事务**：只 `add` + `flush`，**不 commit**。调用方在业务写入的同一个
   session/事务里调用本函数——业务回滚则审计一起回滚，业务提交则审计同时可见
   （「审计写失败则整笔操作失败」不需要额外开关，事务本身就是开关）。
2. **只增不改**：本模块不提供 update/delete；审计行一旦提交即不可篡改。
3. **不塞敏感明文**：`detail` 只允许放 ID、字段名、枚举值与变更前后快照
   （房间/床位管理放 `before` / `after` / `trace_id`；姓名、手机号、地址、token 一律不写）。

Day 6 a0（员工-长者改绑）可直接复用本函数，不要另建第二套审计写入口。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_trace_id
from app.models import AuditLog

#: actor_type 三值白名单（对齐 ER §4.4.6 CHECK 约束）
ACTOR_TYPES: frozenset[str] = frozenset({"staff", "guardian", "system"})


async def record_audit(
    session: AsyncSession,
    *,
    actor_type: str,
    actor_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    detail: Mapping[str, Any] | None = None,
) -> AuditLog:
    """写一条审计并 flush（不 commit）；返回刚写入的行（audit_id 已可用）。

    调用方必须保证本函数与业务写操作在**同一事务**内、且在其后统一 commit。
    `trace_id` 由请求上下文自动注入 `detail`，便于与结构化日志/Langfuse 串联。
    """
    if actor_type not in ACTOR_TYPES:
        raise ValueError(f"unknown audit actor_type: {actor_type!r}")

    payload: dict[str, Any] = dict(detail) if detail is not None else {}
    trace_id = get_trace_id()
    if trace_id:
        payload.setdefault("trace_id", trace_id)

    row = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=payload or None,
    )
    session.add(row)
    await session.flush()
    return row


__all__ = ["ACTOR_TYPES", "record_audit"]
