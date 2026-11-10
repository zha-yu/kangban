"""StateCenter · 会话状态中心（contracts.md §5.2 冻结契约，N05）。

四组正交状态 + 版本 CAS + Redis 存储 + 降级逻辑：

- Redis key `session:{thread_id}:state`，TTL 2h
- CAS 用 Lua 脚本原子 compare-and-swap（expected_version 不符即抛 StateVersionConflict）
- 12 种事件各改哪些字段**显式写在 `_APPLY_EVENT` 映射表里**，禁止整体重写 JSON
- Redis 不可用时：普通闲聊退化为单进程内存字典，用药任务抛 StateUnavailable
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, Protocol

from redis.exceptions import RedisError

logger = logging.getLogger("app.services.state_service")

# 内存降级 store 上限（P2-15）：Redis 长期故障时防止无限增长
_MEMORY_STORE_MAX: Final[int] = 256

# ------------------------------------------------------------------ 类型别名

EventPayload = dict[str, Any]

# ------------------------------------------------------------------ StateSnapshot / StateEvent


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """会话状态快照（contracts.md v1.0 §5.2 冻结）。

    四组正交状态：identity / input / output / media / task，各自独立演化。
    调用方**不得直接 mutate dict**；全部变更通过 `StateCenter.apply(events)` 提交。
    """

    thread_id: str
    version: int
    identity: dict[str, Any] = field(default_factory=dict)
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    media: dict[str, Any] = field(default_factory=dict)
    task: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.version < 0:
            raise ValueError("version must be non-negative")
        if not self.thread_id:
            raise ValueError("thread_id must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "version": self.version,
            "identity": self.identity,
            "input": self.input,
            "output": self.output,
            "media": self.media,
            "task": self.task,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateSnapshot:
        """反序列化 + schema 校验（P2-15）：版本必须是非负 int，五个分区必须是 dict。

        修复前 `int(data["version"])` 会把任意可强转值静默收下——存进 Redis 的
        损坏载荷会在读回时污染状态机。校验失败一律 ValueError（调用方按坏数据处理）。
        """
        if not isinstance(data, dict):
            raise ValueError(f"state snapshot payload must be a dict, got {type(data).__name__}")
        thread_id = data.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError(
                f"state snapshot requires non-empty string thread_id, got {thread_id!r}"
            )
        raw_version = data.get("version")
        if not isinstance(raw_version, int) or isinstance(raw_version, bool) or raw_version < 0:
            raise ValueError(
                f"state snapshot version must be a non-negative int, got {raw_version!r}"
            )
        sections: dict[str, dict[str, Any]] = {}
        for name in ("identity", "input", "output", "media", "task"):
            value = data.get(name, {})
            if not isinstance(value, dict):
                raise ValueError(
                    f"state snapshot section {name!r} must be a dict, got {type(value).__name__}"
                )
            sections[name] = value
        return cls(thread_id=thread_id, version=raw_version, **sections)


@dataclass(frozen=True, slots=True)
class StateEvent:
    """状态变更事件（contracts.md v1.0 §3 冻结 12 种）。"""

    event_type: Literal[
        "turn_started",
        "asr_final_committed",
        "response_started",
        "response_segment_delivered",
        "response_interrupted",
        "response_completed",
        "tool_started",
        "tool_succeeded",
        "tool_failed",
        "task_waiting",
        "task_resumed",
        "turn_completed",
    ]
    payload: EventPayload = field(default_factory=dict)


# ------------------------------------------------------------------ 异常


class StateVersionConflict(Exception):
    """apply 时 expected_version != 当前 version → CAS 拒绝覆盖。"""

    def __init__(self, thread_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"state version conflict on {thread_id}: expected {expected}, got {actual}"
        )
        self.thread_id = thread_id
        self.expected = expected
        self.actual = actual


class StateUnavailable(Exception):
    """Redis 不可用且是用药任务 → 不静默退化，交由上层提示人工。"""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"state store unavailable for medication task on {thread_id}")
        self.thread_id = thread_id


# ------------------------------------------------------------------ 事件→状态映射表（12 种事件显式列出，禁止整体重写 JSON）


def _apply_event(snap: StateSnapshot, event: StateEvent) -> StateSnapshot:
    """单事件 → 新快照。**纯函数**，不碰外部存储。"""
    payload = event.payload
    new_input = dict(snap.input)
    new_output = dict(snap.output)
    new_task = dict(snap.task)

    match event.event_type:
        case "turn_started":
            new_input.update(
                status="active",
                active_turn_id=payload.get("turn_id"),
                asr_partial="",
            )
        case "asr_final_committed":
            new_input.update(asr_partial="")
        case "response_started":
            new_output.update(
                status="playing",
                response_id=payload.get("response_id"),
                generation_epoch=payload.get("generation_epoch", 0),
                last_acked_segment=-1,
            )
        case "response_segment_delivered":
            new_output.update(last_acked_segment=payload.get("segment_id", -1))
        case "response_interrupted":
            new_output.update(status="interrupted")
        case "response_completed":
            new_output.update(status="completed")
        case "tool_started" | "tool_succeeded" | "tool_failed":
            # 工具事件不改 StateSnapshot 四组字段 —— 只进 conversation_events 流水
            pass
        case "task_waiting":
            new_task.update(
                active_task_id=payload.get("task_id"),
                kind=payload.get("kind"),
                waiting_for=payload.get("waiting_for"),
            )
        case "task_resumed":
            new_task.update(active_task_id=None, waiting_for=None)
        case "turn_completed":
            new_input.update(status="idle", active_turn_id=None)
        case _:
            # 未知事件类型 —— 跳过并 warn（不抛异常，允许未来增量）
            logger.warning("unknown_state_event", extra={"event_type": event.event_type})
            return snap

    return replace(
        snap,
        version=snap.version + 1,
        input=new_input,
        output=new_output if new_output != snap.output else snap.output,
        task=new_task if new_task != snap.task else snap.task,
    )


# ------------------------------------------------------------------ Redis 客户端协议（鸭子类型，便于 fakeredis 测试替身）


class RedisClient(Protocol):
    async def get(self, key: str) -> bytes | str | None: ...

    async def set(self, key: str, value: str, *, ex: int | None = None) -> Any: ...

    async def delete(self, key: str) -> Any: ...

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any: ...


# ------------------------------------------------------------------ CAS Lua 脚本


CAS_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then
    if tonumber(ARGV[1]) ~= 0 then
        return -1
    end
else
    local ok, decoded = pcall(cjson.decode, current)
    if not ok then
        return -2
    end
    if tonumber(decoded.version) ~= tonumber(ARGV[1]) then
        return -1
    end
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', tonumber(ARGV[3]))
return tonumber(ARGV[4])
"""

