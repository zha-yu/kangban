#!/usr/bin/env python
"""长者-房间/床位对账脚本（本地/演示环境，幂等）。

本次要解决的问题
----------------
1. 护工「负责长者」看不到新建长者：E7 对 care_worker 只返回
   ``elder_staff_assignments`` 里分配给自己的长者。脚本把有效长者分配给指定护工。
2. 房间只有 101、床位不足：调用 ``scripts/seed_rooms_beds.py`` 补 201/202 房间与床位。
3. 向导多次提交产生重复长者：保留「有床 + 有设备」的那条，删除其余**无任何引用**的重复。

边界（与 E19 / E9 模型一致）
--------------------------
- 一个长者同时只能占一张床（E19 是换床语义）；本脚本**不移动**长者，201/202 仅作为可用房配置。
- 设备与长者 1:1；本脚本**不换设备**。已有设备的一次性密钥入库时只存哈希，无法重新取出；
  如需完整的新 ID+密钥，必须走「解绑旧设备 → 目标房新建 E16 → E9 绑定」，不在本脚本范围。
- 只删无引用重复：beds / devices / elder_guardians / elder_staff_assignments /
  voiceprints / conversation_threads 任一有引用就跳过并报告。

用法
----
    # 预演：只打印，不写库、不删除
    uv run python scripts/reconcile_elder_room_bindings.py --dry-run

    # 执行：补 201/202、分配给护工、删除两条无引用重复（先写 JSON 备份）
    uv run python scripts/reconcile_elder_room_bindings.py --delete-duplicates

    # 指定其他长者/护工/房间
    uv run python scripts/reconcile_elder_room_bindings.py \
      --elder-name "吴三桂" --care-worker-phone 13700000003 \
      --room "201:A,B" --room "202:A,B" --delete-duplicates

退出码：0 成功；1 数据库/接口/子进程错误；2 参数或数据冲突。
"""

# ruff: noqa: E402
#
# 为什么整文件豁免 E402：按路径执行脚本时 Python 只把 scripts/ 放进 sys.path[0]，
# 仓库根不在其中，``import app...`` 会 ModuleNotFoundError。sys.path 引导必须排在所有
# app import 之前（与 seed_rooms_beds.py / seed_personas.py 同款）。

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.db import engine, session_factory
from app.models.organization import Bed, Device, Elder, Room
from app.models.relation import ElderStaffAssignment
from app.models.staff import StaffAccount

_REF_TABLES: tuple[str, ...] = (
    "beds",
    "devices",
    "elder_guardians",
    "elder_staff_assignments",
    "voiceprints",
    "conversation_threads",
)
_DEFAULT_ROOMS: tuple[str, ...] = ("201:A,B", "202:A,B")
_DEFAULT_ELDER_NAME = "吴三桂"
_DEFAULT_CARE_WORKER_PHONE = "13700000003"
_CARE_RELATION = "primary_care"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="长者-房间/床位对账（补房、分配护工、清理无引用重复）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  uv run python scripts/reconcile_elder_room_bindings.py --dry-run\n"
            "  uv run python scripts/reconcile_elder_room_bindings.py --delete-duplicates"
        ),
    )
    parser.add_argument("--elder-name", default=_DEFAULT_ELDER_NAME, help="长者姓名")
    parser.add_argument("--elder-id", help="指定长者 UUID；不传则自动选「有床有设备」的一条")
    parser.add_argument(
        "--care-worker-phone",
        default=_DEFAULT_CARE_WORKER_PHONE,
        help="分配给该护工；默认 13700000003（界面护工）",
    )
    parser.add_argument(
        "--room",
        action="append",
        metavar="ROOM:A,B",
        help="要补的房间；不传则默认 201:A,B / 202:A,B",
    )
    parser.add_argument(
        "--delete-duplicates",
        action="store_true",
        help="删除无引用的同名重复长者；删除前写 JSON 备份",
    )
    parser.add_argument("--backup-dir", help="备份目录；默认系统临时目录下 silvermate_backups")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库/不删除")
    parser.add_argument(
        "--api-base",
        default="http://127.0.0.1:8000",
        help="用于可选 E7 复核的后端地址",
    )
    parser.add_argument(
        "--care-worker-password",
        default=os.environ.get("SILVERMATE_CARE_PASSWORD"),
        help="护工口令；不传则读 SILVERMATE_CARE_PASSWORD，未设置时跳过 E7 复核",
    )
    return parser


