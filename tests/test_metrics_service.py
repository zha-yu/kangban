"""metrics_service 用例（A-1 · 裁决 B6/D2）。

隔离策略：conftest 共享引擎（阶段 B 夹具治理，P-03 根治）+ 每用例一事务、
结束回滚、主库零残留；无库时 skip 不静默通过。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
)

from app.models.metrics import Metric
from app.services.metrics_service import STAGES, MetricsRecorder

TRACE = "trace-metrics-test-0001"


@pytest.fixture
async def np_conn(db_conn: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    """conftest 共享引擎上的每用例一事务（原自建 NullPool 引擎已统一，P-03 根治）。"""
    yield db_conn


def _db_session(conn: AsyncConnection) -> AsyncSession:
    """把夹具那条未提交的事务借给被测代码；残留由夹具回滚（同 test_voiceprint_service）。"""
    return AsyncSession(bind=conn, expire_on_commit=False)


class _TxSessionFactory:
    """假 session_factory：每次 `async with` 都把 flush 绑到夹具的测试事务连接上。"""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    def __call__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return _db_session(self._conn)

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        return False


class _BoomFactory:
    """模拟 DB 故障：__aenter__ 即抛错，验证 flush 失败降级不抛出。"""

    def __call__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        return False


async def _count(db_conn: AsyncConnection, trace_id: str = TRACE) -> int:
    async with _db_session(db_conn) as session:
        stmt = select(func.count()).select_from(Metric).where(Metric.trace_id == trace_id)
        return int((await session.execute(stmt)).scalar_one())


@pytest.fixture
async def recorder(np_conn):  # type: ignore[no-untyped-def]
    rec = MetricsRecorder(_TxSessionFactory(np_conn), flush_interval_seconds=60)
    yield rec
    await rec.stop()


async def test_stage_whitelist_matches_model_check() -> None:
    """七值白名单与 models/metrics.py CHECK 约束一字不差。"""
    assert (
        frozenset(
            {
                "wakeup",
                "connection",
                "asr",
                "route",
                "llm_first_token",
                "tts_first_packet",
                "playback",
            }
        )
        == STAGES
    )


async def test_valid_rows_persist_with_fields(np_conn, recorder) -> None:  # type: ignore[no-untyped-def]
    """合法行批量落库：七 stage 可入、elder_id 可空可回查、duration 取整、source 透传。"""
    elder = uuid.uuid4()
    assert recorder.record_stage(TRACE, elder, "wakeup", 812.6, "client") is True
    assert recorder.record_stage(TRACE, None, "connection", 140, "backend") is True
    assert recorder.record_stage(TRACE, elder, "asr", 990, None) is True
    assert recorder.record_stage(TRACE, None, "llm_first_token", 733, "non_stream") is True
    assert recorder.record_stage(TRACE, None, "tts_first_packet", 402, "non_stream") is True
    assert recorder.record_stage(TRACE, elder, "route", 30, "rules") is True
    assert recorder.record_stage(TRACE, elder, "playback", 1500, "client") is True

    assert await recorder.drain_once(limit=10) == 7
    assert await _count(np_conn) == 7

    async with _db_session(np_conn) as session:
        rows = (
            (
                await session.execute(
                    select(Metric).where(Metric.trace_id == TRACE).order_by(Metric.created_at)
                )
            )
            .scalars()
            .all()
        )
    by_stage = {r.stage: r for r in rows}
    assert by_stage["wakeup"].elder_id == elder
    assert by_stage["connection"].elder_id is None
    assert by_stage["wakeup"].duration_ms == 813  # 四舍五入取整
    assert by_stage["llm_first_token"].source == "non_stream"


async def test_invalid_stage_dropped(np_conn, recorder) -> None:  # type: ignore[no-untyped-def]
    assert recorder.record_stage(TRACE, None, "not_a_stage", 100, None) is False
    assert await recorder.drain_once() == 0
    assert await _count(np_conn) == 0


async def test_missing_trace_id_dropped(recorder) -> None:
    """自裁 Z4：trace_id 缺失的行不入库（只留日志轨）。"""
    assert recorder.record_stage(None, None, "asr", 100, None) is False
    assert recorder.record_stage("", None, "asr", 100, None) is False


async def test_negative_duration_dropped(recorder) -> None:
    assert recorder.record_stage(TRACE, None, "asr", -5, None) is False


async def test_invalid_elder_id_set_null_not_dropped(np_conn, recorder) -> None:  # type: ignore[no-untyped-def]
    """elder_id 非法只置空不丢行：耗时数据仍有价值（ER §4.4.7 可空口径）。"""
    assert recorder.record_stage(TRACE, "not-a-uuid", "asr", 300, None) is True
    assert await recorder.drain_once() == 1
    async with _db_session(np_conn) as session:
        rows = (
            (await session.execute(select(Metric).where(Metric.trace_id == TRACE))).scalars().all()
        )
    assert len(rows) == 1 and rows[0].elder_id is None


async def test_flush_failure_degrades_not_raises(recorder) -> None:
    """落库失败：整批丢弃 + 不向调用方抛错（观测旁路纪律）。"""
    broken = MetricsRecorder(_BoomFactory())
    assert broken.record_stage(TRACE, None, "asr", 100, None) is True
    assert await broken.drain_once() == 0  # 失败不抛
    assert await broken.drain_once() == 0  # 行已丢弃，不重试堆积
    await broken.stop()


async def test_stop_drains_remaining(np_conn, recorder) -> None:  # type: ignore[no-untyped-def]
    """优雅停机：缓冲中未刷新的行在 stop() 时冲刷落库。"""
    assert recorder.record_stage(TRACE, None, "route", 25, "rules") is True
    await recorder.stop()
    assert await _count(np_conn) == 1
