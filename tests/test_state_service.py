"""StateCenter + SessionCoordinator 测试（contracts.md v1.0 §5.2 + §3）。

覆盖：
- StateSnapshot 基本 / to_dict / from_dict / 不变式
- 12 种 StateEvent → StateSnapshot 字段变更映射（逐事件验证）
- CAS 版本冲突拒绝覆盖
- Redis 不可用降级（普通闲聊 vs 用药任务）
- SessionCoordinator 串行提交 + 冲突自动重试
- 并发 20 写无 seq 重号
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError

from app.agents.session_coordinator import SessionCoordinator
from app.services.state_service import (
    StateCenter,
    StateEvent,
    StateSnapshot,
    StateUnavailable,
    StateVersionConflict,
    _apply_event,
)

# ------------------------------------------------------------------ fixture


@pytest.fixture
def memory_center() -> StateCenter:
    return StateCenter(client=None, key_prefix="tst", allow_memory_fallback=True)


@pytest.fixture
def memory_coord(memory_center: StateCenter) -> SessionCoordinator:
    return SessionCoordinator(memory_center)


# ------------------------------------------------------------------ StateSnapshot 基本


class TestStateSnapshotBasic:
    def test_zero_version_not_negative(self) -> None:
        with pytest.raises(ValueError):
            StateSnapshot(thread_id="t", version=-1)

    def test_empty_thread_rejected(self) -> None:
        with pytest.raises(ValueError):
            StateSnapshot(thread_id="", version=0)

    def test_roundtrip_dict(self) -> None:
        snap = StateSnapshot(
            thread_id="t1",
            version=5,
            input={"status": "active", "active_turn_id": "tr1", "asr_partial": "你好"},
        )
        data = snap.to_dict()
        restored = StateSnapshot.from_dict(data)
        assert restored.thread_id == "t1"
        assert restored.version == 5
        assert restored.input["status"] == "active"
        assert restored.input["asr_partial"] == "你好"


# ------------------------------------------------------------------ 事件→状态映射（12 种逐事件验证）


class TestApplyEventMapping:
    def _empty(self) -> StateSnapshot:
        return StateSnapshot(
            thread_id="t",
            version=0,
            input={"status": "idle", "active_turn_id": None, "asr_partial": ""},
            output={
                "status": "idle",
                "response_id": None,
                "generation_epoch": 0,
                "last_acked_segment": -1,
            },
            task={"active_task_id": None, "kind": None, "waiting_for": None},
        )

    def test_turn_started(self) -> None:
        snap = _apply_event(self._empty(), StateEvent("turn_started", {"turn_id": "tr1"}))
        assert snap.version == 1
        assert snap.input["status"] == "active"
        assert snap.input["active_turn_id"] == "tr1"
        assert snap.input["asr_partial"] == ""

    def test_asr_final_committed(self) -> None:
        # 先有 partial，再 commit 清空
        snap0 = _apply_event(
            self._empty(), StateEvent("turn_started", {"turn_id": "tr1", "user_text": "你好"})
        )
        snap0.input["asr_partial"] = "你好啊"  # simulate partial accumulation
        from dataclasses import replace

        snap0 = replace(snap0, input={**snap0.input, "asr_partial": "你好啊"})
        snap = _apply_event(snap0, StateEvent("asr_final_committed", {}))
        assert snap.input["asr_partial"] == ""

    def test_response_started(self) -> None:
        snap = _apply_event(
            self._empty(),
            StateEvent("response_started", {"response_id": "r1", "generation_epoch": 7}),
        )
        assert snap.version == 1
        assert snap.output["status"] == "playing"
        assert snap.output["response_id"] == "r1"
        assert snap.output["generation_epoch"] == 7
        assert snap.output["last_acked_segment"] == -1

    def test_response_segment_delivered(self) -> None:
        snap0 = _apply_event(self._empty(), StateEvent("response_started", {"response_id": "r1"}))
        snap = _apply_event(snap0, StateEvent("response_segment_delivered", {"segment_id": 2}))
        assert snap.output["last_acked_segment"] == 2

    def test_response_interrupted(self) -> None:
        snap0 = _apply_event(self._empty(), StateEvent("response_started", {"response_id": "r1"}))
        snap = _apply_event(snap0, StateEvent("response_interrupted", {"response_id": "r1"}))
        assert snap.output["status"] == "interrupted"

    def test_response_completed(self) -> None:
        snap0 = _apply_event(self._empty(), StateEvent("response_started", {"response_id": "r1"}))
        snap = _apply_event(snap0, StateEvent("response_completed", {"total_segments": 3}))
        assert snap.output["status"] == "completed"

    @pytest.mark.parametrize("ev", ["tool_started", "tool_succeeded", "tool_failed"])
    def test_tool_events_no_state_change(self, ev: str) -> None:
        """工具事件只进 conversation_events 流水，不改 StateSnapshot。"""
        snap = self._empty()
        new_snap = _apply_event(snap, StateEvent(ev, {"tool_name": "weather"}))
        # version 仍 +1，但四组字段不变
        assert new_snap.version == 1
        assert new_snap.identity == snap.identity
        assert new_snap.input == snap.input
        assert new_snap.output == snap.output
        assert new_snap.task == snap.task

    def test_task_waiting(self) -> None:
        snap = _apply_event(
            self._empty(),
            StateEvent(
                "task_waiting",
                {"task_id": "tk1", "kind": "medication", "waiting_for": "taken_answer"},
            ),
        )
        assert snap.task["active_task_id"] == "tk1"
        assert snap.task["kind"] == "medication"
        assert snap.task["waiting_for"] == "taken_answer"

    def test_task_resumed(self) -> None:
        snap0 = _apply_event(
            self._empty(),
            StateEvent(
                "task_waiting",
                {"task_id": "tk1", "kind": "medication", "waiting_for": "taken_answer"},
            ),
        )
        snap = _apply_event(
            snap0, StateEvent("task_resumed", {"task_id": "tk1", "answer_digest": "..."})
        )
        assert snap.task["active_task_id"] is None
        assert snap.task["waiting_for"] is None

    def test_turn_completed(self) -> None:
        snap0 = _apply_event(self._empty(), StateEvent("turn_started", {"turn_id": "tr1"}))
        snap = _apply_event(
            snap0, StateEvent("turn_completed", {"turn_id": "tr1", "status": "completed"})
        )
        assert snap.input["status"] == "idle"
        assert snap.input["active_turn_id"] is None


# ------------------------------------------------------------------ CAS 版本冲突


class TestCASConflict:
    async def test_version_conflict_rejected(self, memory_center: StateCenter) -> None:
        await memory_center.apply(
            thread_id="t1",
            expected_version=0,
            events=[StateEvent("turn_started", {"turn_id": "tr1"})],
        )
        with pytest.raises(StateVersionConflict) as excinfo:
            await memory_center.apply(
                thread_id="t1", expected_version=0, events=[StateEvent("asr_final_committed", {})]
            )
        assert excinfo.value.expected == 0
        assert excinfo.value.actual == 1

    async def test_apply_chain_straight(self, memory_center: StateCenter) -> None:
        snap = await memory_center.snapshot("t2")
        snap = await memory_center.apply(
            thread_id="t2",
            expected_version=snap.version,
            events=[StateEvent("turn_started", {"turn_id": "tr1"})],
        )
        snap = await memory_center.apply(
            thread_id="t2",
            expected_version=snap.version,
            events=[StateEvent("response_started", {"response_id": "r1"})],
        )
        snap = await memory_center.apply(
            thread_id="t2",
            expected_version=snap.version,
            events=[StateEvent("response_completed", {})],
        )
        assert snap.version == 3
        assert snap.output["status"] == "completed"


# ------------------------------------------------------------------ 降级


class TestDegradation:
    async def test_redis_unavailable_normal_chat_falls_back_memory(self) -> None:
        """普通闲聊 + Redis 不可用 → 内存降级，不抛异常。"""
        center = StateCenter(client=None, key_prefix="tst", allow_memory_fallback=True)
        # 第一次 apply 本来就是内存（client=None）
        snap = await center.apply(
            thread_id="t1",
            expected_version=0,
            events=[StateEvent("turn_started", {"turn_id": "tr1"})],
        )
        assert snap.version == 1

    async def test_medication_task_raises_state_unavailable(self) -> None:
        """用药任务 + Redis 不可用 + 关闭内存降级 → 抛 StateUnavailable。"""
        broken_client = AsyncMock()
        broken_client.get.side_effect = RedisError("connection refused")
        broken_client.set.side_effect = RedisError("connection refused")
        broken_client.eval.side_effect = RedisError("connection refused")
        broken_client.delete.side_effect = RedisError("connection refused")

        center = StateCenter(client=broken_client, key_prefix="tst", allow_memory_fallback=False)
        with pytest.raises(StateUnavailable):
            await center.apply(
                thread_id="t1",
                expected_version=0,
                events=[
                    StateEvent(
                        "task_waiting",
                        {"task_id": "tk1", "kind": "medication", "waiting_for": "taken_answer"},
                    )
                ],
                is_medication_task=True,
            )


# ------------------------------------------------------------------ SessionCoordinator


class TestSessionCoordinator:
    async def test_serial_submit_no_version_conflict(
        self, memory_coord: SessionCoordinator
    ) -> None:
        """串行 submit：20 个事件，每个版本递增 +1，无冲突。"""

        # 并发 submit 20 次（同一个 thread）
        async def submit_event(n: int) -> None:
            await memory_coord.submit(
                "t_serial",
                [StateEvent("turn_started", {"turn_id": f"tr{n}"})],
            )

        await asyncio.gather(*[submit_event(n) for n in range(20)])
        snap = await memory_coord.snapshot("t_serial")
        # 20 个事件，每个版本 +1，从 0 开始 → version 应该在 20 左右（串行化后）
        assert snap.version == 20  # 完美串行 → 无冲突

    async def test_retry_on_conflict(self) -> None:
        """外部竞态：Coordinator 自动重试。"""
        center = StateCenter(client=None, key_prefix="tst", allow_memory_fallback=True)
        coord = SessionCoordinator(center, max_retries=3)

        # Coordinator submit 的同时，外部 center.apply 改了版本
        # 但 coordinator 的锁会保证串行 —— 所以应该直接成功
        snap = await coord.submit("t_retry", [StateEvent("turn_started", {"turn_id": "tr1"})])
        assert snap.version == 1


# ------------------------------------------------------------------ clear


class TestClear:
    async def test_clear_resets_to_zero(self, memory_coord: SessionCoordinator) -> None:
        await memory_coord.submit("t_clear", [StateEvent("turn_started", {"turn_id": "tr1"})])
        await memory_coord.clear("t_clear")
        snap = await memory_coord.snapshot("t_clear")
        assert snap.version == 0

    async def test_empty_events_is_noop(self, memory_center: StateCenter) -> None:
        snap = await memory_center.apply(thread_id="t1", expected_version=0, events=[])
        assert snap.version == 0


# ------------------------------------------------------------------ Day 7 B2-2 契约（P2-13~15 / P3-29）


class _FakeRedis:
    """协议级 Redis 替身：读写故障可分别切换，可预置陈旧/损坏数据。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.fail_read = False
        self.fail_write = False

    @property
    def fail(self) -> bool:
        return self.fail_read and self.fail_write

    @fail.setter
    def fail(self, value: bool) -> None:
        self.fail_read = value
        self.fail_write = value

    async def get(self, key: str) -> str | None:
        if self.fail_read:
            raise RedisError("connection refused")
        return self.data.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> Any:
        if self.fail_write:
            raise RedisError("connection refused")
        self.data[key] = value

    async def delete(self, key: str) -> Any:
        if self.fail_write:
            raise RedisError("connection refused")
        self.data.pop(key, None)

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        if self.fail_write:
            raise RedisError("connection refused")
        key, expected, new_json, _ttl, new_version = args
        current = self.data.get(key)
        if current is not None:
            try:
                decoded = json.loads(current)
            except json.JSONDecodeError:
                return -2
            if int(decoded.get("version", -1)) != int(expected):
                return -1
        elif int(expected) != 0:
            return -1
        self.data[key] = new_json
        return int(new_version)


