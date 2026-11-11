"""健康数据采集域 REST（Day 8 · C1）。

口径来源：
- 代填提交：《接口契约_照护台业务数据》§3.3（H-2）；
- 复核留痕：§3.5（R-1/R-2/R-3/R-4）+ Q-1 裁决（2026-11-11 主理人拍板「补列」，
  `review_note` / `superseded_reason` 随 TD 修订补列，列由 dev-A 0017 迁移落，
  本层只做字段透传）；
- 医生趋势：任务卡 day08 C1②（端点编号待 B1 冻结版分配，docstring 占位）。

骨架约定（计划 S1）：
- 本文件只做参数校验 + `require_roles` 门禁 + 调 `app.services.health_service`
  （S1 骨架期服务未落地，处理器统一 `NotImplementedError` 桩；S3 联调段接入
  真实实现后本文件不再改动处理器签名）；
- severity / review_status / operator 一律服务端注入，客户端传值丢弃（AGENTS §4 红线 9）；
- 端点编号沿用契约 v0.1 草案，dev-B 冻结版发布后回填 docstring。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.core.auth import require_roles
from app.core.runtime_context import Identity
from app.db import get_session

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
    - 操作者身份从 `runtime_context` 注入，不收客户端 `operator_id`；
    - 长者负责范围过滤在服务层（护工仅可代填名下长者）。
    """
    raise NotImplementedError  # S3 联调段接入 app.services.health_service


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
    raise NotImplementedError  # S3 联调段接入


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
    raise NotImplementedError  # S3 联调段接入


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
    raise NotImplementedError  # S3 联调段接入


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
    """[编号 R-2b｜契约 §3.5] 版本链按 version 升序（Mock getRecordChain 同口径）。"""
    raise NotImplementedError  # S3 联调段接入


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
    raise NotImplementedError  # S3 联调段接入


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
    raise NotImplementedError  # S3 联调段接入
