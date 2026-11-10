"""工单的唯一建单与流转口（A12 的落点）。

为什么要有这个文件
------------------
`work_orders` 目前有两条写入路径：dev-C 的 `safety_service`（急症，内联 Core insert）
与本文件（漏服，A12）。**主理人 2026-11-09 裁决走 B：两套并存、登记 WD-1**，
理由是 C 那条路径的入参是 `SafetyDecision` 且只接受 `severity="red"`，
而漏服是 yellow 且没有决策对象——硬接要改他已验收的红色告警链路。

并存的前提是**分叉面足够小**，所以本文件与 C 的实现在三处严格对齐：

| 项 | 口径 | 两侧是否一致 |
| --- | --- | --- |
| `order_no` | `WO-<UTC日期>-<8 位大写 hex>` | ✅（本文件用 sha256 派生，见 `_new_order_no`） |
| `severity` | 建单快照，**不随后同步**；升红走 `reopened` + 新单 | ✅ |
| 幂等 | 同一来源只建一单 | ⚠️ 机制不同：C 靠 `event_id` 查重，本文件靠**确定性单号**，原因见下 |

关于幂等：为什么不用 `(elder_id, type, source_id)`
-------------------------------------------------
设计稿 §2 原定四元幂等键 `(elder_id, order_type, source_type, source_id)`。
落地时核对 0010 迁移发现 **`work_orders` 只有 `event_id`（FK→`risk_events`），
没有 `source_type` / `source_id`**，而漏服来源是 `medication_events`，
不是 `risk_events`——填不进 `event_id`。

0011 已由 dev-C 建完（notifications），本日不再新增迁移（写锁 L11），
所以本文件改用**确定性单号**做幂等：`order_no` 由来源 token 派生，
同一来源重复建单必然撞 `uq_work_orders_order_no`，**不需要新列、不需要新索引**。

代价与边界（如实写清，不含糊）：

- 单号里带的是 sha256 前 8 位，**不可逆**，不会从工单号反推 reminder_id；
- 同一 UTC 日期内两次不同来源碰撞概率约 2^-32，撞上会走 `IntegrityError` 兜底
  并回查——回查不到就抛出，**不会静默丢单**；
- 跨日期同一来源会得到不同单号（日期段不同），这是有意的：**漏服是每天的事**，
  昨天的漏服不该吞掉今天的单。

谁不该用这个文件
----------------
**急症（N27）不要绕过 `safety_service`**。它同时写 `risk_events` + `work_orders` +
`notifications` 三张表，且要用 `existing_event_id` 复用；只把这一个文件当入口会
漏掉另外两张表。WD-1 收敛时再评估能否合并（见 `a12-工单建单服务接口设计.md` §9.5）。
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.medication import WorkOrder, WorkOrderTransition
from app.models.relation import ElderStaffAssignment

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 枚举（与 0010 的 CHECK 同源）

#: `work_orders.type` 的全部合法值（0010 的 CHECK 逐字对齐）。
ORDER_TYPES: frozenset[str] = frozenset(
    {
        "missed_medication",
        "sensitive_alert",
        "health_alert",
        "device_fault",
        "sos",
        "collection_missed",
        "drug_question",
        "follow_up",
    }
)

#: `work_orders.severity`。只有红黄两档——AC-04 的急症是红，漏服是黄。
SEVERITIES: frozenset[str] = frozenset({"red", "yellow"})

#: `work_orders.status` 六值。
STATUSES: tuple[str, ...] = (
    "new",
    "acknowledged",
    "in_progress",
    "resolved",
    "closed",
    "reopened",
)

#: 终态。进入后只能 `reopened` 回到处理中（不能直接改别的）。
_TERMINAL: frozenset[str] = frozenset({"resolved", "closed"})

#: 合法迁移。**刻意不含 `new → resolved`**：跳过分诊直接判定「已处理」会让
#: 「谁接的单、谁处置的」无从追溯，护工看板的接单数也就没有意义。
_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"acknowledged", "in_progress", "closed"}),
    "acknowledged": frozenset({"in_progress", "resolved", "closed"}),
    "in_progress": frozenset({"resolved", "closed"}),
    "resolved": frozenset({"closed", "reopened"}),
    "closed": frozenset({"reopened"}),
    "reopened": frozenset({"acknowledged", "in_progress", "closed"}),
}


# ------------------------------------------------------------------ 异常


class WorkOrderError(Exception):
    """工单域的基础异常。调用方 catch 它即可兜住全部业务失败。"""


class WorkOrderNotFound(WorkOrderError):
    """工单不存在。"""


class InvalidTransition(WorkOrderError):
    """非法状态迁移。**不静默忽略**——忽略会让看板上的状态与真实处置脱节。"""


class WorkOrderConflict(WorkOrderError):
    """并发建单撞唯一键且回查不到（理论上不该出现，出现即是数据问题）。"""


# ------------------------------------------------------------------ 返回值


@dataclasses.dataclass(frozen=True, slots=True)
class WorkOrderRef:
    """建单结果。

    `created=False` 表示命中幂等、返回的是既有单。调用方据此决定要不要
    再发一次通知——重复通知比漏通知更伤（护工一天接三通同样的电话会直接忽略）。
    """

    order_id: uuid.UUID
    order_no: str
    status: str
    severity: str
    created: bool


# ------------------------------------------------------------------ 内部


def _new_order_no(dedupe_token: str | None = None) -> str:
    """生成人类可读单号：`WO-<UTC日期>-<8 位大写 hex>`。

    `dedupe_token` 给定时走**确定性派生**（同一 token 同一天必得同一单号），
    这是本文件在「表里没有 source_id」前提下实现幂等的唯一手段。
    不给时退回随机，与 C 的 `safety_service._new_order_no()` 完全同格式。
    """
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")
    if dedupe_token:
        digest = hashlib.sha256(dedupe_token.encode("utf-8")).hexdigest()[:8].upper()
        return f"WO-{stamp}-{digest}"
    return f"WO-{stamp}-{uuid.uuid4().hex[:8].upper()}"


def _validate(order_type: str, severity: str) -> None:
    if order_type not in ORDER_TYPES:
        raise WorkOrderError(f"unknown work order type: {order_type!r}")
    if severity not in SEVERITIES:
        raise WorkOrderError(f"unknown severity: {severity!r}")


async def _find_by_order_no(session: AsyncSession, order_no: str) -> WorkOrder | None:
    return (
        await session.execute(select(WorkOrder).where(WorkOrder.order_no == order_no))
    ).scalar_one_or_none()


def _ref(order: WorkOrder, *, created: bool) -> WorkOrderRef:
    return WorkOrderRef(
        order_id=order.order_id,
        order_no=order.order_no,
        status=order.status,
        severity=order.severity,
        created=created,
    )


# ------------------------------------------------------------------ 建单


async def open_work_order(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    order_type: str,
    severity: str,
    dedupe_token: str | None = None,
    event_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
) -> WorkOrderRef:
    """建一张工单，**同一来源只建一次**。

    :param dedupe_token: 幂等来源标识（如 `medication_event:<reminder_id>`）。
        给定时单号确定性派生，重复调用返回既有单且 `created=False`。
        不给定则每次都是新单（随机单号）——**只在确实允许重复建单时省略**。
    :param event_id: 关联的 `risk_events`（急症类才有）。漏服传 None：
        它不是风险事件，硬塞一个假 event_id 会污染红色的统计口径。
    :param assignee_id: 负责护工。查不到护工时传 None——**不要为了「有值」而填自己**，
        看板上的「未分派」就是真实状态，比一个错的指派人有用。
    """
    _validate(order_type, severity)
    order_no = _new_order_no(dedupe_token)

    if dedupe_token:
        existing = await _find_by_order_no(session, order_no)
        if existing is not None:
            logger.info(
                "work_order_idempotent_hit order_no=%s elder_id=%s type=%s",
                order_no,
                elder_id,
                order_type,
            )
            return _ref(existing, created=False)

    # 主键显式生成：表上虽有 `server_default gen_random_uuid()`，但 ORM 的 flush 不保证
    # 一定回填（取决于方言与 eager_defaults），而本函数返回时要立刻用到 order_id。
    # 显式给值最省事，也不影响迁移里的默认值（它仍是外部写入的兜底）。
    order = WorkOrder(
        order_id=uuid.uuid4(),
        order_no=order_no,
        elder_id=elder_id,
        event_id=event_id,
        type=order_type,
        severity=severity,
        status="new",
        assignee_id=assignee_id,
        false_alarm=False,
    )
    session.add(order)
    try:
        # **必须走 SAVEPOINT**。直接 `flush()` + `rollback()` 会把调用方在同一事务里
        # 已经写完的东西一起撤销——子图 `finalize` 的顺序正是「先写终态、再建单」，
        # 用外层 rollback 会把「这次确实没确认上」这个事实抹掉，症状是漏服事件永远
        # 停在 `calling`、工单却在库里，两边对不上。
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        # 并发同 token：另一个请求刚插进去。回查后按幂等返回，而不是把异常抛给上层
        # ——上层抛出去会让整轮用药确认失败，而事实是「单已经建好了」。
        existing = await _find_by_order_no(session, order_no)
        if existing is None:
            raise WorkOrderConflict(f"duplicate order_no {order_no} but not found") from exc
        return _ref(existing, created=False)

    # 首条流转记录。`from_status` 在 0010 是 NOT NULL，没有「无前态」的取值，
    # 所以记 `new → new` 表示「创建即进入 new」。这不是理想建模，
    # 但把首条写成别的状态（比如 `acknowledged`）会让流转表与工单状态对不上。
    session.add(
        WorkOrderTransition(
            transition_id=uuid.uuid4(),
            order_id=order.order_id,
            from_status="new",
            to_status="new",
            actor_id=None,
            note="created",
        )
    )
    await session.flush()

    logger.info(
        "work_order_created order_id=%s order_no=%s elder_id=%s type=%s severity=%s",
        order.order_id,
        order_no,
        elder_id,
        order_type,
        severity,
    )
    return _ref(order, created=True)


async def resolve_primary_care_staff(
    session: AsyncSession, *, elder_id: uuid.UUID
) -> uuid.UUID | None:
    """取长者的责任护工（`relation='primary_care'`），没配则返回 None。

    **不做 fallback 到「任意一个护工」**：看板上的「未分派」是真实状态，
    随便指一个人会让「谁该负责」在两套口径间漂移（PRD FR-ADM-06 的绑定关系
    是唯一依据）。这条查询与 dev-C 的 `_resolve_primary_care` 逻辑一致，
    但两侧各自持有——合并属 WD-1 的收敛范围，本日不动他的文件。
    """
    return (
        await session.execute(
            select(ElderStaffAssignment.staff_id)
            .where(
                ElderStaffAssignment.elder_id == elder_id,
                ElderStaffAssignment.relation == "primary_care",
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def open_missed_medication(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    reminder_id: uuid.UUID,
    assignee_id: uuid.UUID | None = None,
) -> WorkOrderRef:
    """漏服升级单（A12 的语义入口）。

    几个刻意的选择：

    - **severity 固定 `yellow`**：漏服不是急症。判红会让 AC-22 的「本周红色工单数」
      失去意义，也会让真正需要立刻上门的急症被稀释。
    - **`dedupe_token` 用 `medication_event:<reminder_id>`**：同一提醒事件重复跑完
      子图（重试、重启恢复）不会多建单。
    - **不传 `event_id`**：漏服不是风险事件。若日后要建，应当是新建一条
      `risk_events` 再关联，而不是借一个别的 event_id。
    """
    return await open_work_order(
        session,
        elder_id=elder_id,
        order_type="missed_medication",
        severity="yellow",
        dedupe_token=f"medication_event:{reminder_id}",
        assignee_id=assignee_id,
    )


# ------------------------------------------------------------------ 流转


async def transition(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    to_status: str,
    actor_id: uuid.UUID | None = None,
    note: str | None = None,
    false_alarm: bool = False,
    false_alarm_reason: str | None = None,
    actual_handled_at: datetime.datetime | None = None,
) -> None:
    """流转工单状态，并写一条 `work_order_transitions`。

    两条硬规则（都属于「不允许为了走得通而放宽」的那类）：

    1. **误报必须给原因**。只置 `false_alarm=true` 而无原因，等于把一条红色告警
       悄悄归档，事后无法解释「为什么当时判错了」，也无从改进词表。
    2. **红单关闭必须有实际处置时间**。AC-22 要求红色事件可回溯到「谁在几点处置」；
       没有 `actual_handled_at` 的关闭会让这条取证断掉。
    """
    if to_status not in STATUSES:
        raise WorkOrderError(f"unknown status: {to_status!r}")

    order = (
        await session.execute(select(WorkOrder).where(WorkOrder.order_id == order_id))
    ).scalar_one_or_none()
    if order is None:
        raise WorkOrderNotFound(f"work order {order_id} does not exist")

    current = order.status
    if current == to_status:
        # 幂等：重复点击「已处理」不该报错，也不该写一条同态流转记录
        logger.info("work_order_transition_noop order_id=%s status=%s", order_id, current)
        return
    if to_status not in _TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(f"{current} -> {to_status} is not allowed")

    if false_alarm and not false_alarm_reason:
        raise WorkOrderError("false_alarm requires false_alarm_reason")

    if (
        order.severity == "red"
        and to_status in _TERMINAL
        and actual_handled_at is None
        and order.actual_handled_at is None
    ):
        raise WorkOrderError("red work order cannot be closed without actual_handled_at")

    order.status = to_status
    if false_alarm:
        order.false_alarm = True
        order.false_alarm_reason = false_alarm_reason
    if actual_handled_at is not None:
        order.actual_handled_at = actual_handled_at
    if note is not None:
        order.note = note
    if to_status == "closed":
        order.closed_at = datetime.datetime.now(datetime.UTC)

    session.add(
        WorkOrderTransition(
            transition_id=uuid.uuid4(),
            order_id=order.order_id,
            from_status=current,
            to_status=to_status,
            actor_id=actor_id,
            note=note,
        )
    )
    await session.flush()
    logger.info(
        "work_order_transitioned order_id=%s from=%s to=%s actor=%s",
        order_id,
        current,
        to_status,
        actor_id,
    )


# ------------------------------------------------------------------ 查询（f2-D1 console 工单看板真实接口，2026-11-10）
#
# 只读查询面：给 app/api/workorders.py 提供「工单 + 冗余展示字段 + 全程流转」的
# 一体化取数。冗余字段**只算不存**（elder_name / room_label 由 join 推导，
# 前端 Mock 层的注释口径即「真实接口由服务端 join 返回」）。


@dataclasses.dataclass(frozen=True, slots=True)
class TransitionRow:
    """单条流转记录 + 操作人姓名。

    `work_order_transitions` 只存 `actor_id`（FK→staff_accounts），姓名由这里
    join 补齐；系统自动流转（actor_id 为空）或员工被删（SET NULL）时为 None。
    """

    transition: WorkOrderTransition
    actor_name: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class WorkOrderRow:
    """看板行：工单本体 + 冗余展示字段 + 全程流转（FR-ALT-02）。"""

    order: WorkOrder
    elder_name: str | None
    room_no: str | None
    assignee_name: str | None
    transitions: tuple[TransitionRow, ...]

    @property
    def room_label(self) -> str | None:
        """Mock 前端的展示口径（如 "A-301"）；未入住床位时为 None。"""
        return self.room_no


def _occupied_room_sq() -> Any:
    """在住长者的房号子查询：Bed(elder_id) → Room(room_no)。

    `Bed.elder_id` 无唯一约束（业务上一长者一床，但不赌数据），
    `DISTINCT` 兜底避免脏数据把工单 join 成多行。
    """
    from app.models.organization import Bed, Room

    return (
        select(Bed.elder_id.label("elder_id"), Room.room_no.label("room_no"))
        .select_from(Bed)
        .join(Room, Bed.room_id == Room.room_id)
        .where(Bed.elder_id.is_not(None))
        .distinct()
        .subquery()
    )


async def _load_transition_rows(
    session: AsyncSession, order_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[TransitionRow, ...]]:
    """按 order_id 分组取全程流转（含操作人姓名），按发生时间升序。"""
    if not order_ids:
        return {}
    from app.models.staff import StaffAccount

    rows = (
        await session.execute(
            select(WorkOrderTransition, StaffAccount.name)
            .outerjoin(StaffAccount, WorkOrderTransition.actor_id == StaffAccount.staff_id)
            .where(WorkOrderTransition.order_id.in_(order_ids))
            .order_by(WorkOrderTransition.at, WorkOrderTransition.transition_id)
        )
    ).all()
    grouped: dict[uuid.UUID, list[TransitionRow]] = {}
    for transition, actor_name in rows:
        grouped.setdefault(transition.order_id, []).append(
            TransitionRow(transition=transition, actor_name=actor_name)
        )
    return {k: tuple(v) for k, v in grouped.items()}


async def list_work_orders(
    session: AsyncSession, *, limit: int = 500
) -> list[WorkOrderRow]:
    """看板列表（Mock 前端口径：一次返回全量，筛选/红档置顶在页面层做）。

    长者用 `OUTER JOIN`：长者被归档不该让工单在看板上消失（FR-ALT-02 全程留痕），
    兜底 `elder_name=None`，由 API 层渲染成「未知长者」。
    """
    from app.models.organization import Elder
    from app.models.staff import StaffAccount

    room_sq = _occupied_room_sq()
    assignee = aliased(StaffAccount)
    rows = (
        await session.execute(
            select(WorkOrder, Elder.name, room_sq.c.room_no, assignee.name)
            .join(Elder, WorkOrder.elder_id == Elder.elder_id)
            .outerjoin(room_sq, room_sq.c.elder_id == Elder.elder_id)
            .outerjoin(assignee, WorkOrder.assignee_id == assignee.staff_id)
            .order_by(WorkOrder.created_at.desc())
            .limit(limit)
        )
    ).all()
    transition_map = await _load_transition_rows(session, [o.order_id for o, _, _, _ in rows])
    return [
        WorkOrderRow(
            order=order, elder_name=elder_name, room_no=room_no,
            assignee_name=assignee_name,
            transitions=transition_map.get(order.order_id, ()),
        )
        for order, elder_name, room_no, assignee_name in rows
    ]


async def get_work_order(
    session: AsyncSession, *, order_id: uuid.UUID
) -> WorkOrderRow | None:
    """单张工单（含全程流转）。查不到返回 None。"""
    from app.models.organization import Elder
    from app.models.staff import StaffAccount

    room_sq = _occupied_room_sq()
    assignee = aliased(StaffAccount)
    row = (
        await session.execute(
            select(WorkOrder, Elder.name, room_sq.c.room_no, assignee.name)
            .join(Elder, WorkOrder.elder_id == Elder.elder_id)
            .outerjoin(room_sq, room_sq.c.elder_id == Elder.elder_id)
            .outerjoin(assignee, WorkOrder.assignee_id == assignee.staff_id)
            .where(WorkOrder.order_id == order_id)
        )
    ).first()
    if row is None:
        return None
    order, elder_name, room_no, assignee_name = row
    transition_map = await _load_transition_rows(session, [order.order_id])
    return WorkOrderRow(
        order=order, elder_name=elder_name, room_no=room_no,
        assignee_name=assignee_name,
        transitions=transition_map.get(order.order_id, ()),
    )


__all__ = [
    "ORDER_TYPES",
    "SEVERITIES",
    "STATUSES",
    "InvalidTransition",
    "TransitionRow",
    "WorkOrderConflict",
    "WorkOrderError",
    "WorkOrderNotFound",
    "WorkOrderRef",
    "WorkOrderRow",
    "open_missed_medication",
    "open_work_order",
    "resolve_primary_care_staff",
    "transition",
]
