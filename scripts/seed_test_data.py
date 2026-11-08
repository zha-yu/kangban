"""本地开发测试数据种子。

用途：给前端三端（pad / console / family）提供能正常登录的账号，
以及设备/房间/床/长者等关联数据。

依赖顺序：Room → Bed → Elder → Device / StaffAccount / GuardianAccount → 绑定关系。

账号清单（全部 123456）：

| 端       | 角色    | 手机号          | 密码    |
|----------|---------|-----------------|---------|
| console  | admin   | 13800000000     | 123456  |
| console  | 护工    | 13800000001     | 123456  |
| console  | 医生    | 13800000002     | 123456  |
| family   | 家属    | 13900000000     | 123456  |

pad 端走 device_id + device_secret，脚本会打印明文密钥。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import date, datetime, UTC

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_factory
from app.core.security import hash_password, sha256_hex
from app.models import Elder, StaffAccount, GuardianAccount, ElderStaffAssignment, ElderGuardian
from app.models.organization import Room, Bed, Device

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed")


async def _clean(session: AsyncSession) -> None:
    """清掉旧的测试数据（按 FK 反向顺序）。"""
    log.info("清理旧数据…")
    for tbl in [
        ElderStaffAssignment, ElderGuardian,
        Device, Bed, Elder,
        StaffAccount, GuardianAccount,
        Room,
    ]:
        await session.execute(delete(tbl))
    await session.commit()


async def _seed(session: AsyncSession) -> None:
    # ---------- 房间 ----------
    rooms: dict[str, Room] = {}
    for no in ["A-301", "A-302", "B-201"]:
        r = Room(room_no=no, status="available")
        session.add(r)
        rooms[no] = r
    await session.flush()
    log.info("✓ 房间：%s", list(rooms))

    # ---------- 床位 + 长者 ----------
    elders: list[Elder] = []
    beds: list[Bed] = []

    # A-301：双人间，两位长者
    e1 = Elder(
        name="张桂芬", birth_date=date(1948, 5, 12), gender="女", status="active",
    )
    e2 = Elder(
        name="李建国", birth_date=date(1945, 11, 3), gender="男", status="active",
    )
    session.add_all([e1, e2])
    await session.flush()
    elders.extend([e1, e2])

    beds.append(Bed(room_id=rooms["A-301"].room_id, bed_no="1号床", elder_id=e1.elder_id, status="occupied"))
    beds.append(Bed(room_id=rooms["A-301"].room_id, bed_no="2号床", elder_id=e2.elder_id, status="occupied"))
    beds.append(Bed(room_id=rooms["A-302"].room_id, bed_no="1号床", elder_id=None, status="available"))
    beds.append(Bed(room_id=rooms["B-201"].room_id, bed_no="1号床", elder_id=None, status="available"))
    session.add_all(beds)
    await session.flush()
    log.info("✓ 长者：张桂芬 / 李建国；床位 4 张")

    # ---------- 设备（pad 登录用）----------
    # 生成明文密钥，seed 脚本打印给用户，库里只存 sha256
    device_plaintexts: dict[str, str] = {}  # device_id 文本 → 明文密钥

    for room, elder, label in [
        (rooms["A-301"], e1, "A-301-01（张桂芬）"),
        (rooms["A-301"], e2, "A-301-02（李建国）"),
    ]:
        plaintext = "sk-" + secrets.token_urlsafe(16)
        dev = Device(
            room_id=room.room_id,
            elder_id=elder.elder_id,
            token_hash=sha256_hex(plaintext),
            status="online",
        )
        session.add(dev)
        await session.flush()
        device_plaintexts[str(dev.device_id)] = plaintext
        log.info("✓ 设备 %s", label)

    # ---------- 员工账号 ----------
    staff_in: list[dict] = [
        {"name": "王管理员", "role": "admin", "phone": "13800000000"},
        {"name": "刘护工",   "role": "care_worker", "phone": "13800000001"},
        {"name": "陈医生",   "role": "doctor", "phone": "13800000002"},
    ]
    staff_rows: list[StaffAccount] = []
    for s in staff_in:
        row = StaffAccount(
            name=s["name"],
            role=s["role"],
            phone=s["phone"],
            password_hash=hash_password("123456"),
            status="active",
        )
        session.add(row)
        staff_rows.append(row)
    await session.flush()
    log.info("✓ 员工账号 3 个（密码均为 123456）")

    # ---------- 家属账号 ----------
    g1 = GuardianAccount(
        name="张小明", phone="13900000000",
        password_hash=hash_password("123456"), status="active",
    )
    session.add(g1)
    await session.flush()
    log.info("✓ 家属账号：张小明（13900000000 / 123456）")

    # ---------- 绑定关系 ----------
    # 护工 / 医生 → 长者
    session.add_all([
        ElderStaffAssignment(elder_id=e1.elder_id, staff_id=staff_rows[1].staff_id, relation="primary_care"),
        ElderStaffAssignment(elder_id=e2.elder_id, staff_id=staff_rows[1].staff_id, relation="primary_care"),
        ElderStaffAssignment(elder_id=e1.elder_id, staff_id=staff_rows[2].staff_id, relation="doctor"),
        ElderStaffAssignment(elder_id=e2.elder_id, staff_id=staff_rows[2].staff_id, relation="doctor"),
    ])
    # 家属 → 长者
    session.add_all([
        ElderGuardian(elder_id=e1.elder_id, guardian_id=g1.guardian_id, relation="母子", status="active"),
    ])
    await session.commit()
    log.info("✓ 绑定关系已落库")

    # ---------- 打印明文密钥 ----------
    print()
    print("=" * 60)
    print("  PAD 设备登录（device_id + device_secret）")
    print("=" * 60)
    for did, secret in device_plaintexts.items():
        print(f"  device_id  : {did}")
        print(f"  device_secret: {secret}")
        print()
    print("=" * 60)
    print("  CONSOLE / FAMILY 登录（手机号 + 密码，统一 123456）")
    print("=" * 60)
    print("  console  管理员  13800000000  王管理员")
    print("  console  护工    13800000001  刘护工")
    print("  console  医生    13800000002  陈医生")
    print("  family   家属    13900000000  张小明（绑定张桂芬）")
    print("=" * 60)


async def main() -> None:
    async with session_factory() as session:
        await _clean(session)
        await _seed(session)
    log.info("完成。")


if __name__ == "__main__":
    asyncio.run(main())
