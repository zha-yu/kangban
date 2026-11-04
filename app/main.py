"""FastAPI 装配层（技术文档 §2.1：`app/main.py` 承担 FastAPI 装配、`/health`、trace_id 中间件）。

分层约束（AGENTS.md §2）：本文件只做装配，不写业务逻辑；路由在 `app/api/`，
领域逻辑与写库在 `app/services/`。

启动：`uvicorn app.main:app --reload --port 8000`
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import health
from app.core.config import Settings, get_settings
from app.core.context import TRACE_HEADER
from app.core.logging import configure_logging
from app.core.trace import TraceIdMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程级生命周期。Day 2 起在这里挂：DB 连接池、Redis、APScheduler。

    现在只记日志 —— 不建连接，保证没有 PG/Redis 也能启动（1.4 验收口径）。
    """
    logger = logging.getLogger("app.main")
    logger.info("application starting", extra={"app_env": app.state.settings.app_env})
    yield
    logger.info("application stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂：便于测试注入不同配置（`create_app(Settings(app_env="test"))`）。"""
    settings = settings or get_settings()
    configure_logging(settings.log_level, service=settings.otel_service_name)

    application = FastAPI(
        title=f"{settings.app_name} backend",
        version=__version__,
        description="康伴（SilverMate）3D 数字人陪伴系统后端",
        lifespan=lifespan,
    )
    application.state.settings = settings

    # 中间件：后添加者在外层，因此 CORS 包住 TraceId，异常响应也带链路头
    application.add_middleware(TraceIdMiddleware)
    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[TRACE_HEADER],
        )

    application.include_router(health.router)
    return application


app = create_app()
