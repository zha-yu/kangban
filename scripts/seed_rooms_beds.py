#!/usr/bin/env python
"""房间 / 床位管理 seed（幂等，只写 rooms / beds）。

用途：本地或演示库补房间与床位，**不碰 personas / elders / devices**。
默认 seed：``101:A,B``、``102:A,B``、``103:A,B``（每间双人间）。

规则
----
1. 房间按 ``room_no`` 查；存在就复用，不存在用 uuid5 稳定 ID 新建。
2. 床位按 ``(room_id, bed_no)`` 查；已存在**绝不覆盖**，占用/维护中的床保持原样。
3. ``rooms.status`` 由 beds 聚合：无床 available；全占用 full；有占用或有维护 partial；
   全部维护 maintenance；房间本身是 maintenance 时不动。
4. 脚本只增不删，可重复执行。

用法
----
    uv run python scripts/seed_rooms_beds.py --dry-run
    uv run python scripts/seed_rooms_beds.py
    uv run python scripts/seed_rooms_beds.py --room "201:A,B" --room "202:A,B"

退出码：0 成功；1 连不上库 / 写库失败（先确认 ``alembic upgrade head`` 与数据库已启动）；
2 参数非法。
"""

# ruff: noqa: E402
#
# 为什么整文件豁免 E402：按路径执行 ``scripts/seed_rooms_beds.py`` 时，Python 只把
# ``scripts/`` 放进 sys.path[0]，仓库根不在其中，``import app...`` 会 ModuleNotFoundError。
# 因此下面的 sys.path 引导必须排在所有 app import 之前（与 seed_personas.py 同款）。

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# 仓库根补进 sys.path：scripts/seed_rooms_beds.py -> scripts -> 根
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import engine, session_factory
from app.models.organization import Bed, Room

_DEFAULT_ROOMS: tuple[str, ...] = ("101:A,B", "102:A,B", "103:A,B")
_ROOM_STATUS_AVAILABLE = "available"
_ROOM_STATUS_PARTIAL = "partial"
_ROOM_STATUS_FULL = "full"
_ROOM_STATUS_MAINTENANCE = "maintenance"
_BED_STATUS_AVAILABLE = "available"
_BED_STATUS_OCCUPIED = "occupied"
_BED_STATUS_MAINTENANCE = "maintenance"


@dataclass(frozen=True)
class RoomSpec:
    """一条 ``房号:床号1,床号2`` 配置。"""

    room_no: str
    bed_nos: tuple[str, ...]


def _parse_room_spec(raw: str) -> RoomSpec:
    """解析 ``101:A,B``；非法格式抛 ValueError，由 argparse 转成退出码 2。"""
    if ":" not in raw:
        raise ValueError(f"缺少冒号，应为 房号:床号1,床号2：{raw!r}")
    room_no, bed_part = raw.split(":", 1)
    room_no = room_no.strip()
    bed_nos = tuple(bed.strip() for bed in bed_part.split(",") if bed.strip())
    if not room_no:
        raise ValueError(f"房号为空：{raw!r}")
    if not bed_nos:
        raise ValueError(f"至少给一个床号：{raw!r}")
    if len(set(bed_nos)) != len(bed_nos):
        raise ValueError(f"同一房间床号重复：{raw!r}")
    return RoomSpec(room_no=room_no, bed_nos=bed_nos)


