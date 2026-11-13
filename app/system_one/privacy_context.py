"""privacy_scope 本轮传递通道 + 双人间判定（Day 10 · B3，F23 消费接线）。

**为什么存在这个模块**：`privacy_scope` 信号在 Router（`decision.py`）产出，消费点在
Agent（`companion.py`）的上下文组装——中间隔着 turn_service（他人写域）。给
`agent.run` 加参数要连改三处且动他人文件；`decide()` 与 `agent.run` 在**同一个
asyncio task 里串行 await**，ContextVar 天然跨这两个 await 可见，每轮 `decide()`
入口强制重置，不跨轮残留。

**纪律**：
- 通道只传信号值（"personal"/"shared"/"none"/None），不传文本、不传 PII；
- `None` 语义是「这轮没问出来」（shadow 超时/熔断/短文本跳过）——消费方必须把
  `None` 与 `"none"` 区分对待：前者不收敛也不编造，后者是「确认无隐私内容」；
- 双人间判定（`is_shared_room_for_elder`）是**按需惰性查询**：只在信号为
  `personal` 且身份非 generic 时才查一次 DB——路由 200ms 热路径上不做任何查询。
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from typing import Final, Protocol

logger = logging.getLogger("app.system_one.privacy_context")

#: 合法信号值（与 `schemas.PrivacyScope` 三值一致；None 表示本轮无信号）。
_VALID_SCOPES: Final[frozenset[str]] = frozenset({"personal", "shared", "none"})

_privacy_scope_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "privacy_scope", default=None
)


def set_current_privacy_scope(value: str | None) -> None:
    """写入本轮信号。非法值一律落 `None`（宁可无信号，不传可疑值给消费方）。"""
    if value is not None and value not in _VALID_SCOPES:
        logger.warning("privacy_scope_invalid_value_dropped value_type=%s", type(value).__name__)
        value = None
    _privacy_scope_var.set(value)


def current_privacy_scope() -> str | None:
    """读本轮信号；从未设置/已重置时返回 `None`（≠ "none"）。"""
    return _privacy_scope_var.get()


class SharedRoomProbe(Protocol):
    """双人间判定的注入缝：`elder_id` → 该长者当前房间是否 ≥2 人在住。"""

    async def __call__(self, *, elder_id: str) -> bool: ...


async def is_shared_room_for_elder(*, elder_id: str) -> bool:
    """真身判定：长者的 bed → room → 同房间在住床位数 > 1 即双人间。

    数据口径与声纹候选集一致（`device_service`：候选集只来自床位表）：
    `beds.room_id` 由 `beds.elder_id` 反查，在住 = `beds.elder_id IS NOT NULL`。
    查不到（未入住/无床位/身份异常）→ `False`：单人或未知**不收敛**，保守放行，
    不因为查不到房间就把公开回答压成含蓄话术。
    """
    from sqlalchemy import func, select

    from app.db import get_session
    from app.models.organization import Bed

    try:
        elder_uuid = uuid.UUID(str(elder_id))
    except ValueError:
        logger.warning("privacy_probe_bad_elder_id elder_id_len=%d", len(str(elder_id)))
        return False

    async with get_session() as session:
        room_id = (
            await session.execute(select(Bed.room_id).where(Bed.elder_id == elder_uuid))
        ).scalar_one_or_none()
        if room_id is None:
            return False
        occupied = (
            await session.execute(
                select(func.count(Bed.bed_id)).where(
                    Bed.room_id == room_id,
                    Bed.elder_id.is_not(None),
                )
            )
        ).scalar_one()
    return occupied > 1


__all__ = [
    "SharedRoomProbe",
    "current_privacy_scope",
    "is_shared_room_for_elder",
    "set_current_privacy_scope",
]
