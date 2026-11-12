"""`decision_logs` 写库与只读查询（Day 7 · A3）：**唯一写口**。

三条纪律：

1. **只 INSERT**：本表没有 UPDATE 语义。改结论的办法是「下一轮写新的一行」。
2. **落库前脱敏**（ER §2.4）：`input_text` 不得出现手机号/身份证/房间号明文。
   脱敏放在写口而不是调用方——调用方有三个（turn_service / 评测脚本 / 将来的补录任务），
   少放一处就会漏一处。
3. **失败不阻断对话**：写日志失败顶多丢一条统计数据，绝不能让长者听到「出错了」。
   实现上用 `SAVEPOINT`：失败只回滚到保存点，**不污染**调用方的事务（PG 里一条语句
   失败会让整个事务进入 aborted 状态，直接 `except` 吞掉会让后续 commit 全部失败）。

另外提供 `was_clarified()`：低置信「只澄清一次」的判据（契约 §5）。

> 为什么不用 `decision_logs.action` 判定是否澄清过？——这张表按 TD §3.4 只有 14 列，
> 没有 `action` 列，也不允许为图方便加列。澄清的两条可判定特征是：`confidence < 阈值`
> 且 `escalated_to_s2=false`（澄清那轮不升级大模型）。这个推断由本模块单向负责，
> 别处不要再写第二份判定。
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

# 脱敏唯一实现在 app/core/redact.py（Day 9 A1b 集中化）；保留 redact_input 别名
# 仅为既有调用方与测试的导入路径不变——不是第二套实现。
from app.core.redact import redact_text as redact_input
from app.models.decision import DecisionLog

logger = logging.getLogger("app.services.decision_log_service")

#: 低置信澄清的默认阈值（与 `system_one_confidence_threshold` 同源；调用方应显式传入配置值）
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.85


class DecisionLike(Protocol):
    """`RouteDecision` 的消费面（结构化，不 import `app.router.decision`）。

    服务层不反向依赖路由层：`services` 只认「有这几个属性」的对象，
    谁产出的（Router / 评测脚本 / 测试桩）与写库无关。
    """

    route_layer: str
    action: str
    intent: str
    confidence: float
    is_sensitive: bool
    escalated_to_s2: bool
    latency_ms: int


def _as_decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(f"{max(0.0, min(1.0, float(value))):.3f}")


async def record_decision(
    session: AsyncSession,
    *,
    thread_id: str,
    elder_id: str | uuid.UUID | None,
    trace_id: str | None,
    input_text: str,
    decision: DecisionLike,
    model_version: str,
    asr_confidence: float | None = None,
) -> bool:
    """写一行路由日志。返回是否写成（`False` = 已降级记录，不影响对话）。

    `elder_id is None`（generic 会话）**直接跳过**：本列 NOT NULL，而 generic 轮次
    没有身份——契约 §4 已定「generic 轮次不写本表」。
    """
    if elder_id is None:
        logger.info(
            "decision_log_skipped_generic trace_id=%s thread_id=%s", trace_id or "-", thread_id
        )
        return False

    try:
        async with session.begin_nested():  # SAVEPOINT：失败只回滚这一条 INSERT
            session.add(
                DecisionLog(
                    thread_id=thread_id,
                    trace_id=trace_id or "-",
                    elder_id=uuid.UUID(str(elder_id)),
                    input_text=redact_input(input_text),
                    intent=decision.intent,
                    confidence=_as_decimal(decision.confidence),
                    is_sensitive=bool(decision.is_sensitive),
                    route_layer=decision.route_layer,
                    escalated_to_s2=bool(decision.escalated_to_s2),
                    latency_ms=max(0, int(decision.latency_ms)),
                    asr_confidence=_as_decimal(asr_confidence),
                    model_version=model_version or "-",
                    # `created_at` 用 DB 的 clock_timestamp()：既不用 `now()`（事务开始时间，
                    # 同轮两行会同时间戳），也不用宿主 Python 时钟（粒度粗，Windows 约 15.6ms，
                    # 同轮两行同样会撞同一 tick）。时间戳一旦并列，`ORDER BY created_at DESC
                    # LIMIT 1` 取到哪行不确定，「只澄清一次」判据就失效。
                    created_at=sa.func.clock_timestamp(),
                )
            )
            await session.flush()
    except Exception as exc:  # 统计丢一条可以，对话不能断
        logger.error(
            "decision_log_failed trace_id=%s thread_id=%s error=%s",
            trace_id or "-",
            thread_id,
            f"{type(exc).__name__}: {exc}",
        )
        return False

    logger.info(
        "decision_log_recorded trace_id=%s thread_id=%s layer=%s action=%s intent=%s latency_ms=%d",
        trace_id or "-",
        thread_id,
        decision.route_layer,
        decision.action,
        decision.intent,
        decision.latency_ms,
    )
    return True


async def was_clarified(
    session: AsyncSession,
    thread_id: str,
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> bool:
    """同一 `thread_id` 的上一轮是否已经澄清过（只看最近一条，不回溯整段历史）。

    判定：最近一行为「低置信且未升级大模型」→ 那一轮就是澄清轮。
    查询走 `ix_decision_logs_thread_id_created_at`（`ORDER BY created_at DESC LIMIT 1`）。
    """
    row = (
        await session.execute(
            sa.select(
                DecisionLog.confidence,
                DecisionLog.escalated_to_s2,
            )
            .where(DecisionLog.thread_id == thread_id)
            .order_by(DecisionLog.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return False
    confidence, escalated = float(row.confidence or 0), bool(row.escalated_to_s2)
    return confidence < confidence_threshold and not escalated


async def last_decision(session: AsyncSession, thread_id: str) -> Any | None:
    """最近一条日志（联调/排障用；不参与路由决策）。"""
    return (
        (
            await session.execute(
                sa.select(DecisionLog)
                .where(DecisionLog.thread_id == thread_id)
                .order_by(DecisionLog.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DecisionLike",
    "last_decision",
    "record_decision",
    "redact_input",
    "was_clarified",
]
