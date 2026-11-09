"""房间与床位管理领域服务（计划 v1.5 §3 / §5）。

本文件是房间/床位的**唯一写库层**，API 只做校验、权限与序列化。硬口径：

1. `rooms.status` 只读、由 beds 聚合（`org_status.aggregate_room_status`）；
   增删改床位后在这里重算，不提供任何手工改房间状态的入口。
2. `beds.status='occupied'` 只能由 `beds.elder_id` 派生：PATCH 显式传 `occupied`
   在请求模型层即 422，服务层再兜一道；已有入住长者的床位不允许改成空闲/维修/停用。
3. 删除房间前先查床位与设备：任一存在即 409，避免裸奔的 FK 500。
4. 每次成功写操作在同一事务内写一条 `audit_logs`（`record_audit` 只 add+flush，不 commit）；
   `detail` 固定 `{"before": …, "after": …}`，只放房号/床号/状态/elder_id，不含姓名等 PII。
5. 批量操作先按 ids `SELECT … FOR UPDATE` 取全、去重、逐条校验；任一非法整批拒绝，
   校验通过后才落第一次 flush，绝不留半批写入。
6. 唯一约束冲突捕获 `IntegrityError` 转 409，不把数据库内部细节漏成 500。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.runtime_context import Identity
from app.models import Bed, Device, Room
from app.services.audit_log_service import record_audit
from app.services.org_status import aggregate_room_status

#: 房间状态枚举（与 `rooms.status` CHECK 一致）
ROOM_STATUSES: tuple[str, ...] = ("available", "partial", "full", "maintenance")
#: 床位状态枚举（与 `beds.status` CHECK 一致）
BED_STATUSES: tuple[str, ...] = ("available", "occupied", "maintenance", "disabled")
#: 允许手工写入的床位状态；`occupied` 只能由 elder_id 派生
BED_MUTABLE_STATUSES: tuple[str, ...] = ("available", "maintenance", "disabled")


class OrgServiceError(Exception):
    """房间/床位领域错误基类：`message` 可直接回给调用方，不含内部实现细节。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class OrgNotFoundError(OrgServiceError):
    """房间 / 床位不存在。"""


class OrgConflictError(OrgServiceError):
    """资源状态冲突：重复房号/床号、删除受阻、批量含占用床位。"""


class OrgValidationError(OrgServiceError):
    """业务校验失败：空房号/床号、非法状态、手工写 occupied。"""


@dataclass(frozen=True, slots=True)
class BedSummary:
    """房间列表里的床位摘要（含空床），供管理页展示。"""

    bed_id: uuid.UUID
    bed_no: str
    status: str
    elder_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class RoomItem:
    room_id: uuid.UUID
    room_no: str
    status: str
    beds: list[BedSummary]


@dataclass(frozen=True, slots=True)
class RoomPage:
    items: list[RoomItem]
    total: int


@dataclass(frozen=True, slots=True)
class BedItem:
    """床位列表项：`room_no` 由 JOIN rooms 得出。"""

    bed_id: uuid.UUID
    room_id: uuid.UUID
    room_no: str
    bed_no: str
    status: str
    elder_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class BedPage:
    items: list[BedItem]
    total: int


# ------------------------------------------------------------------ 内部工具


def _clean_text(value: str, label: str) -> str:
    """去首尾空白并拒绝空值；空字符串不得落库（房号/床号是人工识别键）。"""
    text = value.strip()
    if not text:
        raise OrgValidationError(f"{label}不能为空")
    return text


def _like_pattern(value: str) -> str:
    """LIKE 模糊匹配串：转义 `%` / `_` / `\\`，避免用户输入被当通配符。"""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _room_snapshot(room: Room) -> dict[str, Any]:
    return {"room_no": room.room_no, "status": room.status}


def _bed_snapshot(bed: Bed, *, room_no: str | None) -> dict[str, Any]:
    return {
        "room_no": room_no,
        "bed_no": bed.bed_no,
        "status": bed.status,
        "elder_id": None if bed.elder_id is None else str(bed.elder_id),
    }


