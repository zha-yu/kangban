"""异步数据库访问层：唯一的 engine / 会话工厂 / FastAPI 依赖。

约定（task-3 卡）：
1. **导入不建连接** —— `create_async_engine` 只构造对象，首次 checkout 才真正连库，
   保证没有 PostgreSQL 时 `uvicorn app.main:app` 也能起来（/health 不依赖库）；
2. 池参数全部来自 `app/core/config.py` 的 `database_pool_*`，不散写数字；
3. `get_session` 是请求级依赖：成功提交、异常回滚；写库动作只发生在
   `app/services/**`（AGENTS §2 分层硬约束），本文件不写任何业务 SQL。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_settings = get_settings()

engine: AsyncEngine = create_async_engine(
    _settings.database_url,
    pool_size=_settings.database_pool_size,
    max_overflow=_settings.database_pool_max_overflow,
    pool_pre_ping=True,  # 取连接前探活，避开数据库重启后的死连接
)

session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个会话，请求成功统一提交，异常回滚。"""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
