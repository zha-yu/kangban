"""M5.4 退房端到端联调预演（Day 9 S6，dev-A）。

模拟 console→API 全链路（非测试装配：独立 uvicorn 进程 + 本地 .env 配置 + 真实
登录链路签发 token），对退房流水线做真库断言。三方联调（C 触发页面 / D 前端
表现）到场后在本脚本基础上补页面级证据即可复跑。

前置：
    docker compose up -d postgres redis
    uv run uvicorn app.main:app --port 8000
运行：
    uv run python scripts/lifecycle_e2e_dryrun.py

断言清单（X12 顺序口径）：
  ① 声纹 2 行 → 0 行；② 设备解绑（elder_id 空 / offline / token_hash 轮换）；
  ③ 家属绑定 active → unbound（F0 清单失权）；④ elder.status → archived；
  ⑤ audit_logs lifecycle.checkout.* 恰 5 行（桩步带 stub=true）；
  ⑥ 幂等重跑全 skip、审计仍 5 行；⑦ 旧设备 token → 401 TOKEN_REVOKED。
"""

from __future__ import annotations

import asyncio
import sys
import uuid

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.security import hash_password, sha256_hex
from app.models import (
    AuditLog,
    Device,
    Elder,
    ElderGuardian,
    GuardianAccount,
    Room,
    StaffAccount,
    Voiceprint,
)

BASE = "http://127.0.0.1:8000"

ROOM_ID = uuid.UUID("aaaaaaaa-e2e0-0000-0000-000000000001")
ELDER_ID = uuid.UUID("bbbbbbbb-e2e0-0000-0000-000000000001")
DEVICE_ID = uuid.UUID("cccccccc-e2e0-0000-0000-000000000001")
STAFF_ID = uuid.UUID("dddddddd-e2e0-0000-0000-000000000001")
GUARDIAN_ID = uuid.UUID("eeeeeeee-e2e0-0000-0000-000000000001")

ADMIN_PHONE = "13900001111"
ADMIN_PASSWORD = "DryRun#2026"  # noqa: S105 - 预演造数口令，非真实凭证
GUARDIAN_PHONE = "13900002222"
GUARDIAN_PASSWORD = "DryRun#2026"  # noqa: S105 - 同上
DEVICE_SECRET = "e2e-device-secret"  # noqa: S105 - 同上

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, evidence: str) -> None:
    (PASS if ok else FAIL).append(f"{name}: {evidence}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {evidence}")


