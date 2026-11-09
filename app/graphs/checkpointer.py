"""LangGraph checkpointer：独立 schema + 独立连接池（N10 / 技术设计 §5.3）。

三条边界，写在这里是因为它们最容易在「顺手复用主库会话」时被破坏：

1. **连接池独立**：checkpoint 的读写频率远高于业务写，且 `setup()` 会建表。
   共用 `app.db` 的池会让「一次 checkpoint 抖动」直接变成业务请求超时。
2. **schema 独立**（默认 `langgraph`）：checkpoint 表由 LangGraph 自己管理，
   与 26/30 张业务表混在 `public` 里，`alembic` 的 autogenerate 会把它当成漂移反复报错。
3. **`setup()` 只在启动时跑**：它执行 `CREATE TABLE IF NOT EXISTS`。放在请求路径里，
   每次对话都多几条 DDL 往返；更要紧的是 DDL 会拿表锁，和正在跑的图状态更新互斥。

schema 通过连接串的 `options=-csearch_path=<schema>` 指定，而不是 `SET search_path`：
`SET` 是**会话级**的，池里换一条连接就丢了，checkpoint 表会静默落到 `public`。
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: schema 名只允许小写标识符：它会被拼进 DDL 与连接参数，不接受任何外部输入
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class CheckpointerConfigError(ValueError):
    """checkpoint 配置非法（schema 名不合法、连接串缺库名等）。启动期就该失败。"""


def _check_schema(schema: str) -> str:
    if not _SCHEMA_RE.match(schema):
        raise CheckpointerConfigError(
            f"invalid langgraph checkpoint schema {schema!r}; expected ^[a-z_][a-z0-9_]{{0,62}}$"
        )
    return schema


def dsn_with_schema(dsn: str, schema: str) -> str:
    """把 `search_path` 钉进连接串（会话级 `SET` 会在换连接时丢失）。"""
    _check_schema(schema)
    option = f"options=-csearch_path%3D{schema}"
    if "options=" in dsn:
        # 已有 options：不静默覆盖，让调用方自己决定（覆盖会造成"以为改了 schema"的假象）
        raise CheckpointerConfigError("dsn already carries an 'options' parameter")
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}{option}"


async def ensure_schema(dsn: str, schema: str) -> None:
    """确保 schema 存在（幂等）。

    用一条独立短连接做 DDL，不复用 checkpoint 池：池里的连接带着
    `search_path=<schema>`，而 schema 还没建时那正是会失败的时刻。
    """
    _check_schema(schema)
    async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=5) as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        await conn.commit()


@asynccontextmanager
async def medication_checkpointer(
    *,
    dsn: str | None = None,
    schema: str | None = None,
) -> AsyncIterator[AsyncPostgresSaver]:
    """打开一个可用的 checkpointer（用完关闭池）。

    `setup()` 在这里调用一次；测试与生产的调用点都是「进程/模块启动时进上下文」。
    """
    settings = get_settings()
    conninfo = dsn or settings.langgraph_checkpoint_database_url
    target_schema = schema or settings.langgraph_checkpoint_schema

    await ensure_schema(conninfo, target_schema)

    pool = AsyncConnectionPool(
        conninfo=dsn_with_schema(conninfo, target_schema),
        min_size=settings.langgraph_checkpoint_pool_min_size,
        max_size=settings.langgraph_checkpoint_pool_max_size,
        open=False,
        # checkpointer 自己管事务边界，连接必须是 autocommit
        kwargs={"autocommit": True},
    )
    await pool.open(wait=False)
    try:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        logger.info("langgraph_checkpointer_ready schema=%s", target_schema)
        yield saver
    finally:
        await pool.close()


__all__ = [
    "CheckpointerConfigError",
    "dsn_with_schema",
    "ensure_schema",
    "medication_checkpointer",
]
