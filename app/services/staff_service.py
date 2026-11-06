"""建号领域服务：员工账号、家属账号、家属绑定（T2）。

owner=建号接口（adhoc-20261106）· 依据 docs/plans/adhoc-20261106-员工与家属建号接口.md

分层定位（AGENTS §2）：`app/services/` 是唯一写库层，本模块只被 `app/api/` 调用，
不 import 任何既有 service（避免与 A1/A2 的并发修改冲突）。

口径要点：
- 手机号在 `staff_accounts` 与 `guardian_accounts` 之间**全局唯一** —— 与登录侧
  `login_with_phone` 的跨表碰撞检查保持同一判据（否则登录会出现二义性）。
- 改角色或停用一律 `token_version += 1`，旧 Token 立即失效（AC-24 / FR-ADM-04）。
- 每位长者 `status='active'` 的监护人 ≤ 2：不落 DDL，按 `app/models/relation.py` 头注释的
  「服务层校验」执行，只数 active，`unbound` 不占名额；并发绑定靠长者行锁串行化。
- **校验前置于写库**：抛域异常时不留已 flush 的脏行 —— 调用方 `get_session` 在请求正常
  返回时会 commit，异常若被 API 层捕获成 404/409，脏行就会被一起提交进库。
- 只 `flush()` 不 `commit()`：事务边界交给调用方（API 依赖或测试夹具）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import STAFF_ROLES, hash_password
from app.models.guardian import GuardianAccount
from app.models.organization import Elder
from app.models.relation import ElderGuardian, ElderStaffAssignment
from app.models.staff import StaffAccount

logger = logging.getLogger(__name__)

#: 员工 role → elder_staff_assignments.relation（该列 CHECK 只有 primary_care / doctor）
_ROLE_TO_RELATION: dict[str, str] = {
    "care_worker": "primary_care",
    "doctor": "doctor",
}

#: 每位长者 active 监护人上限（PRD FR-ADM-02 / ER §5.2，服务层校验）
MAX_ACTIVE_GUARDIANS_PER_ELDER = 2


class StaffServiceError(Exception):
    """建号服务的领域异常基类：`message` 可直接回给调用方（不含内部细节）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class StaffConflictError(StaffServiceError):
    """手机号已被占用（跨员工/家属两表）。"""


class StaffNotFoundError(StaffServiceError):
    """员工账号不存在。"""


class StaffRoleConflictError(StaffServiceError):
    """角色不允许该操作（如给 admin 分配长者）。"""


class ElderNotFoundError(StaffServiceError):
    """长者不存在。"""


class GuardianNotFoundError(StaffServiceError):
    """家属账号不存在。"""


class GuardianAlreadyBoundError(StaffServiceError):
    """该家属已绑定这位长者。"""


class GuardianLimitExceededError(StaffServiceError):
    """该长者的 active 监护人已满 2 位。"""


def mask_phone(phone: str | None) -> str:
    """手机号脱敏：`13800000001` → `138****0001`（响应体与日志统一用它）。"""
    if not phone:
        return ""
    if len(phone) < 7:  # pragma: no cover - 入库前已校验 11 位
        return "*" * len(phone)
    return f"{phone[:3]}****{phone[-4:]}"


async def _phone_taken(session: AsyncSession, phone: str) -> bool:
    """手机号在两套账号表里是否已被占用。"""
    staff_hit = await session.execute(
        select(StaffAccount.staff_id).where(StaffAccount.phone == phone).limit(1)
    )
    if staff_hit.scalar_one_or_none() is not None:
        return True
    guardian_hit = await session.execute(
        select(GuardianAccount.guardian_id).where(GuardianAccount.phone == phone).limit(1)
    )
    return guardian_hit.scalar_one_or_none() is not None


