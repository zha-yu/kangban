"""FR-ALT-05 运营统计端点（Day 9 · C4，PRD line 380）。

输出三项：本周红色事件数 / 平均确认时长（分钟）/ 各楼栋漏采次数。
聚合逻辑在 `stats_service.read_weekly_stats`（只读），本文件只做鉴权 + 序列化。

- **admin-only**（FR-ALT-05 原文「护工长打开统计页」；对齐 F13/F01/F26 的 admin 前例。
  若联调期裁定「护工长」为独立角色，仅需改 require_roles 一行，聚合口径不变）；
- 「本周」按 Asia/Shanghai 自然周（周一 00:00）切分，落库 UTC（AGENTS §8）。
"""

from __future__ import annotations

import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.core.auth import require_roles
from app.core.runtime_context import Identity
from app.db import get_session
from app.services.stats_service import read_weekly_stats

router = APIRouter(
    prefix="/api/stats",
    tags=["stats"],
    route_class=ContractAPIRoute,
)


@router.get("/weekly", response_model=dict)
async def get_weekly_stats(
    session: Annotated[AsyncSession, Depends(get_session)],
    _staff: Annotated[Identity, Depends(require_roles("admin"))],  # FR-ALT-05：护工长口径
) -> dict[str, object]:
    """本周运营统计（FR-ALT-05）：红档事件数 / 平均确认时长 / 各楼栋漏采次数。"""
    return await read_weekly_stats(session, now=datetime.datetime.now(datetime.UTC))
