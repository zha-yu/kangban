"""F26 运营指标端点（Day 9 · C4 · TD §8.3 Day 10 技术验收行「/metrics 输出分层命中率、
S1 闭环率与 token 消耗」）。

- **admin-only**（对齐 F13 关闭事件 / F01 手动重扫的 admin 前例；运营观测不做楼栋范围过滤）；
- 聚合逻辑在 `metrics_service.read_decision_metrics`，本文件只做鉴权 + 序列化；
- **token 消耗**：`llm_service` 已埋点回传 usage（dev-B，2026-11-12），读
  `metrics_service.snapshot_llm_usage()`（进程内计数，跨重启归零——持久化等
  `llm_tokens` stage 迁移）。无 usage 数据时 `available=false` 如实呈现，不编造数值。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute
from app.core.auth import require_roles
from app.core.metrics import snapshot_llm_cache
from app.core.runtime_context import Identity
from app.db import get_session
from app.services.metrics_service import read_decision_metrics, snapshot_llm_usage

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
    snap = snapshot_llm_usage()
    token_usage: dict[str, object] = {
        "available": snap["tokens_available"],
        "calls": snap["calls"],
        "prompt_tokens": snap["prompt_tokens"],
        "completion_tokens": snap["completion_tokens"],
        "total_tokens": snap["total_tokens"],
    }
    if not snap["tokens_available"]:
        token_usage["reason"] = "暂无带回 usage 的 LLM 调用（进程内计数，重启归零）"

    # FR-TC-08（B2 · D-1 裁决方案①）：prompt cache 命中率＝进程内计数，
    # 与 token_usage 同口径（重启归零、无数据如实标注 available=false）。
    cache_snap = snapshot_llm_cache()
    prompt_cache: dict[str, object] = {
        "available": cache_snap["cache_info_available"],
        "calls_with_cache_info": cache_snap["calls_with_cache_info"],
        "cache_hit_tokens": cache_snap["cache_hit_tokens"],
        "cache_miss_tokens": cache_snap["cache_miss_tokens"],
        "hit_rate": cache_snap["hit_rate"],
    }
    if not cache_snap["cache_info_available"]:
        prompt_cache["reason"] = "暂无带回 cache 字段的 LLM 调用（进程内计数，重启归零）"

    return {"routing": routing, "token_usage": token_usage, "prompt_cache": prompt_cache}
