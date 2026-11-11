"""家属端日报 REST：F2 详情（Day 8 · D1）。

## 范围

契约《家属端数据》v1.2 §4：
- **F0**：`GET /api/family/elders` —— 绑定长者清单（切换器数据源，三字段裁剪）。
- **F1**：`GET /api/family/elders/{elder_id}/reports` —— 按长者的日报列表。
- **F2**：`GET /api/family/reports/{report_id}` —— 详情（任意版本可查，F3 复用本端点）。

F1 的越权裁决在 **api 层按 F0 绑定清单**判：`list_reports_for_guardian()` 对不在
绑定范围的 `elder_id` 返回空集（它的 docstring 明确「越权分流由 API 层裁决」），
所以这里先查绑定清单，不在清单就 403+审计，在清单才取列表。

## 分层与口径

- 身份只从 `current_guardian` 注入；客户端传的 `guardian_id` 一律不认。
- 范围过滤（绑定校验）在 service 里做，api 不拼查询；403/404 由 service 的
  `GuardianReportForbidden` / `GuardianReportNotFound` 区分。
- **403 必审计**（契约 §4 F2 括注）：`record_audit()` 只 flush 不 commit，所以
  这里**正常 return 403 响应**让 `get_session` 提交；raise 会把审计回滚掉。
- `attention_events[].type` 原样透传 `risk_events` 五枚举（计划 §13 K-1），
  **不做别名映射**——映射在 service 里按 risk 类型产出中文 short label。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import current_guardian
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import report_service
from app.services.audit_log_service import record_audit
from app.services.report_service import GuardianReportForbidden, GuardianReportNotFound

router = APIRouter(
    prefix="/api/family",
    tags=["family"],
    route_class=ContractAPIRoute,
)


class AttentionEventOut(BaseModel):
    """`risk_events` 一行的事件视图。`type` 保持五枚举原值（K-1）。"""

    model_config = ConfigDict(extra="forbid")

    level: str
    type: str
    brief: str
    occurred_at: str


class ReportDetailOut(BaseModel):
    """F2 响应，字段 = 契约 §4 F2 示例。"""

    model_config = ConfigDict(extra="forbid")

    report_id: uuid.UUID
    elder_id: uuid.UUID
    report_date: str
    version: int
    status: str
    summary_text: str
    medication_stats: dict[str, Any]
    health_summary: str
    attention_events: list[AttentionEventOut]
    audio_url: str | None
    pushed_at: str | None


def _iso(value: object) -> str | None:
    return value.isoformat() if value is not None else None  # type: ignore[attr-defined]


def _serialize(report: Any, health_summary: str, events: list[dict[str, Any]]) -> ReportDetailOut:
    return ReportDetailOut(
        report_id=report.report_id,
        elder_id=report.elder_id,
        report_date=report.report_date.isoformat(),
        version=int(report.version),
        status=report.status,
        summary_text=report.summary_text,
        # 四键必全（契约 §2.1 S-01）：缺键补 0，不合并、不改名
        medication_stats={
            "taken_reported": int((report.medication_stats or {}).get("taken_reported", 0)),
            "refused": int((report.medication_stats or {}).get("refused", 0)),
            "missed_unconfirmed": int((report.medication_stats or {}).get("missed_unconfirmed", 0)),
            "notification_failed": int(
                (report.medication_stats or {}).get("notification_failed", 0)
            ),
        },
        health_summary=health_summary,
        attention_events=[
            AttentionEventOut(
                level=str(ev.get("level", "")),
                type=str(ev.get("type", "")),
                brief=str(ev.get("brief", "")),
                occurred_at=str(ev.get("occurred_at", "")),
            )
            for ev in events
        ],
        audio_url=report.audio_url,
        pushed_at=_iso(report.pushed_at),
    )


async def _forbidden(session: AsyncSession, guardian: Identity, report_id: uuid.UUID):
    """403 + 审计（契约 §4 F2「403 日报所属长者不在绑定范围（审计）」）。"""
    await record_audit(
        session,
        actor_type="guardian",
        actor_id=uuid.UUID(guardian.guardian_id),
        action="daily_report_read_forbidden",
        resource_type="daily_report",
        resource_id=report_id,
        detail={"reason": "elder_not_bound"},
    )
    return _error_response(403, "SCOPE_FORBIDDEN", "report is outside your scope")


class BoundElderOut(BaseModel):
    """F0 单条：三字段裁剪（契约 S-05 最小披露）。"""

    model_config = ConfigDict(extra="forbid")

    elder_id: uuid.UUID
    name: str
    relation: str


class ReportSummaryOut(BaseModel):
    """F1 列表项（契约 §4 F1）。`summary_preview` 截 80 字（S-06）。"""

    model_config = ConfigDict(extra="forbid")

    report_id: uuid.UUID
    elder_id: uuid.UUID
    report_date: str
    version: int
    status: str
    summary_preview: str
    audio_url: str | None
    pushed_at: str | None


_PREVIEW_LIMIT = 80


@router.get("/elders", response_model=dict)
async def list_bound_elders(
    session: Annotated[AsyncSession, Depends(get_session)],
    guardian: Annotated[Identity, Depends(current_guardian)],
) -> dict[str, object]:
    """F0：我绑定的长者（未绑定返回空数组，不是 404——S-08）。"""
    rows = await report_service.list_bound_elders_for_guardian(
        session, guardian_id=uuid.UUID(guardian.guardian_id)
    )
    return {
        "items": [
            BoundElderOut(elder_id=elder_id, name=name, relation=relation)
            for elder_id, name, relation in rows
        ]
    }


async def _list_forbidden(session: AsyncSession, guardian: Identity, elder_id: uuid.UUID):
    """F1 越权：403 + 审计（契约 §4 F1「403 elder 不在绑定范围（审计）」）。"""
    await record_audit(
        session,
        actor_type="guardian",
        actor_id=uuid.UUID(guardian.guardian_id),
        action="daily_report_list_forbidden",
        resource_type="elder",
        resource_id=elder_id,
        detail={"reason": "elder_not_bound"},
    )
    return _error_response(403, "SCOPE_FORBIDDEN", "elder is outside your scope")


@router.get("/elders/{elder_id}/reports", response_model=dict)
async def list_reports(
    elder_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    guardian: Annotated[Identity, Depends(current_guardian)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """F1：某个长者的日报列表（每日期只出最新版 S-07，`report_date` 倒序）。"""
    guardian_id = uuid.UUID(guardian.guardian_id)
    bound = await report_service.list_bound_elders_for_guardian(session, guardian_id=guardian_id)
    if elder_id not in {row[0] for row in bound}:
        # 未绑定（含根本没有绑定关系）一律 403，不泄露该长者是否有日报
        return await _list_forbidden(session, guardian, elder_id)

    rows, total = await report_service.list_reports_for_guardian(
        session,
        guardian_id=guardian_id,
        elder_id=elder_id,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [
            ReportSummaryOut(
                report_id=row.report_id,
                elder_id=row.elder_id,
                report_date=row.report_date.isoformat(),
                version=int(row.version),
                status=row.status,
                summary_preview=row.summary_text[:_PREVIEW_LIMIT],
                audio_url=row.audio_url,
                pushed_at=_iso(row.pushed_at),
            )
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/reports/{report_id}", response_model=ReportDetailOut)
async def get_report_detail(
    report_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    guardian: Annotated[Identity, Depends(current_guardian)],
):
    """F2：日报详情（含历史版本）。

    - 不存在 → 404 `NOT_FOUND`；
    - 存在但长者不在绑定范围 → 403 `SCOPE_FORBIDDEN` + 审计。
    """
    try:
        report, health_summary, events = await report_service.get_report_detail_for_guardian(
            session,
            guardian_id=uuid.UUID(guardian.guardian_id),
            report_id=report_id,
        )
    except GuardianReportForbidden:
        return await _forbidden(session, guardian, report_id)
    except GuardianReportNotFound:
        return _error_response(404, "NOT_FOUND", "report not found")
    return _serialize(report, health_summary, events)
