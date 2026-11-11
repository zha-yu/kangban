"""MedicationPlan 的**唯一写库出口**（M2 · Day 8 dev-A H+1）。

分层硬约束（AGENTS §2）：
- API 层只做参数校验 + 权限门禁（role 过滤，不做 scope），写库一律走本 service
- Service 层 flush 不 commit（与 work_order_service / medication_service 对齐），API 层统一 commit
- Cron 校验走 APScheduler CronTrigger（与 medication_due_service 同款解析器，避免口径漂移）

权限门禁（Day 8 主理人裁决 v1.4）：
- API 层一律放行 `require_roles("doctor","care_worker","admin")`——**禁止 API 层直接 require_roles("doctor")**
- Service 层读操作 scope 过滤：admin 全量；doctor/care_worker 只看自己在 elder_staff_assignments 里有 active assignment 的长者（relation IN primary_care/doctor）
- Service 层写操作双重门禁：role=doctor + 该 staff 是该长者的负责医生（relation='doctor'）
- 越权读 list 返回空集合 / get 返回 404（不暴露存在性）+ 审计 action=medication_plan_read_forbidden, detail.reason="read_scope_outside_assignment"
- 越权写抛 HTTPException(403) + 审计 action=medication_plan_write_forbidden, reason="role_not_doctor" 或 "not_attending_doctor_for_elder"

版本管理（G1 裁决落地，迁移 0019）：
- create: version=1, status='active'
- update: 查当前 plan.version → version+1, 所有业务字段更新, status 保持 active
- stop:   查当前 plan.version → version+1, status→'stopped', 其他字段不变
- 停用后不可再改（service 层校验 status!='stopped'）
- plan_version 联动 medication_events 唯一键（三列），改药单后新事件自动带新版本号
- 历史事件 drug_snapshot 是独立 JSONB 列，plan 改版不影响——AC-27 自动满足
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Final

from apscheduler.triggers.cron import CronTrigger
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.runtime_context import get_identity
from app.models.medication import MedicationPlan
from app.models.relation import ElderStaffAssignment
from app.models.staff import StaffAccount
from app.services.audit_log_service import record_audit

logger = logging.getLogger(__name__)

# ------------------------------------------------------------ 域异常


class PlanError(ValueError):
    """药单域异常基类。"""


class PlanNotFound(PlanError):
    """药单不存在。"""


class PlanStopped(PlanError):
    """药单已停用，不可修改/停。"""


class PlanInvalidCron(PlanError):
    """cron 表达式不合法。"""


# ------------------------------------------------------------ 常量与数据类


class PlanPermissionDenied(PlanError):
    """权限不足（内部使用，对外不直接抛——Service 层会转 HTTPException + 审计）。"""


_PLAN_STATUS_ACTIVE: Final[str] = "active"
_PLAN_STATUS_STOPPED: Final[str] = "stopped"

# elder_staff_assignments.relation 可作为"负责"的枚举值
# 写操作（create/update/stop）要求 relation='doctor'；读操作允许 relation IN 两者
_WRITE_RELATION: Final[str] = "doctor"
_READ_RELATIONS: Final[tuple[str, ...]] = ("primary_care", "doctor")


@dataclass(frozen=True, slots=True)
class PlanRow:
    """查询返回结构：plan ORM 对象 + 创建人姓名（JOIN staff_accounts）。"""

    plan: MedicationPlan
    created_by_name: str | None  # NULL 兜底（理论上 FK RESTRICT 不会让被删医生出现在这里）


# ------------------------------------------------------------ 辅助：身份 / scope 解析


def _uuid_staff_id() -> uuid.UUID | None:
    """从 runtime_context 取 staff_id 并转 UUID；无身份或非 staff 时返回 None。"""
    identity = get_identity()
    if identity is None or identity.kind != "staff":
        return None
    sid = getattr(identity, "staff_id", None)
    if sid is None:
        return None
    try:
        return uuid.UUID(str(sid))
    except (ValueError, AttributeError):
        return None


def _current_role() -> str | None:
    identity = get_identity()
    return getattr(identity, "role", None) if identity is not None else None


def _is_admin() -> bool:
    return _current_role() == "admin"


async def _staff_has_relation(
    session: AsyncSession, *, staff_id: uuid.UUID, elder_id: uuid.UUID, relation: str
) -> bool:
    """查 elder_staff_assignments：该 staff 与该长者之间是否存在指定 relation。"""
    row = (
        await session.execute(
            select(ElderStaffAssignment.id).where(
                ElderStaffAssignment.staff_id == staff_id,
                ElderStaffAssignment.elder_id == elder_id,
                ElderStaffAssignment.relation == relation,
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def _allowed_elder_ids_for_read(
    session: AsyncSession, *, staff_id: uuid.UUID
) -> set[uuid.UUID]:
    """该 staff 负责的所有 elder_id 集合（relation IN primary_care/doctor 都算）。"""
    rows = (
        (
            await session.execute(
                select(ElderStaffAssignment.elder_id).where(
                    ElderStaffAssignment.staff_id == staff_id,
                    ElderStaffAssignment.relation.in_(_READ_RELATIONS),
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


# ------------------------------------------------------------ 辅助：审计


async def _record_write_forbidden(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID | None,
    reason: str,
    elder_id: uuid.UUID | None,
    plan_id: uuid.UUID | None,
    intended_action: str,
) -> None:
    await record_audit(
        session,
        actor_type="staff",
        actor_id=staff_id,
        action="medication_plan_write_forbidden",
        resource_type="medication_plan",
        resource_id=plan_id,
        detail={
            "reason": reason,
            "elder_id": str(elder_id) if elder_id else None,
            "intended_action": intended_action,
        },
    )


async def _record_read_forbidden(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID | None,
    elder_id: uuid.UUID | None,
    plan_id: uuid.UUID | None,
    intended_action: str,
) -> None:
    await record_audit(
        session,
        actor_type="staff",
        actor_id=staff_id,
        action="medication_plan_read_forbidden",
        resource_type="medication_plan",
        resource_id=plan_id,
        detail={
            "reason": "read_scope_outside_assignment",
            "elder_id": str(elder_id) if elder_id else None,
            "intended_action": intended_action,
        },
    )


# ------------------------------------------------------------ 辅助：双重门禁


async def _assert_doctor_authorized(session: AsyncSession, *, elder_id: uuid.UUID) -> uuid.UUID:
    """写操作双重门禁：role=doctor + 该 staff 是该长者的负责医生。

    通过则返回 staff_id（UUID）。不通过：写审计 + 抛 HTTPException(403)。
    """
    staff_id = _uuid_staff_id()

    # ① 角色门禁
    if staff_id is None or _current_role() != "doctor":
        await _record_write_forbidden(
            session,
            staff_id=staff_id,
            reason="role_not_doctor",
            elder_id=elder_id,
            plan_id=None,
            intended_action="create/update/stop",
        )
        raise HTTPException(status_code=403, detail="药单管理仅负责医生可操作")

    # ② 数据范围门禁
    if not await _staff_has_relation(
        session, staff_id=staff_id, elder_id=elder_id, relation=_WRITE_RELATION
    ):
        await _record_write_forbidden(
            session,
            staff_id=staff_id,
            reason="not_attending_doctor_for_elder",
            elder_id=elder_id,
            plan_id=None,
            intended_action="create/update/stop",
        )
        raise HTTPException(status_code=403, detail="您不是该长者的负责医生，无权操作药单")

    return staff_id


# ------------------------------------------------------------ 辅助：cron / 日期校验


def _validate_cron(expr: str) -> None:
    try:
        CronTrigger.from_crontab(expr, timezone="Asia/Shanghai")
    except Exception as exc:
        raise PlanInvalidCron(f"cron 表达式不合法: {expr!r}") from exc


def _validate_date_range(start_date: datetime.date, end_date: datetime.date | None) -> None:
    if end_date is not None and start_date > end_date:
        raise PlanError(f"start_date {start_date} 不能晚于 end_date {end_date}")


# ------------------------------------------------------------ 辅助：JOIN 回填姓名 + scope 过滤查询


async def _fetch_plan_rows(
    session: AsyncSession,
    *,
    elder_id_filter: uuid.UUID | None = None,
    plan_id_filter: uuid.UUID | None = None,
    include_stopped: bool = True,
    scope_check: bool = True,
) -> list[PlanRow]:
    """按 elder_id / plan_id 查药单（JOIN staff_accounts 回填 created_by_name + scope 过滤）。

    scope_check=True 时：
    - admin → 全量返回
    - doctor/care_worker → 只返回 allowed_elder_ids 里的长者的药单
      · elder_id_filter 不在 allowed_elder_ids 里 → 越权读审计 + 跳过该 elder（list 返回空 / get 返回 None）
    """
    stmt = select(MedicationPlan, StaffAccount.name.label("creator_name")).select_from(
        MedicationPlan.__table__.join(
            StaffAccount.__table__,
            MedicationPlan.created_by == StaffAccount.staff_id,
            isouter=True,
        )
    )

    if plan_id_filter is not None:
        stmt = stmt.where(MedicationPlan.plan_id == plan_id_filter)
    if elder_id_filter is not None:
        stmt = stmt.where(MedicationPlan.elder_id == elder_id_filter)
    if not include_stopped:
        stmt = stmt.where(MedicationPlan.status == _PLAN_STATUS_ACTIVE)

    rows: list[PlanRow] = []

    if not scope_check or _is_admin():
        # admin / 不检查 scope → 不加 elder_id 过滤
        result = (await session.execute(stmt)).all()
        for plan_obj, creator_name in result:
            rows.append(PlanRow(plan=plan_obj, created_by_name=creator_name))
        return rows

    # doctor / care_worker → scope 过滤
    staff_id = _uuid_staff_id()
    if staff_id is None:
        # 没有合法 staff 身份 → 返回空，同时审计（无 staff 身份能走到这里应该是不可能的，但防御性处理）
        if elder_id_filter is not None or plan_id_filter is not None:
            await _record_read_forbidden(
                session,
                staff_id=None,
                elder_id=elder_id_filter,
                plan_id=plan_id_filter,
                intended_action="list/get",
            )
        return []

    allowed_elder_ids = await _allowed_elder_ids_for_read(session, staff_id=staff_id)

    # 越权读审计：请求里指定的 elder_id / plan_id（对应 elder_id）不在允许集合里
    if elder_id_filter is not None and elder_id_filter not in allowed_elder_ids:
        await _record_read_forbidden(
            session,
            staff_id=staff_id,
            elder_id=elder_id_filter,
            plan_id=None,
            intended_action="list",
        )
        return []

    # plan_id_filter → 先查 plan.elder_id 是否在允许集合里
    if plan_id_filter is not None:
        plan = (
            await session.execute(
                select(MedicationPlan).where(MedicationPlan.plan_id == plan_id_filter)
            )
        ).scalar_one_or_none()
        if plan is not None and plan.elder_id not in allowed_elder_ids:
            await _record_read_forbidden(
                session,
                staff_id=staff_id,
                elder_id=plan.elder_id,
                plan_id=plan_id_filter,
                intended_action="get",
            )
            return []

    # 加 WHERE elder_id IN allowed_elder_ids
    stmt = stmt.where(MedicationPlan.elder_id.in_(allowed_elder_ids))

    result = (await session.execute(stmt)).all()
    for plan_obj, creator_name in result:
        rows.append(PlanRow(plan=plan_obj, created_by_name=creator_name))
    return rows


# ------------------------------------------------------------ 只读接口


async def list_plans_by_elder(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    include_stopped: bool = True,
) -> list[PlanRow]:
    """按长者查药单（含 scope 过滤 + 越权读审计）。"""
    return await _fetch_plan_rows(
        session,
        elder_id_filter=elder_id,
        include_stopped=include_stopped,
        scope_check=True,
    )


async def get_plan(session: AsyncSession, plan_id: uuid.UUID) -> PlanRow | None:
    """查单条药单（含 scope 校验 + 越权读审计）。"""
    rows = await _fetch_plan_rows(
        session,
        plan_id_filter=plan_id,
        include_stopped=True,
        scope_check=True,
    )
    return rows[0] if rows else None


# ------------------------------------------------------------ 写接口


async def create_plan(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    drug_name: str,
    dose: str,
    schedule_cron: str,
    start_date: datetime.date,
    end_date: datetime.date | None,
    created_by: uuid.UUID,
) -> PlanRow:
    """新建药单。version=1、status=active。

    双重门禁 + cron/日期校验 + INSERT + 审计。调用方（API 层）负责 commit。
    """
    # ① 双重门禁（内部会抛 403 + 写审计）
    await _assert_doctor_authorized(session, elder_id=elder_id)

    # ② 校验 cron / 日期范围
    _validate_cron(schedule_cron)
    _validate_date_range(start_date, end_date)

    # ③ INSERT
    new_plan = MedicationPlan(
        plan_id=uuid.uuid4(),
        elder_id=elder_id,
        drug_name=drug_name.strip(),
        dose=dose.strip(),
        schedule_cron=schedule_cron,
        start_date=start_date,
        end_date=end_date,
        version=1,
        created_by=created_by,
        status=_PLAN_STATUS_ACTIVE,
    )
    session.add(new_plan)

    # ④ 审计
    await record_audit(
        session,
        actor_type="staff",
        actor_id=created_by,
        action="medication_plan_create",
        resource_type="medication_plan",
        resource_id=new_plan.plan_id,
        detail={
            "elder_id": str(elder_id),
            "drug_name": new_plan.drug_name,
            "dose": new_plan.dose,
            "schedule_cron": new_plan.schedule_cron,
            "start_date": str(new_plan.start_date),
            "end_date": str(new_plan.end_date) if new_plan.end_date else None,
        },
    )

    await session.flush()

    # ⑤ 回填 created_by_name（created_by 就是当前 staff_id，直接从 staff_accounts 查）
    creator = (
        await session.execute(select(StaffAccount.name).where(StaffAccount.staff_id == created_by))
    ).scalar_one_or_none()
    return PlanRow(plan=new_plan, created_by_name=creator)


async def update_plan(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    drug_name: str,
    dose: str,
    schedule_cron: str,
    start_date: datetime.date,
    end_date: datetime.date | None,
    updated_by: uuid.UUID,
) -> PlanRow:
    """修改药单。version+1。已停用不可修改。cron 不变则审计 detail 打 cron_unchanged 标记。

    G1 裁决落地：plan.version+1 后 scheduler 下次扫到同一分钟时，因为 plan_version 不同
    会自动建新事件（唯一键已改为三列），不再触发 MedicationConflict。
    """
    # ① 先查 plan（拿 elder_id 用于双重门禁 + 后续校验 + 审计 before 快照）
    plan_row = await _fetch_plan_rows(
        session, plan_id_filter=plan_id, include_stopped=True, scope_check=False
    )
    if not plan_row:
        raise PlanNotFound(f"药单不存在: {plan_id}")
    plan = plan_row[0].plan

    # ② 双重门禁
    await _assert_doctor_authorized(session, elder_id=plan.elder_id)

    # ③ 校验 status / cron / 日期范围
    if plan.status == _PLAN_STATUS_STOPPED:
        raise PlanStopped("已停用药单不可修改")

    _validate_cron(schedule_cron)
    _validate_date_range(start_date, end_date)

    # ④ 比对 cron 是否变化
    cron_changed = schedule_cron != plan.schedule_cron
    next_version = plan.version + 1

    # ⑤ UPDATE
    plan.drug_name = drug_name.strip()
    plan.dose = dose.strip()
    plan.schedule_cron = schedule_cron
    plan.start_date = start_date
    plan.end_date = end_date
    plan.version = next_version

    # ⑥ 审计（before/after 快照）
    detail: dict[str, object] = {
        "before": {
            "drug_name": plan_row[0].plan.drug_name,
            "dose": plan_row[0].plan.dose,
            "schedule_cron": plan_row[0].plan.schedule_cron,
            "start_date": str(plan_row[0].plan.start_date),
            "end_date": str(plan_row[0].plan.end_date) if plan_row[0].plan.end_date else None,
            "version": plan_row[0].plan.version,
        },
        "after": {
            "drug_name": drug_name.strip(),
            "dose": dose.strip(),
            "schedule_cron": schedule_cron,
            "start_date": str(start_date),
            "end_date": str(end_date) if end_date else None,
            "version": next_version,
        },
    }
    if not cron_changed:
        detail["cron_unchanged"] = True

    await record_audit(
        session,
        actor_type="staff",
        actor_id=updated_by,
        action="medication_plan_update",
        resource_type="medication_plan",
        resource_id=plan_id,
        detail=detail,
    )

    await session.flush()

    # ⑦ 回填姓名（复用 plan_row[0].created_by_name，created_by 没改）
    return PlanRow(plan=plan, created_by_name=plan_row[0].created_by_name)


async def stop_plan(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    stopped_by: uuid.UUID,
) -> PlanRow:
    """停用药单。version+1。无删除语义。已停用不可重复停。"""
    # ① 先查 plan
    plan_row = await _fetch_plan_rows(
        session, plan_id_filter=plan_id, include_stopped=True, scope_check=False
    )
    if not plan_row:
        raise PlanNotFound(f"药单不存在: {plan_id}")
    plan = plan_row[0].plan

    # ② 双重门禁
    await _assert_doctor_authorized(session, elder_id=plan.elder_id)

    # ③ 校验 status
    if plan.status == _PLAN_STATUS_STOPPED:
        raise PlanStopped("已停用药单不可再次停用")

    # ④ UPDATE status + version
    plan.status = _PLAN_STATUS_STOPPED
    plan.version = plan.version + 1

    # ⑤ 审计
    await record_audit(
        session,
        actor_type="staff",
        actor_id=stopped_by,
        action="medication_plan_stop",
        resource_type="medication_plan",
        resource_id=plan_id,
        detail={"prev_version": plan_row[0].plan.version},
    )

    await session.flush()

    return PlanRow(plan=plan, created_by_name=plan_row[0].created_by_name)


__all__ = [
    "PlanError",
    "PlanNotFound",
    "PlanRow",
    "PlanStopped",
    "create_plan",
    "get_plan",
    "list_plans_by_elder",
    "stop_plan",
    "update_plan",
]