async def _ensure_elders(session: AsyncSession, elder_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """整批校验长者存在，返回去重后的 id 列表（一条 IN 查询，不逐个往返）。

    去重是必须的：`elder_staff_assignments` 的唯一约束是 (elder_id, staff_id, relation)，
    同一个 id 传两遍会在 flush 时撞唯一约束 —— 客户端的重复项不该变成 500。
    整批先查完再返回，是为了让「有长者不存在」这条路径在写任何行之前就结束。
    """
    wanted = list(dict.fromkeys(elder_ids))
    if not wanted:
        return wanted
    found = set(
        (await session.execute(select(Elder.elder_id).where(Elder.elder_id.in_(wanted))))
        .scalars()
        .all()
    )
    missing = [elder_id for elder_id in wanted if elder_id not in found]
    if missing:
        raise ElderNotFoundError(f"长者不存在：{', '.join(str(elder_id) for elder_id in missing)}")
    return wanted


async def _lock_elder(session: AsyncSession, elder_id: uuid.UUID) -> None:
    """校验长者存在并锁住该行（`SELECT … FOR UPDATE`），供「先数后写」的绑定名额使用。

    没有行锁时，两个并发绑定会各自数到 1 位再各插一行，落成 3 位 active 监护人；
    DDL 的唯一约束是 (elder_id, guardian_id)，看不出位次，拦不住这种超发。
    只锁一位长者，且事务内不再取第二把长者锁，不会构成死锁环。
    """
    hit = await session.execute(
        select(Elder.elder_id).where(Elder.elder_id == elder_id).with_for_update()
    )
    if hit.scalar_one_or_none() is None:
        raise ElderNotFoundError(f"长者不存在：{elder_id}")


async def create_staff(
    session: AsyncSession,
    *,
    name: str,
    role: str,
    phone: str,
    password: str,
    elder_ids: Sequence[uuid.UUID] = (),
) -> StaffAccount:
    """建员工账号（可选同时分配长者）。

    顺序固定为：角色映射 → 手机号跨表查重 → 长者存在性 → 写 staff → 写 assignment。
    前三步任一失败都在写库之前结束（见模块 docstring 的「校验前置于写库」）。
    """
    if role not in STAFF_ROLES:
        raise StaffRoleConflictError(f"未知员工角色：{role}")

    relation: str | None = _ROLE_TO_RELATION.get(role)
    # 三个合法角色里只有 admin 没有映射：管理员不绑长者（裁决 D-01）
    if relation is None and elder_ids:
        raise StaffRoleConflictError("管理员账号不参与长者绑定（裁决 D-01）")

    if await _phone_taken(session, phone):
        raise StaffConflictError("手机号已被其他账号使用")

    assign_elder_ids = await _ensure_elders(session, elder_ids)

    staff = StaffAccount(
        name=name,
        role=role,
        phone=phone,
        password_hash=hash_password(password),
        status="active",
    )
    session.add(staff)
    await session.flush()

    # relation 为 None 只可能是 admin，而 admin 带 elder_ids 已在上方被拒；
    # 带上 relation is not None 的条件，既让类型收窄成立，也避免 admin 空转一次 flush。
    if assign_elder_ids and relation is not None:
        for elder_id in assign_elder_ids:
            session.add(
                ElderStaffAssignment(elder_id=elder_id, staff_id=staff.staff_id, relation=relation)
            )
        await session.flush()

    logger.info(
        "staff created: staff_id=%s role=%s phone=%s assignments=%d",
        staff.staff_id,
        role,
        mask_phone(phone),
        len(assign_elder_ids),
    )
    return staff


async def update_staff(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    role: str | None = None,
    status: str | None = None,
) -> StaffAccount:
    """改角色 / 停用。任一字段真的变了就让旧 Token 失效（`token_version += 1`）。

    取值合法性由请求模型（`Literal`）与表 CHECK 约束兜底，服务层不再重复判定；
    这里只保证「传了但值没变」不会误伤 —— 空 PATCH 与同值 PATCH 都不加版本号。
    """
    staff = (
        await session.execute(select(StaffAccount).where(StaffAccount.staff_id == staff_id))
    ).scalar_one_or_none()
    if staff is None:
        raise StaffNotFoundError(f"员工账号不存在：{staff_id}")

    changed: list[str] = []
    if role is not None and role != staff.role:
        staff.role = role
        changed.append("role")
    if status is not None and status != staff.status:
        staff.status = status
        changed.append("status")
    if changed:
        staff.token_version = staff.token_version + 1

    await session.flush()
    logger.info(
        "staff updated: staff_id=%s changed=%s token_version=%d",
        staff.staff_id,
        ",".join(changed) or "none",
        staff.token_version,
    )
    return staff


async def create_guardian(
    session: AsyncSession,
    *,
    name: str,
    phone: str,
    password: str,
) -> GuardianAccount:
    """建家属账号（契约缺口 G1）。手机号同样跨两表查重。"""
    if await _phone_taken(session, phone):
        raise StaffConflictError("手机号已被其他账号使用")

    guardian = GuardianAccount(
        name=name,
        phone=phone,
        password_hash=hash_password(password),
        status="active",
    )
    session.add(guardian)
    await session.flush()

    logger.info("guardian created: guardian_id=%s", guardian.guardian_id)
    return guardian


async def bind_guardian(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    guardian_id: uuid.UUID,
    relation: str,
) -> ElderGuardian:
    """把家属账号绑定到长者（E13 / 契约缺口 G2 的写入侧）。

    判定顺序：长者存在（并加行锁）→ 家属存在 → 同对未绑定 → active 名额未满 → 插入。
    `status` 显式写 `active`：不依赖 INSERT 的 server_default 回填，响应体可直接取值。
    """
    await _lock_elder(session, elder_id)

    guardian = (
        await session.execute(
            select(GuardianAccount.guardian_id).where(GuardianAccount.guardian_id == guardian_id)
        )
    ).scalar_one_or_none()
    if guardian is None:
        raise GuardianNotFoundError(f"家属账号不存在：{guardian_id}")

    existing = (
        await session.execute(
            select(ElderGuardian.id).where(
                ElderGuardian.elder_id == elder_id,
                ElderGuardian.guardian_id == guardian_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise GuardianAlreadyBoundError("该家属已绑定这位长者")

    # 只数 active：`unbound` 是解绑保痕（AC-24），不占 2 个名额
    active_count = (
        await session.execute(
            select(func.count())
            .select_from(ElderGuardian)
            .where(
                ElderGuardian.elder_id == elder_id,
                ElderGuardian.status == "active",
            )
        )
    ).scalar_one()
    if active_count >= MAX_ACTIVE_GUARDIANS_PER_ELDER:
        raise GuardianLimitExceededError(
            f"该长者 active 监护人已达 {MAX_ACTIVE_GUARDIANS_PER_ELDER} 位"
        )

    binding = ElderGuardian(
        elder_id=elder_id,
        guardian_id=guardian_id,
        relation=relation,
        status="active",
    )
    session.add(binding)
    await session.flush()

    logger.info(
        "guardian bound: elder_id=%s guardian_id=%s relation=%s",
        elder_id,
        guardian_id,
        relation,
    )
    return binding


__all__ = [
    "MAX_ACTIVE_GUARDIANS_PER_ELDER",
    "ElderNotFoundError",
    "GuardianAccount",
    "GuardianAlreadyBoundError",
    "GuardianLimitExceededError",
    "GuardianNotFoundError",
    "StaffAccount",
    "StaffConflictError",
    "StaffNotFoundError",
    "StaffRoleConflictError",
    "StaffServiceError",
    "bind_guardian",
    "create_guardian",
    "create_staff",
    "mask_phone",
    "update_staff",
]
