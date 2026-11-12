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

import datetime
import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redact import mask_phone as _core_mask_phone
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


class GuardianDisabledError(StaffServiceError):
    """家属账号已停用，不能新增绑定（P1 修复 2026-11-06）。"""


# 手机号脱敏唯一实现在 app/core/redact.py（Day 9 A1b 集中化）；
# 这里 import re-export，保持 `staff_service.mask_phone` 的既有调用面（API 层与测试）。
mask_phone = _core_mask_phone


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
    operator_id: uuid.UUID | None = None,
) -> StaffAccount:
    """改角色 / 停用。任一字段真的变了就让旧 Token 失效（`token_version += 1`）。

    取值合法性由请求模型（`Literal`）与表 CHECK 约束兜底，服务层不再重复判定；
    这里只保证「传了但值没变」不会误伤 —— 空 PATCH 与同值 PATCH 都不加版本号。

    P2-20 自我保护（停用 admin 时）：不能停用自己；不能停用最后一位 active admin
    ——否则后台会失去全部可登录的管理员，锁死在门外。

    角色变更联动（P1 修复 2026-11-06）：
    `doctor ↔ care_worker` 互转时，同步更新 `elder_staff_assignments.relation`
    （`primary_care ↔ doctor`），否则 `relation` 与 `staff.role` 会漂移。
    `admin ↔ 非 admin` 不涉及联动：admin 本就不参与绑定（裁决 D-01），
    非 admin → admin 时既有 assignment 保留历史（不自动清）。
    """
    staff = (
        await session.execute(select(StaffAccount).where(StaffAccount.staff_id == staff_id))
    ).scalar_one_or_none()
    if staff is None:
        raise StaffNotFoundError(f"员工账号不存在：{staff_id}")

    if status == "disabled" and staff.role == "admin":
        if operator_id is not None and operator_id == staff_id:
            raise StaffProtectedError("不能停用自己的管理员账号")
        active_admins = (
            await session.execute(
                select(func.count())
                .select_from(StaffAccount)
                .where(StaffAccount.role == "admin", StaffAccount.status == "active")
            )
        ).scalar_one()
        if active_admins <= 1:
            raise StaffProtectedError("不能停用最后一位 active 管理员")

    changed: list[str] = []
    if role is not None and role != staff.role:
        # role 变更 → 同步 assignment.relation（doctor ↔ care_worker 互转）
        old_role, new_role = staff.role, role
        old_relation = _ROLE_TO_RELATION.get(old_role)
        new_relation = _ROLE_TO_RELATION.get(new_role)
        if old_relation is not None and new_relation is not None and old_relation != new_relation:
            # 同一个 staff 的所有 assignment 的 relation 一起换
            await session.execute(
                update(ElderStaffAssignment)
                .where(ElderStaffAssignment.staff_id == staff_id)
                .values(relation=new_relation)
            )
            logger.info(
                "assignment relation synced: staff_id=%s %s → %s",
                staff_id,
                old_relation,
                new_relation,
            )

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
            select(GuardianAccount).where(GuardianAccount.guardian_id == guardian_id)
        )
    ).scalar_one_or_none()
    if guardian is None:
        raise GuardianNotFoundError(f"家属账号不存在：{guardian_id}")
    if guardian.status != "active":
        raise GuardianDisabledError(f"家属账号已停用（status={guardian.status}），不能绑定")

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


class PrimaryCareGapError(StaffServiceError):
    """移除后某长者失去全部责任护工（服务层校验，裁决 C4）。"""


class StaleWriteConflictError(StaffServiceError):
    """E20 乐观并发冲突（P2-20）：页面加载后该长者的绑定已被别人改过，后提交者 409。"""


class StaffProtectedError(StaffServiceError):
    """受保护 admin：不能停用自己，也不能停用最后一位 active admin（P2-20 自我保护）。"""


