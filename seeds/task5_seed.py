"""task-5 手动 seed 脚本（给运行中的服务提供测试数据）。"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import hash_password, sha256_hex


async def main() -> None:
    eng = create_async_engine(get_settings().database_url, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.execute(text(
            "TRUNCATE elder_guardians, elder_staff_assignments, guardian_accounts, "
            "staff_accounts, staff_tokens, elder_staff_assignments, devices, "
            "elders, beds, personas, rooms CASCADE"
        ))

        room_id = uuid.uuid4()
        elder_id = uuid.uuid4()
        staff_id = uuid.uuid4()
        guardian_id = uuid.uuid4()
        bed_id = uuid.uuid4()
        device_id = uuid.uuid4()

        # 1. Room
        await conn.execute(
            text("INSERT INTO rooms(room_id, room_no) VALUES(:id, 'A-301')"),
            {"id": room_id},
        )
        # 2. Elder
        await conn.execute(
            text("INSERT INTO elders(elder_id, name, status) VALUES(:id, '张桂芬', 'active')"),
            {"id": elder_id},
        )
        # 3. Bed + Device（FK 已就绪）
        await conn.execute(
            text("INSERT INTO beds(bed_id, room_id, bed_no, elder_id) VALUES(:bid, :rid, '1号床', :eid)"),
            {"bid": bed_id, "rid": room_id, "eid": elder_id},
        )
        await conn.execute(
            text("INSERT INTO devices(device_id, room_id, elder_id, token_hash, status) VALUES(:did, :rid, :eid, :th, 'offline')"),
            {"did": device_id, "rid": room_id, "eid": elder_id, "th": sha256_hex("device-long-secret-plain-abc123")},
        )
        # 4. Staff + Guardian
        await conn.execute(
            text("INSERT INTO staff_accounts(staff_id, name, role, phone, password_hash, token_version, status) VALUES(:id, '王护工', 'care_worker', '13800000001', :ph, 0, 'active')"),
            {"id": staff_id, "ph": hash_password("test123")},
        )
        await conn.execute(
            text("INSERT INTO guardian_accounts(guardian_id, name, phone, password_hash, token_version, status) VALUES(:id, '张女士', '13900000001', :ph, 0, 'active')"),
            {"id": guardian_id, "ph": hash_password("test123")},
        )
        # 5. 关系
        await conn.execute(
            text("INSERT INTO elder_staff_assignments(elder_id, staff_id, relation) VALUES(:eid, :sid, 'primary_care')"),
            {"eid": elder_id, "sid": staff_id},
        )
        await conn.execute(
            text("INSERT INTO elder_guardians(elder_id, guardian_id, relation, status) VALUES(:eid, :gid, 'daughter', 'active')"),
            {"eid": elder_id, "gid": guardian_id},
        )

    await eng.dispose()
    print("SEED OK")


asyncio.run(main())
