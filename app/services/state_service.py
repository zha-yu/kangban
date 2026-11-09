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
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from redis.exceptions import RedisError

logger = logging.getLogger("app.services.state_service")

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
        return cls(
            thread_id=data["thread_id"],
            version=int(data["version"]),
            identity=data.get("identity", {}),
            input=data.get("input", {}),
            output=data.get("output", {}),
            media=data.get("media", {}),
            task=data.get("task", {}),
        )


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
        self._memory_store: dict[str, StateSnapshot] = {}

    def _key_for(self, thread_id: str) -> str:
        return f"{self._key_prefix}:session:{thread_id}:state"

    # --- 读 ---

    async def snapshot(self, thread_id: str) -> StateSnapshot:
        """读当前快照；不存在返回零版快照（version=0，四组空 dict）。"""
        if self._redis is not None:
            try:
                data = await self._redis.get(self._key_for(thread_id))
                if data is None:
                    return self._empty_snapshot(thread_id)
                raw = data.decode("utf-8") if isinstance(data, bytes) else data
                return StateSnapshot.from_dict(json.loads(raw))
            except (RedisError, OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "state_center_redis_read_fallback",
                    extra={"thread_id": thread_id, "error_type": type(exc).__name__},
                )
                if self._allow_memory_fallback:
                    return self._memory_store.get(thread_id, self._empty_snapshot(thread_id))
                raise
        return self._memory_store.get(thread_id, self._empty_snapshot(thread_id))

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
        - Redis 不可用 + 用药任务 → 抛 StateUnavailable
        - Redis 不可用 + 普通闲聊 → 内存字典降级
        """
        if not events:
            return await self._snapshot_with_fallback(thread_id, is_medication_task)

        try:
            current = await self.snapshot(thread_id)
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
            self._memory_store[thread_id] = new_snap
            return new_snap
        except (RedisError, OSError) as exc:
            logger.warning(
                "state_center_redis_write_fallback",
                extra={"thread_id": thread_id, "error_type": type(exc).__name__},
            )
            if is_medication_task:
                raise StateUnavailable(thread_id) from exc
            if self._allow_memory_fallback:
                self._memory_store[thread_id] = new_snap
                return new_snap
            raise

    async def _snapshot_with_fallback(
        self, thread_id: str, is_medication_task: bool
    ) -> StateSnapshot:
        """空事件时的安全快照读取（内部用）。"""
        try:
            return await self.snapshot(thread_id)
        except (RedisError, OSError) as exc:
            if is_medication_task:
                raise StateUnavailable(thread_id) from exc
            if self._allow_memory_fallback:
                return self._memory_store.get(thread_id, self._empty_snapshot(thread_id))
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
            # 存储损坏 —— 覆盖写（罕见）
            logger.error("state_center_corrupted_state_overwritten", extra={"thread_id": thread_id})
            await self._redis.set(self._key_for(thread_id), new_state_json, ex=self._ttl)
            return new_snap
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