_DEFAULT_TTL_SECONDS = 7200  # 2h（contracts.md §5.2）


# ------------------------------------------------------------------ StateCenter


class StateCenter:
    """会话状态中心：snapshot / apply（CAS）/ clear。

    存储：Redis `session:{thread_id}:state`（JSON 序列化，TTL 2h）。
    降级：Redis 不可用时普通闲聊走进程内内存字典；用药任务抛 StateUnavailable。
    """

    def __init__(
        self,
        client: RedisClient | None,
        *,
        key_prefix: str = "silvermate",
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        allow_memory_fallback: bool = True,
    ) -> None:
        self._redis = client
        self._key_prefix = key_prefix
        self._ttl = ttl_seconds
        self._allow_memory_fallback = allow_memory_fallback
        # P2-15：内存降级 store 加 LRU + TTL 上限（与 Redis TTL 同口径）
        self._memory_store: OrderedDict[str, StateSnapshot] = OrderedDict()
        self._memory_ts: dict[str, float] = {}

    def _key_for(self, thread_id: str) -> str:
        return f"{self._key_prefix}:session:{thread_id}:state"

    def _mem_get(self, thread_id: str) -> StateSnapshot | None:
        """内存降级读取：过期淘汰 + LRU 触碰（P2-15）。"""
        snap = self._memory_store.get(thread_id)
        if snap is None:
            return None
        ts = self._memory_ts.get(thread_id)
        if ts is not None and time.monotonic() - ts > self._ttl:
            self._memory_store.pop(thread_id, None)
            self._memory_ts.pop(thread_id, None)
            return None
        self._memory_store.move_to_end(thread_id)
        return snap

    def _mem_set(self, thread_id: str, snap: StateSnapshot) -> None:
        """内存降级写入：LRU 淘汰最旧条目（P2-15）。"""
        self._memory_store[thread_id] = snap
        self._memory_ts[thread_id] = time.monotonic()
        self._memory_store.move_to_end(thread_id)
        while len(self._memory_store) > _MEMORY_STORE_MAX:
            old_key, _ = self._memory_store.popitem(last=False)
            self._memory_ts.pop(old_key, None)

    # --- 读 ---

    async def snapshot(self, thread_id: str, *, strict: bool = False) -> StateSnapshot:
        """读当前快照；不存在返回零版快照（version=0，四组空 dict）。

        - `strict=True`：Redis 异常（含载荷损坏）直接上抛，给 apply 的
          用药任务路径用——那里必须区分「读不到」和「读到旧版」（P2-13）；
        - `strict=False`（默认）：Redis 异常降级内存，保持 Day 4 行为；
        - Redis 读取成功后与内存降级版本对账（P2-14）：内存版本更新（写故障
          窗口内积累的事实）优先返回并回写 Redis，防止恢复后被旧数据覆盖。
        """
        if self._redis is not None:
            try:
                data = await self._redis.get(self._key_for(thread_id))
                if data is None:
                    snap = self._empty_snapshot(thread_id)
                else:
                    raw = data.decode("utf-8") if isinstance(data, bytes) else data
                    snap = StateSnapshot.from_dict(json.loads(raw))
            except (RedisError, OSError, json.JSONDecodeError) as exc:
                if strict:
                    raise
                logger.warning(
                    "state_center_redis_read_fallback",
                    extra={"thread_id": thread_id, "error_type": type(exc).__name__},
                )
                if self._allow_memory_fallback:
                    return self._mem_get(thread_id) or self._empty_snapshot(thread_id)
                raise
            return await self._reconcile_memory(thread_id, snap)
        return self._mem_get(thread_id) or self._empty_snapshot(thread_id)

    async def _reconcile_memory(self, thread_id: str, redis_snap: StateSnapshot) -> StateSnapshot:
        """P2-14：Redis 恢复后对账——内存降级版本更新时优先返回并回写 Redis。"""
        if not self._allow_memory_fallback:
            return redis_snap
        mem = self._mem_get(thread_id)
        if mem is None or mem.version <= redis_snap.version:
            return redis_snap
        logger.info(
            "state_center_memory_newer_writeback",
            extra={
                "thread_id": thread_id,
                "memory_version": mem.version,
                "redis_version": redis_snap.version,
            },
        )
        try:
            assert self._redis is not None  # 仅在 snapshot 的 Redis 路径调用
            await self._redis.set(
                self._key_for(thread_id),
                json.dumps(mem.to_dict(), ensure_ascii=False),
                ex=self._ttl,
            )
        except (RedisError, OSError) as exc:
            # 回写失败不阻塞读取：内存版仍是事实来源，下次 apply 再尝试
            logger.warning(
                "state_center_memory_writeback_failed",
                extra={"thread_id": thread_id, "error_type": type(exc).__name__},
            )
        return mem

    # --- 写（CAS）---

    async def apply(
        self,
        *,
        thread_id: str,
        expected_version: int,
        events: list[StateEvent],
        is_medication_task: bool = False,
    ) -> StateSnapshot:
        """原子 apply：读快照 → 校验 expected_version → 依次 apply 事件 → CAS 写回。

        - expected_version 不符 → 抛 StateVersionConflict，**不覆盖**
        - Redis 不可用 + 用药任务 → 抛 StateUnavailable（读阶段就走 strict，
          不会先降级成空快照再撞 StateVersionConflict，P2-13）
        - Redis 不可用 + 普通闲聊 → 内存字典降级
        - 写失败时先把已算出的新事实落内存降级再抛错（P3-29：不全轮回滚；
          `turn_failed` 事件兜底由回合结算侧在捕获异常后写 conversation_events）
        """
        if not events:
            return await self._snapshot_with_fallback(thread_id, is_medication_task)

        try:
            # 用药任务/禁降级模式用 strict 读：Redis 故障必须以 StateUnavailable 语义上抛
            current = await self.snapshot(
                thread_id, strict=is_medication_task or not self._allow_memory_fallback
            )
        except (RedisError, OSError) as exc:
            if is_medication_task:
                raise StateUnavailable(thread_id) from exc
            if self._allow_memory_fallback:
                logger.warning(
                    "state_center_redis_read_fallback_apply",
                    extra={"thread_id": thread_id, "error_type": type(exc).__name__},
                )
                current = self._empty_snapshot(thread_id)
            else:
                raise

        if current.version != expected_version:
            raise StateVersionConflict(thread_id, expected_version, current.version)

        new_snap = current
        for event in events:
            new_snap = _apply_event(new_snap, event)

        # 原子写回
        try:
            if self._redis is not None:
                return await self._apply_via_redis(thread_id, expected_version, new_snap)
            self._mem_set(thread_id, new_snap)
            return new_snap
        except (RedisError, OSError) as exc:
            logger.warning(
                "state_center_redis_write_fallback",
                extra={"thread_id": thread_id, "error_type": type(exc).__name__},
            )
            # P3-29：无论何种任务，先把已算出的新事实降级落内存（恢复后由
            # _reconcile_memory 回写 Redis），再按任务类型决定异常语义
            self._mem_set(thread_id, new_snap)
            if is_medication_task:
                raise StateUnavailable(thread_id) from exc
            if self._allow_memory_fallback:
                return new_snap
            raise

    async def _snapshot_with_fallback(
        self, thread_id: str, is_medication_task: bool
    ) -> StateSnapshot:
        """空事件时的安全快照读取（内部用）。"""
        try:
            return await self.snapshot(
                thread_id, strict=is_medication_task or not self._allow_memory_fallback
            )
        except (RedisError, OSError) as exc:
            if is_medication_task:
                raise StateUnavailable(thread_id) from exc
            if self._allow_memory_fallback:
                return self._mem_get(thread_id) or self._empty_snapshot(thread_id)
            raise

    # --- 清 ---

    async def clear(self, thread_id: str) -> None:
        """删除会话状态（会话结束 / 重置）。"""
        if self._redis is not None:
            try:
                await self._redis.delete(self._key_for(thread_id))
            except (RedisError, OSError) as exc:
                logger.warning(
                    "state_center_redis_clear_fallback",
                    extra={"thread_id": thread_id, "error_type": type(exc).__name__},
                )
        self._memory_store.pop(thread_id, None)
        self._memory_ts.pop(thread_id, None)

    # --- 内部 ---

    async def _apply_via_redis(
        self,
        thread_id: str,
        expected_version: int,
        new_snap: StateSnapshot,
    ) -> StateSnapshot:
        assert self._redis is not None
        new_state_json = json.dumps(new_snap.to_dict(), ensure_ascii=False)
        result = await self._redis.eval(
            CAS_LUA,
            1,  # numkeys
            self._key_for(thread_id),
            str(expected_version),
            new_state_json,
            str(self._ttl),
            str(new_snap.version),
        )
        # Redis eval 返回 int；-1 = 版本冲突，-2 = 存储损坏
        if result == -1:
            # 重读拿真实版本
            current = await self.snapshot(thread_id)
            raise StateVersionConflict(thread_id, expected_version, current.version)
        if result == -2:
            # P2-15：存储损坏不再无条件覆盖写——按冲突语义上抛，由调用方
            # 决定重试或 clear() 重建；actual=-2 即「存储载荷损坏」标记。
            # 修复前的无条件 SET 会把损坏原因抹掉，复盘时无法区分「谁写坏了」。
            logger.error(
                "state_center_corrupted_state_conflict",
                extra={"thread_id": thread_id, "expected_version": expected_version},
            )
            raise StateVersionConflict(thread_id, expected_version, -2)
        # result == new_snap.version（CAS 成功）
        return new_snap

    @staticmethod
    def _empty_snapshot(thread_id: str) -> StateSnapshot:
        return StateSnapshot(
            thread_id=thread_id,
            version=0,
            identity={},
            input={"status": "idle", "active_turn_id": None, "asr_partial": ""},
            output={
                "status": "idle",
                "response_id": None,
                "generation_epoch": 0,
                "last_acked_segment": -1,
            },
            media={"status": "idle", "media_id": None, "position_ms": 0},
            task={
                "active_task_id": None,
                "kind": None,
                "waiting_for": None,
            },
        )


__all__ = [
    "CAS_LUA",
    "RedisClient",
    "StateCenter",
    "StateEvent",
    "StateSnapshot",
    "StateUnavailable",
    "StateVersionConflict",
    "_apply_event",
]