async def _write_audit(
    session: AsyncSession,
    identity: Identity,
    *,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    before: Any,
    after: Any,
    extra: dict[str, Any] | None = None,
) -> None:
    """同事务写一条管理端审计（actor_type=staff，actor_id=当前员工）。"""
    detail: dict[str, Any] = {"before": before, "after": after}
    if extra is not None:
        detail.update(extra)
    actor_id = uuid.UUID(identity.staff_id) if identity.staff_id else None
    await record_audit(
        session,
        actor_type="staff",
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
    )


async def _lock_room(session: AsyncSession, room_id: uuid.UUID) -> Room | None:
    return (
        await session.execute(select(Room).where(Room.room_id == room_id).with_for_update())
    ).scalar_one_or_none()


async def _lock_bed(session: AsyncSession, bed_id: uuid.UUID) -> Bed | None:
    return (
        await session.execute(select(Bed).where(Bed.bed_id == bed_id).with_for_update())
    ).scalar_one_or_none()


async def _bed_statuses(session: AsyncSession, room_id: uuid.UUID) -> list[str]:
    result = await session.execute(select(Bed.status).where(Bed.room_id == room_id))
    return list(result.scalars().all())


async def _sync_room_status(session: AsyncSession, room_id: uuid.UUID) -> None:
    """按该房间当前床位重算 `rooms.status`；房间不存在时静默跳过（FK 保证不会发生）。"""
    room = await session.get(Room, room_id)
    if room is None:  # pragma: no cover - beds.room_id 的 FK 保证房间存在
        return
    room.status = aggregate_room_status(await _bed_statuses(session, room_id))


async def _sync_rooms(session: AsyncSession, room_ids: Sequence[uuid.UUID]) -> None:
    for room_id in room_ids:
        await _sync_room_status(session, room_id)
    await session.flush()


