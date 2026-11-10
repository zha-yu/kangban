"""MemoryService 骨架测试（N22）。

任务卡 §3.3 验收点：
- 三条硬校验：空 source_turn_ids / sensitivity=health / generic 身份
- extract_preference_candidates 模板匹配骨架
- recall_active_chunks 闸门②（DB 集成场景需 PostgreSQL，这里 skip 标记）
- insert_candidate 的同键 diff 处理（值同跳过，值不同新插一条）
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.services import memory_service as memory_service_module
from app.services.memory_service import (
    MemoryCandidate,
    MemoryInsertSkipped,
    MemoryValidationError,
    _validate_candidate,
    extract_after_turn,
    extract_preference_candidates,
    recall_for_turn,
    schedule_memory_extraction,
)

# ------------------------------------------------------------------ _validate_candidate 三条硬校验


class TestValidateCandidate:
    def _good(self) -> MemoryCandidate:
        return MemoryCandidate(
            elder_id="e1",
            category="preference",
            subject="e1",
            predicate="preferred_opera_genre",
            object_value="越剧",
            source_turn_ids=["tr1"],
            confidence=0.8,
        )

    def test_good_candidate_passes(self) -> None:
        _validate_candidate(self._good())  # 不抛

    def test_empty_source_turn_ids_rejected(self) -> None:
        c = self._good()
        c.source_turn_ids = []
        with pytest.raises(MemoryValidationError, match="source_turn_ids must be non-empty"):
            _validate_candidate(c)

    def test_health_sensitivity_rejected(self) -> None:
        c = self._good()
        c.sensitivity = "health"
        with pytest.raises(MemoryValidationError, match="health"):
            _validate_candidate(c)

    def test_family_sensitivity_rejected(self) -> None:
        c = self._good()
        c.sensitivity = "family"
        with pytest.raises(MemoryValidationError, match="family"):
            _validate_candidate(c)

    def test_unknown_category_rejected(self) -> None:
        c = self._good()
        c.category = "fact"  # Day 6 只允许 preference
        with pytest.raises(MemoryValidationError, match="Day 6 only supports preference"):
            _validate_candidate(c)

    def test_confidence_out_of_range(self) -> None:
        c = self._good()
        c.confidence = 1.5
        with pytest.raises(MemoryValidationError):
            _validate_candidate(c)

    def test_empty_elder_id_rejected(self) -> None:
        c = self._good()
        c.elder_id = "   "
        with pytest.raises(MemoryValidationError):
            _validate_candidate(c)

    def test_normal_sensitivity_allowed(self) -> None:
        c = self._good()
        c.sensitivity = "normal"
        _validate_candidate(c)  # 不抛

    def test_private_sensitivity_allowed(self) -> None:
        c = self._good()
        c.sensitivity = "private"
        _validate_candidate(c)  # 不抛


# ------------------------------------------------------------------ extract_preference_candidates 模板匹配


class TestExtractPreferenceCandidates:
    @pytest.mark.asyncio
    async def test_generic_mode_returns_empty(self) -> None:
        turns = [{"user": "我喜欢越剧", "turn_id": "tr1"}]
        result = await extract_preference_candidates(
            elder_id=None,
            is_generic=True,
            turns=turns,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_no_matching_content_returns_empty(self) -> None:
        turns = [{"user": "今天天气怎么样", "turn_id": "tr1"}]
        result = await extract_preference_candidates(
            elder_id="e1",
            is_generic=False,
            turns=turns,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_one_match_extracted(self) -> None:
        turns = [{"user": "我喜欢越剧", "turn_id": "tr1"}]
        result = await extract_preference_candidates(
            elder_id="e1",
            is_generic=False,
            turns=turns,
        )
        assert len(result) == 1
        c = result[0]
        assert c.elder_id == "e1"
        assert c.category == "preference"
        assert c.predicate == "preferred_opera_genre"
        assert c.object_value == "越剧"
        assert c.polarity == "positive"
        assert c.source_turn_ids == ["tr1"]
        assert c.sensitivity == "normal"

    @pytest.mark.asyncio
    async def test_negative_polarity(self) -> None:
        turns = [{"user": "我讨厌红色", "turn_id": "tr2"}]
        result = await extract_preference_candidates(
            elder_id="e2",
            is_generic=False,
            turns=turns,
        )
        assert len(result) >= 1
        c = result[0]
        assert c.polarity == "negative"

    @pytest.mark.asyncio
    async def test_empty_turns_returns_empty(self) -> None:
        result = await extract_preference_candidates(
            elder_id="e1",
            is_generic=False,
            turns=[],
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_turns_with_empty_user_text_skipped(self) -> None:
        turns = [
            {"user": "", "turn_id": "tr1"},
            {"user": "  ", "turn_id": "tr2"},
            {"user": "我喜欢京剧", "turn_id": "tr3"},
        ]
        result = await extract_preference_candidates(
            elder_id="e3",
            is_generic=False,
            turns=turns,
        )
        assert len(result) == 1
        assert result[0].source_turn_ids == ["tr3"]

    @pytest.mark.asyncio
    async def test_candidate_validation_on_output(self) -> None:
        """抽取出来的候选必须通过硬校验（source_turn_ids / sensitivity / category）。"""
        turns = [{"user": "我喜欢越剧", "turn_id": "tr1"}]
        result = await extract_preference_candidates(
            elder_id="e1",
            is_generic=False,
            turns=turns,
        )
        for c in result:
            _validate_candidate(c)  # 不抛 → 通过


# ------------------------------------------------------------------ N22 样例：完整跑一次展示


class TestMemoryServiceExample:
    @pytest.mark.asyncio
    async def test_full_extract_example(self) -> None:
        turns = [
            {"user": "今天天气不错", "turn_id": "tr1"},
            {"user": "嗯，我喜欢越剧，不爱看京剧", "turn_id": "tr2"},
            {"user": "我还讨厌蓝色，喜欢红色", "turn_id": "tr3"},
        ]
        result = await extract_preference_candidates(
            elder_id="e-demo-12345",
            is_generic=False,
            turns=turns,
        )

        print("\n" + "=" * 60)
        print("N22 讲义样例：extract_preference_candidates 输出")
        print("=" * 60)
        for i, c in enumerate(result):
            print(f"\n[{i}] predicate={c.predicate} value={c.object_value} polarity={c.polarity}")
            print(f"    fact={c.fact}")
            print(f"    source_turn_ids={c.source_turn_ids}")
            print(f"    confidence={c.confidence} sensitivity={c.sensitivity}")
        print(f"\n共 {len(result)} 条候选")
        print("=" * 60)

        # 断言：至少抽到"喜欢越剧"和某种颜色偏好
        assert len(result) >= 1
        predicates = {c.predicate for c in result}
        assert "preferred_opera_genre" in predicates or "preferred_color" in predicates


# ------------------------------------------------------------------ Day 7 B2-3 契约（P1-2 关联：记忆生产接线）


def _spy_insert(monkeypatch: pytest.MonkeyPatch, *, fail_first: bool = False) -> list:
    """替换 insert_candidate 为捕获器：记录候选，可选首条抛跳过/异常。"""
    captured: list = []
    calls = {"n": 0}

    async def fake_insert(candidate: MemoryCandidate, session=None) -> dict[str, str]:
        calls["n"] += 1
        if fail_first and calls["n"] == 1:
            raise MemoryInsertSkipped(candidate.elder_id, candidate.subject, candidate.predicate)
        captured.append(candidate)
        return {"chunk_id": "c1", "elder_id": candidate.elder_id}

    monkeypatch.setattr(memory_service_module, "insert_candidate", fake_insert)
    return captured


class TestDay7MemoryWiring:
    async def test_memory_extract_after_turn_persists_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B2 验收必过用例：单轮入口提取并入库，候选带非空 source_turn_ids。"""
        captured = _spy_insert(monkeypatch)
        inserted = await extract_after_turn(
            elder_id="e1",
            thread_id="t1",
            turn_id="tr1",
            user_text="我喜欢越剧",
            assistant_text="好的，记住了",
        )
        assert inserted == 1
        assert len(captured) == 1
        c = captured[0]
        assert c.source_turn_ids == ["tr1"]  # 非空且指向本轮
        assert c.predicate == "preferred_opera_genre"
        assert c.object_value == "越剧"

    async def test_extract_after_turn_generic_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """generic / 无 elder_id → 0，完全不触发提取（闸门②）。"""
        called: list[int] = []

        async def fake_extract(**kwargs):
            called.append(1)
            return []

        monkeypatch.setattr(memory_service_module, "extract_preference_candidates", fake_extract)
        assert (
            await extract_after_turn(
                elder_id=None, thread_id="t", turn_id="tr", user_text="x", assistant_text=""
            )
            == 0
        )
        assert (
            await extract_after_turn(
                elder_id="", thread_id="t", turn_id="tr", user_text="x", assistant_text=""
            )
            == 0
        )
        assert called == []

    async def test_extract_after_turn_insert_failure_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """入库失败只记日志不抛（后台任务纪律），返回 0。"""

        async def broken_insert(candidate: MemoryCandidate, session=None) -> dict[str, str]:
            raise RuntimeError("db down")

        monkeypatch.setattr(memory_service_module, "insert_candidate", broken_insert)
        assert (
            await extract_after_turn(
                elder_id="e1",
                thread_id="t",
                turn_id="tr1",
                user_text="我喜欢越剧",
                assistant_text="",
            )
            == 0
        )

    async def test_extract_after_turn_duplicate_skipped_keeps_counting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同键值同 → MemoryInsertSkipped 幂等跳过，不计入插入数，也不抛。"""

        def _cand(value: str) -> MemoryCandidate:
            return MemoryCandidate(
                elder_id="e1",
                category="preference",
                subject="e1",
                predicate="preferred_opera_genre",
                object_value=value,
                source_turn_ids=["tr1"],
            )

        async def fake_extract(**kwargs):
            return [_cand("越剧"), _cand("京剧")]  # 首条将撞幂等跳过

        monkeypatch.setattr(memory_service_module, "extract_preference_candidates", fake_extract)
        captured = _spy_insert(monkeypatch, fail_first=True)
        inserted = await extract_after_turn(
            elder_id="e1",
            thread_id="t",
            turn_id="tr1",
            user_text="我喜欢越剧，也爱听京剧",
            assistant_text="",
        )
        assert inserted == 1  # skipped 不计，第二条成功
        assert len(captured) == 1
        assert captured[0].object_value == "京剧"

    async def test_schedule_memory_extraction_runs_in_background(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """后台触发：create_task 执行完成前调用方不被阻塞；完成后候选入库。"""
        captured = _spy_insert(monkeypatch)
        t0 = time.perf_counter()
        schedule_memory_extraction(
            elder_id="e1",
            thread_id="t",
            turn_id="tr1",
            user_text="我喜欢越剧",
            assistant_text="",
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 50  # 首包延迟口径：调度本身不 >50ms
        assert captured == []  # 尚未入库（异步）

        # 等后台任务跑完
        pending = [t for t in memory_service_module._BACKGROUND_TASKS if not t.done()]
        await asyncio.gather(*pending)
        assert len(captured) == 1
        assert captured[0].source_turn_ids == ["tr1"]

    async def test_recall_for_turn_returns_notes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """C1 交接契约：recall_for_turn(elder_id, query) → 文本 notes 列表。"""

        async def fake_recall(**kwargs):
            return [SimpleNamespace(content="长者喜欢越剧"), SimpleNamespace(content="长者爱散步")]

        monkeypatch.setattr(memory_service_module, "recall_active_chunks", fake_recall)
        notes = await recall_for_turn(elder_id="e1", query="越剧")
        assert notes == ["长者喜欢越剧", "长者爱散步"]

    async def test_recall_for_turn_generic_or_missing_elder_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(**kwargs):
            raise AssertionError("generic/missing elder must not hit the store")

        monkeypatch.setattr(memory_service_module, "recall_active_chunks", boom)
        assert await recall_for_turn(elder_id=None, query="x") == []
        assert await recall_for_turn(elder_id="", query="x") == []

    async def test_recall_for_turn_store_failure_is_empty_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C1 口径：无记忆/存储故障返回空列表，不报错。"""

        async def broken(**kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(memory_service_module, "recall_active_chunks", broken)
        assert await recall_for_turn(elder_id="e1", query="x") == []


__all__ = []
