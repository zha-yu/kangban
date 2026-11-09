"""SessionCoordinator · 会话提交协调器（N05，contracts.md §5.2 配套）。

每 `thread_id` 一个 `asyncio.Lock`，保证 apply 串行提交：

1. 外部只暴露 `submit(thread_id, events, is_med)` —— 内部按"读快照 → 校验版本 → apply → 追加事件"顺序执行
2. 版本冲突自动重试（最多 3 次），避免调用方手动循环
3. `submit` 内部**不**自己管理 per-thread lock 清理 —— 进程级规模可控

与 StateCenter 的关系：Coordinator 是 StateCenter 的"串行化包装"，StateCenter 本身仍可被直接调用（无锁环境、脚本等）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.services.state_service import (
    StateCenter,
    StateEvent,
    StateSnapshot,
    StateUnavailable,
    StateVersionConflict,
)

if TYPE_CHECKING:  # pragma: no cover - 运行时循环 import 保护
    pass

logger = logging.getLogger("app.agents.session_coordinator")

_MAX_RETRIES = 3


class SessionCoordinator:
    """每 thread 串行提交 StateEvent 列表。"""

    def __init__(self, state_center: StateCenter, *, max_retries: int = _MAX_RETRIES) -> None:
        self._center = state_center
        self._max_retries = max_retries
        self._locks: dict[str, asyncio.Lock] = {}
        # 进程内等待队列长度（可观测）
        self._submit_count = 0

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[thread_id] = lock
        return lock

    async def snapshot(self, thread_id: str) -> StateSnapshot:
        """读快照 —— 走同一把锁，保证与 submit 的读写一致。"""
        async with self._lock_for(thread_id):
            return await self._center.snapshot(thread_id)

    async def submit(
        self,
        thread_id: str,
        events: list[StateEvent],
        *,
        is_medication_task: bool = False,
    ) -> StateSnapshot:
        """串行提交事件列表；版本冲突自动重试。"""
        self._submit_count += 1
        lock = self._lock_for(thread_id)

        async with lock:
            last_conflict: StateVersionConflict | None = None
            for attempt in range(self._max_retries + 1):
                try:
                    snap = await self._center.snapshot(thread_id)
                    return await self._center.apply(
                        thread_id=thread_id,
                        expected_version=snap.version,
                        events=events,
                        is_medication_task=is_medication_task,
                    )
                except StateVersionConflict as exc:
                    last_conflict = exc
                    if attempt >= self._max_retries:
                        break
                    logger.info(
                        "session_coordinator_retry",
                        extra={
                            "thread_id": thread_id,
                            "attempt": attempt + 1,
                            "expected": exc.expected,
                            "actual": exc.actual,
                        },
                    )
                    continue
                except StateUnavailable:
                    raise
            assert last_conflict is not None
            # 超过重试次数 —— 把真实冲突往上抛，让调用方决定（通常是记录日志 + 降级）
            logger.warning(
                "session_coordinator_version_conflict_exhausted",
                extra={
                    "thread_id": thread_id,
                    "expected": last_conflict.expected,
                    "actual": last_conflict.actual,
                    "retries": self._max_retries,
                },
            )
            raise last_conflict from None

    async def clear(self, thread_id: str) -> None:
        """清会话 —— 走同一把锁。"""
        async with self._lock_for(thread_id):
            await self._center.clear(thread_id)
            self._locks.pop(thread_id, None)

    @property
    def active_threads(self) -> int:
        return len(self._locks)


__all__ = ["SessionCoordinator"]