async def _room_no_map(
    session: AsyncSession, room_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    rows = await session.execute(
        select(Room.room_id, Room.room_no).where(Room.room_id.in_(list(room_ids)))
    )
    return {row.room_id: row.room_no for row in rows}


def _dedupe(ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """按传入顺序去重；重复 id 不应变成「第二条 409」或重复审计。"""
    return list(dict.fromkeys(ids))


# ------------------------------------------------------------------ 读：分页与筛选


async def list_rooms(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    room_no: str | None,
    status: str | None,
) -> RoomPage:
    """房间分页列表：房号模糊 + 状态过滤，每个房间带全量床位（含空床）。"""
    filters: list[Any] = []
    if room_no:
        filters.append(Room.room_no.ilike(_like_pattern(room_no), escape="\\"))
    if status is not None:
        filters.append(Room.status == status)

    total = int(
        (await session.execute(select(func.count()).select_from(Room).where(*filters))).scalar_one()
    )
    rooms = (
        (
            await session.execute(
                select(Room)
                .where(*filters)
                .order_by(Room.room_no)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    by_room: dict[uuid.UUID, list[BedSummary]] = {}
    if rooms:
        beds = (
            (
                await session.execute(
                    select(Bed)
                    .where(Bed.room_id.in_([room.room_id for room in rooms]))
                    .order_by(Bed.bed_no)
                )
            )
            .scalars()
            .all()
        )
        for bed in beds:
            by_room.setdefault(bed.room_id, []).append(
                BedSummary(
                    bed_id=bed.bed_id,
                    bed_no=bed.bed_no,
                    status=bed.status,
                    elder_id=bed.elder_id,
                )
            )

    return RoomPage(
        items=[
            RoomItem(
                room_id=room.room_id,
                room_no=room.room_no,
                status=room.status,
                beds=by_room.get(room.room_id, []),
            )
            for room in rooms
        ],
        total=total,
    )


async def list_beds(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    room_id: uuid.UUID | None,
    bed_no: str | None,
    status: str | None,
) -> BedPage:
    """床位分页列表：房间、床号模糊与状态过滤，响应含 room_no。"""
    filters: list[Any] = []
    if room_id is not None:
        filters.append(Bed.room_id == room_id)
    if bed_no:
        filters.append(Bed.bed_no.ilike(_like_pattern(bed_no), escape="\\"))
    if status is not None:
        filters.append(Bed.status == status)

    total = int(
        (await session.execute(select(func.count()).select_from(Bed).where(*filters))).scalar_one()
    )
    rows = (
        await session.execute(
            select(Bed, Room.room_no)
            .join(Room, Room.room_id == Bed.room_id)
            .where(*filters)
            .order_by(Room.room_no, Bed.bed_no)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return BedPage(
        items=[
            BedItem(
                bed_id=bed.bed_id,
                room_id=bed.room_id,
                room_no=room_no,
                bed_no=bed.bed_no,
                status=bed.status,
                elder_id=bed.elder_id,
            )
            for bed, room_no in rows
        ],
        total=total,
    )


# ------------------------------------------------------------------ 房间写操作


async def create_room(session: AsyncSession, identity: Identity, *, room_no: str) -> Room:
    """新建房间：房号非空且全局唯一；初始状态 `available`。"""
    clean_no = _clean_text(room_no, "房号")
    exists = (
        await session.execute(select(Room.room_id).where(Room.room_no == clean_no).limit(1))
    ).scalar_one_or_none()
    if exists is not None:
        raise OrgConflictError(f"房号已存在：{clean_no}")

    room = Room(room_no=clean_no, status="available")
    session.add(room)
    try:
        await session.flush()
        await session.refresh(room)
    except IntegrityError:
        # flush 失败会让 session 进入 pending-rollback；API 会把本异常映射成 409 并正常返回，
        # 请求级依赖随后还要 commit，所以必须就地回滚，不能把坏事务留给下一层。
        await session.rollback()
        raise OrgConflictError(f"房号已存在：{clean_no}") from None

    await _write_audit(
        session,
        identity,
        action="room_create",
        resource_type="room",
        resource_id=room.room_id,
        before=None,
        after=_room_snapshot(room),
    )
    return room


async def update_room(
    session: AsyncSession, identity: Identity, room_id: uuid.UUID, *, room_no: str
) -> Room:
    """改房号：先锁房间行串行化唯一性判定，再捕获 `IntegrityError` 兜竞态。"""
    room = await _lock_room(session, room_id)
    if room is None:
        raise OrgNotFoundError(f"房间不存在：{room_id}")

    clean_no = _clean_text(room_no, "房号")
    before = _room_snapshot(room)
    if room.room_no != clean_no:
        room.room_no = clean_no
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise OrgConflictError(f"房号已存在：{clean_no}") from None

    await _write_audit(
        session,
        identity,
        action="room_update",
        resource_type="room",
        resource_id=room.room_id,
        before=before,
        after=_room_snapshot(room),
    )
    return room


async def delete_room(session: AsyncSession, identity: Identity, room_id: uuid.UUID) -> None:
    """删除房间：该房间必须无床位且无设备，否则 409（避免 FK 违约变 500）。"""
    room = await _lock_room(session, room_id)
    if room is None:
        raise OrgNotFoundError(f"房间不存在：{room_id}")

    bed_count = int(
        (
            await session.execute(
                select(func.count()).select_from(Bed).where(Bed.room_id == room_id)
            )
        ).scalar_one()
    )
    if bed_count:
        raise OrgConflictError(f"房间下仍有 {bed_count} 张床位，请先删除床位后再删除房间")

    device_count = int(
        (
            await session.execute(
                select(func.count()).select_from(Device).where(Device.room_id == room_id)
            )
        ).scalar_one()
    )
    if device_count:
        raise OrgConflictError(f"房间仍被 {device_count} 台设备引用，请先解绑或删除设备")

    before = _room_snapshot(room)
    await session.delete(room)
    await session.flush()
    await _write_audit(
        session,
        identity,
        action="room_delete",
        resource_type="room",
        resource_id=room_id,
        before=before,
        after=None,
    )


# ------------------------------------------------------------------ 床位写操作


async def create_bed(
    session: AsyncSession, identity: Identity, *, room_id: uuid.UUID, bed_no: str
) -> Bed:
    """新建床位：房间必须存在，床号在同一房间内唯一，初始状态 `available`。"""
    clean_no = _clean_text(bed_no, "床号")
    room = await _lock_room(session, room_id)
    if room is None:
        raise OrgNotFoundError(f"房间不存在：{room_id}")

    duplicate = (
        await session.execute(
            select(Bed.bed_id).where(Bed.room_id == room_id, Bed.bed_no == clean_no).limit(1)
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise OrgConflictError(f"同一房间下床号已存在：{clean_no}")

    bed = Bed(room_id=room_id, bed_no=clean_no, status="available", elder_id=None)
    session.add(bed)
    try:
        await session.flush()
        await session.refresh(bed)
    except IntegrityError:
        await session.rollback()
        raise OrgConflictError(f"同一房间下床号已存在：{clean_no}") from None

    await _sync_room_status(session, room_id)
    await session.flush()
    await _write_audit(
        session,
        identity,
        action="bed_create",
        resource_type="bed",
        resource_id=bed.bed_id,
        before=None,
        after=_bed_snapshot(bed, room_no=room.room_no),
    )
    return bed


async def update_bed(
    session: AsyncSession,
    identity: Identity,
    bed_id: uuid.UUID,
    *,
    bed_no: str | None,
    status: str | None,
) -> Bed:
    """改床号/状态：`occupied` 不可手写；已有入住长者的床位不得改成空闲/维修/停用。"""
    bed = await _lock_bed(session, bed_id)
    if bed is None:
        raise OrgNotFoundError(f"床位不存在：{bed_id}")

    room = await session.get(Room, bed.room_id)
    room_no = room.room_no if room is not None else None
    before = _bed_snapshot(bed, room_no=room_no)

    if status is not None:
        if status == "occupied":
            raise OrgValidationError("床位状态 occupied 只能由入住长者派生，不能手工设置")
        if status not in BED_MUTABLE_STATUSES:
            raise OrgValidationError(f"非法床位状态：{status}")
        if bed.elder_id is not None:
            raise OrgConflictError(
                "床位已有入住长者，状态只能由 elder_id 派生，不能改为空闲/维修/停用"
            )
        bed.status = status

    if bed_no is not None:
        clean_no = _clean_text(bed_no, "床号")
        if clean_no != bed.bed_no:
            duplicate = (
                await session.execute(
                    select(Bed.bed_id)
                    .where(
                        Bed.room_id == bed.room_id,
                        Bed.bed_no == clean_no,
                        Bed.bed_id != bed.bed_id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                raise OrgConflictError(f"同一房间下床号已存在：{clean_no}")
            bed.bed_no = clean_no

    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise OrgConflictError("同一房间下床号已存在") from None

    await _sync_room_status(session, bed.room_id)
    await session.flush()
    await _write_audit(
        session,
        identity,
        action="bed_update",
        resource_type="bed",
        resource_id=bed.bed_id,
        before=before,
        after=_bed_snapshot(bed, room_no=room_no),
    )
    return bed


async def delete_bed(session: AsyncSession, identity: Identity, bed_id: uuid.UUID) -> None:
    """删除床位：只允许无入住长者且状态非 occupied 的床。"""
    bed = await _lock_bed(session, bed_id)
    if bed is None:
        raise OrgNotFoundError(f"床位不存在：{bed_id}")
    if bed.elder_id is not None or bed.status == "occupied":
        raise OrgConflictError("床位已有入住长者，不能删除")

    room_no_row = (
        await session.execute(select(Room.room_no).where(Room.room_id == bed.room_id))
    ).scalar_one_or_none()
    room_id = bed.room_id
    before = _bed_snapshot(bed, room_no=room_no_row)
    bed_id_value = bed.bed_id

    await session.delete(bed)
    await session.flush()
    await _sync_room_status(session, room_id)
    await session.flush()
    await _write_audit(
        session,
        identity,
        action="bed_delete",
        resource_type="bed",
        resource_id=bed_id_value,
        before=before,
        after=None,
    )


# ------------------------------------------------------------------ 批量操作


async def batch_update_beds(
    session: AsyncSession,
    identity: Identity,
    *,
    bed_ids: Sequence[uuid.UUID],
    status: str,
) -> list[Bed]:
    """批量改状态：先取全并逐条校验，任一占用/不存在整批拒绝，校验通过后才写。"""
    if status == "occupied":
        raise OrgValidationError("床位状态 occupied 只能由入住长者派生，不能批量设置")
    if status not in BED_MUTABLE_STATUSES:
        raise OrgValidationError(f"非法床位状态：{status}")

    wanted = _dedupe(bed_ids)
    if not wanted:
        raise OrgValidationError("bed_ids 不能为空")

    beds = (
        (await session.execute(select(Bed).where(Bed.bed_id.in_(wanted)).with_for_update()))
        .scalars()
        .all()
    )
    found = {bed.bed_id for bed in beds}
    missing = [str(bed_id) for bed_id in wanted if bed_id not in found]
    if missing:
        raise OrgNotFoundError("以下床位不存在：" + "、".join(missing))

    blocked = [bed.bed_no for bed in beds if bed.elder_id is not None]
    if blocked:
        raise OrgConflictError("以下床位已有入住长者，不能批量改状态：" + "、".join(blocked))

    room_ids = {bed.room_id for bed in beds}
    room_no_map = await _room_no_map(session, list(room_ids))
    before = [_bed_snapshot(bed, room_no=room_no_map.get(bed.room_id)) for bed in beds]

    for bed in beds:
        bed.status = status
    await session.flush()
    await _sync_rooms(session, list(room_ids))
    after = [_bed_snapshot(bed, room_no=room_no_map.get(bed.room_id)) for bed in beds]

    await _write_audit(
        session,
        identity,
        action="bed_batch_status",
        resource_type="bed",
        resource_id=None,
        before=before,
        after=after,
        extra={"bed_ids": [str(bed.bed_id) for bed in beds]},
    )
    return list(beds)


async def batch_delete_beds(
    session: AsyncSession,
    identity: Identity,
    *,
    bed_ids: Sequence[uuid.UUID],
) -> int:
    """批量删空床：任一床位有入住长者/处于 occupied 时整批拒绝（409）。"""
    wanted = _dedupe(bed_ids)
    if not wanted:
        raise OrgValidationError("bed_ids 不能为空")

    beds = (
        (await session.execute(select(Bed).where(Bed.bed_id.in_(wanted)).with_for_update()))
        .scalars()
        .all()
    )
    found = {bed.bed_id for bed in beds}
    missing = [str(bed_id) for bed_id in wanted if bed_id not in found]
    if missing:
        raise OrgNotFoundError("以下床位不存在：" + "、".join(missing))

    blocked = [bed.bed_no for bed in beds if bed.elder_id is not None or bed.status == "occupied"]
    if blocked:
        raise OrgConflictError("以下床位已有入住长者，不能批量删除：" + "、".join(blocked))

    room_ids = {bed.room_id for bed in beds}
    room_no_map = await _room_no_map(session, list(room_ids))
    before = [_bed_snapshot(bed, room_no=room_no_map.get(bed.room_id)) for bed in beds]
    bed_ids_value = [str(bed.bed_id) for bed in beds]

    for bed in beds:
        await session.delete(bed)
    await session.flush()
    await _sync_rooms(session, list(room_ids))

    await _write_audit(
        session,
        identity,
        action="bed_batch_delete",
        resource_type="bed",
        resource_id=None,
        before=before,
        after=[],
        extra={"bed_ids": bed_ids_value},
    )
    return len(beds)


__all__ = [
    "BED_MUTABLE_STATUSES",
    "BED_STATUSES",
    "ROOM_STATUSES",
    "BedItem",
    "BedPage",
    "BedSummary",
    "OrgConflictError",
    "OrgNotFoundError",
    "OrgServiceError",
    "OrgValidationError",
    "RoomItem",
    "RoomPage",
    "batch_delete_beds",
    "batch_update_beds",
    "create_bed",
    "create_room",
    "delete_bed",
    "delete_room",
    "list_beds",
    "list_rooms",
    "update_bed",
    "update_room",
]
