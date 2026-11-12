"""设备绑定领域服务（接口契约 E9–E11）。

关键口径：

- **`device : elder` 1:1**：service 预检查 + 数据库部分唯一索引 `uq_devices_elder_id` 双保险；
  捕获 `IntegrityError` 后必须 `rollback()` 再映射 409，不能留下 failed transaction。
- **解绑即失效**（AC-24）：`elder_id = NULL`、`status = 'offline'`、轮换 `token_hash`。
  `bind` 传 `elder_id=null` 走**同一个** `_clear_binding`，不存在绕过哈希轮换的后门。
- **候选集只来自床位**：`beds WHERE room_id = devices.room_id AND elder_id IS NOT NULL`，
  不拿 `devices.elder_id` 当集合，因此不会混入他房长者。
- 新 `token_hash` 由 `secrets.token_urlsafe` 生成后做 SHA-256；明文 secret 不返回、不记录。

审计按契约 A-02 只写结构化日志（`trace_id / audit_action / actor / target / result`），
不写 token、哈希或长者姓名。
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import RoleForbiddenError, check_roles
from app.core.context import get_trace_id
from app.core.redact import mask_name as _mask_display_name
from app.core.runtime_context import Identity
from app.core.security import KIND_DEVICE, KIND_STAFF, sha256_hex
from app.models import Bed, Device, Elder, Room

logger = logging.getLogger(__name__)


class DeviceServiceError(Exception):
    """设备领域错误基类。"""


class DeviceNotFoundError(DeviceServiceError):
    """admin 查询不存在的设备；设备主体自身缺行按撤销处理（S-25），不会走到这里。"""


class DeviceScopeForbiddenError(DeviceServiceError):
    """设备主体查别的设备，或非 admin / 非设备的其他角色访问候选接口。"""


class DeviceConflictError(DeviceServiceError):
    """绑定冲突：长者已绑其他设备、长者不在目标房间床位、目标房间不存在。"""


@dataclass(frozen=True, slots=True)
class DeviceCreated:
    """E16 建档结果：明文 secret 只在此处出现一次，调用方必须立即交给前端展示。"""

    device_id: uuid.UUID
    room_id: uuid.UUID
    status: str
    device_secret: str


@dataclass(frozen=True, slots=True)
class DeviceListItem:
    """E17 列表项：room_no 由 JOIN rooms 得出，供照护台展示。"""

    device_id: uuid.UUID
    room_id: uuid.UUID
    room_no: str
    elder_id: uuid.UUID | None
    status: str


@dataclass(frozen=True, slots=True)
class DeviceBinding:
    device_id: uuid.UUID
    room_id: uuid.UUID
    elder_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ElderCandidate:
    elder_id: uuid.UUID
    bed_no: str
    display_name: str


@dataclass(frozen=True, slots=True)
class CandidateList:
    room_id: uuid.UUID
    mode: str
    candidates: list[ElderCandidate]


def _audit(
    identity: Identity,
    *,
    action: str,
    target: str,
    result: str,
    reason: str | None = None,
) -> None:
    fields: dict[str, object] = {
        "trace_id": get_trace_id() or "-",
        "audit_action": action,
        "actor": f"{identity.kind}:{identity.principal_id}",
        "target": target,
        "result": result,
    }
    if reason is not None:
        fields["reason"] = reason
    logger.info("audit", extra=fields)


def _require_admin(identity: Identity, *, action: str, target: str) -> None:
    """E9/E10 仅 admin；复用 task-4 的角色判定并落审计。"""
    try:
        check_roles(identity, ("admin",))
    except RoleForbiddenError:
        _audit(identity, action=action, target=target, result="forbidden", reason="role")
        raise


def _require_staff_write(identity: Identity, *, action: str, target: str) -> None:
    """E16/E17 门禁：admin 与 care_worker 均可（契约 v1.8，主理人 2026-11-06 裁决）。

    与 elder_service 的同名门禁同一节奏：拒绝先落审计再抛。
    """
    try:
        check_roles(identity, ("admin", "care_worker"))
    except RoleForbiddenError:
        _audit(identity, action=action, target=target, result="forbidden", reason="role")
        raise


def _clear_binding(device: Device) -> None:
    """解绑的唯一定义：清长者、置 offline、轮换长期令牌哈希。

    `bind(elder_id=null)` 与 `unbind` 都调用本函数，防止出现一条不轮换哈希的旁路。
    新哈希来自密码学安全随机值，明文 secret 在函数内即被丢弃，不返回也不记录。
    """
    device.elder_id = None
    device.status = "offline"
    device.token_hash = sha256_hex(secrets.token_urlsafe(32))


def _binding(device: Device) -> DeviceBinding:
    return DeviceBinding(
        device_id=device.device_id, room_id=device.room_id, elder_id=device.elder_id
    )


async def unbind_all_for_elder(session: AsyncSession, *, elder_id: uuid.UUID) -> int:
    """退房流水线步骤⑤（Day 9 A3 / X12「A 自有」行）：解绑该长者名下全部设备。

    与 E10 `unbind_device` 的差异：**无 admin 身份检查、不写 DB 审计**——权限由
    调用方（lifecycle REST，admin-only）把关，审计由 lifecycle 流水线统一写
    （X12：审计归 A，每步 1 条，各域不重复写）。解绑定义与 `_clear_binding`
    严格同源：清长者、置 offline、轮换 token_hash（旧设备令牌即 401）。
    """
    devices = (
        (await session.execute(select(Device).where(Device.elder_id == elder_id))).scalars().all()
    )
    for device in devices:
        _clear_binding(device)
    await session.commit()
    logger.info(
        "lifecycle unbind_all",
        extra={
            "trace_id": get_trace_id() or "-",
            "audit_action": "device.unbind_all",
            "target": f"elder:{elder_id}",
            "result": f"unbound:{len(devices)}",
        },
    )
    return len(devices)


# 候选姓名脱敏唯一实现在 app/core/redact.py（Day 9 A1b 集中化），
# `_mask_display_name` 已在文件头 import 处以别名 re-export，调用点签名不变。


async def bind_device(
    session: AsyncSession,
    identity: Identity,
    device_id: uuid.UUID,
    *,
    room_id: uuid.UUID,
    elder_id: uuid.UUID | None,
) -> DeviceBinding:
    """E9：admin / care_worker（契约 v1.8）把设备绑到房间（及可选长者）。

    长者必须在目标房间的床位里，否则 409；长者已绑其他设备也是 409。
    预检查负责给出可读原因，唯一索引负责兜住并发；两者都在，不是二选一。
    """
    target = f"device:{device_id}"
    _require_staff_write(identity, action="device.bind", target=target)

    device = await session.get(Device, device_id)
    if device is None:
        _audit(identity, action="device.bind", target=target, result="not_found")
        raise DeviceNotFoundError(str(device_id))

    room = await session.get(Room, room_id)
    if room is None:
        # 契约 E9 只把「设备不存在」定义为 404；房间不存在等价于目标房间没有床位，归 409。
        _audit(identity, action="device.bind", target=target, result="conflict", reason="room")
        raise DeviceConflictError("target room does not exist")

    if elder_id is None:
        # 显式绑定为「空长者」＝解绑语义，必须同样轮换哈希（AC-24）。
        device.room_id = room_id
        _clear_binding(device)
        await _flush_or_conflict(session, identity, action="device.bind", target=target)
        _audit(identity, action="device.bind", target=target, result="success")
        return _binding(device)

    in_room = (
        await session.execute(
            select(Bed.bed_id).where(Bed.room_id == room_id, Bed.elder_id == elder_id).limit(1)
        )
    ).first()
    if in_room is None:
        _audit(
            identity,
            action="device.bind",
            target=target,
            result="conflict",
            reason="elder_not_in_room",
        )
        raise DeviceConflictError("elder is not in a bed of the target room")

    other_device = (
        await session.execute(
            select(Device.device_id)
            .where(Device.elder_id == elder_id, Device.device_id != device_id)
            .limit(1)
        )
    ).first()
    if other_device is not None:
        _audit(
            identity,
            action="device.bind",
            target=target,
            result="conflict",
            reason="elder_already_bound",
        )
        raise DeviceConflictError("elder is already bound to another device")

    device.room_id = room_id
    device.elder_id = elder_id
    await _flush_or_conflict(session, identity, action="device.bind", target=target)
    _audit(identity, action="device.bind", target=target, result="success")
    return _binding(device)


async def unbind_device(
    session: AsyncSession, identity: Identity, device_id: uuid.UUID
) -> DeviceBinding:
    """E10：admin 解绑；旧设备会话因 token_hash 轮换而立即 401。"""
    target = f"device:{device_id}"
    _require_admin(identity, action="device.unbind", target=target)

    device = await session.get(Device, device_id)
    if device is None:
        _audit(identity, action="device.unbind", target=target, result="not_found")
        raise DeviceNotFoundError(str(device_id))

    _clear_binding(device)
    await _flush_or_conflict(session, identity, action="device.unbind", target=target)
    _audit(identity, action="device.unbind", target=target, result="success")
    return _binding(device)


async def _flush_or_conflict(
    session: AsyncSession, identity: Identity, *, action: str, target: str
) -> None:
    """flush；唯一约束冲突时 rollback 恢复事务，再映射 409。

    不 rollback 的后果不是「多一条错误日志」，而是同请求后续任何 SQL 都撞
    `current transaction is aborted`，且依赖层会把半成品一起提交或报 500。
    """
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        _audit(identity, action=action, target=target, result="conflict", reason="integrity")
        raise DeviceConflictError("device binding violates a unique constraint") from None


async def list_elder_candidates(
    session: AsyncSession, identity: Identity, device_id: uuid.UUID
) -> CandidateList:
    """E11：admin 或设备本人读取本房间候选长者（单人间 1 位 / 双人间 2 位）。

    候选来源严格是 `beds`：即使 `devices.elder_id` 为空（刚解绑），只要床位上还有人，
    平板仍需要列出候选供声纹身份使用。
    """
    target = f"device:{device_id}"
    if identity.kind == KIND_STAFF:
        try:
            check_roles(identity, ("admin",))
        except RoleForbiddenError:
            _audit(
                identity,
                action="device.candidates",
                target=target,
                result="forbidden",
                reason="role",
            )
            raise
    elif identity.kind == KIND_DEVICE:
        if identity.device_id != str(device_id):
            _audit(
                identity,
                action="device.candidates",
                target=target,
                result="forbidden",
                reason="other_device",
            )
            raise DeviceScopeForbiddenError("a device may only query its own candidates")
    else:
        _audit(
            identity,
            action="device.candidates",
            target=target,
            result="forbidden",
            reason="principal_kind",
        )
        raise DeviceScopeForbiddenError("candidate list is not available to this principal")

    device = await session.get(Device, device_id)
    if device is None:
        _audit(identity, action="device.candidates", target=target, result="not_found")
        raise DeviceNotFoundError(str(device_id))

    rows = (
        await session.execute(
            select(Bed.elder_id, Bed.bed_no, Elder.name)
            .join(Elder, Elder.elder_id == Bed.elder_id)
            .where(Bed.room_id == device.room_id, Bed.elder_id.is_not(None))
            .order_by(Bed.bed_no)
        )
    ).all()

    candidates = [
        ElderCandidate(
            elder_id=elder_id,
            bed_no=bed_no,
            display_name=_mask_display_name(name),
        )
        for elder_id, bed_no, name in rows
        if elder_id is not None  # Bed.elder_id 可空，SQL 已过滤，此处仅为类型收窄
    ]
    # PRD §3.2：单人间 1 位、双人间 2 位。0 位（空房）按 single 返回空列表。
    mode = "double" if len(candidates) == 2 else "single"
    return CandidateList(room_id=device.room_id, mode=mode, candidates=candidates)


async def create_device(
    session: AsyncSession,
    identity: Identity,
    *,
    room_id: uuid.UUID,
) -> DeviceCreated:
    """E16：设备建档。生成一次性明文 secret，库里只落 SHA-256（同 E10 轮换口径）。

    房间不存在 404（Device.room_id NOT NULL，建档必须先选房间）；
    明文 secret 只经返回值交给 API 层展示一次，不进日志、不再可查。
    """
    target = "device:new"
    _require_staff_write(identity, action="device.create", target=target)

    room = await session.get(Room, room_id)
    if room is None:
        _audit(identity, action="device.create", target=target, result="not_found", reason="room")
        raise DeviceNotFoundError(str(room_id))

    device_secret = secrets.token_urlsafe(32)
    device = Device(room_id=room_id, token_hash=sha256_hex(device_secret), status="offline")
    session.add(device)
    await session.flush()
    _audit(identity, action="device.create", target=f"device:{device.device_id}", result="success")
    return DeviceCreated(
        device_id=device.device_id,
        room_id=device.room_id,
        status=device.status,
        device_secret=device_secret,
    )


async def list_devices(session: AsyncSession, identity: Identity) -> list[DeviceListItem]:
    """E17：全量设备列表（含未绑定），room_no 由 JOIN rooms 得出。"""
    _require_staff_write(identity, action="device.list", target="device:list")

    rows = (
        await session.execute(
            select(Device, Room.room_no)
            .join(Room, Room.room_id == Device.room_id)
            .order_by(Room.room_no, Device.device_id)
        )
    ).all()
    return [
        DeviceListItem(
            device_id=device.device_id,
            room_id=device.room_id,
            room_no=room_no,
            elder_id=device.elder_id,
            status=device.status,
        )
        for device, room_no in rows
    ]


__all__ = [
    "CandidateList",
    "DeviceBinding",
    "DeviceConflictError",
    "DeviceCreated",
    "DeviceListItem",
    "DeviceNotFoundError",
    "DeviceScopeForbiddenError",
    "DeviceServiceError",
    "ElderCandidate",
    "bind_device",
    "create_device",
    "list_devices",
    "list_elder_candidates",
    "unbind_device",
]
