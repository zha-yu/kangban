"""X1 回执端到端联调脚本（Day 9 dev-D · D10 12:00 联调预演，可复跑）。

全链路：设备换证 → WS 信令 auth → 造真实用药事件＋触达通知行（C3 真函数）→
pad 侧模拟 client_receipt → receipt_ack → DB `notifications.status='delivered'`
断言 → 幂等回执 duplicate=true 断言。

用法：
    uv run python scripts/x1_receipt_e2e.py [--base http://127.0.0.1:8000]

前置：后端已起（uvicorn app.main:app）、postgres 容器 healthy、迁移已 upgrade。
产出：终端证据清单（可截屏归档到计划件执行记录）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import websockets

from app.db import session_factory
from app.services import notify_service
from app.services.medication_service import payload_hash

PASS_MARK: str = "✅"  # noqa: S105 —— 判定标记符号，非凭据
FAIL_MARK: str = "❌"
results: list[tuple[bool, str]] = []


def record(ok: bool, msg: str) -> None:
    results.append((ok, msg))
    print(f"{PASS_MARK if ok else FAIL_MARK} {msg}", flush=True)


async def seed_event(device_id: str, elder_id: str, plan_id: str) -> tuple[str, str]:
    """造真实用药事件行＋触达通知行（C3 真函数），推通知到 pending。返回 (reminder_id, notification_id)。"""
    reminder_id = str(uuid.uuid4())
    drug_snapshot = {
        "drugName": "氨氯地平（联调测试）",
        "dose": "1 片",
        "usage": "口服，每日一次",
    }
    async with session_factory() as s:
        await s.execute(
            __import__("sqlalchemy").text(
                """INSERT INTO medication_events
                   (reminder_id, plan_id, elder_id, scheduled_at, drug_snapshot,
                    payload_hash, triggered_by, status, retry_count)
                   VALUES (:rid, :pid, :eid, now(), CAST(:snap AS jsonb), :phash, 'scheduler',
                           'calling', 0)"""
            ),
            {
                "rid": reminder_id,
                "pid": plan_id,
                "eid": elder_id,
                "snap": json.dumps(drug_snapshot, ensure_ascii=False),
                "phash": payload_hash(drug_snapshot),
            },
        )
        await s.commit()

    async with session_factory() as s:
        nid = await notify_service.send_medication_touch_notification(
            s,
            elder_id=__import__("uuid").UUID(elder_id),
            reminder_id=__import__("uuid").UUID(reminder_id),
            attempt_no=1,
            drug_name=drug_snapshot["drugName"],
        )
        assert nid is not None, "触达通知行创建失败（guardian 未绑定？）"
        # C3 F01 后触达行创建即 pending（dedupe_key=medication_touch:{rid}:{attempt}），
        # 无需再推 generated→pending；直接 commit 进入回执流程
        await s.commit()
    return reminder_id, str(nid)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    async with session_factory() as s:
        from sqlalchemy import text

        row = (
            await s.execute(
                text(
                    """SELECT d.device_id, d.elder_id, p.plan_id FROM devices d
                       JOIN elders e ON e.elder_id = d.elder_id
                       JOIN medication_plans p ON p.elder_id = e.elder_id
                       JOIN elder_guardians eg ON eg.elder_id = e.elder_id AND eg.status='active'
                       LIMIT 1"""
                )
            )
        ).first()
    assert row is not None, "无满足条件的设备（绑定长者＋有用药计划＋有 active guardian）"
    device_id, elder_id, plan_id = str(row[0]), str(row[1]), str(row[2])
    print(f"[setup] 设备={device_id} 长者={elder_id} 计划={plan_id}")

    # ① 设备换证（E4：device_id 直接换证）
    async with httpx.AsyncClient(base_url=base, timeout=15) as client:
        resp = await client.post("/api/device-sessions", json={"device_id": device_id})
        assert resp.status_code == 200, f"换证失败 {resp.status_code}: {resp.text}"
        token = resp.json()["access_token"]
    record(True, "① 设备换证拿到 access_token")

    # ② 造事件＋触达通知行（C3 真函数）
    reminder_id, notification_id = await seed_event(device_id, elder_id, plan_id)
    record(True, f"② 造用药事件 {reminder_id[:8]}… ＋触达通知行 {notification_id[:8]}…（pending）")

    # ③④⑤ WS 信令全链
    ws_url = base.replace("http", "ws", 1) + "/ws/signal"
    async with websockets.connect(ws_url, open_timeout=10) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        auth_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert auth_resp.get("type") == "auth_ok", f"auth 失败: {auth_resp}"
        record(True, "③ WS 信令 auth_ok（设备鉴权）")

        receipt = {
            "type": "client_receipt",
            "receipt_kind": "card_rendered",
            "task_id": reminder_id,
            "notification_id": notification_id,
            "ts": "2026-11-12T21:30:00.000Z",
        }
        await ws.send(json.dumps(receipt))
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        ok_ack = (
            ack.get("type") == "receipt_ack"
            and ack.get("status") == "delivered"
            and ack.get("duplicate") is False
        )
        record(ok_ack, f"④ receipt_ack：{json.dumps(ack, ensure_ascii=False)}")

        # ⑥ 幂等：重复回执返回原结果 duplicate=true，不重复写库
        await ws.send(json.dumps(receipt))
        ack_dup = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        ok_dup = (
            ack_dup.get("type") == "receipt_ack"
            and ack_dup.get("status") == "delivered"
            and ack_dup.get("duplicate") is True
        )
        record(ok_dup, f"⑥ 幂等回执：duplicate={ack_dup.get('duplicate')}")

    # ⑤ DB 断言
    async with session_factory() as s:
        from sqlalchemy import text

        row = (
            await s.execute(
                text(
                    "SELECT status FROM notifications WHERE notification_id = :nid"
                ),
                {"nid": notification_id},
            )
        ).first()
    ok_db = row is not None and row[0] == "delivered"
    record(ok_db, f"⑤ DB 断言：notifications.status='{row[0] if row else '??'}'（X1 口径：仅状态机流转，不新增列）")

    print("\n===== X1 端到端联调证据汇总 =====")
    for ok, msg in results:
        print(f"{PASS_MARK if ok else FAIL_MARK} {msg}")
    all_ok = all(ok for ok, _ in results)
    print("RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
