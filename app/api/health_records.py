"""健康数据采集域 REST（Day 8 · C1）。

口径来源：
- 代填提交：《接口契约_照护台业务数据》§3.3（H-2）；
- 复核留痕：§3.5（R-1/R-2/R-3/R-4）+ Q-1 裁决（2026-11-11 主理人拍板「补列」，
  `review_note` / `superseded_reason` 随 TD 修订补列，列由 dev-A 0017 迁移落，
  本层只做字段透传）；
- 医生趋势：任务卡 day08 C1②（端点编号待 B1 冻结版分配，docstring 占位）。

骨架约定（计划 S1）：
- 本文件只做参数校验 + `require_roles` 门禁 + 调 `app.services.health_service`
  （S3 联调段已接入真实服务：查询函数 list_pending_reviews / get_record_row /
  get_record_chain / get_health_trend 由 dev-C 增量补进 health_service）；
- severity / review_status / operator 一律服务端注入，客户端传值丢弃（AGENTS §4 红线 9）；
- 端点编号沿用契约 v0.1 草案，dev-B 冻结版发布后回填 docstring。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.core.auth import require_roles
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import audit_log_service, health_service

router = APIRouter(
    prefix="/api/health-records",
    route_class=ContractAPIRoute,
    tags=["health-records"],
)

# --------------------------------------------------------------- 门禁依赖

#: 代填写入口（H-2）：护工/医生/管理员
_record_writer = require_roles("care_worker", "doctor", "admin")

#: 复核队列与操作（R-1/R-3/R-4）：三角色可进，黄/红档范围过滤在服务层
_health_reviewer = require_roles("care_worker", "doctor", "admin")

#: 医生趋势（C1②）：仅医生/管理员
_trend_reader = require_roles("doctor", "admin")

_Session = Annotated[AsyncSession, Depends(get_session)]


# --------------------------------------------------------------- 服务端注入口径

#: follow_up 提示文案（PRD §4.6：红→紧急工单+通知；黄→人工复核任务；绿→入库日报）
_FOLLOW_UP: dict[str, str] = {
    "red": "已触发紧急告警：将创建紧急工单并同步通知家属，请立即跟进",
    "yellow": "已生成人工复核任务，请在复核队列处理",
    "green": "数据正常，已入库并计入健康日报",
}

#: 红档复核角色（任务卡 C1①：红档仅医生/管理员，护工 403）
_RED_REVIEW_ROLES = ("doctor", "admin")

#: R-4 纠错字段白名单（五项指标 + 两个备注字段，与 H-2 提交面一致）
_CORRECT_FIELDS_ALLOWED = {
    "systolic",
    "diastolic",
    "sleep_hours",
    "mood_score",
    "mood_note",
    "complaint",
}


def _view_row(row: health_service.HealthRecordRow) -> HealthRecordView:
    """服务行 → 契约视图（D-4：JOIN 展示字段服务端拼）。"""
    r = row.record
    return HealthRecordView(
        record_id=r.record_id,
        elder_id=r.elder_id,
        elder_name=row.elder_name,
        room_no=row.room_no,
        bed_no=row.bed_no,
        operator_id=r.operator_id,
        operator_name=row.operator_name,
        recorded_at=r.recorded_at,
        source=r.source,
        systolic=r.systolic,
        diastolic=r.diastolic,
        sleep_hours=r.sleep_hours,
        mood_score=r.mood_score,
        mood_note=r.mood_note,
        complaint=r.complaint,
        severity=r.severity,
        rule_version=r.rule_version,
        version=r.version,
        review_status=r.review_status,
        reviewed_by=r.reviewed_by,
        # TD health_records 无 reviewed_at 列（契约 161 行引注有误，dev-A 落列正确）；
        # 偏差已登记计划 §10，待 dev-B 契约勘误（去字段或裁决补列）。
        reviewed_at=None,
        reviewed_by_name=row.reviewer_name,
        review_note=r.review_note,
        superseded_by=r.superseded_by,
        superseded_reason=r.superseded_reason,
    )


async def _audit_denial(
    session: AsyncSession,
    identity: Identity,
    *,
    action: str,
    resource_type: str,
    resource_id: UUID,
    reason: str,
) -> None:
    """越权拒绝写审计（AGENTS §8：越权 → 拒绝 + 审计 + trace_id）。"""
    await audit_log_service.record_audit(
        session,
        actor_type="staff",
        actor_id=UUID(identity.staff_id),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail={"reason": reason, "role": identity.role},
    )


async def _ensure_red_reviewer(
    session: AsyncSession,
    identity: Identity,
    *,
    record_id: UUID,
    severity: str,
) -> None:
    """红档复核角色门禁（越权三例之二）：非医生/管理员 → 403 + 审计。"""
    if severity == "red" and identity.role not in _RED_REVIEW_ROLES:
        await _audit_denial(
            session,
            identity,
            action="health_review_denied",
            resource_type="health_record",
            resource_id=record_id,
            reason="red_requires_doctor",
        )
        raise HTTPException(status_code=403, detail="红档记录仅医生/管理员可复核")


# --------------------------------------------------------------- 请求模型


class HealthRecordSubmit(BaseModel):
    """H-2 代填提交：五项指标任一非空；severity 服务端计算，不收客户端值。

    区间口径：契约 §3.3——systolic/diastolic 0–300 CHECK、sleep_hours 0–24、
    mood_score 1–5。
    """

    model_config = ConfigDict(extra="forbid")

    elder_id: UUID
    source: Literal["staff_scheduled", "staff_active"]
    systolic: int | None = Field(default=None, ge=0, le=300)
    diastolic: int | None = Field(default=None, ge=0, le=300)
    sleep_hours: float | None = Field(default=None, ge=0, le=24)
    mood_score: int | None = Field(default=None, ge=1, le=5)
    mood_note: str | None = Field(default=None, max_length=200)
    complaint: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _at_least_one_metric(self) -> HealthRecordSubmit:
        if all(
            value is None
            for value in (
                self.systolic,
                self.diastolic,
                self.sleep_hours,
                self.mood_score,
                self.mood_note,
                self.complaint,
            )
        ):
            raise ValueError("至少填写一项健康指标（血压/睡眠/情绪/主诉）")
        return self


class HealthReviewConfirm(BaseModel):
    """R-3 确认：备注可选，落 `review_note` 列（Q-1 补列后）。"""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=500)


class HealthReviewCorrect(BaseModel):
    """R-4 纠错：`fields` 键白名单（五项指标）由服务层校验并生成新版本。"""

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, Any]
    review_note: str | None = Field(default=None, max_length=500)


# --------------------------------------------------------------- 响应模型


class HealthRecordCreated(BaseModel):
    """H-2 响应（契约 v1.0 §3.3 冻结口径：恰好五字段，无扩展）。"""

    record_id: UUID
    severity: Literal["green", "yellow", "red"]
    rule_version: str
    recorded_at: datetime
    follow_up: str


class HealthRecordView(BaseModel):
    """R-1/R-2 复核队列/详情/版本链视图（契约 §3.5 全列 + JOIN 展示字段）。

    `review_note` / `superseded_reason`：Q-1 已裁决补列，dev-A 0017 迁移落列，
    服务层接入前这两字段恒为 None。
    """

    record_id: UUID
    elder_id: UUID
    elder_name: str
    room_no: str | None
    bed_no: str | None
    operator_id: UUID | None
    operator_name: str | None
    recorded_at: datetime
    source: Literal["staff_scheduled", "staff_active"]
    systolic: int | None
    diastolic: int | None
    sleep_hours: float | None
    mood_score: int | None
    mood_note: str | None
    complaint: str | None
    severity: Literal["green", "yellow", "red"]
    rule_version: str
    version: int
    review_status: Literal["pending", "confirmed", "corrected"]
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    reviewed_by_name: str | None
    review_note: str | None
    superseded_by: UUID | None
    superseded_reason: str | None


class HealthTrendPoint(BaseModel):
    recorded_at: datetime
    systolic: int | None
    diastolic: int | None
    sleep_hours: float | None
    mood_score: int | None


class HealthTrend(BaseModel):
    elder_id: UUID
    window_days: Literal[14] = 14
    points: list[HealthTrendPoint]


# --------------------------------------------------------------- 端点


@router.post(
    "",
    response_model=HealthRecordCreated,
    status_code=200,
    summary="[H-2] 代填提交健康指标",
)
async def submit_health_record(
    body: HealthRecordSubmit,
    identity: Annotated[Identity, Depends(_record_writer)],
    session: _Session,
) -> HealthRecordCreated:
    """[编号 H-2｜契约 §3.3] 护工/医生/管理员代填五项健康指标。

    - severity 由服务端分级引擎计算（green/yellow/red，红档同步触发通知）；
      红档 dedupe_key 按 dev-A A3 落库口径：`risk_event:{event_id}:staff` /
      `risk_event:{event_id}:guardian` / 升级 `risk_event:{event_id}:escalated`
      —— 锚点是 event_id 而非 elder_id+日期（AC-12：同一事件不建第二
      risk_event；同日两次独立红档各自成事件，按日期去重会吞第二条）；
    - 长者负责范围过滤在服务层（护工仅可代填名下长者，AC-32）。
    """
    try:
        ref = await health_service.create_health_record(
            session,
            elder_id=body.elder_id,
            source=body.source,
            systolic=body.systolic,
            diastolic=body.diastolic,
            sleep_hours=body.sleep_hours,
            mood_score=body.mood_score,
            mood_note=body.mood_note,
            complaint=body.complaint,
            operator_id=UUID(identity.staff_id),
        )
    except PermissionError as exc:
        await _audit_denial(
            session,
            identity,
            action="health_submit_denied",
            resource_type="elder",
            resource_id=body.elder_id,
            reason="not_primary_care",
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    row = await health_service.get_record_row(session, record_id=ref.record_id)
    assert row is not None  # 同事务刚写入
    return HealthRecordCreated(
        record_id=ref.record_id,
        severity=ref.severity,
        rule_version=row.record.rule_version,
        recorded_at=row.record.recorded_at,
        follow_up=_FOLLOW_UP[ref.severity],
    )


@router.get(
    "/pending-reviews",
    response_model=list[HealthRecordView],
    summary="[R-1] 复核队列（黄档护工可看；红档仅医生/管理员——服务层过滤）",
)
async def list_pending_reviews(
    identity: Annotated[Identity, Depends(_health_reviewer)],
    session: _Session,
) -> list[HealthRecordView]:
    """[编号 R-1｜契约 §3.5] 复核队列，按 recorded_at 倒序。

    范围口径：黄档 care_worker 可见；红档仅 doctor/admin（服务层按
    identity.role 过滤），护工看不到红档记录（越权三例之一，S3 落测试）。
    """
    rows = await health_service.list_pending_reviews(session, role=identity.role)
    return [_view_row(r) for r in rows]


@router.get(
    "/trend/{elder_id}",
    response_model=HealthTrend,
    summary="[趋势·编号待 B1 分配] 医生视角近 14 天指标序列",
)
async def get_health_trend(
    elder_id: UUID,
    identity: Annotated[Identity, Depends(_trend_reader)],
    session: _Session,
) -> HealthTrend:
    """[任务卡 C1②｜编号待 dev-B 冻结版回填] 近 14 天血压/睡眠/情绪序列。

    仅 doctor/admin；护工不开放（溢出池 O1「医生趋势图」的数据源）。
    """
    records = await health_service.get_health_trend(session, elder_id=elder_id)
    if records is None:
        raise HTTPException(status_code=404, detail="长者不存在")
    return HealthTrend(
        elder_id=elder_id,
        points=[
            HealthTrendPoint(
                recorded_at=r.recorded_at,
                systolic=r.systolic,
                diastolic=r.diastolic,
                sleep_hours=r.sleep_hours,
                mood_score=r.mood_score,
            )
            for r in records
        ],
    )


@router.get(
    "/{record_id}",
    response_model=HealthRecordView,
    summary="[R-2] 记录详情（含复核留痕字段）",
)
async def get_health_record(
    record_id: UUID,
    identity: Annotated[Identity, Depends(_health_reviewer)],
    session: _Session,
) -> HealthRecordView:
    """[编号 R-2｜契约 §3.5] 单条记录详情；不存在与越权同口径 404。"""
    row = await health_service.get_record_row(session, record_id=record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return _view_row(row)


@router.get(
    "/{record_id}/chain",
    response_model=list[HealthRecordView],
    summary="[R-2b] 版本链（同 elder_id + 同 recorded_at，version 升序）",
)
async def get_health_record_chain(
    record_id: UUID,
    identity: Annotated[Identity, Depends(_health_reviewer)],
    session: _Session,
) -> list[HealthRecordView]:
    """[编号 R-2b｜契约 §3.5] 版本链按 version 升序（Mock getRecordChain 同口径）。

    链接口径按 R-4/AC-22 以 superseded_by 链接（契约 169 行「同 recorded_at」
    与 R-4 矛盾，偏差已登记，待 dev-B 勘误）。
    """
    rows = await health_service.get_record_chain(session, record_id=record_id)
    if not rows:
        raise HTTPException(status_code=404, detail="记录不存在")
    return [_view_row(r) for r in rows]


@router.post(
    "/{record_id}/confirm",
    response_model=HealthRecordView,
    summary="[R-3] 复核确认（备注落 review_note）",
)
async def confirm_health_record(
    record_id: UUID,
    body: HealthReviewConfirm,
    identity: Annotated[Identity, Depends(_health_reviewer)],
    session: _Session,
) -> HealthRecordView:
    """[编号 R-3｜契约 §3.5] 确认动作；红档仅 doctor/admin（服务层校验）。

    幂等：已 confirmed 的记录重复确认返回原结果（服务层口径，S3 落测试）。
    """
    row = await health_service.get_record_row(session, record_id=record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    await _ensure_red_reviewer(session, identity, record_id=record_id, severity=row.record.severity)
    if row.record.review_status == "confirmed":
        return _view_row(row)  # 幂等：重复确认返回原结果
    if row.record.review_status == "corrected":
        raise HTTPException(status_code=409, detail="记录已被纠错取代，请复核最新版本")
    await health_service.review_health_record(
        session,
        record_id=record_id,
        reviewer_id=UUID(identity.staff_id),
        action="confirm",
        review_note=body.note,
    )
    updated = await health_service.get_record_row(session, record_id=record_id)
    assert updated is not None
    return _view_row(updated)


@router.post(
    "/{record_id}/correct",
    response_model=HealthRecordView,
    summary="[R-4] 复核纠正（生成 corrected 新版本 + superseded 留痕）",
)
async def correct_health_record(
    record_id: UUID,
    body: HealthReviewCorrect,
    identity: Annotated[Identity, Depends(_health_reviewer)],
    session: _Session,
) -> HealthRecordView:
    """[编号 R-4｜契约 §3.5] 纠错生成新版本；原版本 superseded_reason 留痕。

    纠错写审计（audit_logs），红档纠正需 doctor/admin（服务层校验）。
    """
    row = await health_service.get_record_row(session, record_id=record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    await _ensure_red_reviewer(session, identity, record_id=record_id, severity=row.record.severity)
    if row.record.review_status == "corrected":
        raise HTTPException(status_code=409, detail="记录已被纠错取代，不能重复纠错")
    if not body.fields:
        raise HTTPException(status_code=422, detail="至少修改一项指标")
    invalid = set(body.fields) - _CORRECT_FIELDS_ALLOWED
    if invalid:
        raise HTTPException(status_code=422, detail=f"纠错字段不支持：{sorted(invalid)}")

    ref = await health_service.review_health_record(
        session,
        record_id=record_id,
        reviewer_id=UUID(identity.staff_id),
        action="correct",
        corrected_values=body.fields,
        review_note=body.review_note,
    )
    await audit_log_service.record_audit(
        session,
        actor_type="staff",
        actor_id=UUID(identity.staff_id),
        action="health_record_correct",
        resource_type="health_record",
        resource_id=ref.record_id,
        detail={
            "old_record_id": str(record_id),
            "severity": ref.severity,
            "version": ref.version,
        },
    )
    updated = await health_service.get_record_row(session, record_id=ref.record_id)
    assert updated is not None
    return _view_row(updated)
