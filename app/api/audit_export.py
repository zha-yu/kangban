"""审计日志 CSV 导出（Day 9 A4 / FR-ADM-05 P2）。

## 为什么单独一个文件

审计导出是**管理端合规面**，不是员工告警域（alerts.py）也不是账号域
（staff.py）：`GET /api/audit-logs/export`，**admin-only**，支持按时间区间与
操作人筛选，产 CSV。console 侧「审计日志」入口消费本端点。

## 验收口径（任务书 A4）

- 行数与看板查询**一致**（同一 where 条件族）；
- **不含 PII 明文**：detail 序列化后过 `redact.py` 文本级捞取（手机号/身份证/
  房间号/明文密钥掩码化）——审计本就不该有 PII（audit_log_service.py:10），
  出口再加一道闸，验收 grep 零命中双保险；
- CSV 抽样与 `audit_logs` 表逐字段核对一致。

## 边界

- 不做分页：审计是合规取证数据，截断会让「行数一致」失真；当前演示规模
  （Day 9）单次导出量可控，若未来量级上来再加流式输出（不改变口径）。
- `detail` 是 JSONB，CSV 单元格序列化为 JSON 文本（`ensure_ascii=False`），
  消费方按 JSON 解析；`None` → 空串。
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

from app.api.elders import ContractAPIRoute
from app.core.auth import require_roles
from app.core.redact import redact_text
from app.db import get_session
from app.models import AuditLog

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


@router.get(
    "/audit-logs/export",
    summary="审计日志 CSV 导出（admin-only；按时间区间与操作人筛选；出口脱敏）",
    response_class=Response,
)
async def export_audit_logs(
    admin: Annotated[object, Depends(require_roles("admin"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    start: Annotated[
        datetime.datetime | None,
        Query(description="起始时间（含），ISO 8601；缺省不限"),
    ] = None,
    end: Annotated[
        datetime.datetime | None,
        Query(description="结束时间（含），ISO 8601；缺省不限"),
    ] = None,
    actor_id: Annotated[
        uuid.UUID | None,
        Query(description="按操作人主体 ID 筛选（staff/guardian/device 的 ID）"),
    ] = None,
) -> Response:
    """导出审计日志为 CSV（UTF-8 with BOM，Excel 友好）。

    where 条件族与看板查询一致：`created_at ∈ [start, end]` ＋ `actor_id` 相等；
    `created_at` 升序（取证阅读顺序，与写入顺序一致）。
    """
    _ = admin  # require_roles("admin") 已完成权限判定；无业务用途
    stmt = select(AuditLog).order_by(AuditLog.created_at.asc())
    if start is not None:
        stmt = stmt.where(AuditLog.created_at >= start)
    if end is not None:
        stmt = stmt.where(AuditLog.created_at <= end)
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)

    rows = (await session.execute(stmt)).scalars().all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_HEADERS)
    for row in rows:
        # 出口脱敏：detail JSON 文本过 redact_text（手机号/身份证/房间/密钥掩码化）
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
