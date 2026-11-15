"""临时：CDP 订阅 console/log + reload，收集 KWS 加载失败原因。"""

import asyncio
import json
import time

import httpx
import websockets


async def main() -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        ts = (await c.get("http://127.0.0.1:9223/json")).json()
    target = next(p for p in ts if p.get("type") == "page" and "5173" in (p.get("url") or ""))
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=20 << 20) as ws:
        for mid in ("Runtime.enable", "Log.enable", "Network.enable"):
            await ws.send(json.dumps({"id": len(mid), "method": mid}))
        await ws.send(json.dumps({"id": 99, "method": "Page.reload"}))
        end = time.monotonic() + 45
        while time.monotonic() < end:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            except TimeoutError:
                continue
            m = msg.get("method", "")
            if m == "Runtime.consoleAPICalled":
                args = [a.get("value") or a.get("description") for a in msg["params"]["args"]]
                print(f"[console.{msg['params']['type']}]", *args, sep=" ")
            elif m == "Log.entryAdded":
                e = msg["params"]["entry"]
                print(f"[log.{e['level']}]", e.get("text", "")[:200])
            elif m == "Network.loadingFailed":
                p = msg["params"]
                print("[network-fail]", p.get("errorText"), p.get("blockedReason", ""))


asyncio.run(main())
