"""F26 运营指标端点（Day 9 · C4 · TD §8.3 Day 10 技术验收行「/metrics 输出分层命中率、
S1 闭环率与 token 消耗」）。

- **admin-only**（对齐 F13 关闭事件 / F01 手动重扫的 admin 前例；运营观测不做楼栋范围过滤）；
- 聚合逻辑在 `metrics_service.read_decision_metrics`，本文件只做鉴权 + 序列化；
- **token 消耗**：当前无落库来源（`llm_service` 未回传 usage，B 侧适配器埋点后接入），
  以 ``available=false`` 如实呈现，**不编造数值**（AGENTS §8「不编造」口径）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.core.auth import require_roles
from app.core.runtime_context import Identity
from app.db import get_session
from app.services.metrics_service import read_decision_metrics

router = APIRouter(
    prefix="/api/metrics",
    tags=["metrics"],
    route_class=ContractAPIRoute,
)


@router.get("", response_model=dict)
async def get_operational_metrics(
    session: Annotated[AsyncSession, Depends(get_session)],
    _staff: Annotated[Identity, Depends(require_roles("admin"))],  # F26：admin-only
) -> dict[str, object]:
    """路由观测指标（TD:1182）：分层命中率 + S1 闭环率 + S2 升级率 + token 消耗状态。"""
    routing = await read_decision_metrics(session)
    return {
        "routing": routing,
        "token_usage": {
            "available": False,
            "reason": "llm_service 未回传 usage（B 侧适配器埋点后接入，届时改 available=true）",
        },
    }
