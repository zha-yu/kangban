"""共享测试夹具（task-3 建立；阶段 B 统一引擎、阶段 C 独立测试库 + 事务回滚）。

数据库用例的隔离策略（阶段 C 根治）：
- **独立测试库**：所有 DB 用例走 `DATABASE_URL_TEST` 指向的库（未配置时从
  `database_url` 推导 `<库名>_test`，**绝不回退到开发库本身**）；
- **session 引导一次**：advisory lock 串行化（xdist 多 worker 安全）后
  建库（如缺）+ `alembic upgrade head`；
- **全程事务回滚**：每用例一事务、结束回滚（service 层真实 commit 只释放
  savepoint），无 TRUNCATE、主库零残留——并行化后共享库也不会互相踩数据。

引擎治理（阶段 B，P-03 根治）：
- pyproject 把 `asyncio_default_test_loop_scope` / `asyncio_default_fixture_loop_scope`
  钉为 `module`——同一模块的用例与夹具共享一条事件循环，循环不再每用例关闭；
- conftest 统一提供 module 级共享引擎（默认连接池）：在模块循环上创建、循环关闭前
  dispose，asyncpg 连接**绝不跨事件循环存活**，连接池在同一模块内复用。

无库时 skip 并打印明确原因，**不许静默通过**（否则覆盖率虚高，task-3 卡明文要求）。
用法：需要数据库的用例声明 `db_engine` / `db_conn` 参数即可；`/health` 等不依赖库的用例不受影响。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
from sqlalchemy import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.config import Settings, get_settings

# psycopg 3 的**异步**连接不支持 Windows 默认的 ProactorEventLoop（会在 connect 时直接抛
# InterfaceError）。LangGraph 的 checkpointer 走 psycopg 异步池，所以 Windows 本地跑
# 用药子图用例必须换 Selector；生产在 Linux 容器里，不受影响。
# 放在模块顶部是因为策略必须在**任何** event loop 创建之前生效（pytest-asyncio 按需建循环）。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_REPO_ROOT = Path(__file__).resolve().parent.parent
# pg advisory lock 固定键：xdist 多 worker 各自跑 session 引导，用它串行化「建库 + 迁移」
_PG_ADVISORY_LOCK_KEY = 913_744


def _test_database_url(settings: Settings) -> str:
    """测试专用库 URL：优先 DATABASE_URL_TEST；未配置时从 database_url 推导 `<库名>_test`。

    红线：**绝不回退到开发库本身**——并行化之后测试写进开发库就是数据事故。
    """
    if settings.database_url_test:
        return settings.database_url_test
    url = make_url(settings.database_url)
    if url.database and not url.database.endswith("_test"):
        url = url.set(database=f"{url.database}_test")
    return url.render_as_string(hide_password=False)


def _sync_dsn(url: URL) -> str:
    """asyncpg URL → psycopg 同步 DSN（建库走同步驱动）。

    注意：psycopg 3 的 conninfo 只认 `postgresql://`，不认 `+psycopg` 驱动后缀；
    且必须从 URL 对象 render（hide_password=False），经 str(URL) 转一手密码会被遮成 ***。
    """
    return url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )


@pytest.fixture(scope="session")
def _test_db_bootstrap() -> bool:
    """session 级引导（每个 xdist worker 各跑一次，advisory lock 串行化）：
    建库（如缺）→ `alembic upgrade head`。

    返回 False 表示 PostgreSQL 不可达（`db_engine` 据此 skip 数据库用例）；
    迁移失败则直接报错（配置问题必须响亮地失败，不许静默）。
    """
    settings = get_settings()
    test_url = make_url(_test_database_url(settings))
    try:
        with psycopg.connect(
            _sync_dsn(test_url.set(database="postgres")), autocommit=True, connect_timeout=5
        ) as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (_PG_ADVISORY_LOCK_KEY,))
            try:
                exists = conn.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (test_url.database,)
                ).fetchone()
                if not exists:
                    conn.execute(f'CREATE DATABASE "{test_url.database}"')
                # alembic 在子进程里跑：Settings 的优先级是「进程环境变量 > .env」，
                # 传 DATABASE_URL=<测试库> 即可让 env.py 原样工作，不用改 alembic 本身
                subprocess.run(
                    [sys.executable, "-m", "alembic", "upgrade", "head"],
                    check=True,
                    cwd=_REPO_ROOT,
                    env={**os.environ, "DATABASE_URL": _test_database_url(settings)},
                )
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_PG_ADVISORY_LOCK_KEY,))
    except psycopg.OperationalError:
        return False
    return True


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def db_engine(_test_db_bootstrap: bool) -> AsyncIterator[AsyncEngine]:
    """module 级共享引擎（默认 QueuePool，池在模块内复用），指向独立测试库。

    阶段 C：不再 TRUNCATE——session 引导已把 schema 迁到 head，用例间靠事务
    回滚隔离，独立测试库零残留。引擎生命周期与模块事件循环严格同步（P-03 根治）。
    """
    if not _test_db_bootstrap:
        pytest.skip(
            f"PostgreSQL 不可达（{_test_database_url(get_settings())}），跳过数据库用例"
        )
    engine = create_async_engine(_test_database_url(get_settings()), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def db_conn(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """每测试一个事务，yield 后回滚（契约与 task-3 原 db_conn 一致）。"""
    try:
        conn = await db_engine.connect()
    except Exception as exc:  # 任何连接失败（超时/拒绝/口令错）都算「无库」
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")

    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()
