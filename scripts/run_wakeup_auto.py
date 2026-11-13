"""AC-42 唤醒实测 · 全自动驱动（录音驱动口径，台账 §10.2 登记后使用）。

链路：edge TTS 预合成循环音频（gen_wakeup_loop.py）→ Chrome headless 假音频设备
（--use-file-for-fake-audio-capture）→ pad 页面 KWS 端侧监听命中「小伴」→ 自动进
会话 → ASR 识别循环里的「再见」→ 挂断回待机 → 再命中……打点由页面
window.__wakeupSamples 累积，本脚本经 CDP 注入设备会话并轮询取数出 AC-42 统计。

运行（后端 8000 / vite 5173 / 循环音频已就绪）：
    uv run python scripts/run_wakeup_auto.py [--rounds 20] [--max-wait 360]
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from pathlib import Path

import httpx
import websockets

BASE = "http://127.0.0.1:8000"
PAGE = "http://localhost:5173/"
CDP = "http://127.0.0.1:9223"
DEVICE_PAD = "adbd0f3c-5a4c-425b-b18a-6b88382c31df"  # 张兰 pad（台账 §4）
WAV = Path(".workbuddy/tmp/xiaoban_loop.wav").resolve()
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
PROFILE = Path(".workbuddy/tmp/chrome-kws-auto").resolve()


def launch_chrome() -> subprocess.Popen[bytes]:
    chrome = next((c for c in CHROME_CANDIDATES if Path(c).exists()), None)
    if chrome is None:
        raise RuntimeError("未找到 chrome.exe")
    return subprocess.Popen(  # noqa: S603 — chrome 路径来自本地固定候选列表，非不可信输入
        [
            chrome,
            "--headless=new",
            f"--user-data-dir={PROFILE}",
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",  # Day 10 实证：headless 无 UI 授权，
            # 不加会 NotAllowedError（mic.start 静默 catch 落「唤醒不可用」降级）
            f"--use-file-for-fake-audio-capture={WAV}",
            "--remote-debugging-port=9223",
            "--remote-allow-origins=*",
            "--autoplay-policy=no-user-gesture-required",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,900",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # 返回句柄供 finally 整树 kill


async def cdp_eval(ws_url: str, expression: str, tmo: float = 15) -> str:
    async with websockets.connect(ws_url, max_size=10 << 20) as ws:
        await ws.send(
            json.dumps(
                {"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression}}
            )
        )
        deadline = time.monotonic() + tmo
        while time.monotonic() < deadline:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                result = msg["result"]["result"]
                if result.get("subtype") == "error":
                    raise RuntimeError(result.get("description", "evaluate error"))
                return result.get("value", "")
    raise TimeoutError("CDP evaluate timeout")


async def get_page_ws(client: httpx.AsyncClient) -> str:
    targets = (await client.get(f"{CDP}/json")).json()
    pages = [t for t in targets if t.get("type") == "page"]
    return pages[0]["webSocketDebuggerUrl"]


async def setup_and_collect(rounds: int, max_wait: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        # 设备会话（真 token，页面 sessionStorage 注入用）
        ds = (
            await client.post(f"{BASE}/api/device-sessions", json={"device_id": DEVICE_PAD})
        ).json()
        token, expires_in = ds["access_token"], ds["expires_in"]
        device = json.dumps(ds["device"], separators=(",", ":"))

        # 等 CDP 就绪并导航到 pad 页
        deadline = time.monotonic() + 30
        ws_url = None
        while time.monotonic() < deadline:
            try:
                ws_url = await get_page_ws(client)
                break
            except Exception:
                await asyncio.sleep(1)
        if ws_url is None:
            raise RuntimeError("CDP 30s 未就绪")

        await cdp_eval(ws_url, f"location.href = {json.dumps(PAGE)}")
        await asyncio.sleep(3)
        # sessionStorage 注入（per-origin，须在 5173 上下文执行）
        expires_at = int(time.time() * 1000 + expires_in * 1000)
        inject = (
            f"sessionStorage.setItem('kb_access_token', {json.dumps(token)});"
            f"sessionStorage.setItem('kb_expires_at', {expires_at});"
            f"sessionStorage.setItem('kb_device', {json.dumps(device)});"
            "'injected'"
        )
        value = await cdp_eval(ws_url, inject)
        if value != "injected":
            raise RuntimeError(f"sessionStorage 注入失败：{value}")
        await cdp_eval(ws_url, "location.reload()")
        print("会话注入完成，页面重载，KWS 加载中（假音频循环驱动）……")

        # Day 10 实证：会话断开回待机后，KWS wasm 单例重挂可能失败（页面落
        # 「唤醒不可用」降级）——循环靠「无进展且已断开 → reload」兜住，
        # sessionStorage 随 tab 保留、无需重注入；reload 加载期不计入打点口径。
        deadline = time.monotonic() + max_wait
        samples: list[dict] = []
        last_count, last_progress_at = 0, time.monotonic()
        while True:
            await asyncio.sleep(12)
            try:
                state_raw = await cdp_eval(
                    ws_url,
                    "JSON.stringify({s: JSON.parse(sessionStorage.getItem('kb_wakeup_samples')||'[]'),"
                    " body: window.document.body.innerText.slice(0,150)})",
                )
                state = json.loads(state_raw)
                samples = state.get("s", [])
            except Exception as exc:
                print(f"取数异常（页面可能 reload）：{exc}")
                continue
            print(
                f"  已命中 {len(samples)}/{rounds}  "
                + " ".join(f"{s['duration_ms']}ms" for s in samples[-5:])
            )
            if len(samples) >= rounds or time.monotonic() > deadline:
                break
            if len(samples) > last_count:
                last_count, last_progress_at = len(samples), time.monotonic()
            elif time.monotonic() - last_progress_at > 45:
                # Day 10 实证：「再见」不在规则层（RULESET 10 条无挂断规则，缺口已
                # 提单待裁决），会话不会被合成音挂断——reload 强切（等效人手刷新：
                # 断 WS/RTC 回待机），sessionStorage 保留，加载期不计入打点口径。
                print("  45s 无进展 → reload 强切会话回待机")
                last_progress_at = time.monotonic()
                await cdp_eval(ws_url, "location.reload()")
        return samples


def report(samples: list[dict], rounds: int) -> int:
    used = samples[:rounds]
    durs = [s["duration_ms"] for s in used]
    print(f"\n===== AC-42 判定（录音驱动口径；判据 ≤1.5s 且 {rounds} 次 ≥18 次）=====")
    print(f"有效样本 {len(used)} 条")
    if not durs:
        print("无样本，不达标")
        return 1
    within = sum(1 for d in durs if d <= 1500)
    print(f"≤1.5s：{within}/{len(used)}")
    print(f"min={min(durs)}ms  p50={statistics.median(durs):.0f}ms  max={max(durs)}ms")
    for i, s in enumerate(used, 1):
        mark = "✓" if s["duration_ms"] <= 1500 else "✗"
        print(f"  #{i:02d} {mark} {s['duration_ms']}ms @ {s['at']}")
    ok = len(used) >= rounds and within >= 18
    print(f"结果：{'达标' if ok else '未达标'}")
    Path(".workbuddy/tmp/wakeup_samples.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("样本已存 .workbuddy/tmp/wakeup_samples.json")
    return 0 if ok else 1


async def main_async() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--max-wait", type=int, default=360)
    args = ap.parse_args()

    proc = launch_chrome()
    try:
        async with httpx.AsyncClient(timeout=15) as probe:
            for _ in range(20):
                try:
                    await probe.get(f"{CDP}/json/version")
                    break
                except Exception:
                    await probe.get(f"{BASE}/health")
                    await asyncio.sleep(1)
        samples = await setup_and_collect(args.rounds, args.max_wait)
        return report(samples, args.rounds)
    finally:
        proc.kill()  # headless 实例整树回收，不依赖外部 powershell


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main_async()))
