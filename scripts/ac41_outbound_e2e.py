"""AC-41 外呼端到端自测（Day 9 dev-D · 模拟 pad 端；D11 10:00 正式联调同脚本取证）。

链路：改 plan cron → 服务端 scheduler tick（每分钟）→ 触达（tts_text＋notification_id）
→ 外呼（服务端 offer 经信令下发）→ 本脚本模拟 pad 自动应答（aiortc answer＋ICE）
→ 服务端 outbound_call_answered → 握手收口。

前置：uvicorn 已起（scheduler 已装配 outbound_fn）；postgres healthy。
用法：uv run python scripts/ac41_outbound_e2e.py --device <device_id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription

results: list[tuple[bool, str]] = []


def record(ok: bool, msg: str) -> None:
    results.append((ok, msg))
    print(("✅" if ok else "❌") + " " + msg, flush=True)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--device", required=True)
    ap.add_argument("--wait-ticks", type=int, default=150, help="最多等待秒数（覆盖 ≥2 个 tick）")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    # ① 设备换证
    async with httpx.AsyncClient(base_url=base, timeout=15) as client:
        resp = await client.post("/api/device-sessions", json={"device_id": args.device})
        assert resp.status_code == 200, f"换证失败: {resp.text}"
        token = resp.json()["access_token"]
    record(True, "① 设备换证")

    ws_url = base.replace("http", "ws", 1) + "/ws/signal"
    async with websockets.connect(ws_url, open_timeout=10) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert auth.get("type") == "auth_ok", f"auth 失败: {auth}"
        record(True, "② WS auth_ok（模拟 pad 在线）")

        # 心跳：C→S 单向 ping 30s 周期（服务端 45s 无帧判死，signaling.py:19）
        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    await ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    return

        hb = asyncio.create_task(_heartbeat())

        got_touch = False
        got_offer = False
        answered = False
        pc: RTCPeerConnection | None = None

        async def answer_offer(sdp: str) -> None:
            """模拟 pad 自动接听：aiortc answerer（recvonly，不挂轨也可协商）。"""
            nonlocal pc, answered
            pc = RTCPeerConnection()
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))

            @pc.on("icecandidate")
            async def _on_cand(e):
                if e.candidate:
                    await ws.send(json.dumps({"type": "ice", "candidate": json.dumps({
                        "candidate": e.candidate.candidate,
                        "sdpMid": e.candidate.sdpMid,
                        "sdpMLineIndex": e.candidate.sdpMLineIndex,
                    })}))

            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await ws.send(json.dumps({"type": "answer", "sdp": pc.localDescription.sdp}))
            answered = True

        print(f"[wait] 等待服务端 tick 触达＋外呼（≤{args.wait_ticks}s，cron 需已改为 */2）…")
        try:
            while args.wait_ticks > 0:
                raw = await asyncio.wait_for(ws.recv(), timeout=args.wait_ticks)
                args.wait_ticks -= 1
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "tts_text" and msg.get("touch_id"):
                    got_touch = True
                    record(True, f"③ 触达帧到达（touch_id={str(msg.get('touch_id'))[:8]}… "
                                 f"notification_id={str(msg.get('notification_id'))[:8]}…）")
                elif mtype == "offer":
                    got_offer = True
                    record(True, "④ 服务端外呼 offer 到达（X1/B1：信令下发）")
                    await answer_offer(msg["sdp"])
                elif mtype == "ice":
                    if pc is not None:
                        await pc.addIceCandidate(json.loads(msg["candidate"]))
                elif mtype == "error":
                    record(False, f"服务端错误帧: {msg}")
                    break
                if got_touch and got_offer and answered:
                    await asyncio.sleep(3)  # 等 ICE 协商
                    break
        except TimeoutError:
            pass
        finally:
            hb.cancel()

        ok_e2e = got_touch and got_offer and answered
        record(ok_e2e, f"⑤ 端到端：触达={got_touch} offer={got_offer} 自动应答={answered}")

    if pc is not None:
        await pc.close()

    print("\n===== AC-41 外呼端到端自测汇总 =====")
    for ok, msg in results:
        print(("✅" if ok else "❌") + " " + msg)
    all_ok = all(ok for ok, _ in results)
    print("RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
