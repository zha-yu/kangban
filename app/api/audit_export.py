"""审计日志查询与 CSV 导出（Day 9 A4 / FR-ADM-05 P2）。

## 为什么单独一个文件

审计是**管理端合规面**，不是员工告警域（alerts.py）也不是账号域（staff.py）：
- `GET /api/audit-logs`（json 列表，时间/操作人筛选）——console「审计日志」页消费；
- `GET /api/audit-logs/export.csv`（CSV 导出）——FR-ADM-05 导出。

两处均 **admin-only**，路径与筛选参数对齐 C6 `stats-api.ts` 定稿：
`start_date` / `end_date`（`yyyy-mm-dd`，含）/ `actor`（姓名或 ID 模糊匹配）。

## 验收口径（任务书 A4）

- 列表行数与 CSV 行数均与 `audit_logs` 表按同一筛选条件一致；
- **不含 PII 明文**：detail 序列化后过 `redact.py` 文本级捞取（手机号/身份证/
  房间号/明文密钥掩码化）——审计本就不该有 PII（audit_log_service.py:10），
  出口再加一道闸，验收 grep 零命中双保险；
- 行形态对齐前端 `AuditLogItem`（可选字段＋未知透传）：`id / action / actor_id /
  actor_name / created_at / detail / trace_id`；`actor_name` 由 staff/guardian
  账号表批量联查填充（审计行本身不存姓名，避免 PII 落库）。

## 边界

- 不做分页：审计是合规取证数据，截断会让「行数一致」失真；当前演示规模
  （Day 9）单次返回量可控，若未来量级上来再加流式输出（不改变口径）。
- 时间边界按 UTC（存储口径 UTC，AGENTS §8）；前端传 `yyyy-mm-dd`，服务端
  解释为 UTC 当日 [00:00, 23:59:59.999999]。
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import require_roles
from app.core.redact import redact_text
from app.db import get_session
from app.models import AuditLog, GuardianAccount, StaffAccount

router = APIRouter(
    prefix="/api",
    tags=["audit-export"],
    route_class=ContractAPIRoute,
)

_CSV_HEADERS = (
    "audit_id",
    "created_at",
    "actor_type",
    "actor_id",
    "action",
    "resource_type",
    "resource_id",
    "detail",
)


async def _query_audit_logs(
    session: AsyncSession,
    *,
    start_date: datetime.date | None,
    end_date: datetime.date | None,
    actor: str | None,
) -> list[AuditLog]:
    """筛选查询（列表与 CSV 共用同一 where 条件族，保证行数一致）。

    `actor` 口径：可解析为 uuid → actor_id 精确匹配；否则按操作人姓名模糊匹配
    （staff_accounts / guardian_accounts.name ilike %actor%）。
    """
    stmt = select(AuditLog).order_by(AuditLog.created_at.asc())
    if start_date is not None:
        stmt = stmt.where(
            AuditLog.created_at
            >= datetime.datetime.combine(start_date, datetime.time.min, tzinfo=datetime.UTC)
        )
    if end_date is not None:
        stmt = stmt.where(
            AuditLog.created_at
            <= datetime.datetime.combine(end_date, datetime.time.max).replace(tzinfo=datetime.UTC)
        )
    if actor:
        try:
            actor_uuid = uuid.UUID(actor)
            stmt = stmt.where(AuditLog.actor_id == actor_uuid)
        except ValueError:
            names = (
                (
                    await session.execute(
                        select(StaffAccount.staff_id).where(StaffAccount.name.ilike(f"%{actor}%"))
                    )
                )
                .scalars()
                .all()
            )
            names += (
                (
                    await session.execute(
                        select(GuardianAccount.guardian_id).where(
                            GuardianAccount.name.ilike(f"%{actor}%")
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not names:
                return []
            stmt = stmt.where(AuditLog.actor_id.in_(names))
    return list((await session.execute(stmt)).scalars().all())


async def _actor_name_map(session: AsyncSession, rows: list[AuditLog]) -> dict[uuid.UUID, str]:
    """批量联查操作人姓名（staff/guardian；设备无姓名留空）。"""
    ids = {row.actor_id for row in rows if row.actor_id is not None}
    if not ids:
        return {}
    mapping: dict[uuid.UUID, str] = {}
    staff_rows = (
        await session.execute(
            select(StaffAccount.staff_id, StaffAccount.name).where(StaffAccount.staff_id.in_(ids))
        )
    ).all()
    mapping.update({sid: name for sid, name in staff_rows})
    guardian_rows = (
        await session.execute(
            select(GuardianAccount.guardian_id, GuardianAccount.name).where(
                GuardianAccount.guardian_id.in_(ids)
            )
        )
    ).all()
    mapping.update({gid: name for gid, name in guardian_rows})
    return mapping


def _parse_date(value: str | None) -> datetime.date | None:
    if not value:
        return None
    return datetime.date.fromisoformat(value)


@router.get(
    "/audit-logs",
    response_model=None,
    summary="审计日志列表（admin-only；时间/操作人筛选；行形态对齐前端 AuditLogItem）",
)
async def list_audit_logs(
    admin: Annotated[object, Depends(require_roles("admin"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    start_date: Annotated[str | None, Query(description="yyyy-mm-dd（含）起，UTC")] = None,
    end_date: Annotated[str | None, Query(description="yyyy-mm-dd（含）止，UTC")] = None,
    actor: Annotated[str | None, Query(description="操作人姓名或 ID 模糊匹配")] = None,
) -> dict[str, object] | object:
    _ = admin  # require_roles("admin") 已完成权限判定；无业务用途
    try:
        rows = await _query_audit_logs(
            session,
            start_date=_parse_date(start_date),
            end_date=_parse_date(end_date),
            actor=actor,
        )
    except ValueError:
        return _error_response(422, "VALIDATION_ERROR", "invalid date, expected yyyy-mm-dd")
    names = await _actor_name_map(session, rows)
    items = []
    for row in rows:
        detail_json = (
            "" if row.detail is None else redact_text(json.dumps(row.detail, ensure_ascii=False))
        )
        items.append(
            {
                "id": str(row.audit_id),
                "action": row.action,
                "actor_type": row.actor_type,
                "actor_id": "" if row.actor_id is None else str(row.actor_id),
                "actor_name": names.get(row.actor_id, ""),
                "created_at": row.created_at.isoformat(),
                "detail": detail_json,
                "trace_id": (row.detail or {}).get("trace_id", ""),
            }
        )
    return {"items": items, "total": len(items)}


@router.get(
    "/audit-logs/export.csv",
    response_model=None,
    summary="审计日志 CSV 导出（admin-only；与列表同筛选；UTF-8 BOM，Excel 直开不乱码）",
    response_class=Response,
)
async def export_audit_logs_csv(
    admin: Annotated[object, Depends(require_roles("admin"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    start_date: Annotated[str | None, Query(description="yyyy-mm-dd（含）起，UTC")] = None,
    end_date: Annotated[str | None, Query(description="yyyy-mm-dd（含）止，UTC")] = None,
    actor: Annotated[str | None, Query(description="操作人姓名或 ID 模糊匹配")] = None,
) -> Response | object:
    _ = admin
    try:
        rows = await _query_audit_logs(
            session,
            start_date=_parse_date(start_date),
            end_date=_parse_date(end_date),
            actor=actor,
        )
    except ValueError:
        return _error_response(422, "VALIDATION_ERROR", "invalid date, expected yyyy-mm-dd")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_HEADERS)
    for row in rows:
        detail_json = (
            "" if row.detail is None else redact_text(json.dumps(row.detail, ensure_ascii=False))
        )
        writer.writerow(
            [
                str(row.audit_id),
                row.created_at.isoformat(),
                row.actor_type,
                "" if row.actor_id is None else str(row.actor_id),
                row.action,
                row.resource_type,
                "" if row.resource_id is None else str(row.resource_id),
                detail_json,
            ]
        )

    content = buffer.getvalue().encode("utf-8-sig")  # BOM：Excel 直接打开不乱码
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit_logs.csv"},
    )