async def _reference_counts(session: Any, elder_id: uuid.UUID) -> dict[str, int]:
    """统计各表对该长者的引用；用于删除前的安全闸门。"""
    counts: dict[str, int] = {}
    for table in _REF_TABLES:
        count = (
            await session.execute(
                # 表名来自固定白名单 _REF_TABLES，不接受外部输入；值走参数绑定。
                text(f"SELECT count(*) FROM {table} WHERE elder_id = :elder_id"),  # noqa: S608
                {"elder_id": elder_id},
            )
        ).scalar_one()
        counts[table] = int(count)
    return counts


async def _resolve_elder(session: Any, name: str, elder_id: str | None) -> Elder:
    """按 UUID 或「有床有设备」规则选唯一有效长者；多条候选时要求显式 --elder-id。"""
    if elder_id:
        try:
            target_id = uuid.UUID(elder_id)
        except ValueError as exc:
            raise ValueError(f"--elder-id 不是合法 UUID：{elder_id}") from exc
        elder = await session.get(Elder, target_id)
        if elder is None or elder.name != name:
            raise ValueError(f"找不到姓名为 {name!r} 的长者 {target_id}")
        return elder

    candidates = (await session.execute(select(Elder).where(Elder.name == name))).scalars().all()
    if not candidates:
        raise ValueError(f"找不到姓名为 {name!r} 的长者")

    scored: list[tuple[Elder, int, int]] = []
    for candidate in candidates:
        bed_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Bed).where(Bed.elder_id == candidate.elder_id)
                )
            ).scalar_one()
        )
        device_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Device)
                    .where(Device.elder_id == candidate.elder_id)
                )
            ).scalar_one()
        )
        scored.append((candidate, bed_count, device_count))

    preferred = [item for item, bed, device in scored if bed > 0 and device > 0]
    if len(preferred) == 1:
        return preferred[0]
    if len(preferred) > 1:
        raise ValueError("多条同名长者都有床和设备，请用 --elder-id 指定保留哪一条")
    raise ValueError("没有「同时有床和设备」的同名长者，请用 --elder-id 指定")


