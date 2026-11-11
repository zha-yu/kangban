"""console 药单管理 REST（Day 8 dev-A H+1，2026-11-11）。

owner=console 药单接口 · 依据 frontend/src/console/medication-api.ts 的 Mock 契约
冻结签名（页面层已按 `PlanWriteInput` / `MedicationPlan` 类型开发）；
上游依据 PRD §4.5（用药提醒状态机）、FR-DLG-10（预算熔断）、AC-27（历史快照隔离）。

只做三件事：
1. Pydantic 校验（cron 合法性 + start_date ≤ end_date 在 service 层用同一校验器）；
2. API 层统一放行 `require_roles("doctor","care_worker","admin")`——**绝不**在这里
   写 `require_roles("doctor")` 提前拦截，因为越权写必须进入 service 层才能被审计
   （service 层双重门禁：role=doctor + elder_staff_assignments relation='doctor' scope）；
3. 调 `app/services/medication_plan_service.py`。不拼查询、不写库、不碰 ORM。

权限口径（v1.4 主理人裁决）：
- API 层一律放行 `require_roles("doctor","care_worker","admin")`——禁止 API 层直接
  `require_roles("doctor")` 提前拦截导致审计落空；
- Service 层读 scope 过滤：admin 全量；doctor/care_worker 只看自己在
  elder_staff_assignments 有 active assignment 的长者（relation IN
  primary_care/doctor）；越权读 list 返回空集合 / get 返回 None（API 层转 404）+
  审计 action=medication_plan_read_forbidden, detail.reason="read_scope_outside_assignment"；
- Service 层写双重门禁：role=doctor + 该 staff 是该长者的负责医生（relation='doctor'）；
  抛 403 + 审计 reason="role_not_doctor" 或 "not_attending_doctor_for_elder"。

版本管理（G1 裁决落地，迁移 0019）：
- create → version=1, status='active'
- update → version+1, status 保持 active；停用后不可修改
- stop → version+1, status→'stopped'；无删除语义；已停用不可重复停
- plan_version 联动 medication_events 唯一键三列，改药单后新事件自动带新版本号

不碰：`app/main.py`（写锁 L7-2），经 `app/api/__init__.py` 的 ROUTERS 注册表挂载。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.core.auth import require_roles
from app.core.runtime_context import get_identity
from app.db import get_session
from app.services import medication_plan_service as plan_svc
from app.services.medication_plan_service import (
    PlanError,
    PlanInvalidCron,
    PlanNotFound,
    PlanStopped,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/medication-plans",
    tags=["medication-plans"],
    route_class=ContractAPIRoute,
)

#: API 层放行所有 staff 角色；Service 层内部做双重门禁 + scope 过滤。
_STAFF_ROLES = ("doctor", "care_worker", "admin")


# ===================================================== Schema（就地定义，与前端 Mock 逐字段对齐）


class PlanCreateIn(BaseModel):
    """新建药单请求体——与前端 `PlanWriteInput` 对齐。"""

    model_config = ConfigDict(extra="forbid")

    elder_id: uuid.UUID = Field(..., description="长者 ID")
    drug_name: str = Field(..., min_length=1, max_length=200, description="药品名")
    dose: str = Field(..., min_length=1, max_length=200, description="剂量")
    schedule_cron: str = Field(
        ..., min_length=1, max_length=200, description="cron 表达式（Asia/Shanghai）"
    )
    start_date: datetime.date = Field(..., description="开始日期")
    end_date: datetime.date | None = Field(default=None, description="结束日期（None = 永久）")


class PlanUpdateIn(BaseModel):
    """修改药单请求体——同 PlanCreateIn（前端 Mock 签名一致）。"""

    model_config = ConfigDict(extra="forbid")

    elder_id: uuid.UUID = Field(..., description="长者 ID（不可改，API 层忽略，用 plan 表原值）")
    drug_name: str = Field(..., min_length=1, max_length=200)
    dose: str = Field(..., min_length=1, max_length=200)
    schedule_cron: str = Field(..., min_length=1, max_length=200)
    start_date: datetime.date
    end_date: datetime.date | None = Field(default=None)


class PlanOut(BaseModel):
    """药单返回体——与前端 `MedicationPlan` 逐字段对齐（含 created_by_name、created_at）。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    plan_id: uuid.UUID
    elder_id: uuid.UUID
    drug_name: str
    dose: str
    schedule_cron: str
    start_date: datetime.date
    end_date: datetime.date | None
    version: int
    created_by_name: str | None
    status: str
    created_at: datetime.datetime


# ===================================================== 辅助：序列化 + 异常映射


def _serialize(row: plan_svc.PlanRow) -> PlanOut:
    return PlanOut(
        plan_id=row.plan.plan_id,
        elder_id=row.plan.elder_id,
        drug_name=row.plan.drug_name,
        dose=row.plan.dose,
        schedule_cron=row.plan.schedule_cron,
        start_date=row.plan.start_date,
        end_date=row.plan.end_date,
        version=row.plan.version,
        created_by_name=row.created_by_name,
        status=row.plan.status,
        created_at=row.plan.created_at,
    )


