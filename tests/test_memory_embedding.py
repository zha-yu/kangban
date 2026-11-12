"""Day 9 B2 · embedding 客户端 + cosine 召回隔离（计划 §2.3 / 契约 X5）。

覆盖判据：
- `memory_chunks.embedding` 新写入行**非空且恰好 1536 维**（写入接线）；
- 维度不一致 → `EmbeddingDimensionError`（**不截断/不补零**）；
- 批量 ≤16；5xx/超时重试 ≤1 次，仍失败 → `EmbeddingUnavailable`；
- 跨长者向量检索**零泄漏**（闸门②硬在 SQL 里，AC-39②）；
- 召回失败 → 本轮不加载记忆 + `memory_recall_degraded` 降级标记（AGENTS §6）。

embedding 一律 `httpx.MockTransport` / monkeypatch 桩，**不连外网**；
DB 用例连测试库（PG 不可达自动 skip）。
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.memory import MemoryChunk
from app.models.organization import Elder
from app.services import memory_service as memory_service_module
from app.services.memory_service import (
    EmbeddingDimensionError,
    EmbeddingUnavailable,
    MemoryCandidate,
    insert_candidate,
    recall_active_chunks,
)

_DIM = 1536


# ------------------------------------------------------------------ 夹具与桩


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    try:
        conn = await db_engine.connect()
    except Exception as exc:  # pragma: no cover - 本地无 PG 时跳过
        pytest.skip(f"PostgreSQL 不可达，跳过记忆向量 DB 用例：{exc!r}")

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


async def _new_elder(session: AsyncSession, name: str) -> uuid.UUID:
    elder = Elder(name=name, status="active")
    session.add(elder)
    await session.flush()
    return elder.elder_id


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "embedding_base_url": "https://embedding.test/v1",
        "embedding_api_key": "test-key",
        "embedding_model": "test-embedding-1536",
        "embedding_dim": _DIM,
        "embedding_batch_size": 16,
        "embedding_timeout_seconds": 1,
    }
    base.update(overrides)
    return Settings(**base)


def _unit_vector(axis: int = 0) -> list[float]:
    vector = [0.0] * _DIM
    vector[axis] = 1.0
    return vector


def _mock_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)


def _ok_body(count: int, *, dim: int = _DIM) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": [0.01] * dim} for i in range(count)
        ],
        "model": "test-embedding-1536",
    }


async def _seed_chunk(
    session: AsyncSession,
    elder: uuid.UUID,
    *,
    content: str,
    axis: int | None,
    status: str = "active",
) -> uuid.UUID:
    chunk = MemoryChunk(
        elder_id=elder,
        subject=str(elder),
        predicate="preferred_opera_genre",
        object_value=content[:4],
        polarity="positive",
        content=content,
        confidence=0.8,
        sensitivity="private",
        source_type="elder_explicit",
        source_turn_ids=["tr-seed"],
        status=status,
        valid_from=datetime.datetime.now(datetime.UTC),
    )
    session.add(chunk)
    await session.flush()
    if axis is not None:
        await session.execute(
            text("UPDATE memory_chunks SET embedding = CAST(:vec AS vector) WHERE chunk_id = :cid"),
            {
                "vec": "[" + ",".join(str(v) for v in _unit_vector(axis)) + "]",
                "cid": chunk.chunk_id,
            },
        )
        await session.flush()
    return chunk.chunk_id


# ------------------------------------------------------------------ _embed_texts


class TestEmbedTexts:
    async def test_returns_vectors_for_all_texts(self) -> None:
        calls: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content))
            assert request.headers["authorization"] == "Bearer test-key"
            return httpx.Response(200, json=_ok_body(len(json.loads(request.content)["input"])))

        async with _mock_client(handler) as client:
            vectors = await memory_service_module._embed_texts(
                ["长者喜欢越剧", "长者喜欢京剧"], client=client, settings=_settings()
            )

        assert len(vectors) == 2
        assert all(len(v) == _DIM for v in vectors)
        assert len(calls) == 1  # 两条一批（≤16）
        assert calls[0]["model"] == "test-embedding-1536"
        assert calls[0]["input"] == ["长者喜欢越剧", "长者喜欢京剧"]

    async def test_dimension_mismatch_raises_without_truncation(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_body(1, dim=1024))

        async with _mock_client(handler) as client:
            with pytest.raises(EmbeddingDimensionError) as excinfo:
                await memory_service_module._embed_texts(
                    ["长者喜欢越剧"], client=client, settings=_settings()
                )
        assert excinfo.value.actual == 1024
        assert excinfo.value.expected == _DIM

    async def test_batch_size_never_exceeds_16(self) -> None:
        sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            sizes.append(len(payload["input"]))
            return httpx.Response(200, json=_ok_body(len(payload["input"])))

        texts = [f"记忆{i}" for i in range(20)]
        async with _mock_client(handler) as client:
            vectors = await memory_service_module._embed_texts(
                texts, client=client, settings=_settings(embedding_batch_size=64)
            )

        assert len(vectors) == 20
        assert sizes == [16, 4]  # 配置放大到 64 也夹到 16

    async def test_5xx_retried_once_then_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(memory_service_module, "_EMBEDDING_RETRY_BACKOFF_SECONDS", 0)
        statuses = [503, 200]

        def handler(request: httpx.Request) -> httpx.Response:
            status = statuses.pop(0)
            if status != 200:
                return httpx.Response(status, json={"error": "upstream"})
            return httpx.Response(200, json=_ok_body(1))

        async with _mock_client(handler) as client:
            vectors = await memory_service_module._embed_texts(
                ["越剧"], client=client, settings=_settings()
            )

        assert len(vectors) == 1
        assert statuses == []  # 两次请求都用掉了

    async def test_5xx_twice_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(memory_service_module, "_EMBEDDING_RETRY_BACKOFF_SECONDS", 0)
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(503, json={"error": "upstream"})

        async with _mock_client(handler) as client:
            with pytest.raises(EmbeddingUnavailable):
                await memory_service_module._embed_texts(
                    ["越剧"], client=client, settings=_settings()
                )
        assert len(attempts) == 2  # 首次 + 重试 1 次，不再多试

    async def test_timeout_retried_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(memory_service_module, "_EMBEDDING_RETRY_BACKOFF_SECONDS", 0)
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            raise httpx.TimeoutException("timeout", request=request)

        async with _mock_client(handler) as client:
            with pytest.raises(EmbeddingUnavailable):
                await memory_service_module._embed_texts(
                    ["越剧"], client=client, settings=_settings()
                )
        assert len(attempts) == 2

    async def test_4xx_not_retried(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(401, json={"error": "bad key"})

        async with _mock_client(handler) as client:
            with pytest.raises(EmbeddingUnavailable):
                await memory_service_module._embed_texts(
                    ["越剧"], client=client, settings=_settings()
                )
        assert len(attempts) == 1

    async def test_not_configured_raises_without_request(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            attempts.append(1)
            return httpx.Response(200, json=_ok_body(1))

        async with _mock_client(handler) as client:
            with pytest.raises(EmbeddingUnavailable):
                await memory_service_module._embed_texts(
                    ["越剧"], client=client, settings=_settings(embedding_model="")
                )
        assert attempts == []

    async def test_empty_input_returns_empty(self) -> None:
        assert await memory_service_module._embed_texts([], settings=_settings()) == []


class TestVectorLiteral:
    def test_format_and_dimension(self) -> None:
        literal = memory_service_module._vector_literal([1.0, 0.5], dim=2)
        assert literal == "[1.0,0.5]"

    def test_wrong_length_rejected(self) -> None:
        with pytest.raises(EmbeddingDimensionError):
            memory_service_module._vector_literal([1.0], dim=_DIM)


# ------------------------------------------------------------------ 写入接线


class TestInsertWritesEmbedding:
    async def test_new_row_has_1536_dim_embedding(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elder = await _new_elder(session, "周慧兰")

        async def fake_embed(texts: Any, **_kwargs: Any) -> list[list[float]]:
            return [_unit_vector(0) for _ in texts]

        monkeypatch.setattr(memory_service_module, "_embed_texts", fake_embed)

        result = await insert_candidate(
            MemoryCandidate(
                elder_id=str(elder),
                category="preference",
                subject=str(elder),
                predicate="preferred_opera_genre",
                object_value="越剧",
                fact="长者喜欢越剧",
                source_turn_ids=["tr-1"],
                confidence=0.8,
            ),
            session=session,
        )

        row = (
            await session.execute(
                text(
                    "SELECT vector_dims(embedding) AS dims, embedding IS NOT NULL AS present "
                    "FROM memory_chunks WHERE chunk_id = :cid"
                ),
                {"cid": uuid.UUID(result["chunk_id"])},
            )
        ).one()
        assert row.present is True
        assert row.dims == _DIM

    async def test_embedding_failure_still_inserts_text_row(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """embedding 不可用 → 行照常落库（向量可后补），不阻塞记忆写入。"""
        elder = await _new_elder(session, "吴德海")

        async def broken(_texts: Any, **_kwargs: Any) -> list[list[float]]:
            raise EmbeddingUnavailable("upstream down")

        monkeypatch.setattr(memory_service_module, "_embed_texts", broken)

        result = await insert_candidate(
            MemoryCandidate(
                elder_id=str(elder),
                category="preference",
                subject=str(elder),
                predicate="preferred_opera_genre",
                object_value="越剧",
                fact="长者喜欢越剧",
                source_turn_ids=["tr-1"],
                confidence=0.8,
            ),
            session=session,
        )

        row = await session.get(MemoryChunk, uuid.UUID(result["chunk_id"]))
        assert row is not None
        assert row.embedding is None


# ------------------------------------------------------------------ 召回：cosine + 隔离


class TestRecallCosineIsolation:
    async def test_cosine_orders_nearest_first(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elder = await _new_elder(session, "郑月琴")
        await _seed_chunk(session, elder, content="长者喜欢越剧", axis=0)
        await _seed_chunk(session, elder, content="长者喜欢京剧", axis=1)

        async def query_embed(texts: Any, **_kwargs: Any) -> list[list[float]]:
            return [_unit_vector(0) for _ in texts]  # 与「越剧」同向

        monkeypatch.setattr(memory_service_module, "_embed_texts", query_embed)
        rows = await recall_active_chunks(
            elder_id=str(elder), is_generic=False, query="越剧", session=session
        )
        assert [r.content for r in rows] == ["长者喜欢越剧", "长者喜欢京剧"]

    async def test_cross_elder_zero_leakage(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """闸门②：B 的记忆不出现在 A 的召回（AC-39②）。"""
        elder_a = await _new_elder(session, "长者甲")
        elder_b = await _new_elder(session, "长者乙")
        await _seed_chunk(session, elder_a, content="长者甲喜欢越剧", axis=0)
        await _seed_chunk(session, elder_b, content="长者乙喜欢越剧", axis=0)

        async def query_embed(texts: Any, **_kwargs: Any) -> list[list[float]]:
            return [_unit_vector(0) for _ in texts]

        monkeypatch.setattr(memory_service_module, "_embed_texts", query_embed)
        rows = await recall_active_chunks(
            elder_id=str(elder_a), is_generic=False, query="越剧", session=session
        )
        assert [r.content for r in rows] == ["长者甲喜欢越剧"]
        assert all(r.elder_id == elder_a for r in rows)

    async def test_top5_limit(self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
        elder = await _new_elder(session, "冯国栋")
        for index in range(7):
            await _seed_chunk(session, elder, content=f"记忆{index}", axis=index)

        async def query_embed(texts: Any, **_kwargs: Any) -> list[list[float]]:
            return [_unit_vector(0) for _ in texts]

        monkeypatch.setattr(memory_service_module, "_embed_texts", query_embed)
        rows = await recall_active_chunks(
            elder_id=str(elder), is_generic=False, query="越剧", session=session
        )
        assert len(rows) == 5  # TD line 933：top5

    async def test_null_embedding_rows_sorted_last(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elder = await _new_elder(session, "许凤英")
        await _seed_chunk(session, elder, content="无向量记忆", axis=None)
        await _seed_chunk(session, elder, content="有向量记忆", axis=0)

        async def query_embed(texts: Any, **_kwargs: Any) -> list[list[float]]:
            return [_unit_vector(0) for _ in texts]

        monkeypatch.setattr(memory_service_module, "_embed_texts", query_embed)
        rows = await recall_active_chunks(
            elder_id=str(elder), is_generic=False, query="越剧", session=session
        )
        assert [r.content for r in rows] == ["有向量记忆", "无向量记忆"]  # NULLS LAST

    async def test_deleted_chunk_not_recalled(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elder = await _new_elder(session, "崔志强")
        await _seed_chunk(session, elder, content="长者喜欢越剧", axis=0, status="deleted")

        async def query_embed(texts: Any, **_kwargs: Any) -> list[list[float]]:
            return [_unit_vector(0) for _ in texts]

        monkeypatch.setattr(memory_service_module, "_embed_texts", query_embed)
        rows = await recall_active_chunks(
            elder_id=str(elder), is_generic=False, query="越剧", session=session
        )
        assert rows == []


# ------------------------------------------------------------------ 召回降级（不臆造）


class TestRecallDegradation:
    async def test_missing_query_returns_empty_with_degraded_marker(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        elder = uuid.uuid4()
        with caplog.at_level(logging.WARNING, logger="app.services.memory_service"):
            rows = await recall_active_chunks(elder_id=str(elder), is_generic=False, query="  ")
        assert rows == []
        assert any(getattr(record, "reason", None) == "no_query" for record in caplog.records)

    async def test_embedding_unavailable_returns_empty_with_marker(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def broken(_texts: Any, **_kwargs: Any) -> list[list[float]]:
            raise EmbeddingUnavailable("upstream 503")

        monkeypatch.setattr(memory_service_module, "_embed_texts", broken)
        with caplog.at_level(logging.WARNING, logger="app.services.memory_service"):
            rows = await recall_active_chunks(
                elder_id=str(uuid.uuid4()), is_generic=False, query="越剧"
            )
        assert rows == []
        assert any(
            getattr(record, "reason", None) == "embedding_unavailable" for record in caplog.records
        )

    async def test_generic_identity_never_queries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def boom(_texts: Any, **_kwargs: Any) -> list[list[float]]:
            raise AssertionError("generic 身份不得触发 embedding 调用")

        monkeypatch.setattr(memory_service_module, "_embed_texts", boom)
        assert await recall_active_chunks(elder_id=None, is_generic=True, query="越剧") == []
        assert await recall_active_chunks(elder_id="e1", is_generic=True, query="越剧") == []


__all__: list[str] = []