async def seed_orm(engine) -> None:  # type: ignore[no-untyped-def]
    """幂等造数：先清后种（固定 uuid，重跑无残留）。"""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with engine.begin() as conn:
        await conn.execute(delete(AuditLog).where(AuditLog.resource_id == ELDER_ID))
        await conn.execute(delete(Voiceprint).where(Voiceprint.elder_id == ELDER_ID))
        await conn.execute(delete(Device).where(Device.device_id == DEVICE_ID))
        await conn.execute(delete(ElderGuardian).where(ElderGuardian.elder_id == ELDER_ID))
        await conn.execute(delete(Elder).where(Elder.elder_id == ELDER_ID))
        await conn.execute(
            delete(GuardianAccount).where(GuardianAccount.guardian_id == GUARDIAN_ID)
        )
        await conn.execute(delete(StaffAccount).where(StaffAccount.staff_id == STAFF_ID))
        await conn.execute(delete(Room).where(Room.room_id == ROOM_ID))

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all(
            [
                Room(room_id=ROOM_ID, room_no="E2E-01"),
                Elder(elder_id=ELDER_ID, name="预演长者", status="active"),
                StaffAccount(
                    staff_id=STAFF_ID,
                    name="预演管理员",
                    role="admin",
                    phone=ADMIN_PHONE,
                    password_hash=hash_password(ADMIN_PASSWORD),
                    token_version=0,
                    status="active",
                ),
                GuardianAccount(
                    guardian_id=GUARDIAN_ID,
                    name="预演家属",
                    phone=GUARDIAN_PHONE,
                    password_hash=hash_password(GUARDIAN_PASSWORD),
                    token_version=0,
                    status="active",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                ElderGuardian(
                    elder_id=ELDER_ID, guardian_id=GUARDIAN_ID, relation="儿子", status="active"
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Device(
                    device_id=DEVICE_ID,
                    room_id=ROOM_ID,
                    elder_id=ELDER_ID,
                    token_hash=sha256_hex(DEVICE_SECRET),
                    status="online",
                ),
                Voiceprint(
                    elder_id=ELDER_ID,
                    version=1,
                    sample_index=0,
                    embedding=b"\x00" * 16,
                    status="active",
                ),
                Voiceprint(
                    elder_id=ELDER_ID,
                    version=1,
                    sample_index=1,
                    embedding=b"\x01" * 16,
                    status="active",
                ),
            ]
        )
        await session.commit()


async def main() -> int:  # type: ignore[no-untyped-def]
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    print("== S6 退房端到端预演（dev-A） ==")
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
        # 0. 前置
        health = await client.get("/health")
        check("uvicorn /health", health.status_code == 200, f"status={health.status_code}")
        if health.status_code != 200:
            print("先启动：uv run uvicorn app.main:app --port 8000")
            return 1

        await seed_orm(engine)
        print("  [SEED] 造数完成（房间/长者/admin/家属/绑定/设备/声纹×2）")

        # 1. 真实登录链路
        login = await client.post(
            "/api/sessions", json={"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD}
        )
        check("admin 登录 /api/sessions", login.status_code == 200, f"status={login.status_code}")
        admin_token = login.json()["access_token"]
        ah = {"Authorization": f"Bearer {admin_token}"}

        glogin = await client.post(
            "/api/sessions", json={"phone": GUARDIAN_PHONE, "password": "DryRun#2026"}
        )
        check("家属登录", glogin.status_code == 200, f"status={glogin.status_code}")
        gh = {"Authorization": f"Bearer {glogin.json()['access_token']}"}

        dev = await client.post(
            "/api/device-sessions",
            json={"device_id": str(DEVICE_ID), "device_secret": DEVICE_SECRET},
        )
        check("设备会话签发", dev.status_code == 200, f"status={dev.status_code}")
        device_token = dev.json()["access_token"]

        # 2. 退房前基线
        baseline = await client.get(
            "/api/sessions/current", headers={"Authorization": f"Bearer {device_token}"}
        )
        check("退房前设备令牌有效", baseline.status_code == 200, f"status={baseline.status_code}")
        f0_before = await client.get("/api/family/elders", headers=gh)
        check(
            "退房前家属 F0 清单含长者",
            f0_before.status_code == 200 and str(ELDER_ID) in f0_before.text,
            f"status={f0_before.status_code}",
        )

        # 3. 二次确认负例（零副作用）
        no_confirm = await client.post(
            "/api/lifecycle/checkout",
            json={"elder_id": str(ELDER_ID), "confirm": False},
            headers=ah,
        )
        check(
            "无二次确认 → 409 零副作用",
            no_confirm.status_code == 409 and no_confirm.json()["code"] == "CONFIRM_REQUIRED",
            f"status={no_confirm.status_code} code={no_confirm.json().get('code')}",
        )

        # 4. 退房主链路
        checkout = await client.post(
            "/api/lifecycle/checkout",
            json={"elder_id": str(ELDER_ID), "confirm": True},
            headers=ah,
        )
        body = checkout.json() if checkout.status_code == 200 else {}
        steps = {s["step"]: s for s in body.get("steps", [])}
        check("checkout 200", checkout.status_code == 200, f"status={checkout.status_code}")
        check(
            "五步顺序（X12）",
            [s["step"] for s in body.get("steps", [])]
            == [
                "revoke_tokens",
                "purge_voiceprints",
                "purge_memory",
                "archive_health",
                "unbind_devices",
            ],
            f"steps={list(steps)}",
        )
        check(
            "声纹步 affected=2",
            steps.get("purge_voiceprints", {}).get("affected_rows") == 2,
            f"={steps.get('purge_voiceprints')}",
        )
        check(
            "设备步 affected=1",
            steps.get("unbind_devices", {}).get("affected_rows") == 1,
            f"={steps.get('unbind_devices')}",
        )
        check(
            "桩步 stub=true ×2",
            [steps[k]["stub"] for k in ("purge_memory", "archive_health")] == [True, True],
            "purge_memory/archive_health",
        )

        # 5. 真库 SQL 断言（AsyncSession：ORM 实体访问；Core connect 只返回 Row）
        from sqlalchemy.ext.asyncio import AsyncSession as _AS

        async with _AS(engine, expire_on_commit=False) as sdb:
            vp = (
                await sdb.execute(
                    select(func.count())
                    .select_from(Voiceprint)
                    .where(Voiceprint.elder_id == ELDER_ID)
                )
            ).scalar_one()
            check("SQL: 声纹 0 行", vp == 0, f"count={vp}")
            dev_row = (
                await sdb.execute(select(Device).where(Device.device_id == DEVICE_ID))
            ).scalar_one()
            check(
                "SQL: 设备解绑+offline+token 轮换",
                dev_row.elder_id is None
                and dev_row.status == "offline"
                and dev_row.token_hash != sha256_hex(DEVICE_SECRET),
                f"elder_id={dev_row.elder_id} status={dev_row.status}",
            )
            binding = (
                await sdb.execute(select(ElderGuardian).where(ElderGuardian.elder_id == ELDER_ID))
            ).scalar_one()
            check("SQL: 家属绑定 unbound", binding.status == "unbound", f"status={binding.status}")
            elder = (
                await sdb.execute(select(Elder).where(Elder.elder_id == ELDER_ID))
            ).scalar_one()
            check(
                "SQL: elder.status=archived", elder.status == "archived", f"status={elder.status}"
            )
            audit_rows = (
                (
                    await sdb.execute(
                        select(AuditLog).where(
                            AuditLog.resource_id == ELDER_ID,
                            AuditLog.detail["reason"].as_string() == "checkout",
                        )
                    )
                )
                .scalars()
                .all()
            )
            check(
                "SQL: 退房审计恰 5 行（行数=步数）",
                len(audit_rows) == 5,
                f"count={len(audit_rows)} actions={[a.action for a in audit_rows]}",
            )

        # 6. 幂等重跑
        rerun = await client.post(
            "/api/lifecycle/checkout", json={"elder_id": str(ELDER_ID), "confirm": True}, headers=ah
        )
        rerun_body = rerun.json()
        check(
            "幂等重跑全 skip",
            rerun.status_code == 200 and all(s["skipped"] for s in rerun_body["steps"]),
            f"skipped={[s['skipped'] for s in rerun_body['steps']]}",
        )
        async with _AS(engine, expire_on_commit=False) as sdb2:
            n = (
                await sdb2.execute(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.resource_id == ELDER_ID,
                        AuditLog.detail["reason"].as_string() == "checkout",
                    )
                )
            ).scalar_one()
            check("重跑后审计仍 5 行", n == 5, f"count={n}")

        # 7. 失权实测
        replay = await client.get(
            "/api/sessions/current", headers={"Authorization": f"Bearer {device_token}"}
        )
        check(
            "旧设备 token → 401 TOKEN_REVOKED（AC-24）",
            replay.status_code == 401 and replay.json()["code"] == "TOKEN_REVOKED",
            f"status={replay.status_code} code={replay.json().get('code')}",
        )
        f0_after = await client.get("/api/family/elders", headers=gh)
        check(
            "家属 F0 清单失权（不再含长者）",
            f0_after.status_code == 200 and str(ELDER_ID) not in f0_after.text,
            f"status={f0_after.status_code}",
        )

    await engine.dispose()
    print(f"\n== 结果：{len(PASS)} PASS / {len(FAIL)} FAIL ==")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
