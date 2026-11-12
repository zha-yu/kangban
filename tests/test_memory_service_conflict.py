"""B5 冲突检测段测试（Day 8 Step 3，计划 §Step 3）。

必过用例（计划点名）：
- `conflict_flips_preference_keeps_two_versions`：已存「喜欢越剧」→ 新萃取
  「不喜欢越剧了」→ LLM 裁 supersedes → 旧值 superseded、新值 active、
  两版本均保留（FR-MEM-03，当前生效值唯一）；
- `superseded_not_recalled`：superseded/disputed 条目不再被召回注入（FR-MEM-05）；
- `deleted_not_recalled`：删除记忆不再被召回注入（回归）。

补充用例：
- LLM 失败/降级 → R7 降级路径：旧条目保持 active、新条目 pending_review、
  双版本均保留、不阻塞写入；
- 相似度 ≤ 0.9 → 可共存偏好双 active 并存（Day 6 语义不变）；
- sensitivity=normal 在落库边界归一为 private（ORM CHECK 枚举只有
  private/health/family，Day 6/7 单测全程 monkeypatch 未暴露的契约错位）；
- `_judge_conflict_relation` 对 LLM 降级 / 非法 JSON / 合法裁决三种返回。

DB 用例连真实测试 PostgreSQL（savepoint 会话 + 种子长者，PG 不可达自动 skip）；
冲突裁决 LLM 一律 monkeypatch 桩，不打真实 API。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models.memory import MemoryChunk
from app.models.organization import Elder
from app.services import memory_service as memory_service_module
from app.services.memory_service import (
    MemoryCandidate,
    recall_active_chunks,
)

# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    try:
        conn = await db_engine.connect()
    except Exception as exc:  # pragma: no cover - 本地无 PG 时跳过
        pytest.skip(f"PostgreSQL 不可达，跳过记忆冲突检测 DB 用例：{exc!r}")

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
    elder = Elder(name="钱守仁", status="active")
    session.add(elder)
    await session.flush()
    yield str(elder.elder_id)


def _candidate(elder: str, value: str, fact: str) -> MemoryCandidate:
    return MemoryCandidate(
        elder_id=elder,
        category="preference",
        subject=elder,
        predicate="preferred_opera_genre",
        object_value=value,
        polarity="negative" if "不喜欢" in value else "positive",
        fact=fact,
        source_turn_ids=["tr-new"],
        confidence=0.8,
    )


async def _seed_active_chunk(
    session: AsyncSession, elder: str, *, value: str, content: str, status: str = "active"
) -> MemoryChunk:
    chunk = MemoryChunk(
        elder_id=uuid.UUID(elder),
        subject=elder,
        predicate="preferred_opera_genre",
        object_value=value,
        polarity="positive",
        content=content,
        confidence=0.8,
        sensitivity="private",
        source_type="elder_explicit",
        source_turn_ids=["tr-old"],
        status=status,
        valid_from=datetime.datetime.now(datetime.UTC),
    )
    session.add(chunk)
    await session.flush()
    return chunk


def _patch_judge(monkeypatch: pytest.MonkeyPatch, verdict: str | None) -> list[tuple[str, str]]:
    """桩掉语义矛盾判定：返回裁决值（None = LLM 失败降级），并记录调用入参。"""
    calls: list[tuple[str, str]] = []

    async def fake_judge(old_content: str, new_content: str) -> str | None:
        calls.append((old_content, new_content))
        return verdict

    monkeypatch.setattr(memory_service_module, "_judge_conflict_relation", fake_judge)
    return calls


def _patch_embedding(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """桩掉 embedding（Day 9 B2）：召回/写入都不真连外网，返回固定 1536 维向量。"""
    calls: list[list[str]] = []

    async def fake_embed(texts: Any, **_kwargs: Any) -> list[list[float]]:
        calls.append(list(texts))
        # 固定单位向量：种子行无向量（NULLS LAST），排序不影响本组断言
        return [[0.0] * 1535 + [1.0] for _ in texts]

    monkeypatch.setattr(memory_service_module, "_embed_texts", fake_embed)
    return calls


async def _recall(session: AsyncSession, elder: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """召回统一入口：带 query（cosine 需要向量）+ 已桩 embedding。"""
    _patch_embedding(monkeypatch)
    rows = await recall_active_chunks(
        elder_id=elder, is_generic=False, query="越剧", session=session
    )
    return [str(r.content) for r in rows]


# ------------------------------------------------------------------ 必过用例


async def test_conflict_flips_preference_keeps_two_versions(
    session: AsyncSession, elder_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已存「喜欢越剧」→ 新「不喜欢越剧了」：旧 superseded、新 active、两版本均在。"""
    old_chunk = await _seed_active_chunk(session, elder_id, value="越剧", content="长者喜欢越剧")
    _patch_judge(monkeypatch, "supersedes")

    result = await memory_service_module.insert_candidate(
        _candidate(elder_id, "不喜欢越剧了", "长者不喜欢越剧了"), session=session
    )

    rows = (
        (
            await session.execute(
                select(MemoryChunk).where(MemoryChunk.elder_id == uuid.UUID(elder_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2  # 两版本均保留，不物理覆盖
    by_status = {r.status: r for r in rows}
    new_row = next(r for r in rows if str(r.chunk_id) == result["chunk_id"])
    old_row = next(r for r in rows if r.chunk_id == old_chunk.chunk_id)
    # 旧值 superseded + 失效时间；新值 active + supersedes_id 指向旧条目
    assert old_row.status == "superseded"
    assert old_row.valid_to is not None
    assert new_row.status == "active"
    assert new_row.supersedes_id == old_chunk.chunk_id
    assert "superseded" in by_status and "active" in by_status  # 当前生效值唯一

    # 召回只注入新值（Day 9 B2：cosine 检索，embedding 已桩）
    notes = await _recall(session, elder_id, monkeypatch)
    assert notes == ["长者不喜欢越剧了"]


async def test_superseded_not_recalled(
    session: AsyncSession, elder_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """superseded / disputed 条目退出召回（FR-MEM-05）。"""
    await _seed_active_chunk(session, elder_id, value="越剧", content="长者喜欢越剧")
    await _seed_active_chunk(
        session, elder_id, value="京剧", content="长者喜欢京剧", status="superseded"
    )
    await _seed_active_chunk(
        session, elder_id, value="黄梅戏", content="长者喜欢黄梅戏", status="disputed"
    )
    notes = await _recall(session, elder_id, monkeypatch)
    assert notes == ["长者喜欢越剧"]


async def test_deleted_not_recalled(
    session: AsyncSession, elder_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除记忆不再被召回注入（回归，AC-14 语义：deleted=软删，召回过滤）。"""
    await _seed_active_chunk(session, elder_id, value="越剧", content="长者喜欢越剧")
    await _seed_active_chunk(
        session, elder_id, value="京剧", content="长者喜欢京剧", status="deleted"
    )
    notes = await _recall(session, elder_id, monkeypatch)
    assert notes == ["长者喜欢越剧"]


# ------------------------------------------------------------------ 补充用例


async def test_llm_failure_degrades_to_pending_review_keeps_both(
    session: AsyncSession, elder_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7 降级：LLM 失败不阻塞写入——旧条目保持 active，新条目 pending_review 人工复核。"""
    old_chunk = await _seed_active_chunk(session, elder_id, value="越剧", content="长者喜欢越剧")
    calls = _patch_judge(monkeypatch, None)  # None = LLM 失败/降级/解析失败

    result = await memory_service_module.insert_candidate(
        _candidate(elder_id, "不喜欢越剧了", "长者不喜欢越剧了"), session=session
    )

    assert len(calls) == 1  # 确实尝试过语义判定
    new_row = await session.get(MemoryChunk, uuid.UUID(result["chunk_id"]))
    old_row = await session.get(MemoryChunk, old_chunk.chunk_id)
    assert old_row.status == "active"  # 旧条目不动，仍是当前生效值
    assert new_row.status == "pending_review"
    assert new_row.conflicts_with_id == old_chunk.chunk_id  # 指向冲突对象，供人工复核

    notes = await _recall(session, elder_id, monkeypatch)
    assert notes == ["长者喜欢越剧"]  # pending_review 不进召回


async def test_low_similarity_coexists_two_active(
    session: AsyncSession, elder_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相似度 ≤ 0.9：可共存偏好（越剧 vs 京剧）双 active 并存，不触发语义判定。"""
    await _seed_active_chunk(session, elder_id, value="越剧", content="长者喜欢越剧")

    async def must_not_judge(old_content: str, new_content: str) -> str | None:
        raise AssertionError("similarity <= 0.9 must not trigger LLM judgment")

    monkeypatch.setattr(memory_service_module, "_judge_conflict_relation", must_not_judge)

    await memory_service_module.insert_candidate(
        _candidate(elder_id, "京剧", "长者爱听京剧"), session=session
    )

    rows = (
        (
            await session.execute(
                select(MemoryChunk).where(MemoryChunk.elder_id == uuid.UUID(elder_id))
            )
        )
        .scalars()
        .all()
    )
    assert {r.status for r in rows} == {"active"}  # 双 active 并存（Day 6 语义）
    assert {r.object_value for r in rows} == {"越剧", "京剧"}


async def test_sensitivity_normal_mapped_to_private(session: AsyncSession, elder_id: str) -> None:
    """sensitivity=normal 在落库边界归一为 private（ORM CHECK 只有 private/health/family）。"""
    candidate = _candidate(elder_id, "越剧", "长者喜欢越剧")
    assert candidate.sensitivity == "normal"  # 服务层默认值

    result = await memory_service_module.insert_candidate(candidate, session=session)

    row = await session.get(MemoryChunk, uuid.UUID(result["chunk_id"]))
    assert row is not None  # 落库成功（未撞 CHECK）
    assert row.sensitivity == "private"
    assert row.category == "private"  # category=可见性字段（TD §3.4），不透传域类别 preference


# ------------------------------------------------------------------ _judge_conflict_relation 单测


class _StubLLM:
    def __init__(self, *, degraded: bool = False, text: str = "") -> None:
        self._degraded = degraded
        self._text = text
        self.prompts: list[list[dict[str, str]]] = []

    async def chat(self, messages: Any) -> Any:
        self.prompts.append(list(messages))
        return _llm_reply(text=self._text, degraded=self._degraded)


def _llm_reply(*, text: str, degraded: bool) -> Any:
    """构造与 LLMService.LLMReply 同构的轻量对象（避免 import 真适配器依赖配置）。"""
    from types import SimpleNamespace

    return SimpleNamespace(text=text, degraded=degraded)


class TestJudgeConflictRelation:
    async def test_supersedes_verdict_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubLLM(text='{"relation": "supersedes"}')
        monkeypatch.setattr(memory_service_module, "_get_llm", lambda: stub)
        verdict = await memory_service_module._judge_conflict_relation(
            "长者喜欢越剧", "长者不喜欢越剧了"
        )
        assert verdict == "supersedes"
        assert len(stub.prompts) == 1

    async def test_degraded_reply_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubLLM(degraded=True)
        monkeypatch.setattr(memory_service_module, "_get_llm", lambda: stub)
        assert await memory_service_module._judge_conflict_relation("a", "b") is None

    async def test_invalid_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubLLM(text="抱歉，我无法判断")
        monkeypatch.setattr(memory_service_module, "_get_llm", lambda: stub)
        assert await memory_service_module._judge_conflict_relation("a", "b") is None

    async def test_unknown_relation_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubLLM(text='{"relation": "merge"}')
        monkeypatch.setattr(memory_service_module, "_get_llm", lambda: stub)
        assert await memory_service_module._judge_conflict_relation("a", "b") is None


# ------------------------------------------------------------------ 相似度占位实现


class TestTextSimilarity:
    def test_negation_flip_scores_high(self) -> None:
        """同对象正反陈述应被判定为高相似（触发语义矛盾判定，归一化剥否定词）。"""
        assert memory_service_module._text_similarity("长者喜欢越剧", "长者不喜欢越剧了") > 0.9

    def test_different_objects_score_below_threshold(self) -> None:
        assert (
            memory_service_module._text_similarity("长者喜欢越剧", "长者喜欢京剧")
            <= memory_service_module._CONFLICT_SIMILARITY_THRESHOLD
        )

    def test_empty_strings_score_zero(self) -> None:
        assert memory_service_module._text_similarity("", "越剧") == 0.0


__all__ = []
