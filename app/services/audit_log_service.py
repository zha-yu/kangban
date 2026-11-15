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
from contextlib import AbstractAsyncContextManager
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

# ---------------------------------------------------------------------------
# M5.2 红线（dev-B）：Agent 侧 Guardrails 熔断的审计出口。
#
# 背景：ToolExecutor 内的审计搭执行短会话（_SessionToolExecutor commit 闭合），
# 但 Agent 侧 Guardrails 拒绝发生在 ToolExecutor 之前，那一条 tool_guardrail_denied
# 之前完全没有落库点。agents 层不写库（分层红线），故在此提供回调工厂，由装配层
# （main.py）注入 CompanionAgent.audit_sink——与 followup_sink 同款分层口径。

from collections.abc import Awaitable, Callable  # noqa: E402

AgentAuditSink = Callable[..., Awaitable[None]]


def make_agent_audit_sink(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
) -> AgentAuditSink:
    """返回 CompanionAgent.audit_sink 用的回调：独立短会话落一条审计并立即 commit。

    独立会话（不搭 turn 事务）是刻意的：熔断拒绝后本轮对话还要正常收尾，审计不能
    跟着任何后续回滚一起被抹掉——这正是 2026-11-15 语音红档审计零行的根因。
    """

    async def _sink(*, action: str, detail: dict[str, Any]) -> None:
        async with session_factory() as session:
            await record_audit(
                session,
                actor_type="system",
                actor_id=None,
                action=action,
                resource_type="tool_execution",
                resource_id=None,
                detail=detail,
            )
            await session.commit()

    return _sink