def _map_service_error(exc: PlanError) -> None:
    """Service 层领域异常 → HTTPException。"""
    if isinstance(exc, PlanNotFound):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PlanStopped):
        raise HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, PlanInvalidCron):
        raise HTTPException(status_code=422, detail=str(exc))
    # 兜底：其他 PlanError → 400
    raise HTTPException(status_code=400, detail=str(exc))


def _current_staff_uuid() -> uuid.UUID | None:
    """从 runtime_context 取 staff_id 并转 UUID；无身份时返回 None。"""
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


# ===================================================== 只读路由


@router.get("", response_model=list[PlanOut])
async def list_plans_by_elder(
    session: Annotated[AsyncSession, Depends(get_session)],
    _staff: Annotated[object, Depends(require_roles(*_STAFF_ROLES))],
    elder_id: uuid.UUID = Query(..., description="长者 ID"),
    include_stopped: bool = Query(default=True, description="是否包含已停用药单"),
) -> list[PlanOut]:
    """按长者查药单（Service 层 scope 过滤 + 越权读审计）。

    越权读（care_worker/doctor 查非负责长者）→ Service 层返回空集合 + 写审计；
    API 层直接返回 []，不暴露"这个长者有药单但你无权看"。
    """
    rows = await plan_svc.list_plans_by_elder(
        session,
        elder_id=elder_id,
        include_stopped=include_stopped,
    )
    return [_serialize(r) for r in rows]


@router.get("/{plan_id}", response_model=PlanOut)
async def get_plan(
    plan_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _staff: Annotated[object, Depends(require_roles(*_STAFF_ROLES))],
) -> PlanOut:
    """查单条药单（Service 层 scope 校验 + 越权读审计）。

    plan 不存在或 scope 外 → 统一 404 "药单不存在"（不暴露存在性）。
    """
    row = await plan_svc.get_plan(session, plan_id=plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="药单不存在")
    return _serialize(row)


# ===================================================== 写路由（Service 层双重门禁 + 审计）


@router.post("", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(
    body: PlanCreateIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    _staff: Annotated[object, Depends(require_roles(*_STAFF_ROLES))],
) -> PlanOut:
    """新建药单（**Service 层双重门禁**：role=doctor + relation='doctor' scope）。

    created_by 从 JWT 注入；**绝不采信前端传值**——AGENTS §4 红线 9。
    """
    created_by = _current_staff_uuid()
    if created_by is None:
        raise HTTPException(status_code=401, detail="无有效 staff 身份")

    try:
        row = await plan_svc.create_plan(
            session,
            elder_id=body.elder_id,
            drug_name=body.drug_name,
            dose=body.dose,
            schedule_cron=body.schedule_cron,
            start_date=body.start_date,
            end_date=body.end_date,
            created_by=created_by,
        )
    except PlanError as exc:
        _map_service_error(exc)
        raise  # pragma: no cover
    return _serialize(row)


@router.put("/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: uuid.UUID,
    body: PlanUpdateIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    _staff: Annotated[object, Depends(require_roles(*_STAFF_ROLES))],
) -> PlanOut:
    """修改药单（version+1，已停用不可改，Service 层双重门禁）。

    body.elder_id 由前端 Mock 签名要求携带，**API 层忽略**——
    plan.elder_id 由数据库原值决定，前端不能改（plan_id 已锁定）。
    """
    updated_by = _current_staff_uuid()
    if updated_by is None:
        raise HTTPException(status_code=401, detail="无有效 staff 身份")

    try:
        row = await plan_svc.update_plan(
            session,
            plan_id=plan_id,
            drug_name=body.drug_name,
            dose=body.dose,
            schedule_cron=body.schedule_cron,
            start_date=body.start_date,
            end_date=body.end_date,
            updated_by=updated_by,
        )
    except PlanError as exc:
        _map_service_error(exc)
        raise  # pragma: no cover
    return _serialize(row)


@router.post("/{plan_id}/stop", response_model=PlanOut)
async def stop_plan(
    plan_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _staff: Annotated[object, Depends(require_roles(*_STAFF_ROLES))],
) -> PlanOut:
    """停用药单（version+1，无删除语义；已停用不可重复停，Service 层双重门禁）。"""
    stopped_by = _current_staff_uuid()
    if stopped_by is None:
        raise HTTPException(status_code=401, detail="无有效 staff 身份")

    try:
        row = await plan_svc.stop_plan(
            session,
            plan_id=plan_id,
            stopped_by=stopped_by,
        )
    except PlanError as exc:
        _map_service_error(exc)
        raise  # pragma: no cover
    return _serialize(row)


__all__ = ["router"]