async def _lock_elders_for_reassign(
    session: AsyncSession, elder_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, datetime.datetime]:
    """按 `elder_id` 排序对一批长者行 `SELECT … FOR UPDATE`，返回 elder_id → updated_at。

    P2-19：没有行锁时，两个并发改绑各自读到旧计数、双双过检 → 长者 0 位责任护工。
    排序加锁与 `_lock_elder` 的单一锁不同——多行锁必须固定顺序，否则两个事务
    交叉锁两批长者会构成死锁环。
    锁等待结束后本语句返回的是**最新已提交**的 `updated_at`（READ COMMITTED
    语句级快照），并发事务先提交的变更对本事务可见——这是"后提交者 409"的依据。
    """
    if not elder_ids:
        return {}
    rows = (
        await session.execute(
            select(Elder.elder_id, Elder.updated_at)
            .where(Elder.elder_id.in_(elder_ids))
            .order_by(Elder.elder_id.asc())
            .with_for_update()
        )
    ).all()
    return {elder_id: updated_at for elder_id, updated_at in rows}


async def reassign_elders(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID,
    elder_ids: Sequence[uuid.UUID],
    operator_id: uuid.UUID | None,
    expected_updated_at: datetime.datetime | None = None,
) -> dict[str, object]:
    """全量替换式改绑：计算 diff → 行锁 → CAS → 409 判定 → 执行 → 审计。

    并发口径（P2-19/P2-20，总表 §5.7 冻结契约）：
    - 涉改动长者按 `elder_id` 排序行锁；锁后重读 diff，并发先提交者的变更可见，
      "最后一位责任护工"检查据此拦住后到事务（1 成功 1 409）；
    - `expected_updated_at`（可选）为乐观 CAS 基准：涉改动长者任一行的
      `updated_at` 晚于该值 → `StaleWriteConflictError`（API 层映射 409
      STALE_WRITE），页面加载后的 stale 全量覆盖不再静默冲掉他人变更；
      不传则只走行锁（D2 未接入版本前不被阻塞）；
    - 成功路径显式推进涉改动长者的 `updated_at`，作为下一轮 CAS 的新基准；
    - 409/403/404 失败路径写 `audit_logs`（操作者、trace_id、原因、差集）后原样
      重抛——审计与业务同事务，调用方 commit 时一并落库（`get_session` 对错误
      响应体也走正常返回 → commit）。

    事务边界：本函数只 `flush()`，不 `commit()`。
    """
    from app.services.audit_log_service import record_audit

    wanted_ids = list(dict.fromkeys(elder_ids))
    added_ids: set[uuid.UUID] = set()
    removed_ids: set[uuid.UUID] = set()
    staff: StaffAccount | None = None

    try:
        # --- 1. staff 存在性 + 角色合法性 ---
        staff = (
            await session.execute(select(StaffAccount).where(StaffAccount.staff_id == staff_id))
        ).scalar_one_or_none()
        if staff is None:
            raise StaffNotFoundError(f"员工账号不存在：{staff_id}")

        relation = _ROLE_TO_RELATION.get(staff.role)
        if relation is None:
            # admin 不参与绑定（裁决 D-01）；403 让 API 层映射
            raise StaffRoleConflictError("管理员账号不参与长者绑定（裁决 D-01）")

        # --- 2. elder_ids 去重 + 全量存在校验（一条 IN，写前失败不残留） ---
        wanted_ids = await _ensure_elders(session, wanted_ids)

        # --- 3. 无锁预读现有 assignment（只为确定锁集合；权威 diff 在锁后重算） ---
        pre_existing = set(
            (
                await session.execute(
                    select(ElderStaffAssignment.elder_id).where(
                        ElderStaffAssignment.staff_id == staff_id,
                        ElderStaffAssignment.relation == relation,
                    )
                )
            )
            .scalars()
            .all()
        )

        # --- 4. 行锁（P2-19）：wanted ∪ 现有，按 elder_id 排序，防交叉死锁 ---
        locked = await _lock_elders_for_reassign(session, sorted(wanted_ids + list(pre_existing)))

        # --- 5. 锁后重读 + 算 diff：锁等待期间其他事务的提交在这里生效 ---
        existing_ids: set[uuid.UUID] = set(
            (
                await session.execute(
                    select(ElderStaffAssignment.elder_id).where(
                        ElderStaffAssignment.staff_id == staff_id,
                        ElderStaffAssignment.relation == relation,
                    )
                )
            )
            .scalars()
            .all()
        )
        wanted_set: set[uuid.UUID] = set(wanted_ids)
        removed_ids = existing_ids - wanted_set
        added_ids = wanted_set - existing_ids

        # 无变化直接返回（仍写审计，便于审计追踪管理员操作）
        if not removed_ids and not added_ids:
            await record_audit(
                session,
                actor_type="staff",
                actor_id=operator_id,
                action="reassign_elders",
                resource_type="staff",
                resource_id=staff_id,
                detail={"added": [], "removed": [], "role": staff.role},
            )
            return {"staff_id": staff_id, "added": [], "removed": []}

        involved_ids = sorted(removed_ids | added_ids, key=str)

        # --- 6. 乐观 CAS（P2-20）：涉改动长者任一行比客户端基准新 → 后提交者 409 ---
        if expected_updated_at is not None:
            expected = expected_updated_at
            if expected.tzinfo is None:
                # 客户端可能发无时区的 ISO 串：按 UTC 解读，避免 aware/naive 比较报 TypeError
                expected = expected.replace(tzinfo=datetime.UTC)
            for elder_id in involved_ids:
                current = locked.get(elder_id)
                if current is not None and current > expected:
                    raise StaleWriteConflictError(
                        f"长者 {elder_id} 的绑定在页面加载后已被他人修改，请刷新后重试"
                    )

        # --- 7. 409 判定：只在 care_worker 改绑时检查 primary_care 剩余 ---
        if staff.role == "care_worker":
            for elder_id in sorted(removed_ids, key=str):
                remaining = (
                    await session.execute(
                        select(func.count())
                        .select_from(ElderStaffAssignment)
                        .where(
                            ElderStaffAssignment.elder_id == elder_id,
                            ElderStaffAssignment.relation == "primary_care",
                            ElderStaffAssignment.staff_id != staff_id,
                        )
                    )
                ).scalar_one()
                if remaining == 0:
                    raise PrimaryCareGapError(f"长者 {elder_id} 移除后将失去全部责任护工")

        # --- 8. 执行 diff ---
        # 先删后加；两者在同一 flush 内完成
        if removed_ids:
            await session.execute(
                delete(ElderStaffAssignment).where(
                    ElderStaffAssignment.staff_id == staff_id,
                    ElderStaffAssignment.relation == relation,
                    ElderStaffAssignment.elder_id.in_(removed_ids),
                )
            )
        for elder_id in added_ids:
            session.add(
                ElderStaffAssignment(elder_id=elder_id, staff_id=staff_id, relation=relation)
            )

        # --- 9. 推进涉改动长者的 updated_at（下一轮 CAS 的新基准） ---
        # 必须用 DB 的 clock_timestamp()，不能用宿主时钟：baseline 本身来自列默认值
        # `now()`（DB 时钟），两个时钟源一旦有偏差（实测容器 DB 可比宿主快上百毫秒），
        # 写回值可能小于 baseline → 下一轮 CAS 判定失效（该 409 的不 409）。
        # 用 clock_timestamp() 而非 now()：now() 是事务开始时间，同事务内不会推进。
        await session.execute(
            update(Elder)
            .where(Elder.elder_id.in_(involved_ids))
            .values(updated_at=func.clock_timestamp())
        )
        await session.flush()

        # --- 10. 写审计（与业务同事务） ---
        await record_audit(
            session,
            actor_type="staff",
            actor_id=operator_id,
            action="reassign_elders",
            resource_type="staff",
            resource_id=staff_id,
            detail={
                "added": [str(eid) for eid in sorted(added_ids, key=str)],
                "removed": [str(eid) for eid in sorted(removed_ids, key=str)],
                "role": staff.role,
            },
        )
    except (StaffNotFoundError, StaffRoleConflictError, ElderNotFoundError) as exc:
        # 404/403 失败路径也留审计（操作者、trace_id 由 record_audit 注入、原因、已知差集）
        await record_audit(
            session,
            actor_type="staff",
            actor_id=operator_id,
            action="reassign_elders_rejected",
            resource_type="staff",
            resource_id=staff_id,
            detail={
                "reason": type(exc).__name__,
                "message": exc.message,
                "added": [str(eid) for eid in sorted(added_ids, key=str)],
                "removed": [str(eid) for eid in sorted(removed_ids, key=str)],
            },
        )
        raise
    except (PrimaryCareGapError, StaleWriteConflictError) as exc:
        # 409 失败路径：此时 diff 已算出，审计带上差集便于复盘"谁想动谁"
        await record_audit(
            session,
            actor_type="staff",
            actor_id=operator_id,
            action="reassign_elders_rejected",
            resource_type="staff",
            resource_id=staff_id,
            detail={
                "reason": type(exc).__name__,
                "message": exc.message,
                "role": staff.role if staff is not None else None,
                "expected_updated_at": expected_updated_at.isoformat()
                if expected_updated_at is not None
                else None,
                "added": [str(eid) for eid in sorted(added_ids, key=str)],
                "removed": [str(eid) for eid in sorted(removed_ids, key=str)],
            },
        )
        raise

    logger.info(
        "elders reassigned: staff_id=%s role=%s added=%d removed=%d",
        staff_id,
        staff.role if staff is not None else "?",
        len(added_ids),
        len(removed_ids),
    )
    return {
        "staff_id": staff_id,
        "added": sorted(added_ids, key=str),
        "removed": sorted(removed_ids, key=str),
    }


