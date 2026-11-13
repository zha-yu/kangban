"""AC-42 唤醒实测 · CDP 取数（配合 gen_wakeup_loop.py + Chrome 假音频）。

经 Chrome DevTools Protocol 连 --remote-debugging-port=9222 的测试实例，
Runtime.evaluate 读取 pad 页面端侧打点 window.__wakeupSamples，
拉满 20 条有效样本后输出 AC-42 统计（≤1.5s 且 ≥18/20 达标，PRD §5.3）。

运行：uv run python scripts/fetch_wakeup_samples.py [--expect 20]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

import httpx
import websockets


def pick_target(targets: list[dict]) -> dict | None:
    """优先 http://localhost:5173 的页面 target（排除 devtools/about）。"""
    pages = [t for t in targets if t.get("type") == "page" and "5173" in (t.get("url") or "")]
    return pages[0] if pages else None


async def fetch_samples() -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        targets = (await client.get("http://127.0.0.1:9222/json")).json()
    target = pick_target(targets)
    if target is None:
        raise RuntimeError(f"未找到 5173 页面 target，现有：{[t.get('url') for t in targets]}")
    expr = "JSON.stringify(window.__wakeupSamples || [])"
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=10 << 20) as ws:
        await ws.send(
            json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expr}})
        )
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                result = msg["result"]["result"]
                if result.get("subtype") == "error":
                    raise RuntimeError(f"evaluate 失败：{result.get('description')}")
                return json.loads(result.get("value", "[]"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", type=int, default=20)
    ap.add_argument("--wait-seconds", type=int, default=0, help="不足时轮询等待秒数")
    args = ap.parse_args()

    deadline = time.monotonic() + args.wait_seconds
    samples: list[dict] = []
    while True:
        samples = asyncio_run(fetch_samples)
        if len(samples) >= args.expect or time.monotonic() >= deadline:
            break
        print(f"样本 {len(samples)}/{args.expect}，继续等待……")
        time.sleep(10)

    print(f"\n共取到 {len(samples)} 条端侧打点样本：")
    for i, s in enumerate(samples, 1):
        print(f"  #{i:02d} {s.get('duration_ms')}ms  @ {s.get('at')}")

    if not samples:
        print("无样本：检查 Chrome 假音频/KWS 命中/打点代码")
        return 1

    used = samples[: args.expect]
    durs = [s["duration_ms"] for s in used]
    within = sum(1 for d in durs if d <= 1500)
    print("\n===== AC-42 判定（口径：KWS 命中→会话链路就绪，端侧 performance.now）=====")
    print(f"样本数 {len(used)} ｜ ≤1.5s 命中 {within}/{len(used)}")
    print(f"min={min(durs)}ms  p50={statistics.median(durs):.0f}ms  max={max(durs)}ms")
    ok = within >= min(18, len(used)) and len(used) >= args.expect
    print(f"结果：{'达标' if ok else '未达标'}（判据：≤1.5s 且 20 次中 ≥18 次）")
    return 0 if ok else 1


def asyncio_run(fn, *args):
    import asyncio

    return asyncio.run(fn(*args))


if __name__ == "__main__":
    sys.exit(main())
