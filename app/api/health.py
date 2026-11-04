"""健康检查。刻意**不探数据库 / Redis**。

理由：本课（大纲 1.4）的验收是「没有 `.env` 也能 uvicorn 起来并 `curl /health` 返回 200」，
探依赖会让这条验收依赖 PG/Redis 容器存活。真正的依赖探活等 Day 2 有了连接池再做，
且应拆成 `/health/ready`（依赖就绪）与 `/health/live`（进程存活）两个端点。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import __version__
from app.core.config import get_settings
from app.core.context import get_trace_id

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"], description="进程存活即 ok，不代表依赖就绪")
    service: str = Field(examples=["konban"])
    version: str = Field(examples=["0.1.0"])
    app_env: str = Field(examples=["local"])
    trace_id: str = Field(description="本次请求的链路 ID，与响应头 X-Trace-Id 相同")
    server_time: str = Field(description="服务端 UTC 时间，ISO 8601")


@router.get("/health", response_model=HealthResponse, summary="健康检查（不探依赖）")
async def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        app_env=settings.app_env,
        trace_id=get_trace_id() or "-",
        server_time=datetime.now(UTC).isoformat(timespec="seconds"),
    )