def _stable_room_id(room_no: str) -> uuid.UUID:
    """新建房间用稳定 UUID，重复 seed 不会产生第二个同房号房间。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"silvermate/room/{room_no}")


def _aggregate_room_status(bed_statuses: Sequence[str], current: str) -> str:
    """按 beds 聚合房间状态；房间本身 maintenance 时保持不动。"""
    if current == _ROOM_STATUS_MAINTENANCE:
        return current
    if not bed_statuses:
        return _ROOM_STATUS_AVAILABLE
    if all(status == _BED_STATUS_MAINTENANCE for status in bed_statuses):
        return _ROOM_STATUS_MAINTENANCE
    occupied = sum(1 for status in bed_statuses if status == _BED_STATUS_OCCUPIED)
    if occupied == len(bed_statuses):
        return _ROOM_STATUS_FULL
    if occupied > 0 or any(status == _BED_STATUS_MAINTENANCE for status in bed_statuses):
        return _ROOM_STATUS_PARTIAL
    return _ROOM_STATUS_AVAILABLE


async def _seed_room(
    session: AsyncSession,
    spec: RoomSpec,
    *,
    dry_run: bool,
) -> str:
    """补一间房及其缺失床位，返回一行可打印摘要。"""
    rooms = (
        (
            await session.execute(
                select(Room).where(Room.room_no == spec.room_no).order_by(Room.room_id)
            )
        )
        .scalars()
        .all()
    )
    if len(rooms) > 1:
        print(f"  ! 房号 {spec.room_no} 已存在 {len(rooms)} 条，使用第一条（不合并）")
    room = rooms[0] if rooms else None

    created_room = room is None
    if room is None:
        room = Room(
            room_id=_stable_room_id(spec.room_no),
            room_no=spec.room_no,
            status=_ROOM_STATUS_AVAILABLE,
        )
        session.add(room)
        if not dry_run:
            await session.flush()

    existing_beds = (
        (await session.execute(select(Bed).where(Bed.room_id == room.room_id))).scalars().all()
    )
    existing_by_no = {bed.bed_no: bed for bed in existing_beds}
    added_beds: list[str] = []
    for bed_no in spec.bed_nos:
        if bed_no in existing_by_no:
            continue
        session.add(Bed(room_id=room.room_id, bed_no=bed_no, status=_BED_STATUS_AVAILABLE))
        added_beds.append(bed_no)

    if dry_run:
        predicted = [bed.status for bed in existing_beds] + [_BED_STATUS_AVAILABLE] * len(
            added_beds
        )
    else:
        await session.flush()
        predicted = (
            (await session.execute(select(Bed.status).where(Bed.room_id == room.room_id)))
            .scalars()
            .all()
        )

    new_status = _aggregate_room_status(predicted, room.status)
    if created_room:
        status_note = f"status {new_status}"
    elif new_status == room.status:
        status_note = "status unchanged"
    else:
        status_note = f"status {room.status} -> {new_status}"
    if not dry_run and new_status != room.status:
        room.status = new_status

    parts = [
        "new room" if created_room else "existing room",
        f"beds +{','.join(added_beds)}" if added_beds else "beds +0",
        status_note,
    ]
    return f"{spec.room_no:<6} " + " | ".join(parts)


async def _run(specs: Sequence[RoomSpec], *, dry_run: bool) -> int:
    """执行 seed；成功返回 0，数据库错误返回 1。"""
    try:
        async with session_factory() as session:
            for spec in specs:
                print(await _seed_room(session, spec, dry_run=dry_run))
            if dry_run:
                await session.rollback()
            else:
                await session.commit()
    except SQLAlchemyError as exc:
        print(f"seed 失败：{exc}", file=sys.stderr)
        print("请确认 postgres 已启动，并已执行 `uv run alembic upgrade head`。", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print("dry-run：未写库（去掉 --dry-run 执行）" if dry_run else "seed 完成。")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="房间/床位 seed（幂等，只写 rooms/beds）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  uv run python scripts/seed_rooms_beds.py --dry-run\n"
            "  uv run python scripts/seed_rooms_beds.py\n"
            '  uv run python scripts/seed_rooms_beds.py --room "201:A,B" --room "202:A,B"'
        ),
    )
    parser.add_argument(
        "--room",
        action="append",
        metavar="ROOM:A,B",
        help="房号与床号；可重复。不传则使用默认 101/102/103 双人间",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    raw_specs = args.room if args.room else list(_DEFAULT_ROOMS)
    try:
        specs = [_parse_room_spec(raw) for raw in raw_specs]
    except ValueError as exc:
        parser.error(str(exc))  # 退出码 2
    return asyncio.run(_run(specs, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
