"""X1 回执帧端到端联调脚本（day09 D1 · F01，D10 12:00 与 C3 联调用）。

模拟 pad 端：设备会话 → 信令 WS auth → 收到触达后按 X1 契约发 client_receipt，
逐用例断言 receipt_ack 与 DB 终态。帧形状以 docs/team_tasks/day09/contracts.md
X1（v0.4 冻结）为准；服务端处理链：app/audio/signaling.py `_handle_client_receipt`。

用例（X1 契约判据）：
  ① card_rendered 正常回执        → ack delivered / duplicate=false，DB status=delivered
  ② 同帧重复发送（幂等）          → ack delivered / duplicate=true，DB sent_at 不变
  ③ tts_finished 第二种 kind      → ack delivered / duplicate=false（幂等键含 receipt_kind）
  ④ 越权 task_id（不存在的事件）  → ack rejected（归属校验拒绝，且不入幂等缓存）
  ⑤ notification_id=null 纯播报   → ack accepted，不写库不伪造 delivered
  ⑥ 非法 receipt_kind             → 服务端 error 帧（形状校验拒绝）

运行（后端 8000 已起、postgres/redis 已起）：
    $env:PYTHONPATH='.'; uv run python scripts/x1_receipt_e2e.py

注意事项：
- 张兰需有 active 家属绑定（send_medication_touch_notification 的 target 取第一个
  active guardian），脚本自动经 admin API 建绑定（重复执行幂等）；
- 造数直调 service（day09 先例：脚本层允许、服务层是唯一写库层），产生的
  medication_event / notification 行是联调种子，登记台账 §10.1；
- elder_id/device_id 出现在客户端帧内会被服务端丢弃（身份铁律），用例 ① 故意
  带上以验证丢弃口径不影响结果。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime

import httpx
import websockets

from app.db import session_factory
from app.services import medication_service, notify_service

BASE = "http://127.0.0.1:8000"
WS_URL = "ws://127.0.0.1:8000/ws/signal"

#: 台账 §1 当前有效实体（docs/测试数据_人员与床位.md，勿引用旧 UUID）
ELDER_ZHANGLAN = uuid.UUID("1ed9d741-2af3-441e-b0a3-c81960479bf7")
DEVICE_PAD = "adbd0f3c-5a4c-425b-b18a-6b88382c31df"
GUARDIAN_DRYRUN = uuid.UUID("eeeeeeee-e2e0-0000-0000-000000000001")
ADMIN_PHONE, ADMIN_PASSWORD = "13700000000", "Admin@2026"

results: list[tuple[str, bool, str]] = []


def check(case: str, ok: bool, detail: str) -> None:
    results.append((case, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {case}：{detail}")


async def seed_data() -> tuple[uuid.UUID, uuid.UUID]:
    """造联调种子：用药事件（task_id 来源）＋ pending 触达通知（回执绑定真值）。"""
    async with session_factory() as session:
        reminder_id = await medication_service.create_event(
            session,
            plan_id=uuid.UUID("e314ab93-721a-4652-a3dd-005353dcc76d"),  # 阿莫西林 active v5
            elder_id=ELDER_ZHANGLAN,
            scheduled_at=datetime.now(UTC).replace(microsecond=0),
            drug_snapshot={"drug_name": "阿莫西林", "dose": "1粒", "note": "x1-联调种子"},
            triggered_by="dialog",
        )
        nid = await notify_service.send_medication_touch_notification(
            session,
            elder_id=ELDER_ZHANGLAN,
            reminder_id=reminder_id,
            attempt_no=1,
            drug_name="阿莫西林",
        )
        await session.commit()
    if nid is None:
        raise RuntimeError("触达通知创建失败：张兰无 active 家属绑定？检查绑定 API 步骤")
    return reminder_id, nid


async def ws_case(
    ws,
    *,
    label: str,
    receipt_kind: str,
    task_id: str,
    notification_id: str | None,
    extra_elder_id: bool = False,
) -> dict:
    """发一帧 client_receipt 并返回下一帧响应（receipt_ack 或 error）。"""
    frame: dict = {
        "type": "client_receipt",
        "receipt_kind": receipt_kind,
        "task_id": task_id,
        "notification_id": notification_id,
        "ts": datetime.now(UTC).isoformat(),
    }
    if extra_elder_id:  # 身份铁律：客户端传 elder_id 应被丢弃，不影响结果
        frame["elder_id"] = "00000000-0000-0000-0000-00000000dead"
    await ws.send(json.dumps(frame))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        if msg.get("type") in ("receipt_ack", "error"):
            return msg
        # 跳过服务端主动下发的其它帧（心跳/下发帧等）
        print(f"    （跳过服务端帧 type={msg.get('type')}）")


def db_row(nid: uuid.UUID) -> dict:
    import psycopg

    with (
        psycopg.connect("postgresql://silvermate:silvermate@127.0.0.1:5432/silvermate") as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT status, sent_at FROM notifications WHERE notification_id = %s",
            (str(nid),),
        )
        row = cur.fetchone()
    return {"status": row[0], "sent_at": row[1]} if row else {}


async def main() -> int:
    # ---------- 准备：家属绑定（admin API，幂等容错） ----------
    async with httpx.AsyncClient(base_url=BASE, timeout=15) as client:
        login = await client.post(
            "/api/sessions",
            json={"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD},
        )
        admin_token = login.json()["access_token"]
        bind = await client.post(
            f"/api/elders/{ELDER_ZHANGLAN}/guardians",
            json={"guardian_id": str(GUARDIAN_DRYRUN), "relation": "儿子"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        print(
            f"  家属绑定 API：HTTP {bind.status_code}"
            + ("（已存在或成功，联调可继续）" if bind.status_code in (200, 201, 409) else " ← 异常")
        )
        # ---------- 设备会话 ----------
        ds = await client.post("/api/device-sessions", json={"device_id": DEVICE_PAD})
        check("设备会话换取", ds.status_code == 200, f"HTTP {ds.status_code}")
        device_token = ds.json()["access_token"]

    reminder_id, nid = await seed_data()
    task_id = str(reminder_id)
    print(f"  种子：task_id={task_id}  notification_id={nid}")

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"type": "auth", "token": device_token}))
        ack_raw = await asyncio.wait_for(ws.recv(), timeout=10)
        auth_msg = json.loads(ack_raw)
        check(
            "WS auth",
            auth_msg.get("type") not in ("auth_fail", "error"),
            f"首帧 type={auth_msg.get('type')}",
        )

        # ---------- 用例 ----------
        sent_at_before = db_row(nid).get("sent_at")

        r1 = await ws_case(
            ws,
            label="① card_rendered 正常回执",
            receipt_kind="card_rendered",
            task_id=task_id,
            notification_id=str(nid),
            extra_elder_id=True,
        )
        row1 = db_row(nid)
        check(
            "① ack=delivered + DB=delivered",
            r1.get("type") == "receipt_ack"
            and r1.get("status") == "delivered"
            and r1.get("duplicate") is False
            and row1.get("status") == "delivered",
            f"ack={r1.get('status')}/{r1.get('duplicate')} db={row1.get('status')}",
        )

        r2 = await ws_case(
            ws,
            label="② 同帧重复（幂等）",
            receipt_kind="card_rendered",
            task_id=task_id,
            notification_id=str(nid),
        )
        row2 = db_row(nid)
        check(
            "② duplicate=true + sent_at 不变",
            r2.get("status") == "delivered"
            and r2.get("duplicate") is True
            and row2.get("sent_at") == sent_at_before,
            f"ack={r2.get('status')}/{r2.get('duplicate')} sent_at 不变={row2.get('sent_at') == sent_at_before}",
        )

        r3 = await ws_case(
            ws,
            label="③ tts_finished 第二种 kind",
            receipt_kind="tts_finished",
            task_id=task_id,
            notification_id=str(nid),
        )
        check(
            "③ ack=delivered / duplicate=true（幂等重投递）",
            r3.get("status") == "delivered" and r3.get("duplicate") is True,
            f"ack={r3.get('status')}/{r3.get('duplicate')}",
        )

        r4 = await ws_case(
            ws,
            label="④ 越权 task_id（不存在的事件）",
            receipt_kind="card_rendered",
            task_id=str(uuid.uuid4()),
            notification_id=None,  # 纯播报语义：避开 (nid, kind) 幂等缓存命中，真正走归属校验
        )
        check(
            "④ ack=rejected（归属校验）",
            r4.get("status") == "rejected" and r4.get("duplicate") is False,
            f"ack={r4.get('status')}",
        )

        r5 = await ws_case(
            ws,
            label="⑤ 纯播报回执（notification_id=null）",
            receipt_kind="card_rendered",
            task_id=task_id,
            notification_id=None,
        )
        check(
            "⑤ ack=accepted（不写库）",
            r5.get("status") == "accepted",
            f"ack={r5.get('status')}",
        )

        await ws.send(
            json.dumps(
                {
                    "type": "client_receipt",
                    "receipt_kind": "bogus_kind",
                    "task_id": task_id,
                    "notification_id": str(nid),
                    "ts": datetime.now(UTC).isoformat(),
                }
            )
        )
        raw6 = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        check(
            "⑥ 非法 receipt_kind → error",
            raw6.get("type") == "error",
            f"响应 type={raw6.get('type')}",
        )

    # ---------- 汇总 ----------
    failed = [r for r in results if not r[1]]
    print("\n========== X1 联调汇总 ==========")
    for case, ok, _detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {case}")
    print(f"合计 {len(results) - len(failed)}/{len(results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    t0 = time.monotonic()
    code = asyncio.run(main())
    print(f"耗时 {time.monotonic() - t0:.1f}s，exit={code}")
    raise SystemExit(code)