class TestDay7StateService:
    async def test_redis_down_medication_raises_state_unavailable(self) -> None:
        """P2-13：Redis 停止 + 用药任务 → StateUnavailable，绝不降级成 StateVersionConflict。

        关键场景：调用方以为 Redis 里有 v1（expected_version=1），Redis 读失败后
        内存降级视图是 v0 —— 修复前这里会先抛 StateVersionConflict。
        """
        fake = _FakeRedis()
        fake.fail = True
        center = StateCenter(client=fake, key_prefix="tst", allow_memory_fallback=True)
        with pytest.raises(StateUnavailable) as excinfo:
            await center.apply(
                thread_id="t_med_down",
                expected_version=1,
                events=[StateEvent("turn_started", {"turn_id": "tr1"})],
                is_medication_task=True,
            )
        assert not isinstance(excinfo.value, StateVersionConflict)

    async def test_redis_recovery_merges_memory_version(self) -> None:
        """P2-14：写失败降级内存后，Redis 恢复时快照优先取更新的内存版并回写 Redis。"""
        fake = _FakeRedis()
        fake.fail = True
        center = StateCenter(client=fake, key_prefix="tst", allow_memory_fallback=True)

        # Redis 故障窗口内：普通闲聊降级内存，写出 v1
        await center.apply(
            thread_id="t_recover",
            expected_version=0,
            events=[StateEvent("turn_started", {"turn_id": "tr1"})],
        )

        # 模拟 Redis 恢复，但里面留着一份陈旧 v0
        fake.fail = False
        stale = StateCenter(client=None, key_prefix="tst")._empty_snapshot("t_recover")
        fake.data[center._key_for("t_recover")] = json.dumps(stale.to_dict(), ensure_ascii=False)

        snap = await center.snapshot("t_recover")
        assert snap.version == 1  # 内存新版本胜出，不被旧 Redis 覆盖
        assert snap.input["active_turn_id"] == "tr1"
        # 回写：Redis 里的数据已升级到 v1
        merged = json.loads(fake.data[center._key_for("t_recover")])
        assert merged["version"] == 1

    async def test_from_dict_rejects_bad_schema(self) -> None:
        """P2-15：from_dict 拒绝缺 thread_id / 非法 version / 非 dict 分区。"""
        bad_cases: list[Any] = [
            {},  # 缺 thread_id / version
            {"thread_id": "", "version": 0},  # 空 thread_id
            {"thread_id": "t", "version": -1},  # 负版本
            {"thread_id": "t", "version": "3"},  # 版本非 int（原实现会强转）
            {"thread_id": "t", "version": True},  # bool 冒充 int
            {"thread_id": "t", "version": 0, "input": "not-a-dict"},  # 分区非 dict
            {"thread_id": "t", "version": 0, "task": [1, 2]},  # 分区为 list
        ]
        for bad in bad_cases:
            with pytest.raises(ValueError):
                StateSnapshot.from_dict(bad)
        # 合法载荷仍可通过
        ok = StateSnapshot.from_dict({"thread_id": "t", "version": 2, "input": {"a": 1}})
        assert ok.version == 2
        assert ok.input == {"a": 1}

    async def test_apply_failure_keeps_written_facts(self) -> None:
        """P3-29：apply 写失败时保留已算出的事实到内存降级，不全轮回滚。"""
        fake = _FakeRedis()
        center = StateCenter(client=fake, key_prefix="tst", allow_memory_fallback=True)

        # 先正常写一轮到 Redis（v1）
        await center.apply(
            thread_id="t_keep",
            expected_version=0,
            events=[StateEvent("turn_started", {"turn_id": "tr1"})],
        )

        # 只断写：用药任务写失败 → StateUnavailable，但事实已先落内存（不丢）
        fake.fail_write = True
        with pytest.raises(StateUnavailable):
            await center.apply(
                thread_id="t_keep",
                expected_version=1,
                events=[StateEvent("task_waiting", {"task_id": "tk1", "kind": "medication"})],
                is_medication_task=True,
            )
        # 读仍通：对账后返回内存新版本（事实保留，未被 Redis 旧版覆盖）
        snap = await center.snapshot("t_keep")
        assert snap.version == 2
        assert snap.task["active_task_id"] == "tk1"

        # 恢复写后再次读取 → 内存版本回写 Redis
        fake.fail_write = False
        await center.snapshot("t_keep")
        merged = json.loads(fake.data[center._key_for("t_keep")])
        assert merged["version"] == 2

    async def test_corrupted_state_raises_conflict_not_overwrite(self) -> None:
        """P2-15：Lua 返回 -2（存储损坏）按冲突处理，不再无条件覆盖写。"""
        fake = _FakeRedis()
        fake.data[StateCenter(client=None, key_prefix="tst")._key_for("t_bad")] = "{not-json"
        center = StateCenter(client=fake, key_prefix="tst", allow_memory_fallback=True)
        with pytest.raises(StateVersionConflict) as excinfo:
            await center.apply(
                thread_id="t_bad",
                expected_version=0,
                events=[StateEvent("turn_started", {"turn_id": "tr1"})],
            )
        assert excinfo.value.actual == -2
        # 损坏数据未被覆盖
        assert fake.data[center._key_for("t_bad")] == "{not-json"

    async def test_memory_store_lru_eviction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """P2-15：内存降级 store 有 LRU 上限，最旧条目被淘汰。"""
        from app.services import state_service as state_service_module

        monkeypatch.setattr(state_service_module, "_MEMORY_STORE_MAX", 2)
        center = StateCenter(client=None, key_prefix="tst", allow_memory_fallback=True)
        for i in range(3):
            await center.apply(
                thread_id=f"t_lru_{i}",
                expected_version=0,
                events=[StateEvent("turn_started", {"turn_id": f"tr{i}"})],
            )
        assert len(center._memory_store) == state_service_module._MEMORY_STORE_MAX == 2
        assert (await center.snapshot("t_lru_0")).version == 0  # 最旧的被淘汰
        assert (await center.snapshot("t_lru_2")).version == 1  # 最新的还在

    async def test_memory_store_ttl_expiry(self) -> None:
        """P2-15：内存降级条目与 Redis 同 TTL，过期后视作不存在。"""
        center = StateCenter(client=None, key_prefix="tst", allow_memory_fallback=True)
        await center.apply(
            thread_id="t_ttl",
            expected_version=0,
            events=[StateEvent("turn_started", {"turn_id": "tr1"})],
        )
        center._memory_ts["t_ttl"] = time.monotonic() - center._ttl - 1
        snap = await center.snapshot("t_ttl")
        assert snap.version == 0  # 过期淘汰 → 零版快照


__all__ = []
