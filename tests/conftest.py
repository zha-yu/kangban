"""共享测试夹具（task-3）。

数据库用例的隔离策略：每个测试独立事务、结束回滚，主库零残留
（不建独立 silvermate_test 库——教学规模下 rollback 隔离够用，决策见
docs/plans/task-03-库与模型基座.md）。

无库时 skip 并打印明确原因，**不许静默通过**（否则覆盖率虚高，task-3 卡明文要求）。
用法：需要数据库的用例声明 `db_conn` 参数即可；`/health` 等不依赖库的用例不受影响。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import engine


@pytest.fixture
async def db_conn() -> AsyncIterator[AsyncConnection]:
    """每测试一个事务，yield 后回滚；连不上库时 skip 并说明原因。"""
    try:
        conn = await engine.connect()
    except Exception as exc:  # 任何连接失败（超时/拒绝/口令错）都算「无库」
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")

    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()
