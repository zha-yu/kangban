"""`app.services.org_status.aggregate_room_status` 五分支单测（计划 v1.5 §7）。

纯函数，不依赖数据库；每个用例只断言「一组床位状态 → 一个房间状态」。
"""

from __future__ import annotations

import pytest

from app.services.org_status import aggregate_room_status


@pytest.mark.parametrize(
    ("bed_statuses", "expected"),
    [
        # 无床 → 可分配
        ([], "available"),
        # 全可用
        (["available"], "available"),
        (["available", "available"], "available"),
        # 全维护/停用（可混合）→ 整房维护
        (["maintenance"], "maintenance"),
        (["disabled"], "maintenance"),
        (["maintenance", "disabled"], "maintenance"),
        # 全占用 → 满房
        (["occupied"], "full"),
        (["occupied", "occupied"], "full"),
        # 部分占用 / 存在非可用床 → partial
        (["occupied", "available"], "partial"),
        (["occupied", "maintenance"], "partial"),
        (["occupied", "disabled"], "partial"),
        (["available", "maintenance"], "partial"),
        (["available", "disabled"], "partial"),
    ],
)
def test_aggregate_room_status_branches(bed_statuses: list[str], expected: str) -> None:
    assert aggregate_room_status(bed_statuses) == expected


def test_aggregate_room_status_accepts_any_iterable() -> None:
    """入参只读迭代即可（服务层传的是 SQLAlchemy ScalarResult.all() 的 list）。"""
    assert aggregate_room_status(iter(["occupied", "available"])) == "partial"
