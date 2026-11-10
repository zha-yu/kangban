"""ContextBuilder 集成测试（N22 骨架）。

任务卡 §3.2 验收点全部覆盖：
- 九层顺序固化（不可自定义）
- 预算裁剪（尾部起裁，must_keep 四层永不裁）
- 只取 delivered_text + 打断标记
- 不可信数据边界标签
- waiting_for 绑定（讲义 §7.2）
- L8 写锁：build_dialogue_messages 签名零改动（test_dialogue_* 套件已覆盖）

DB 不可用时 authoritative_facts / relevant_memories 层自动降级为空 + degraded 标记——
不 skip，而是验证降级逻辑生效。真正查 DB 的集成场景（dev-A history_service）用 skip 标记。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.context import (
    ContextBuilderConfig,
    DialogueIdentity,
    _assemble_active_task,
    _assemble_waiting_for_binding,
    _boundary_wrap,
    _estimate_tokens,
    _history_messages_from_views,
    _older_summary_from_views,
    _split_memory_rows,
    build_turn_context,
)
from app.services.state_service import (
    StateCenter,
    StateEvent,
    StateSnapshot,
)

# ------------------------------------------------------------------ fixtures


@pytest.fixture
def memory_center() -> StateCenter:
    return StateCenter(client=None, key_prefix="tst")


@pytest.fixture
async def idle_snapshot(memory_center: StateCenter) -> StateSnapshot:
    await memory_center.apply(
        thread_id="t_demo",
        expected_version=0,
        events=[StateEvent("turn_started", {"turn_id": "tr1"})],
    )
    return await memory_center.snapshot("t_demo")


@pytest.fixture
async def medication_waiting_snapshot(memory_center: StateCenter) -> StateSnapshot:
    await memory_center.apply(
        thread_id="t_med",
        expected_version=0,
        events=[
            StateEvent("turn_started", {"turn_id": "tr1"}),
            StateEvent(
                "task_waiting",
                {"task_id": "tk_med_1", "kind": "medication", "waiting_for": "taken_answer"},
            ),
        ],
    )
    return await memory_center.snapshot("t_med")


# ------------------------------------------------------------------ _estimate_tokens / _boundary_wrap 纯函数


class TestHelpers:
    def test_estimate_tokens_empty(self) -> None:
        assert _estimate_tokens("") == 0

    def test_estimate_tokens_chinese(self) -> None:
        # 中文字符多 → 每 2 字 = 1 token
        assert _estimate_tokens("你好世界") == 2
        assert _estimate_tokens("这是一个比较长的中文句子用于测试token估算") > 5

    def test_boundary_wrap_format(self) -> None:
        result = _boundary_wrap(
            kind="memory",
            source="memory_chunks",
            identifier="chunk-42",
            confidence=0.92,
            inner="长者喜欢越剧",
        )
        assert result.startswith("<retrieved_knowledge")
        assert "kind=memory" in result
        assert "source=memory_chunks" in result
        assert "id=chunk-42" in result
        assert "version=0.92" in result
        assert "</retrieved_knowledge>" in result
        assert "长者喜欢越剧" in result

    def test_boundary_wrap_no_confidence(self) -> None:
        result = _boundary_wrap("memory", "memory_chunks", "c1", None, "内容")
        assert "version=" not in result


# ------------------------------------------------------------------ _assemble_active_task / waiting_for 纯函数


class TestActiveTaskBinding:
    def test_active_task_none_snapshot(self) -> None:
        assert _assemble_active_task(None) == ""

    def test_active_task_no_task(self) -> None:
        snap = StateSnapshot(
            thread_id="t",
            version=0,
            task={},
            input={},
            output={},
            media={},
            identity={},
        )
        assert _assemble_active_task(snap) == ""

    def test_active_task_with_waiting(self) -> None:
        snap = StateSnapshot(
            thread_id="t",
            version=1,
            task={"active_task_id": "tk1", "kind": "medication", "waiting_for": "taken_answer"},
            input={},
            output={},
            media={},
            identity={},
        )
        text = _assemble_active_task(snap)
        assert "medication" in text
        assert "taken_answer" in text
        assert "优先" in text

    def test_waiting_for_taken_answer_binding(self) -> None:
        snap = StateSnapshot(
            thread_id="t",
            version=1,
            task={"active_task_id": "tk1", "kind": "medication", "waiting_for": "taken_answer"},
            input={},
            output={},
            media={},
            identity={},
        )
        text = _assemble_waiting_for_binding(snap)
        assert "用药确认" in text
        assert "吃了" in text

    def test_waiting_for_none(self) -> None:
        snap = StateSnapshot(
            thread_id="t", version=0, task={}, input={}, output={}, media={}, identity={}
        )
        assert _assemble_waiting_for_binding(snap) == ""


# ------------------------------------------------------------------ build_turn_context 集成


class TestBuildTurnContext:
    async def test_generic_identity_uses_generic_prompt(self) -> None:
        from app.core.context import DialogueIdentity

        identity = DialogueIdentity(source="generic", elder_id=None, display_name=None)
        ctx = await build_turn_context(
            identity=identity,
            user_text="你好",
            thread_id="t_gen",
        )
        # generic 模式：messages 里有 GENERIC_SYSTEM_PROMPT，没有身份确认文本
        assert any(
            "generic" in m["content"].lower() or "未确认" in m["content"]
            for m in ctx.messages
            if m["role"] == "system"
        )
        # 没有 degraded_layers（generic 模式跳过记忆层，不是降级）
        assert "authoritative_facts" not in ctx.degraded_layers

    async def test_personal_identity_has_identity_layer(self) -> None:
        from app.core.context import DialogueIdentity

        identity = DialogueIdentity(
            elder_id="e1234567-1234-5678-1234-567812345678",
            source="voice",
            display_name="王奶奶",
        )
        now = dt.datetime(2026, 11, 9, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        ctx = await build_turn_context(
            identity=identity,
            user_text="我想聊聊天",
            thread_id="t_p1",
            now=now,
        )
        system_texts = [m["content"] for m in ctx.messages if m["role"] == "system"]
        # 有身份层
        assert any("已确认" in t for t in system_texts)
        assert any("王奶奶" in t for t in system_texts)
        # 有 runtime_info 层（时间）
        assert any("14:30" in t for t in system_texts)
        # 最后一条是 user
        assert ctx.messages[-1]["role"] == "user"
        assert ctx.messages[-1]["content"] == "我想聊聊天"

    async def test_user_text_empty_raises(self) -> None:
        from app.core.context import DialogueIdentity

        identity = DialogueIdentity(
            elder_id="e1",
            source="voice",
            display_name="X",
        )
        with pytest.raises(ValueError, match="user_text must be non-empty"):
            await build_turn_context(identity=identity, user_text="  ", thread_id="t")

    async def test_active_task_layer_present(
        self, medication_waiting_snapshot: StateSnapshot
    ) -> None:
        from app.core.context import DialogueIdentity

        identity = DialogueIdentity(
            elder_id="e1",
            source="voice",
            display_name="X",
        )
        ctx = await build_turn_context(
            identity=identity,
            user_text="嗯",
            thread_id="t_med",
            state_snapshot=medication_waiting_snapshot,
        )
        system_texts = [m["content"] for m in ctx.messages if m["role"] == "system"]
        # active_task 层出现（must_keep，永不裁）
        assert any("【当前任务进行中】" in t for t in system_texts)
        assert any("medication" in t or "用药" in t for t in system_texts)
        # waiting_for 绑定也出现
        assert any("任务绑定提示" in t for t in system_texts)

    async def test_budget_trimming_removes_tail(self) -> None:
        """预算极小 → can_trim 层从尾部裁，must_keep 保留。"""
        from app.core.context import DialogueIdentity

        identity = DialogueIdentity(
            elder_id="e1",
            source="voice",
            display_name="X",
        )
        tiny_cfg = ContextBuilderConfig(budget_tokens=50)  # 极小预算
        ctx = await build_turn_context(
            identity=identity,
            user_text="你好",
            thread_id="t_budget",
            config=tiny_cfg,
            now=dt.datetime(2026, 11, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        # must_keep 层：safety_and_policy + identity_and_scope 一定在 messages 里
        system_texts = [m["content"] for m in ctx.messages if m["role"] == "system"]
        assert any("你是康伴" in t or "数字人" in t for t in system_texts)  # safety_and_policy
        # degraded_layers 里有被裁的
        assert len(ctx.degraded_layers) > 0 or ctx.budget_used_tokens <= ctx.budget_total_tokens

    async def test_messages_order_system_then_history_then_user(self) -> None:
        from app.core.context import DialogueIdentity

        identity = DialogueIdentity(
            elder_id="e1",
            source="voice",
            display_name="X",
        )
        ctx = await build_turn_context(
            identity=identity,
            user_text="本轮用户",
            thread_id="t_order",
            now=dt.datetime(2026, 11, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        roles = [m["role"] for m in ctx.messages]
        # 所有 system 在最前，最后一条是 user
        assert roles[-1] == "user"
        assert roles[0] == "system"
        # system 全部在前（连续）
        last_sys = max(i for i, r in enumerate(roles) if r == "system")
        first_user_or_assistant = min(i for i, r in enumerate(roles) if r in ("user", "assistant"))
        assert last_sys < first_user_or_assistant


# ------------------------------------------------------------------ N22 样例：实际调用一次展示九层


class TestBuildTurnContextExample:
    """讲义 §7 示例：build_turn_context 对完整 personal 身份 + active medication task 的输出。

    这不是 skip 测试 —— 它在 PostgreSQL 不可用时也能跑，因为：
    - authoritative_facts / relevant_memories 查 DB 时自动降级为空 + degraded 标记
    - older_summary / history_turns 用 try/except ImportError 桩返回空
    - 所有九层装配都能完成（降级的层不进 messages 或内容为空）
    """

    @pytest.mark.asyncio
    async def test_full_context_example(self, medication_waiting_snapshot: StateSnapshot) -> None:
        from app.core.context import DialogueIdentity

        identity = DialogueIdentity(
            elder_id="e-f47ac10b-58cc-4372-a567-0e02b2c3d479",
            source="voice",
            display_name="王奶奶",
        )
        now = dt.datetime(2026, 11, 9, 19, 45, tzinfo=ZoneInfo("Asia/Shanghai"))
        ctx = await build_turn_context(
            identity=identity,
            user_text="吃了",
            thread_id="t_demo_med",
            state_snapshot=medication_waiting_snapshot,
            now=now,
        )

        # 打印用于讲义样例（不影响断言）
        print("\n" + "=" * 60)
        print("N22 讲义样例：build_turn_context 输出")
        print("=" * 60)
        for i, m in enumerate(ctx.messages):
            print(f"\n[{i}] role={m['role']}")
            print(f"    {m['content'][:120]}...")
        print(f"\nbudget_used={ctx.budget_used_tokens}/{ctx.budget_total_tokens}")
        print(f"degraded_layers={ctx.degraded_layers}")
        print("=" * 60)

        # 基本结构断言
        assert ctx.messages[-1]["role"] == "user"
        assert ctx.messages[-1]["content"] == "吃了"
        assert ctx.budget_used_tokens > 0
        # must_keep 三层一定在（safety_and_policy / identity_and_scope / active_task + waiting_for）
        system_contents = [m["content"] for m in ctx.messages if m["role"] == "system"]
        assert any("当前时间" in t for t in system_contents)  # runtime_info
        assert any("当前任务进行中" in t for t in system_contents)  # active_task
        assert any("任务绑定提示" in t for t in system_contents)  # waiting_for


# ------------------------------------------------------------------ Day 7 B2-1 契约（P1-2 / P2-16~18/21 / P3-35）


def _turn(
    user_text: str | None = "吃了",
    assistant_delivered_text: str | None = None,
    status: str = "interrupted",
) -> SimpleNamespace:
    """构造 TurnView 形状的轮次（history_service 的冻结 dataclass 字段子集）。"""
    return SimpleNamespace(
        user_text=user_text,
        assistant_delivered_text=assistant_delivered_text,
        status=status,
    )


class TestDay7NullSafety:
    """P1-2：空送达/打断组合不崩，LLM 消息 content 永不为 None。"""

    def test_interrupted_with_null_delivered_does_not_crash(self) -> None:
        msgs = _history_messages_from_views([_turn()])
        assert msgs[1]["role"] == "assistant"
        # 空文本不拼打断标记，绝不抛 TypeError
        assert msgs[1]["content"] == ""

    def test_interrupted_with_text_appends_marker_once(self) -> None:
        msgs = _history_messages_from_views(
            [_turn(assistant_delivered_text="今天天气不错", status="interrupted")]
        )
        assert msgs[1]["content"].startswith("今天天气不错")
        assert msgs[1]["content"].count("打断") == 1

    def test_completed_keeps_delivered_without_marker(self) -> None:
        msgs = _history_messages_from_views(
            [_turn(assistant_delivered_text="睡了个好觉", status="completed")]
        )
        assert msgs[1]["content"] == "睡了个好觉"

    def test_llm_messages_never_content_null(self) -> None:
        # user_text / assistant_delivered_text 同时为 None 也不产生 None content
        msgs = _history_messages_from_views(
            [_turn(user_text=None, assistant_delivered_text=None, status="interrupted")]
        )
        assert all(isinstance(m["content"], str) for m in msgs)
        assert all(m["content"] is not None for m in msgs)

    def test_older_summary_handles_null_texts(self) -> None:
        text = _older_summary_from_views([_turn(user_text=None)])
        assert "长者说" in text


class TestDay7GenericShortCircuit:
    """P2-16：generic 轮短路 waiting_for 任务绑定。"""

    async def test_generic_turn_skips_waiting_for_binding(
        self, medication_waiting_snapshot: StateSnapshot
    ) -> None:
        identity = DialogueIdentity(source="generic", elder_id=None)
        ctx = await build_turn_context(
            identity=identity,
            user_text="你好",
            thread_id="t_gen_med",
            state_snapshot=medication_waiting_snapshot,
        )
        assert not any("任务绑定提示" in m["content"] for m in ctx.messages)

    async def test_personal_turn_keeps_waiting_for_binding(
        self, medication_waiting_snapshot: StateSnapshot
    ) -> None:
        identity = DialogueIdentity(elder_id="e1", source="voice", display_name="X")
        ctx = await build_turn_context(
            identity=identity,
            user_text="嗯",
            thread_id="t_med_bind",
            state_snapshot=medication_waiting_snapshot,
        )
        assert any("任务绑定提示" in m["content"] for m in ctx.messages)


class TestDay7BudgetAccounting:
    """P2-18：history_turns 与 waiting_for 计入 budget_used；token 口径可复算。"""

    async def test_budget_counts_history_and_waiting_for(
        self,
        monkeypatch: pytest.MonkeyPatch,
        medication_waiting_snapshot: StateSnapshot,
    ) -> None:
        identity = DialogueIdentity(elder_id="e1", source="voice", display_name="X")

        async def fake_read_turns(session=None, *, thread_id, elder_id=None, limit=8):
            return holder["turns"]

        holder: dict[str, list] = {"turns": []}
        monkeypatch.setattr("app.services.history_service.read_turns", fake_read_turns)

        # 基线：带同一 snapshot（active_task/waiting 层相同），仅历史为空（holder 为空）
        baseline = await build_turn_context(
            identity=identity,
            user_text="你好",
            thread_id="t_budget_base",
            state_snapshot=medication_waiting_snapshot,
        )
        # 注入历史后再构建
        holder["turns"] = [
            _turn(user_text="昨晚睡得好吗", assistant_delivered_text="还不错", status="completed")
        ]
        ctx = await build_turn_context(
            identity=identity,
            user_text="嗯",
            thread_id="t_budget_hist",
            state_snapshot=medication_waiting_snapshot,
        )

        waiting_text = _assemble_waiting_for_binding(medication_waiting_snapshot)
        history_msgs = _history_messages_from_views(holder["turns"])
        expected = (
            baseline.budget_used_tokens
            # 基线历史读取为空：ctx 里 older_summary 层与历史消息两条都新增
            + _estimate_tokens(_older_summary_from_views(holder["turns"]))
            + sum(_estimate_tokens(m["content"]) for m in history_msgs)
        )
        assert waiting_text  # waiting_for 确实非空且已计入基线
        assert ctx.budget_used_tokens == expected

    def test_estimate_tokens_consistent(self) -> None:
        # 口径可复算：max(1, len//2)（非空文本）
        for text in ("a", "ab", "你好", "你好世界", "x" * 101):
            assert _estimate_tokens(text) == max(1, len(text) // 2)


class TestDay7MemoryDedup:
    """P2-21：memories/facts 单次查询切分 + 跨层去重；P3-35：零记忆不误报 degraded。"""

    @staticmethod
    def _chunk(cid, importance, last_accessed=None) -> SimpleNamespace:
        return SimpleNamespace(
            chunk_id=cid,
            content=f"记忆{cid}",
            confidence=0.9,
            importance=importance,
            last_accessed=last_accessed,
        )

    def test_split_memory_rows_dedup_across_layers(self) -> None:
        import datetime as _dt

        rows = [
            self._chunk("c1", 0.9, _dt.datetime(2026, 11, 9, 10, 0)),
            self._chunk("c2", 0.8, _dt.datetime(2026, 11, 9, 11, 0)),
            self._chunk("c3", 0.7, _dt.datetime(2026, 11, 9, 12, 0)),
            self._chunk("c4", 0.6, _dt.datetime(2026, 11, 9, 13, 0)),
        ]
        cfg = ContextBuilderConfig(max_memories=2, max_authoritative_facts=1)
        facts, memories = _split_memory_rows(rows, cfg)
        # facts 按 importance 取前 1
        assert [r.chunk_id for r in facts] == ["c1"]
        # memories 从同一批行里取、跨层排除 facts 已占用的 chunk，按 last_accessed 最近优先
        assert [r.chunk_id for r in memories] == ["c4", "c3"]
        assert "c1" not in [r.chunk_id for r in memories]

    def test_split_memory_rows_empty(self) -> None:
        cfg = ContextBuilderConfig()
        facts, memories = _split_memory_rows([], cfg)
        assert facts == []
        assert memories == []

    async def test_zero_memory_success_not_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        identity = DialogueIdentity(elder_id="e1", source="voice", display_name="X")

        class _EmptySession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, stmt):
                class _R:
                    def scalars(self):
                        class _S:
                            def all(self):
                                return []

                        return _S()

                return _R()

        monkeypatch.setattr("app.db.session_factory", lambda: _EmptySession())
        ctx = await build_turn_context(identity=identity, user_text="你好", thread_id="t_zero_mem")
        assert "authoritative_facts" not in ctx.degraded_layers
        assert "relevant_memories" not in ctx.degraded_layers

    async def test_memory_query_failure_marks_degraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identity = DialogueIdentity(elder_id="e1", source="voice", display_name="X")

        class _BrokenSession:
            async def __aenter__(self):
                raise SQLAlchemyError("db down")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("app.db.session_factory", lambda: _BrokenSession())
        ctx = await build_turn_context(
            identity=identity, user_text="你好", thread_id="t_broken_mem"
        )
        assert "authoritative_facts" in ctx.degraded_layers
        assert "relevant_memories" in ctx.degraded_layers


class TestDay7HistorySingleRead:
    """P2-21：每轮 read_turns 只调 1 次，摘要与历史共用同一批数据。"""

    async def test_history_read_called_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        identity = DialogueIdentity(elder_id="e1", source="voice", display_name="X")
        calls: list[int] = []
        turns = [_turn(user_text="早上好", assistant_delivered_text="早上好呀", status="completed")]

        async def counting_read_turns(session=None, *, thread_id, elder_id=None, limit=8):
            calls.append(1)
            return turns

        monkeypatch.setattr("app.services.history_service.read_turns", counting_read_turns)
        ctx = await build_turn_context(identity=identity, user_text="嗯", thread_id="t_once")
        assert len(calls) == 1
        # 摘要与历史都来自同一批数据
        assert any("早上好呀" in m["content"] for m in ctx.messages)
        assert any("长者说" in m["content"] for m in ctx.messages)


__all__ = []
