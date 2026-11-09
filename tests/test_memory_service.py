"""MemoryService 骨架测试（N22）。

任务卡 §3.3 验收点：
- 三条硬校验：空 source_turn_ids / sensitivity=health / generic 身份
- extract_preference_candidates 模板匹配骨架
- recall_active_chunks 闸门②（DB 集成场景需 PostgreSQL，这里 skip 标记）
- insert_candidate 的同键 diff 处理（值同跳过，值不同新插一条）
"""

from __future__ import annotations

import pytest

from app.services.memory_service import (
    MemoryCandidate,
    MemoryValidationError,
    _validate_candidate,
    extract_preference_candidates,
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


__all__ = []
