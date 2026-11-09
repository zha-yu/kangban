"""房间状态聚合：`rooms.status` 的唯一计算口径（计划 v1.5 §5）。

`rooms.status` 是只读派生值，由该房间全部床位状态聚合得出；管理端不提供手工改房间状态。
本模块刻意做成**纯函数**：不查库、不写库，`org_service`（增删改床位后重算）与
`elder_service.assign_bed`（E19 入床/换床后同步）共用同一份判定，避免两套口径。

五分支（顺序即优先级）：
1. 无床 → `available`（空房间可分配）；
2. 全部 `maintenance` / `disabled` → `maintenance`（整房不可用，不区分维修与停用）；
3. 全部 `occupied` → `full`；
4. 有 `occupied`，或存在非 `available` 的床（如维修/停用）→ `partial`；
5. 其余（全部 `available`）→ `available`。
"""

from __future__ import annotations

from collections.abc import Iterable

#: 视为「整房维护」的床位状态集合（维修与停用都不接收新入住）
_UNAVAILABLE_STATUSES: frozenset[str] = frozenset({"maintenance", "disabled"})


def aggregate_room_status(bed_statuses: Iterable[str]) -> str:
    """按床位状态聚合出房间状态；入参只读，不修改任何对象。"""
    statuses = list(bed_statuses)
    if not statuses:
        return "available"
    if all(status in _UNAVAILABLE_STATUSES for status in statuses):
        return "maintenance"
    if all(status == "occupied" for status in statuses):
        return "full"
    if any(status == "occupied" or status != "available" for status in statuses):
        return "partial"
    return "available"


__all__ = ["aggregate_room_status"]
