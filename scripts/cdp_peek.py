"""临时：CDP 读 pad 页面打点与页面状态（9223）。"""

import asyncio
import json

import httpx
import websockets


async def main() -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        ts = (await c.get("http://127.0.0.1:9223/json")).json()
    pages = [t for t in ts if t.get("type") == "page"]
    target = next((p for p in pages if "5173" in (p.get("url") or "")), pages[0] if pages else None)
    if target is None:
        print("no page target:", [t.get("url") for t in ts][:5])
        return
    print("page url:", target["url"])
    expr = "JSON.stringify({s:(window.__wakeupSamples||[]), body: window.document.body.innerText.slice(0,150)})"
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=10 << 20) as ws:
        await ws.send(
            json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expr}})
        )
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                print(msg["result"]["result"].get("value", "ERR")[:1200])
                return


asyncio.run(main())