async def _assign_care_worker(session: Any, elder: Elder, phone: str, dry_run: bool) -> str:
    staff = (
        await session.execute(select(StaffAccount).where(StaffAccount.phone == phone))
    ).scalar_one_or_none()
    if staff is None:
        raise ValueError(f"找不到手机号为 {phone} 的员工账号")
    if staff.role != "care_worker":
        raise ValueError(f"账号 {phone} 角色是 {staff.role}，不是 care_worker")

    existing = (
        await session.execute(
            select(ElderStaffAssignment).where(
                ElderStaffAssignment.elder_id == elder.elder_id,
                ElderStaffAssignment.staff_id == staff.staff_id,
                ElderStaffAssignment.relation == _CARE_RELATION,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return f"已存在：{staff.name} -> {elder.name}"
    if not dry_run:
        session.add(
            ElderStaffAssignment(
                elder_id=elder.elder_id,
                staff_id=staff.staff_id,
                relation=_CARE_RELATION,
            )
        )
    return f"新增：{staff.name} -> {elder.name}"


async def _delete_duplicates(
    session: Any,
    elder: Elder,
    *,
    delete: bool,
    dry_run: bool,
    backup_dir: Path,
) -> list[str]:
    """删除同名但无任何引用的重复长者；有引用的一律跳过。"""
    duplicates = (
        (
            await session.execute(
                select(Elder).where(Elder.name == elder.name, Elder.elder_id != elder.elder_id)
            )
        )
        .scalars()
        .all()
    )
    if not duplicates:
        return ["无重复"]

    rows: list[dict[str, Any]] = []
    deletable: list[Elder] = []
    skipped: list[str] = []
    for duplicate in duplicates:
        counts = await _reference_counts(session, duplicate.elder_id)
        rows.append(
            {
                "elder_id": str(duplicate.elder_id),
                "name": duplicate.name,
                "birth_date": str(duplicate.birth_date) if duplicate.birth_date else None,
                "gender": duplicate.gender,
                "persona_id": str(duplicate.persona_id) if duplicate.persona_id else None,
                "status": duplicate.status,
                "session_seq": duplicate.session_seq,
                "created_at": duplicate.created_at.isoformat(),
                "references": counts,
            }
        )
        if any(counts.values()):
            skipped.append(f"{duplicate.elder_id}（有引用，跳过）")
        else:
            deletable.append(duplicate)

    if not delete or dry_run:
        action = "将删除" if delete else "可删除（未传 --delete-duplicates）"
        messages = [f"{action} {len(deletable)} 条，备份预览 {len(rows)} 条"]
        messages.extend(skipped)
        return messages

    await asyncio.to_thread(backup_dir.mkdir, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"duplicate_elders_{stamp}.json"
    await asyncio.to_thread(
        backup_path.write_text,
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for duplicate in deletable:
        await session.delete(duplicate)
    messages = [f"已删除 {len(deletable)} 条无引用重复；备份：{backup_path}"]
    messages.extend(skipped)
    return messages


async def _verify(
    session: Any,
    elder_id: uuid.UUID,
    elder_name: str,
    *,
    rooms: Sequence[str],
    care_worker_phone: str,
) -> list[str]:
    """核对床位、设备、护工范围、201/202 房间床位。"""
    lines: list[str] = [f"长者：{elder_name} ({elder_id})"]

    bed_rows = (
        await session.execute(
            select(Bed, Room)
            .join(Room, Room.room_id == Bed.room_id)
            .where(Bed.elder_id == elder_id)
        )
    ).all()
    if bed_rows:
        for bed, room in bed_rows:
            lines.append(f"床位：{room.room_no}-{bed.bed_no} ({bed.status})")
    else:
        lines.append("床位：无（长者未入床）")

    devices = (
        (await session.execute(select(Device).where(Device.elder_id == elder_id))).scalars().all()
    )
    if devices:
        for device in devices:
            lines.append(
                f"设备：{device.device_id} room_id={device.room_id} status={device.status}"
            )
    else:
        lines.append("设备：无（未绑定）")

    staff = (
        await session.execute(select(StaffAccount).where(StaffAccount.phone == care_worker_phone))
    ).scalar_one_or_none()
    if staff is not None:
        assigned = (
            await session.execute(
                select(ElderStaffAssignment).where(
                    ElderStaffAssignment.elder_id == elder_id,
                    ElderStaffAssignment.staff_id == staff.staff_id,
                    ElderStaffAssignment.relation == _CARE_RELATION,
                )
            )
        ).scalar_one_or_none()
        lines.append(f"护工范围：{'已分配' if assigned else '未分配'}（{staff.name}）")

    for spec in rooms:
        room_no = spec.split(":", 1)[0]
        room = (
            (await session.execute(select(Room).where(Room.room_no == room_no))).scalars().first()
        )
        if room is None:
            lines.append(f"房间 {room_no}：不存在")
            continue
        beds = (
            (await session.execute(select(Bed).where(Bed.room_id == room.room_id))).scalars().all()
        )
        free = sum(1 for bed in beds if bed.elder_id is None)
        lines.append(f"房间 {room_no}：status={room.status} beds={len(beds)} free={free}")
    return lines


def _http_verify(
    api_base: str,
    phone: str,
    password: str | None,
    elder_name: str,
) -> list[str]:
    """可选 E7 复核：用护工账号登录，确认列表里能看到目标长者。"""
    if not password:
        return ["E7 复核：跳过（未提供护工口令）"]
    import urllib.error
    import urllib.request

    base = api_base.rstrip("/")
    if not base.startswith(("http://", "https://")):
        return ["E7 复核：跳过（API 地址必须是 http(s)）"]
    body = json.dumps({"phone": phone, "password": password}).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        f"{base}/api/sessions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            session_body = json.loads(resp.read().decode("utf-8"))
        token = session_body["access_token"]
        req = urllib.request.Request(  # noqa: S310
            f"{base}/api/elders?page=1&page_size=100",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            page = json.loads(resp.read().decode("utf-8"))
        names = [item["name"] for item in page.get("items", [])]
        return [
            f"E7 复核：护工可见 {len(names)} 位长者，包含目标={'是' if elder_name in names else '否'}"
        ]
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as exc:
        return [f"E7 复核：失败（{exc}）"]


def _run_room_seed(specs: Sequence[str], dry_run: bool) -> None:
    """复用 seed_rooms_beds.py 补房间/床位；独立进程，避免重复实现聚合逻辑。"""
    seed = Path(__file__).with_name("seed_rooms_beds.py")
    cmd = [sys.executable, str(seed)]
    for spec in specs:
        cmd.extend(["--room", spec])
    if dry_run:
        cmd.append("--dry-run")
    subprocess.run(cmd, cwd=_REPO_ROOT, check=True)  # noqa: S603 —— cmd 为固定脚本路径，无 shell


async def _run(args: argparse.Namespace) -> int:
    room_specs: tuple[str, ...] = tuple(args.room) if args.room else _DEFAULT_ROOMS
    backup_dir = (
        Path(args.backup_dir)
        if args.backup_dir
        else Path(tempfile.gettempdir()) / "silvermate_backups"
    )

    try:
        _run_room_seed(room_specs, args.dry_run)
    except subprocess.CalledProcessError as exc:
        print(f"补房间失败：{exc}", file=sys.stderr)
        return 1

    try:
        async with session_factory() as session:
            elder = await _resolve_elder(session, args.elder_name, args.elder_id)
            # rollback 会让 ORM 对象过期；后面只传标量，避免 dry-run 回滚后再触发懒加载。
            elder_id = elder.elder_id
            elder_name = elder.name
            print(f"有效长者：{elder_name} ({elder_id})")

            assign_msg = await _assign_care_worker(
                session, elder, args.care_worker_phone, args.dry_run
            )
            print(f"护工分配：{assign_msg}")

            duplicate_msgs = await _delete_duplicates(
                session,
                elder,
                delete=args.delete_duplicates,
                dry_run=args.dry_run,
                backup_dir=backup_dir,
            )
            for message in duplicate_msgs:
                print(f"重复处理：{message}")

            if args.dry_run:
                await session.rollback()
            else:
                await session.commit()

            for line in await _verify(
                session,
                elder_id,
                elder_name,
                rooms=room_specs,
                care_worker_phone=args.care_worker_phone,
            ):
                print(f"核对：{line}")
    except (SQLAlchemyError, ValueError) as exc:
        print(f"对账失败：{exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

    for line in _http_verify(
        args.api_base,
        args.care_worker_phone,
        args.care_worker_password,
        args.elder_name,
    ):
        print(f"核对：{line}")

    print("注意：本脚本按「不换床、不换设备」口径执行；已有设备的一次性密钥不可恢复。")
    print("      如需完整的新设备 ID+密钥，请另行执行「解绑旧设备 → 目标房新建 E16 → E9 绑定」。")
    print("dry-run：未写库/未删除" if args.dry_run else "对账完成。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
