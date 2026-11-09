"""长者档案领域服务（接口契约 E5–E8）。

三条硬口径落在这里，而不是 API 层：

1. **角色与范围判定全部在服务层**（契约 §3「验签 → 角色 → 范围 → 存在」）。
   护工只认 `relation='primary_care'`、医生只认 `relation='doctor'`（A-01 / AC-18），
   不能只按 `staff_id` 过滤；家属与设备读档案一律 403（裁决 A-01）。
2. **范围外不泄露存在性**：先判范围再查 `elders`，所以范围外主体拿到的永远是 403，
   不会因为资源不存在而变成 404。
3. **写库只发生在本文件**（AGENTS §2）：API 只调用这里的函数，不拼查询、不写 ORM。

审计口径按契约 A-02：`audit_logs` 表 Day 9 才建，本轮只写结构化日志，
字段固定为 `trace_id / audit_action / actor / target / result`，**不写姓名、生日、手机号、
token 或任何敏感明文**。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import RoleForbiddenError, check_roles
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.core.security import KIND_STAFF
from app.models import Bed, Elder, ElderStaffAssignment, Persona, Room
from app.services.org_status import aggregate_room_status

logger = logging.getLogger(__name__)

# R4 的 status 枚举（契约 §2 / TD §3.2）。写进常量而不是散在比较式里：
# 「库里到底认哪几个值」只应有一处定义。
ELDER_STATUSES: tuple[str, ...] = ("active", "away", "transferred", "archived")

# 角色 → 服务层强制的 assignment relation。admin 不在表内，表示不受范围限制。
_RELATION_BY_ROLE: dict[str, str] = {"care_worker": "primary_care", "doctor": "doctor"}

# PATCH 允许字段（契约 E8）。`session_seq` 不在其中：客户端不能碰会话计数器。
_PATCHABLE_FIELDS: frozenset[str] = frozenset(
    {"name", "birth_date", "gender", "persona_id", "status"}
)


class ElderServiceError(Exception):
    """长者领域错误基类：只表达「这次请求被拒」，不构造 HTTP 响应。"""


class ElderNotFoundError(ElderServiceError):
    """范围内主体查了不存在的长者（只有 admin 能走到这里）。"""


class ElderScopeForbiddenError(ElderServiceError):
    """主体种类不符，或长者不在当前员工的责任范围内。"""


class ElderValidationError(ElderServiceError):
    """业务校验失败（如 `archived` 夹带其他字段、persona 不存在）。"""


class ElderConflictError(ElderServiceError):
    """资源状态冲突（如目标床位已被其他长者占用）。"""


@dataclass(frozen=True, slots=True)
class ElderDetail:
    """E6 详情：R4 全字段 + 由 `beds` JOIN 得出的 `room_no / bed_no`（裁决 S-05）。"""

    elder_id: uuid.UUID
    name: str
    birth_date: datetime.date | None
    gender: str | None
    persona_id: uuid.UUID | None
    status: str
    session_seq: int
    created_at: datetime.datetime
    room_no: str | None
    bed_no: str | None


@dataclass(frozen=True, slots=True)
class ElderListItem:
    """E7 列表项：刻意不含 `birth_date / gender / persona_id`（裁决 S-07）。"""

    elder_id: uuid.UUID
    name: str
    room_no: str | None
    bed_no: str | None
    status: str


@dataclass(frozen=True, slots=True)
class ElderPage:
    items: list[ElderListItem]
    total: int


@dataclass(frozen=True, slots=True)
class BedInfo:
    """E18 床位项：`elder_id` 非空即已占用。"""

    bed_id: uuid.UUID
    bed_no: str
    elder_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class RoomWithBeds:
    """E18 房间项：房间 + 全量床位（含空床）。"""

    room_id: uuid.UUID
    room_no: str
    status: str
    beds: list[BedInfo]


@dataclass(frozen=True, slots=True)
class BedAssignment:
    """E19 响应：入床后的三个定位字段。"""

    elder_id: uuid.UUID
    bed_id: uuid.UUID
    room_id: uuid.UUID


def _audit(
    identity: Identity,
    *,
    action: str,
    target: str,
    result: str,
    reason: str | None = None,
    **extra: object,
) -> None:
    """写一条结构化审计日志（A-02）。

    只允许放 ID、字段名与判定结果；调用方不得把姓名、生日、token 等敏感值塞进 `extra`。
    """
    fields: dict[str, object] = {
        "trace_id": get_trace_id() or "-",
        "audit_action": action,
        "actor": f"{identity.kind}:{identity.principal_id}",
        "target": target,
        "result": result,
    }
    if reason is not None:
        fields["reason"] = reason
    fields.update(extra)
    logger.info("audit", extra=fields)


def _require_admin(identity: Identity, *, action: str, target: str) -> None:
    """写操作角色门禁：非 admin 一律 403（契约 O5 / FR-ADM-01）。

    复用 task-4 的 `check_roles`，让「角色白名单」只有一处实现；拒绝时先落审计再抛，
    否则 E5 的「403 非 admin → 审计」就没有落点。
    """
    try:
        check_roles(identity, ("admin",))
    except RoleForbiddenError:
        _audit(identity, action=action, target=target, result="forbidden", reason="role")
        raise


def _require_staff_write(identity: Identity, *, action: str, target: str) -> None:
    """建档/入床门禁：admin 与 care_worker 均可（契约 v1.8，主理人 2026-11-06 裁决）。

    医生 / 家属 / 设备 / 工牌一律 403；拒绝时先落审计再抛，
    与 `_require_admin` 同一节奏，让「403 → 审计」有统一落点。
    """
    try:
        check_roles(identity, ("admin", "care_worker"))
    except RoleForbiddenError:
        _audit(identity, action=action, target=target, result="forbidden", reason="role")
        raise


def _scope_relation(identity: Identity, *, action: str, target: str) -> str | None:
    """读操作的「角色 → relation」判定；返回 None 表示 admin 全量。

    非员工主体（家属 / 设备 / 工牌）直接拒绝：工牌不是全权会话（S-24），
    家属读档案一律 403（A-01），设备主体也不具备照护台档案视图。
    """
    if identity.kind != KIND_STAFF:
        _audit(identity, action=action, target=target, result="forbidden", reason="principal_kind")
        raise ElderScopeForbiddenError("elder archive is only readable by staff sessions")
    if identity.role == "admin":
        return None
    relation = _RELATION_BY_ROLE.get(identity.role or "")
    if relation is None:
        # 角色枚举已由 task-4 与 CHECK 约束双重保证；走到这里说明身份被手改坏了。
        _audit(identity, action=action, target=target, result="forbidden", reason="role")
        raise ElderScopeForbiddenError("staff role has no elder scope")
    return relation


def _staff_id(identity: Identity) -> uuid.UUID:
    """员工身份里的 staff_id 已被 Identity 保证为规范 UUID 文本。"""
    if identity.staff_id is None:  # pragma: no cover - Identity 构造时已保证
        raise ElderScopeForbiddenError("staff identity is malformed")
    return uuid.UUID(identity.staff_id)


def _detail_from(elder: Elder, *, room_no: str | None, bed_no: str | None) -> ElderDetail:
    return ElderDetail(
        elder_id=elder.elder_id,
        name=elder.name,
        birth_date=elder.birth_date,
        gender=elder.gender,
        persona_id=elder.persona_id,
        status=elder.status,
        session_seq=elder.session_seq,
        created_at=elder.created_at,
        room_no=room_no,
        bed_no=bed_no,
    )


async def _bed_location(
    session: AsyncSession, elder_id: uuid.UUID
) -> tuple[str | None, str | None]:
    """取长者当前床位与房号；无床位（未入住/已退房）返回 (None, None)。"""
    row = (
        await session.execute(
            select(Bed.bed_no, Room.room_no)
            .join(Room, Room.room_id == Bed.room_id)
            .where(Bed.elder_id == elder_id)
            .order_by(Bed.bed_no)
            .limit(1)
        )
    ).first()
    if row is None:
        return None, None
    return row.bed_no, row.room_no


async def _persona_exists(session: AsyncSession, persona_id: uuid.UUID) -> bool:
    return (
        await session.execute(select(Persona.persona_id).where(Persona.persona_id == persona_id))
    ).first() is not None


async def get_elder(session: AsyncSession, identity: Identity, elder_id: uuid.UUID) -> ElderDetail:
    """E6：admin 全量；护工/医生按各自 relation 判范围；家属/设备 403。"""
    target = f"elder:{elder_id}"
    relation = _scope_relation(identity, action="elder.read", target=target)

    if relation is not None:
        assignment = (
            await session.execute(
                select(ElderStaffAssignment.id)
                .where(
                    ElderStaffAssignment.elder_id == elder_id,
                    ElderStaffAssignment.staff_id == _staff_id(identity),
                    ElderStaffAssignment.relation == relation,
                )
                .limit(1)
            )
        ).first()
        if assignment is None:
            # 范围判定先于存在性判定：范围外主体无论资源在不在，都只看到 403。
            _audit(identity, action="elder.read", target=target, result="forbidden", reason="scope")
            raise ElderScopeForbiddenError("elder is outside the current staff scope")

    elder = await session.get(Elder, elder_id)
    if elder is None:
        _audit(identity, action="elder.read", target=target, result="not_found")
        raise ElderNotFoundError(str(elder_id))

    bed_no, room_no = await _bed_location(session, elder_id)
    return _detail_from(elder, room_no=room_no, bed_no=bed_no)


async def list_elders(
    session: AsyncSession,
    identity: Identity,
    *,
    page: int,
    page_size: int,
    status: str | None,
) -> ElderPage:
    """E7：admin 全量；护工/医生只列各自负责范围；无范围为 empty list，不是 403。"""
    relation = _scope_relation(identity, action="elder.list", target="elder:list")

    filters: list[Any] = []
    if relation is not None:
        filters.append(
            Elder.elder_id.in_(
                select(ElderStaffAssignment.elder_id).where(
                    ElderStaffAssignment.staff_id == _staff_id(identity),
                    ElderStaffAssignment.relation == relation,
                )
            )
        )
    if status is not None:
        filters.append(Elder.status == status)

    total = int(
        (
            await session.execute(select(func.count()).select_from(Elder).where(*filters))
        ).scalar_one()
    )

    rows = (
        await session.execute(
            select(Elder, Bed.bed_no, Room.room_no)
            .outerjoin(Bed, Bed.elder_id == Elder.elder_id)
            .outerjoin(Room, Room.room_id == Bed.room_id)
            .where(*filters)
            .order_by(Elder.elder_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    items = [
        ElderListItem(
            elder_id=elder.elder_id,
            name=elder.name,
            room_no=room_no,
            bed_no=bed_no,
            status=elder.status,
        )
        for elder, bed_no, room_no in rows
    ]
    return ElderPage(items=items, total=total)


async def create_elder(
    session: AsyncSession,
    identity: Identity,
    *,
    name: str,
    birth_date: datetime.date | None,
    gender: str | None,
    persona_id: uuid.UUID | None,
) -> ElderDetail:
    """E5：admin / care_worker（契约 v1.8）；`name` 不唯一，允许重名；默认值由数据库给出。"""
    _require_staff_write(identity, action="elder.create", target="elder:new")

    # 契约 E5 没有 409 口径；persona_id 是请求体里的引用字段，不存在按 422 处理。
    if persona_id is not None and not await _persona_exists(session, persona_id):
        _audit(
            identity,
            action="elder.create",
            target="elder:new",
            result="validation_error",
            reason="persona_not_found",
        )
        raise ElderValidationError("persona does not exist")

    elder = Elder(name=name, birth_date=birth_date, gender=gender, persona_id=persona_id)
    session.add(elder)
    try:
        await session.flush()
        # status / session_seq / created_at 都是 server_default，必须 refresh 才读得到。
        await session.refresh(elder)
    except IntegrityError:
        # 预检查与 flush 之间 persona 被删掉这类竞态：恢复事务后再映射为 422，
        # 不能把 failed transaction 留给请求级依赖去提交。
        await session.rollback()
        _audit(
            identity,
            action="elder.create",
            target="elder:new",
            result="validation_error",
            reason="integrity",
        )
        raise ElderValidationError("elder could not be created") from None

    _audit(
        identity,
        action="elder.create",
        target=f"elder:{elder.elder_id}",
        result="success",
    )
    return _detail_from(elder, room_no=None, bed_no=None)


async def update_elder(
    session: AsyncSession,
    identity: Identity,
    elder_id: uuid.UUID,
    changes: Mapping[str, Any],
) -> ElderDetail:
    """E8：仅 admin；只改白名单字段；`archived` 只允许改状态（不做物理删除）。"""
    target = f"elder:{elder_id}"
    _require_admin(identity, action="elder.update", target=target)

    unknown = set(changes) - _PATCHABLE_FIELDS
    if unknown:
        _audit(
            identity,
            action="elder.update",
            target=target,
            result="validation_error",
            reason="unknown_fields",
        )
        raise ElderValidationError("patch contains fields that are not writable")

    # 角色判定已过，这里才开始查资源：admin 查不到才是 404。
    elder = await session.get(Elder, elder_id)
    if elder is None:
        _audit(identity, action="elder.update", target=target, result="not_found")
        raise ElderNotFoundError(str(elder_id))

    if not changes:
        bed_no, room_no = await _bed_location(session, elder_id)
        return _detail_from(elder, room_no=room_no, bed_no=bed_no)

    new_status = changes.get("status", elder.status)
    if new_status not in ELDER_STATUSES:
        _audit(
            identity,
            action="elder.update",
            target=target,
            result="validation_error",
            reason="status",
        )
        raise ElderValidationError("status is not a valid elder status")

    # 「archived 只允许改状态」按生效状态判：已归档的长者也不能借 PATCH 顺手改档案。
    if new_status == "archived" and set(changes) != {"status"}:
        _audit(
            identity,
            action="elder.update",
            target=target,
            result="validation_error",
            reason="archived_with_other_fields",
        )
        raise ElderValidationError("an archived elder only allows a status change")

    if changes.get("persona_id") is not None and not await _persona_exists(
        session, changes["persona_id"]
    ):
        _audit(
            identity,
            action="elder.update",
            target=target,
            result="validation_error",
            reason="persona_not_found",
        )
        raise ElderValidationError("persona does not exist")

    # 只写白名单字段；`session_seq` 永远不会出现在 changes 里（API 模型 extra=forbid）。
    for field, value in changes.items():
        setattr(elder, field, value)

    try:
        await session.flush()
        await session.refresh(elder)
    except IntegrityError:
        await session.rollback()
        _audit(
            identity,
            action="elder.update",
            target=target,
            result="validation_error",
            reason="integrity",
        )
        raise ElderValidationError("elder could not be updated") from None

    _audit(
        identity,
        action="elder.update",
        target=target,
        result="success",
        changed_fields=sorted(changes),
    )
    bed_no, room_no = await _bed_location(session, elder_id)
    return _detail_from(elder, room_no=room_no, bed_no=bed_no)


async def list_rooms_with_beds(session: AsyncSession, identity: Identity) -> list[RoomWithBeds]:
    """E18：房间 + 全量床位（含空床），供照护台分配床位选择。

    读操作不设角色白名单以外的门槛——与 E7 一致走 `_scope_relation`，
    但房间/床位本身不属于长者责任范围数据，员工（admin/care_worker/doctor）全量可读；
    家属 / 设备 / 工牌 403。
    """
    if identity.kind != KIND_STAFF:
        _audit(
            identity,
            action="room.list",
            target="room:list",
            result="forbidden",
            reason="principal_kind",
        )
        raise ElderScopeForbiddenError("room list is only readable by staff sessions")

    rooms = (await session.execute(select(Room).order_by(Room.room_no))).scalars().all()
    beds = (await session.execute(select(Bed).order_by(Bed.room_id, Bed.bed_no))).scalars().all()

    by_room: dict[uuid.UUID, list[BedInfo]] = {}
    for bed in beds:
        by_room.setdefault(bed.room_id, []).append(
            BedInfo(bed_id=bed.bed_id, bed_no=bed.bed_no, elder_id=bed.elder_id)
        )
    return [
        RoomWithBeds(
            room_id=room.room_id,
            room_no=room.room_no,
            status=room.status,
            beds=by_room.get(room.room_id, []),
        )
        for room in rooms
    ]


async def assign_bed(
    session: AsyncSession,
    identity: Identity,
    elder_id: uuid.UUID,
    *,
    bed_id: uuid.UUID,
) -> BedAssignment:
    """E19：把长者安排到床位；若长者已在其他床，先腾空旧床（换床语义）。

    目标床位被其他长者占用是 409；长者与床位不存在是 404。
    `rooms.status` 是 beds 的派生值：本函数在写完床位后调用
    `org_status.aggregate_room_status` 同步受影响房间，与管理端共用同一口径，
    E19 的请求/响应与冲突语义不变。
    """
    target = f"elder:{elder_id}"
    _require_staff_write(identity, action="elder.assign_bed", target=target)

    elder = await session.get(Elder, elder_id)
    if elder is None:
        _audit(identity, action="elder.assign_bed", target=target, result="not_found")
        raise ElderNotFoundError(str(elder_id))

    bed = await session.get(Bed, bed_id)
    if bed is None:
        _audit(
            identity,
            action="elder.assign_bed",
            target=target,
            result="not_found",
            reason="bed",
        )
        raise ElderNotFoundError(str(bed_id))

    if bed.elder_id is not None and bed.elder_id != elder_id:
        _audit(
            identity,
            action="elder.assign_bed",
            target=target,
            result="conflict",
            reason="bed_occupied",
        )
        raise ElderConflictError("target bed is already occupied")

    # 换床：先腾空该长者当前占用的一切床位，再占新床。
    current = (await session.execute(select(Bed).where(Bed.elder_id == elder_id))).scalars().all()
    affected_rooms: set[uuid.UUID] = {bed.room_id}
    for old in current:
        if old.bed_id != bed.bed_id:
            old.elder_id = None
            old.status = "available"
            affected_rooms.add(old.room_id)

    bed.elder_id = elder_id
    bed.status = "occupied"
    await session.flush()

    # 与管理端共用 `aggregate_room_status`：入床/换床后同步受影响房间的派生状态。
    for room_id in affected_rooms:
        room = await session.get(Room, room_id)
        if room is None:  # pragma: no cover - beds.room_id 的 FK 保证房间存在
            continue
        statuses = (
            (await session.execute(select(Bed.status).where(Bed.room_id == room_id)))
            .scalars()
            .all()
        )
        room.status = aggregate_room_status(statuses)
    await session.flush()

    _audit(
        identity,
        action="elder.assign_bed",
        target=target,
        result="success",
        bed_id=str(bed.bed_id),
    )
    return BedAssignment(elder_id=elder_id, bed_id=bed.bed_id, room_id=bed.room_id)


__all__ = [
    "ELDER_STATUSES",
    "BedAssignment",
    "BedInfo",
    "ElderConflictError",
    "ElderDetail",
    "ElderListItem",
    "ElderNotFoundError",
    "ElderPage",
    "ElderScopeForbiddenError",
    "ElderServiceError",
    "ElderValidationError",
    "RoomWithBeds",
    "assign_bed",
    "create_elder",
    "get_elder",
    "list_elders",
    "list_rooms_with_beds",
    "update_elder",
]
