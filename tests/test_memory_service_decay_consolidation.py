"""B5 衰减归并测试（Day 8 Step 7，计划 §Step 7，FR-MEM-07）。

必过用例（计划点名）：
- `test_consolidation_outputs_merge_record_for_similar_pair`：两条高度相似记忆
  触发归并，输出归并记录（保留条生效 + superseded 条收口 + supersedes_id 链 +
  归并记录返回值）；
- 90 天阈值边界用例：满 90 天整（== cutoff）衰减、89 天不衰减。

补充用例：
- importance 衰减下限收敛（重复执行幂等，不再产生变更）；
- 归并低于阈值不触发（越剧 vs 京剧，相似度 ≤0.9 不合并）；
- 归并幂等（重复执行不再产生归并记录）；
- 非 active 条目（superseded/deleted）不参与衰减与归并；
- job 层驱动链：memory_jobs 两个 tick 只调 service 接口、异常吞掉不抛。

DB 用例连真实测试 PostgreSQL（savepoint 会话 + 种子长者，PG 不可达自动 skip）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models.memory import MemoryChunk
from app.models.organization import Elder
from app.scheduler.memory_jobs import MemorySchedulerRuntime
from app.services import memory_service as memory_service_module

_NOW = datetime.datetime(2026, 11, 11, 12, 0, 0, tzinfo=datetime.UTC)


# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    try:
        conn = await db_engine.connect()
    except Exception as exc:  # pragma: no cover - 本地无 PG 时跳过
        pytest.skip(f"PostgreSQL 不可达，跳过记忆衰减归并 DB 用例：{exc!r}")

    trans = await conn.begin()
    sf = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    async with sf() as sess:
        try:
            yield sess
        finally:
            await sess.rollback()
    await trans.rollback()


@pytest.fixture
async def elder_id(session: AsyncSession) -> AsyncIterator[str]:
    elder = Elder(name="孙久安", status="active")
    session.add(elder)
    await session.flush()
    yield str(elder.elder_id)


async def _seed_chunk(
    session: AsyncSession,
    elder: str,
    *,
    value: str,
    content: str,
    predicate: str = "preferred_opera_genre",
    status: str = "active",
    importance: float = 1.0,
    last_accessed: datetime.datetime | None = None,
    created_at: datetime.datetime | None = None,
) -> MemoryChunk:
    """种子条目：显式给定 last_accessed / created_at，衰减边界可精确构造。"""
    chunk = MemoryChunk(
        elder_id=uuid.UUID(elder),
        subject=elder,
        predicate=predicate,
        object_value=value,
        polarity="positive",
        content=content,
        confidence=0.8,
        sensitivity="private",
        source_type="elder_explicit",
        source_turn_ids=["tr-seed"],
        status=status,
        importance=importance,
        last_accessed=last_accessed,
        created_at=created_at,
        valid_from=_NOW - datetime.timedelta(days=365),
    )
    session.add(chunk)
    await session.flush()
    return chunk


async def _all_chunks(session: AsyncSession, elder: str) -> list[MemoryChunk]:
    rows = (
        (await session.execute(select(MemoryChunk).where(MemoryChunk.elder_id == uuid.UUID(elder))))
        .scalars()
        .all()
    )
    return sorted(rows, key=lambda r: str(r.chunk_id))


# ------------------------------------------------------------------ 90 天衰减


async def test_decay_90_day_boundary(session: AsyncSession, elder_id: str) -> None:
    """90 天阈值边界：满 90 天整（== cutoff）衰减减半；89 天 23h 不衰减。"""
    stale = await _seed_chunk(
        session,
        elder_id,
        value="越剧",
        content="长者喜欢越剧",
        last_accessed=_NOW - datetime.timedelta(days=90),
    )
    fresh = await _seed_chunk(
        session,
        elder_id,
        value="京剧",
        content="长者喜欢京剧",
        last_accessed=_NOW - datetime.timedelta(days=89, hours=23),
    )

    decayed = await memory_service_module.decay_stale(now=_NOW, session=session)

    stale_row = await session.get(MemoryChunk, stale.chunk_id)
    fresh_row = await session.get(MemoryChunk, fresh.chunk_id)
    assert [r["chunk_id"] for r in decayed] == [str(stale.chunk_id)]
    assert float(stale_row.importance) == 0.50  # 1.00 × 0.5
    assert float(fresh_row.importance) == 1.00  # 未满 90 天不动

    # 衰减不改状态、不删内容：召回闸门语义不变，仅排序权重降低
    assert stale_row.status == "active"
    assert stale_row.content == "长者喜欢越剧"


async def test_decay_floor_converges_idempotent(session: AsyncSession, elder_id: str) -> None:
    """importance 到达下限 0.10 后不再衰减：重复执行幂等收敛，无新变更。"""
    chunk = await _seed_chunk(
        session,
        elder_id,
        value="越剧",
        content="长者喜欢越剧",
        importance=0.10,
        last_accessed=_NOW - datetime.timedelta(days=200),
    )

    first = await memory_service_module.decay_stale(now=_NOW, session=session)
    second = await memory_service_module.decay_stale(now=_NOW, session=session)

    assert first == []  # 已在下限：不产生变更
    assert second == []
    row = await session.get(MemoryChunk, chunk.chunk_id)
    assert float(row.importance) == 0.10


async def test_decay_skips_non_active(session: AsyncSession, elder_id: str) -> None:
    """非 active 条目（superseded / deleted）不参与衰减（状态条件扫描）。"""
    superseded = await _seed_chunk(
        session,
        elder_id,
        value="越剧",
        content="长者喜欢越剧",
        status="superseded",
        importance=1.0,
        last_accessed=_NOW - datetime.timedelta(days=200),
    )
    deleted = await _seed_chunk(
        session,
        elder_id,
        value="京剧",
        content="长者喜欢京剧",
        status="deleted",
        importance=1.0,
        last_accessed=_NOW - datetime.timedelta(days=200),
    )

    decayed = await memory_service_module.decay_stale(now=_NOW, session=session)

    assert decayed == []
    assert float((await session.get(MemoryChunk, superseded.chunk_id)).importance) == 1.00
    assert float((await session.get(MemoryChunk, deleted.chunk_id)).importance) == 1.00


# ------------------------------------------------------------------ 每周归并


async def test_consolidation_outputs_merge_record_for_similar_pair(
    session: AsyncSession, elder_id: str
) -> None:
    """FR-MEM-07 验证口径：两条高度相似记忆触发归并，输出归并记录。

    「长者喜欢越剧」×2（重复萃取留下的双条目）→ 保留 created_at 新者，
    旧条 superseded + valid_to 收口；保留条 supersedes_id 指向被归并条；
    归并记录（kept/merged/similarity）随返回值输出；召回只剩一条生效。
    """
    old_dup = await _seed_chunk(
        session,
        elder_id,
        value="越剧",
        content="长者喜欢越剧",
        created_at=_NOW - datetime.timedelta(days=30),
    )
    new_dup = await _seed_chunk(
        session,
        elder_id,
        value="越剧",
        content="长者喜欢越剧",
        created_at=_NOW - datetime.timedelta(days=1),
    )

    records = await memory_service_module.consolidate_similar(now=_NOW, session=session)

    assert len(records) == 1
    record = records[0]
    assert record["kept_chunk_id"] == str(new_dup.chunk_id)  # 并列 importance 取新
    assert record["merged_chunk_id"] == str(old_dup.chunk_id)
    assert record["similarity"] > 0.9
    assert record["elder_id"] == elder_id
    assert record["predicate"] == "preferred_opera_genre"

    kept_row = await session.get(MemoryChunk, new_dup.chunk_id)
    merged_row = await session.get(MemoryChunk, old_dup.chunk_id)
    assert kept_row.status == "active"
    assert kept_row.supersedes_id == old_dup.chunk_id  # 归并记录落链
    assert merged_row.status == "superseded"
    assert merged_row.valid_to == _NOW
    # 两版本均保留，召回只剩保留条
    assert await _all_chunks(session, elder_id) is not None
    assert len(await _all_chunks(session, elder_id)) == 2
    notes = await memory_service_module.recall_active_chunks(
        elder_id=elder_id, is_generic=False, session=session
    )
    assert [str(r.content) for r in notes] == ["长者喜欢越剧"]


async def test_consolidation_idempotent(session: AsyncSession, elder_id: str) -> None:
    """归并后旧条目退出 active：重复执行不再产生归并记录（状态条件扫描幂等）。"""
    await _seed_chunk(
        session,
        elder_id,
        value="越剧",
        content="长者喜欢越剧",
        created_at=_NOW - datetime.timedelta(days=30),
    )
    await _seed_chunk(
        session,
        elder_id,
        value="越剧",
        content="长者喜欢越剧",
        created_at=_NOW - datetime.timedelta(days=1),
    )

    first = await memory_service_module.consolidate_similar(now=_NOW, session=session)
    second = await memory_service_module.consolidate_similar(now=_NOW, session=session)

    assert len(first) == 1
    assert second == []


async def test_consolidation_below_threshold_not_merged(
    session: AsyncSession, elder_id: str
) -> None:
    """相似度 ≤0.9（越剧 vs 京剧）：可共存偏好不归并，双 active 保持。"""
    first = await _seed_chunk(
        session, elder_id, value="越剧", content="长者喜欢越剧", created_at=_NOW
    )
    second = await _seed_chunk(
        session, elder_id, value="京剧", content="长者喜欢京剧", created_at=_NOW
    )

    records = await memory_service_module.consolidate_similar(now=_NOW, session=session)

    assert records == []
    rows = await _all_chunks(session, elder_id)
    assert {r.chunk_id for r in rows} == {first.chunk_id, second.chunk_id}
    assert {r.status for r in rows} == {"active"}


async def test_consolidation_keeper_prefers_higher_importance(
    session: AsyncSession, elder_id: str
) -> None:
    """importance 不同：保留高分条，低分条被归并（不按创建时间）。"""
    low = await _seed_chunk(
        session,
        elder_id,
        value="越剧",
        content="长者喜欢越剧",
        importance=0.30,
        created_at=_NOW - datetime.timedelta(days=1),
    )
    high = await _seed_chunk(
        session,
        elder_id,
        value="越剧",
        content="长者喜欢越剧",
        importance=0.80,
        created_at=_NOW - datetime.timedelta(days=30),
    )

    records = await memory_service_module.consolidate_similar(now=_NOW, session=session)

    assert len(records) == 1
    assert records[0]["kept_chunk_id"] == str(high.chunk_id)
    assert records[0]["merged_chunk_id"] == str(low.chunk_id)
    low_row = await session.get(MemoryChunk, low.chunk_id)
    assert low_row.status == "superseded"


# ------------------------------------------------------------------ job 层驱动链


async def test_memory_job_ticks_call_service_interfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memory_jobs 两个 tick 只调 memory_service 双接口（裁决④），零写库驱动链。"""
    decay_calls: list[int] = []
    consolidate_calls: list[int] = []

    async def fake_decay(**_: object) -> list[dict[str, object]]:
        decay_calls.append(1)
        return [{"chunk_id": "x"}]

    async def fake_consolidate(**_: object) -> list[dict[str, object]]:
        consolidate_calls.append(1)
        return [{"kept_chunk_id": "a", "merged_chunk_id": "b"}]

    monkeypatch.setattr(memory_service_module, "decay_stale", fake_decay)
    monkeypatch.setattr(memory_service_module, "consolidate_similar", fake_consolidate)

    runtime = MemorySchedulerRuntime()
    await runtime._tick_decay()
    await runtime._tick_consolidate()

    assert len(decay_calls) == 1
    assert len(consolidate_calls) == 1


async def test_memory_job_ticks_swallow_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """service 异常只记日志不抛：周维护尽力而为，不影响主链。"""

    async def boom(**_: object) -> list[dict[str, object]]:
        raise RuntimeError("db down")

    monkeypatch.setattr(memory_service_module, "decay_stale", boom)
    monkeypatch.setattr(memory_service_module, "consolidate_similar", boom)

    runtime = MemorySchedulerRuntime()
    await runtime._tick_decay()  # 不抛即通过
    await runtime._tick_consolidate()


__all__ = []
