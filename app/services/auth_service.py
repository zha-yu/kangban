"""身份认证领域服务 —— 唯一写库层（AGENTS §2）。

本模块**是** `app/api/auth.py` 之外唯一能 import ORM 的地方。
`app/api/auth.py` 禁止 import `StaffAccount` / `GuardianAccount` / `Device`，
只能 import 本模块和 `app/core/auth.py` 的 FastAPI 依赖。

依赖的 task-4 交付：
  - app.core.security  —— 密码哈希、JWT 签发、sha256
  - app.core.auth      —— decode_refresh_token、异常类型
  - app.core.runtime_context —— Identity
  - app.db             —— AsyncSession（由 FastAPI 依赖注入进来，本模块不自己建连接）
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    CredentialExpiredError,
    CredentialInvalidError,
    decode_refresh_token,
)
from app.core.config import get_settings
from app.core.runtime_context import Identity
from app.core.security import (
    _DEVICE_TOKEN_HASH_PREFIX_LENGTH,
    KIND_GUARDIAN,
    KIND_STAFF,
    access_ttl_seconds,
    constant_time_equals,
    device_ttl_seconds,
    sha256_hex,
    sign_device_session,
    sign_staff_access,
    sign_staff_refresh,
    verify_password,
)
from app.models import (
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    StaffAccount,
)
from app.models.organization import Bed, Device

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 业务异常 —— 由 auth.py 统一捕获并映射 HTTP 码（契约 §7）
# ---------------------------------------------------------------------------


class AuthServiceError(Exception):
    """auth_service 层抛出的业务异常。auth.py 按 `kind` 字段映射到 HTTP 码。"""

    kind: str = "TOKEN_INVALID"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class AuthInvalidError(AuthServiceError):
    """401 通用鉴权失败（密码错、签名错、类型不符等）。"""

    kind = "TOKEN_INVALID"


class AuthExpiredError(AuthServiceError):
    """401 凭证过期。"""

    kind = "TOKEN_EXPIRED"


class AuthRevokedError(AuthServiceError):
    """401 凭证已失效（token_version 被提、账号停用、设备锁定等）。"""

    kind = "TOKEN_REVOKED"


class AuthConflictError(AuthServiceError):
    """409 手机号同时存在于 staff 与 guardian 两表（契约 S-02 裁决）。"""

    kind = "CONFLICT"


# ---------------------------------------------------------------------------
# 返回 DTO（service 层不把 ORM 对象直接返回，避免泄露 password_hash 等字段）
# ---------------------------------------------------------------------------


@dataclass
class _PrincipalBrief:
    kind: Literal["staff", "guardian"]
    id: str
    role: str | None
    name: str


@dataclass
class LoginResult:
    access_token: str
    refresh_token: str
    expires_in: int
    principal: _PrincipalBrief


@dataclass
class RefreshResult:
    access_token: str
    expires_in: int


@dataclass
class _DeviceBrief:
    id: str
    room_id: str
    elder_id: str | None


@dataclass
class DeviceLoginResult:
    access_token: str
    expires_in: int
    device: _DeviceBrief


@dataclass
class CurrentIdentityResult:
    kind: Literal["staff", "guardian", "device", "staff_card"]
    id: str
    role: str | None
    name: str | None
    scope_elder_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _mask_phone(phone: str | None) -> str:
    """日志脱敏：只留后 4 位。空值返回空串。"""
    if not phone:
        return ""
    tail = phone[-4:]
    return f"****{tail}"


# ---------------------------------------------------------------------------
# 四个公开方法
# ---------------------------------------------------------------------------


async def login_with_phone(
    session: AsyncSession,
    *,
    phone: str,
    password: str,
) -> LoginResult:
    """人账号登录（E1）。

    查询顺序：先 staff_accounts → 再 guardian_accounts → 两表都没命中也是同一文案
    （防枚举：契约硬约束，不区分"账号不存在"与"密码错误"）。
    两表都有同一手机号 → 409（契约 S-02 裁决，唯一约束理应防住，但服务层显式拦截）。
    """
    settings = get_settings()

    # --- 1. 查 staff_accounts ---
    staff_row = (
        await session.execute(select(StaffAccount).where(StaffAccount.phone == phone))
    ).scalar_one_or_none()
    guardian_row = None
    if staff_row is None:
        guardian_row = (
            await session.execute(select(GuardianAccount).where(GuardianAccount.phone == phone))
        ).scalar_one_or_none()
    elif (
        settings.app_env != "prod"
    ):  # prod 也检查一次留日志，非 prod 显式拦截帮助开发排查唯一约束漏落库
        guardian_row = (
            await session.execute(select(GuardianAccount).where(GuardianAccount.phone == phone))
        ).scalar_one_or_none()
        if guardian_row is not None:
            logger.error(
                "phone collision across staff and guardian tables: phone=%s",
                _mask_phone(phone),
            )
            raise AuthConflictError("手机号冲突")

    target = staff_row if staff_row is not None else guardian_row
    if target is None:
        logger.info("login failed: phone=%s not found in either table", _mask_phone(phone))
        raise AuthInvalidError("用户名或密码错误")

    # --- 2. 验密码 ---
    if not verify_password(password, target.password_hash):
        logger.info("login failed: phone=%s password mismatch", _mask_phone(phone))
        raise AuthInvalidError("用户名或密码错误")

    # --- 3. 状态检查 ---
    if target.status != "active":
        logger.warning(
            "login rejected: phone=%s account status=%s",
            _mask_phone(phone),
            target.status,
        )
        raise AuthRevokedError("账号已停用")

    # --- 4. 签发 token ---
    is_staff = staff_row is not None
    kind = KIND_STAFF if is_staff else KIND_GUARDIAN
    principal_id = str(target.staff_id if is_staff else target.guardian_id)
    role = getattr(target, "role", None)  # GuardianAccount 没有 role

    access = sign_staff_access(
        kind=kind,
        staff_id=principal_id,
        role=role or "",
        token_version=target.token_version,
    )
    refresh = sign_staff_refresh(
        kind=kind,
        staff_id=principal_id,
        token_version=target.token_version,
    )

    principal = _PrincipalBrief(
        kind=kind,
        id=principal_id,
        role=role,
        name=target.name,
    )

    logger.info("login success: kind=%s id=%s", kind, principal_id)
    return LoginResult(
        access_token=access,
        refresh_token=refresh,
        expires_in=access_ttl_seconds(),
        principal=principal,
    )


async def refresh_access_token(
    session: AsyncSession,
    *,
    refresh_token: str,
) -> RefreshResult:
    """刷新凭证（E2）。

    流程：解 refresh → 拆 sub 查对应表 → tv 比对 → 状态检查 → 签新 access。
    Refresh 本身不轮换（契约 O2 未要求）；tv 变了 refresh 自然失效。
    """
    try:
        claims = decode_refresh_token(refresh_token)
    except CredentialExpiredError as exc:
        raise AuthExpiredError("refresh token 已过期") from exc
    except CredentialInvalidError as exc:
        raise AuthInvalidError("refresh token 无效") from exc

    sub = str(claims["sub"])  # "staff:<uuid>" 或 "guardian:<uuid>"
    kind, _, raw_id = sub.partition(":")
    if kind == KIND_STAFF:
        row = (
            await session.execute(
                select(StaffAccount).where(StaffAccount.staff_id == uuid.UUID(raw_id))
            )
        ).scalar_one_or_none()
    elif kind == KIND_GUARDIAN:
        row = (
            await session.execute(
                select(GuardianAccount).where(GuardianAccount.guardian_id == uuid.UUID(raw_id))
            )
        ).scalar_one_or_none()
    else:
        raise AuthInvalidError("refresh token type 不符")

    if row is None:
        logger.warning("refresh rejected: principal row not found, sub=%s", sub)
        raise AuthRevokedError("token 已失效")

    # tv 比对（AC-24）
    token_version = int(claims["tv"])
    if token_version != row.token_version:
        logger.warning(
            "refresh rejected: tv mismatch, sub=%s payload=%s db=%s",
            sub,
            token_version,
            row.token_version,
        )
        raise AuthRevokedError("token 已失效")

    # 状态
    if row.status != "active":
        raise AuthRevokedError("账号已停用")

    role = getattr(row, "role", "") or ""
    new_access = sign_staff_access(
        kind=kind,
        staff_id=raw_id,
        role=role,
        token_version=row.token_version,
    )

    logger.info("refresh success: kind=%s id=%s", kind, raw_id)
    return RefreshResult(access_token=new_access, expires_in=access_ttl_seconds())


async def exchange_device_token(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    device_secret: str,
) -> DeviceLoginResult:
    """设备换会话（E4）。

    两层凭证（契约 S-03）：长期层 sha256 比对 → 会话层 24h JWT。
    解绑即失效靠 token_hash 轮换（tv 前缀不匹配），不需要黑名单。
    """
    device_row = (
        await session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one_or_none()
    if device_row is None:
        logger.warning("device-session rejected: device_id=%s not found", device_id)
        raise AuthInvalidError("设备不存在")

    # 长期令牌比对：sha256 后常量时间比较（契约 S-03）
    provided_hash = sha256_hex(device_secret)
    if not constant_time_equals(provided_hash, device_row.token_hash):
        logger.warning("device-session rejected: secret mismatch, device_id=%s", device_id)
        raise AuthInvalidError("设备令牌错误")

    # 设备状态：只有 locked 拒（契约 S-21）
    if device_row.status == "locked":
        logger.warning("device-session rejected: device locked, device_id=%s", device_id)
        raise AuthRevokedError("设备已锁定")

    # 没绑定长者 + 房间内也没在住长者 → 拒（契约 E4）
    elder_id = device_row.elder_id
    if elder_id is None:
        occupied_beds = (
            await session.execute(
                select(func.count(Bed.bed_id)).where(
                    Bed.room_id == device_row.room_id,
                    Bed.elder_id.is_not(None),
                )
            )
        ).scalar_one()
        if occupied_beds == 0:
            logger.warning(
                "device-session rejected: device has no elder binding "
                "and room is empty, device_id=%s",
                device_id,
            )
            raise AuthRevokedError("设备未绑定长者")

    # 签设备会话：tv 是 token_hash 前 16 位（契约 §5.1 / S-03）
    token_hash_prefix = device_row.token_hash[:_DEVICE_TOKEN_HASH_PREFIX_LENGTH]
    access = sign_device_session(
        device_id=device_row.device_id,
        token_hash_prefix=token_hash_prefix,
    )

    device_brief = _DeviceBrief(
        id=str(device_row.device_id),
        room_id=str(device_row.room_id),
        elder_id=str(device_row.elder_id) if device_row.elder_id else None,
    )

    logger.info("device-session success: device_id=%s", device_id)
    return DeviceLoginResult(
        access_token=access,
        expires_in=device_ttl_seconds(),
        device=device_brief,
    )


async def get_current_identity(
    session: AsyncSession,
    *,
    identity: Identity,
) -> CurrentIdentityResult:
    """当前身份详情（E3）。

    scope_elder_ids 的来源：
      - staff   → elder_staff_assignments WHERE staff_id=? AND active（admin 直接返回空列表）
      - guardian → elder_guardians WHERE guardian_id=? AND status='active'
      - device  → devices.elder_id 或按 room 推导（由调用方决定，这里先返回空）
      - staff_card → 同 device
    """
    scope_elder_ids: list[str] = []
    name: str | None = None

    if identity.kind == KIND_STAFF:
        row = (
            await session.execute(
                select(StaffAccount).where(StaffAccount.staff_id == uuid.UUID(identity.staff_id))
            )
        ).scalar_one_or_none()
        if row is None:
            raise AuthRevokedError("账号已不存在")
        name = row.name
        # admin 全量权限 → scope 为空列表
        if row.role != "admin":
            assignments = (
                (
                    await session.execute(
                        select(ElderStaffAssignment.elder_id).where(
                            ElderStaffAssignment.staff_id == row.staff_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            scope_elder_ids = [str(eid) for eid in assignments]

    elif identity.kind == KIND_GUARDIAN:
        row = (
            await session.execute(
                select(GuardianAccount).where(
                    GuardianAccount.guardian_id == uuid.UUID(identity.guardian_id)
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise AuthRevokedError("账号已不存在")
        name = row.name
        bindings = (
            (
                await session.execute(
                    select(ElderGuardian.elder_id).where(
                        ElderGuardian.guardian_id == row.guardian_id,
                        ElderGuardian.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        scope_elder_ids = [str(eid) for eid in bindings]

    elif identity.kind == "device":
        if identity.elder_id:
            scope_elder_ids = [str(identity.elder_id)]
        # device 不需要 name 字段

    elif identity.kind == "staff_card":
        # 工牌：elder_id 从 device 来（S-17），staff_id 走 staff_accounts 拿 name
        name_row = (
            await session.execute(
                select(StaffAccount.name).where(
                    StaffAccount.staff_id == uuid.UUID(identity.staff_id)
                )
            )
        ).scalar_one_or_none()
        name = name_row
        if identity.elder_id:
            scope_elder_ids = [str(identity.elder_id)]

    return CurrentIdentityResult(
        kind=identity.kind,
        id=identity.staff_id or identity.guardian_id or identity.device_id or "",
        role=getattr(identity, "role", None),
        name=name,
        scope_elder_ids=scope_elder_ids,
    )