async def staff_names(
    session: AsyncSession, staff_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """批量取员工展示名（N-1 `acknowledged_by_name` JOIN 展示字段，R-D12 收口）。

    只读不写；空入参返回空 dict；查不到的 id 不出现在结果里（调用方落 null）。
    """
    ids = list({sid for sid in staff_ids if sid is not None})
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(StaffAccount.staff_id, StaffAccount.name).where(StaffAccount.staff_id.in_(ids))
        )
    ).all()
    return {row.staff_id: row.name for row in rows}


async def list_staff_accounts(
    session: AsyncSession,
    *,
    role_filter: str | None = None,
    status_filter: str | None = None,
) -> list[tuple[StaffAccount, list[uuid.UUID]]]:
    """列出所有员工（可选 role/status 过滤）。

    返回 (StaffAccount, bound_elder_ids) 元组列表，按创建时间正序。
    `bound_elder_ids` 来自 `elder_staff_assignments` 全部 relation
    （不仅 primary_care）——改绑 UI 需要当前绑定全集。

    仅用于 GET /api/staff（契约 E12 读入口补齐）。
    """

    stmt = select(StaffAccount)
    if role_filter:
        stmt = stmt.where(StaffAccount.role == role_filter)
    if status_filter:
        stmt = stmt.where(StaffAccount.status == status_filter)
    # 表无 created_at 列（模型亦未定义），按 staff_id 排序保证结果稳定
    stmt = stmt.order_by(StaffAccount.staff_id.asc())

    rows = (await session.execute(stmt)).scalars().all()

    if not rows:
        return []

    staff_ids = [r.staff_id for r in rows]

    # 一次性查出所有 assignment，按 staff_id 分组
    from app.models.relation import ElderStaffAssignment

    assign_stmt = select(ElderStaffAssignment.staff_id, ElderStaffAssignment.elder_id).where(
        ElderStaffAssignment.staff_id.in_(staff_ids)
    )
    assign_rows = (await session.execute(assign_stmt)).all()

    mapping: dict[uuid.UUID, list[uuid.UUID]] = {}
    for staff_id, elder_id in assign_rows:
        mapping.setdefault(staff_id, []).append(elder_id)

    return [(r, mapping.get(r.staff_id, [])) for r in rows]


__all__ = [
    "MAX_ACTIVE_GUARDIANS_PER_ELDER",
    "ElderNotFoundError",
    "GuardianAccount",
    "GuardianAlreadyBoundError",
    "GuardianDisabledError",
    "GuardianLimitExceededError",
    "GuardianNotFoundError",
    "PrimaryCareGapError",
    "StaffAccount",
    "StaffConflictError",
    "StaffNotFoundError",
    "StaffProtectedError",
    "StaffRoleConflictError",
    "StaffServiceError",
    "StaleWriteConflictError",
    "bind_guardian",
    "create_guardian",
    "create_staff",
    "list_staff_accounts",
    "mask_phone",
    "reassign_elders",
    "update_staff",
]
