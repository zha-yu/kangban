"""安全升级的唯一写库路径（AC-04：红色事件 + 工单 + 护工通知三行**同事务**落库）。

分层口径（AGENTS §2 / 计划 dev-c §0.1 第 3 条）：

1. `app/router/safety.py` 只做**纯检测**（无 I/O）；命中红档后由调用方
   （Day 6 C-8 `handle_user_turn`，入口第一步接线）拿本模块的
   `escalate_safety_event` 落库——router 不直接操作 ORM，写库只发生在 services 层；
2. **三行同一事务**：本函数只 `add` / `execute` + `flush`，**不 commit**——
   调用方在业务写入的同一个 session/事务里调用，业务回滚则三行一起回滚
   （与 `audit_log_service.record_audit` 同一套「调用方拥有事务」的纪律）；
3. **落库即视为已通知**（C-0-5 裁决）：通知行 `status='generated'`，30s 重试与
   未读列表等状态机归 Day 8 调度器（技术文档 line 1024），本日不做；
   家属侧 `send_family_notification` 仍走 Day 8 mock-operations（contracts.md v1.1）；
4. **dedupe 防重**：通知行带 `dedupe_key='risk:{event_id}'`，同一红色事件重复
   触发不重复通知；代码先查（幂等快路径），`notifications.dedupe_key` 的部分唯一
   索引是并发兜底（竞态时 IntegrityError 向调用方抛出，由事务回滚兜住）。

关于 risk_events / work_orders 的写法：两表的 ORM 属禁触范围（主理人 2026-11-09
授权仅解禁 notifications 一张表的 ORM），故本模块用**模块私有 Core Table**（独立
MetaData、不挂 Base.metadata，不影响 autogenerate）做 insert-only 写入，列定义
逐列对齐迁移 0010。两表 ORM 建立后应改回 ORM 写法并删除本模块的 Table 定义。

自裁口径（与代码同提交留痕，AGENTS §6.铁律 6）：

- 急症类别 → 工单/通知类型映射：work_orders.type 枚举（0010）与 notifications.type
  开放枚举（TD §3.5）均未定「急症类别→类型」的映射，本模块按下表落地
  （chest_pain/breathing/stroke/fall → health_alert + emergency_health；
  self_harm → sensitive_alert + sensitive）；
- 通知 payload 只放 ID 与词表命中证据，**不落长者原话**（AGENTS §4.10 脱敏红线，
  护工研判先看命中词与类别，原话还原归 Day 8 通知中心）；
- 「负责护工」查 `elder_staff_assignments`（relation='primary_care' 责任护工，
  最早分配者优先）。若该长者没有责任护工绑定，则**跳过通知行并以 warning 留痕**——
  TD line 664 对 target_id 是 NOT NULL，写 None 会违反建表约束；候选方案
  （转派任意在岗护工或接入机构值班表）待裁决。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import get_trace_id
from app.models import ElderStaffAssignment, Notification
from app.router.safety import SafetyDecision

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# risk_events / work_orders 的 insert-only Core 定义（列对齐迁移 0010）
# 独立 MetaData：绝不挂 Base.metadata，避免 autogenerate 把它当待建表产生漂移 diff。
# ---------------------------------------------------------------------------

_LOCAL_META = sa.MetaData()

_risk_events = sa.Table(
    "risk_events",
    _LOCAL_META,
    sa.Column(
        "event_id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("elder_id", UUID(as_uuid=True), nullable=False),
    sa.Column("type", sa.Text(), nullable=False),
    sa.Column("severity", sa.Text(), nullable=False),
    sa.Column("source_type", sa.Text(), nullable=True),
    sa.Column("source_id", UUID(as_uuid=True), nullable=True),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
)

_work_orders = sa.Table(
    "work_orders",
    _LOCAL_META,
    sa.Column(
        "order_id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("order_no", sa.Text(), nullable=False),
    sa.Column("elder_id", UUID(as_uuid=True), nullable=False),
    sa.Column("event_id", UUID(as_uuid=True), nullable=True),
    sa.Column("type", sa.Text(), nullable=False),
    sa.Column("severity", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("assignee_id", UUID(as_uuid=True), nullable=True),
    sa.Column("false_alarm", sa.Boolean(), nullable=False),
    sa.Column("false_alarm_reason", sa.Text(), nullable=True),
    sa.Column("actual_handled_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("note", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
)

#: 急症类别 → 工单类型（work_orders.type 枚举见 0010 迁移；映射为自裁口径，见 docstring）
_ORDER_TYPE_BY_CATEGORY: dict[str, str] = {
    "chest_pain": "health_alert",
    "breathing": "health_alert",
    "stroke": "health_alert",
    "fall": "health_alert",
    "self_harm": "sensitive_alert",
}

#: 急症类别 → 通知类型（TD §3.5 notifications.type 开放枚举的现有取值）
_NOTIFICATION_TYPE_BY_CATEGORY: dict[str, str] = {
    "chest_pain": "emergency_health",
    "breathing": "emergency_health",
    "stroke": "emergency_health",
    "fall": "emergency_health",
    "self_harm": "sensitive",
}


class SafetyEscalationError(Exception):
    """升级路径的数据错误：如指定的 risk 事件不存在（如实告知，不编造）。"""


@dataclass(frozen=True, slots=True)
class EscalationResult:
    """一次红色事件升级的结果。

    - `notification_created=False` 且 `notification_id` 非空：dedupe 命中
      （该事件的通知此前已落库，不重复发）；
    - `notification_id=None`：无责任护工绑定，通知行被跳过（见 docstring 自裁口径）；
    - 重复触发（传入 `existing_event_id`）时复用原事件与原工单，
      `event_created` / `order_created` 均为 False。
    """

    event_id: uuid.UUID
    order_id: uuid.UUID | None
    notification_id: uuid.UUID | None
    staff_id: uuid.UUID | None
    event_created: bool
    order_created: bool
    notification_created: bool


async def _resolve_primary_care(session: AsyncSession, elder_id: uuid.UUID) -> uuid.UUID | None:
    """查责任护工：`elder_staff_assignments` 的 primary_care 绑定，最早分配者优先。"""
    stmt = (
        sa.select(ElderStaffAssignment.staff_id)
        .where(
            ElderStaffAssignment.elder_id == elder_id,
            ElderStaffAssignment.relation == "primary_care",
        )
        .order_by(ElderStaffAssignment.assigned_at)
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    return row.staff_id if row is not None else None


def _new_order_no() -> str:
    """人类可读工单号（work_orders.order_no 唯一约束的业务字段）。

    教学期口径自裁：`WO-<UTC日期>-<8 位随机>`，随机段保证唯一不依赖计数器
    （TD 只定「人类可读编号唯一」，未定格式）。
    """
    return f"WO-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


async def escalate_safety_event(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    decision: SafetyDecision,
    thread_id: str | None = None,
    existing_event_id: uuid.UUID | None = None,
) -> EscalationResult:
    """把一个红档安全决策升级为 红色事件 + 护工工单 + 护工通知 三行（同一事务）。

    - 只接受 `severity="red"` 的决策：黄档判黄复核属 Day 9 边界集，误用即抛错；
    - `existing_event_id` 缺省时新建红色事件；传入已有事件 ID 表示**同一事件的
      重复触发**（如长者连续再说一次「我胸口疼」），此时复用原事件与原工单，
      通知经 `dedupe_key` 防重，不重复下发；
    - 本函数不 commit：事务由调用方拥有（与 record_audit 同纪律）。
    """
    if not decision.is_sensitive or decision.severity != "red":
        raise ValueError("escalate_safety_event() only accepts a red SafetyDecision (AC-04)")

    category = decision.category or ""
    order_type = _ORDER_TYPE_BY_CATEGORY.get(category)
    notification_type = _NOTIFICATION_TYPE_BY_CATEGORY.get(category)
    if order_type is None or notification_type is None:
        raise ValueError(f"unknown safety category: {decision.category!r}")

    trace_id = get_trace_id()

    # ---- 1. 红色事件 ----
    event_created = False
    if existing_event_id is None:
        row = (
            await session.execute(
                sa.insert(_risk_events)
                .values(
                    elder_id=elder_id,
                    type="red_emergency",
                    severity="red",
                    source_type="conversation_thread" if thread_id else None,
                    status="open",
                )
                .returning(_risk_events.c.event_id)
            )
        ).one()
        event_id: uuid.UUID = row.event_id
        event_created = True
        logger.info(
            "safety_escalation_event_created",
            extra={"event_id": str(event_id), "elder_id": str(elder_id), "trace_id": trace_id},
        )
    else:
        event_id = existing_event_id
        exists = (
            await session.execute(
                sa.select(_risk_events.c.event_id).where(_risk_events.c.event_id == event_id)
            )
        ).first()
        if exists is None:
            raise SafetyEscalationError(f"risk event {event_id} does not exist")

    # ---- 2. 负责护工（先查再写，工单 assignee 与通知 target 共用）----
    staff_id = await _resolve_primary_care(session, elder_id)

    # ---- 3. 护工工单（同一事件只建一单）----
    order_id: uuid.UUID | None = None
    order_created = False
    existing_order = (
        await session.execute(
            sa.select(_work_orders.c.order_id).where(_work_orders.c.event_id == event_id).limit(1)
        )
    ).first()
    if existing_order is not None:
        order_id = existing_order.order_id
    else:
        row = (
            await session.execute(
                sa.insert(_work_orders)
                .values(
                    order_no=_new_order_no(),
                    elder_id=elder_id,
                    event_id=event_id,
                    type=order_type,
                    severity="red",  # 建单快照，不随后同步（AC-22 黄升红另走流转）
                    status="new",
                    assignee_id=staff_id,
                    false_alarm=False,
                )
                .returning(_work_orders.c.order_id)
            )
        ).one()
        order_id = row.order_id
        order_created = True
        logger.info(
            "safety_escalation_order_created",
            extra={
                "order_id": str(order_id),
                "event_id": str(event_id),
                "elder_id": str(elder_id),
                "assignee_id": str(staff_id) if staff_id else None,
                "trace_id": trace_id,
            },
        )

    # ---- 4. 护工通知（dedupe_key='risk:{event_id}'，同一事件只发一次）----
    dedupe_key = f"risk:{event_id}"
    notification_id: uuid.UUID | None = None
    notification_created = False
    existing_notification = (
        await session.execute(
            sa.select(Notification.notification_id).where(Notification.dedupe_key == dedupe_key)
        )
    ).first()
    if existing_notification is not None:
        notification_id = existing_notification.notification_id
        logger.info(
            "safety_escalation_notification_deduped",
            extra={"dedupe_key": dedupe_key, "event_id": str(event_id), "trace_id": trace_id},
        )
    elif staff_id is None:
        # TD line 664：target_id NOT NULL。无责任护工绑定时不硬造 target，
        # 跳过通知行并留痕（自裁口径，见模块 docstring），红色事件与工单不受影响。
        logger.warning(
            "safety_escalation_notification_skipped_no_primary_care",
            extra={"event_id": str(event_id), "elder_id": str(elder_id), "trace_id": trace_id},
        )
    else:
        # 通知内容脱敏：只放 ID 与词表命中证据，不落长者原话（payload 纪律，TD line 666）
        payload: dict[str, object] = {
            "elder_id": str(elder_id),
            "event_id": str(event_id),
            "order_id": str(order_id) if order_id is not None else None,
            "thread_id": thread_id,
            "trace_id": trace_id,
            "matched_rule": decision.matched_rule,
            "category": decision.category,
        }
        notification = Notification(
            target_type="staff",
            target_id=staff_id,
            type=notification_type,
            payload=payload,
            channel=get_settings().notify_channel,
            status="generated",  # 落库即视为已通知（C-0-5）；状态机流转归 Day 8
            is_mock=False,
            dedupe_key=dedupe_key,
        )
        session.add(notification)
        await session.flush()
        notification_id = notification.notification_id
        notification_created = True
        logger.info(
            "safety_escalation_notification_created",
            extra={
                "notification_id": str(notification_id),
                "dedupe_key": dedupe_key,
                "target_id": str(staff_id),
                "event_id": str(event_id),
                "trace_id": trace_id,
            },
        )

    return EscalationResult(
        event_id=event_id,
        order_id=order_id,
        notification_id=notification_id,
        staff_id=staff_id,
        event_created=event_created,
        order_created=order_created,
        notification_created=notification_created,
    )


__all__ = [
    "EscalationResult",
    "SafetyEscalationError",
    "escalate_safety_event",
]
