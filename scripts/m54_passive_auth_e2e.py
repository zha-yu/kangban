"""M5.4 退房被动链路 · 前端断言实测（day09 dev-D D1.6，只验证不新写代码）。

前置：后端 8000 / vite 5173 已起。造数口径：长者/设备/管理员复用
lifecycle_e2e_dryrun 的固定 E2E UUID（幂等），家属用本脚本独立账号（见下注）。
与 run_wakeup_auto 并行安全（CDP 端口 9224、独立 profile）。

断言清单（计划件 D1.6 口径：pad 旧 token→401 回登录、原绑定家属→403/空态）：
  A1 设备会话签发（退房前）→ T1；
  A2 退房 checkout 200；
  A3 旧设备 token T1 → 401 TOKEN_REVOKED（AC-24，后端侧）；
  A4 家属 F0 清单 → 200 且不含已退房长者（页面空态数据源）；
  A5 家属查该长者日报 → 403 SCOPE_FORBIDDEN（api/reports.py 未绑定一律 403+审计）；
  B1 pad 注入 T1 → 页面启动即 401/4401 → onLogout 回登录页（DOM 断言＋截图）；
  B2 家属 UI 登录（退房后）→ 首页空态「您还没有绑定长者」（DOM 断言＋截图）。

运行（仓库根目录）：
    uv run python -m scripts.m54_passive_auth_e2e [--skip-browser]
产物：.workbuddy/tmp/m54_*.png / m54_state.json（证据指针登记台账）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from scripts.lifecycle_e2e_dryrun import (
    ADMIN_PASSWORD,
    ADMIN_PHONE,
    DEVICE_ID,
    DEVICE_SECRET,
    ELDER_ID,
    ROOM_ID,
    STAFF_ID,
)
from websockets.asyncio.client import connect

# 独立家属（不复用 dryrun 的 GUARDIAN_*）：2026-11-13 X1 联调已把「预演家属」
# (13900002222) 同时绑到真实长者张兰（台账 §10.2 演示种子，不得动）——复用会令
# B2 空态断言失真，且其 seed_orm 删 GuardianAccount 必撞 FK（潜在 bug，已提单）。
M54_GUARDIAN_ID = uuid.UUID("eeeeeeee-e2e1-0000-0000-000000000001")
M54_GUARDIAN_PHONE = "13900009999"
M54_GUARDIAN_PASSWORD = "DryRun#2026"  # noqa: S105 - 预演造数口令，非真实凭证

BASE = "http://127.0.0.1:8000"
PAGE = "http://localhost:5173/"
FAMILY_PAGE = "http://localhost:5173/family.html"
CDP = "http://127.0.0.1:9224"
PROFILE = Path(".workbuddy/tmp/chrome-kws-m54").resolve()
ART = Path(".workbuddy/tmp")
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, evidence: str) -> None:
    (PASS if ok else FAIL).append(f"{name}: {evidence}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {evidence}")


async def seed_m54(engine) -> None:  # type: ignore[no-untyped-def]
    """幂等造数：长者/设备/管理员复用 dryrun 固定 UUID；家属用 M54 独立账号。

    清理范围只覆盖本脚本自有行——不动张兰的 active 家属绑定（X1 联调种子）。
    """
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import AsyncSession

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

    async with engine.begin() as conn:
        await conn.execute(delete(AuditLog).where(AuditLog.resource_id == ELDER_ID))
        await conn.execute(delete(Voiceprint).where(Voiceprint.elder_id == ELDER_ID))
        await conn.execute(delete(Device).where(Device.device_id == DEVICE_ID))
        await conn.execute(
            delete(ElderGuardian).where(
                (ElderGuardian.elder_id == ELDER_ID)
                | (ElderGuardian.guardian_id == M54_GUARDIAN_ID)
            )
        )
        await conn.execute(delete(Elder).where(Elder.elder_id == ELDER_ID))
        await conn.execute(
            delete(GuardianAccount).where(GuardianAccount.guardian_id == M54_GUARDIAN_ID)
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
                    guardian_id=M54_GUARDIAN_ID,
                    name="M5.4 断言家属",
                    phone=M54_GUARDIAN_PHONE,
                    password_hash=hash_password(M54_GUARDIAN_PASSWORD),
                    token_version=0,
                    status="active",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                ElderGuardian(
                    elder_id=ELDER_ID,
                    guardian_id=M54_GUARDIAN_ID,
                    relation="儿子",
                    status="active",
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
            "--use-fake-ui-for-media-stream",
            "--remote-debugging-port=9224",
            "--remote-allow-origins=*",
            "--autoplay-policy=no-user-gesture-required",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,900",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def cdp_eval(ws_url: str, expression: str, tmo: float = 15) -> str:
    async with connect(ws_url, max_size=20 << 20, open_timeout=10, ping_interval=5) as ws:
        await ws.send(
            json.dumps(
                {"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression}}
            )
        )
        deadline = time.monotonic() + tmo
        while time.monotonic() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if msg.get("id") == 1:
                result = msg["result"]["result"]
                if result.get("subtype") == "error":
                    raise RuntimeError(result.get("description", "evaluate error"))
                return result.get("value", "")
    raise TimeoutError("CDP evaluate timeout")


async def cdp_screenshot(ws_url: str, path: Path) -> None:
    async with connect(ws_url, max_size=20 << 20, open_timeout=10, ping_interval=5) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.enable", "params": {}}))
        await ws.send(
            json.dumps({"id": 2, "method": "Page.captureScreenshot", "params": {"format": "png"}})
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if msg.get("id") == 2:
                data = msg["result"]["data"]
                path.write_bytes(base64.b64decode(data))  # noqa: ASYNC240 — 截图落盘为一次性小写入
                print(f"    截图已存 {path}")
                return
    raise TimeoutError("screenshot timeout")


async def get_page_ws(client: httpx.AsyncClient) -> str:
    targets = (await client.get(f"{CDP}/json")).json()
    pages = [t for t in targets if t.get("type") == "page"]
    return pages[0]["webSocketDebuggerUrl"]


async def wait_for_text(ws_url: str, needles: tuple[str, ...], budget_s: float) -> str:
    """轮询页面正文直到命中任一关键词，返回最终正文（供断言留痕）。"""
    deadline = time.monotonic() + budget_s
    body = ""
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        try:
            body = await cdp_eval(ws_url, "window.document.body.innerText.slice(0, 400)")
        except Exception as exc:  # 页面导航中 evaluate 可能抛错
            print(f"    取正文异常（导航中）：{exc}")
            continue
        if any(n in body for n in needles):
            return body
    return body


async def phase_browser(token: str, device_json: str) -> None:
    print("== B 浏览器页断言 ==")
    async with httpx.AsyncClient(timeout=15) as client:
        proc = launch_chrome()
        try:
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

            # B1 pad：注入已吊销 token → 启动即 401/4401 → 回登录页
            print("  [B1] 导航 pad 页……")
            await cdp_eval(ws_url, f"location.href = {json.dumps(PAGE)}")
            await asyncio.sleep(3)
            expires_at = int(time.time() * 1000 + 2 * 3600 * 1000)
            inject = (
                "sessionStorage.clear();"
                f"sessionStorage.setItem('kb_access_token', {json.dumps(token)});"
                f"sessionStorage.setItem('kb_expires_at', {expires_at});"
                f"sessionStorage.setItem('kb_device', {json.dumps(device_json)});"
                "'injected'"
            )
            await cdp_eval(ws_url, inject)
            await cdp_eval(ws_url, "location.reload()")
            print("  [B1] 等待被动登出回登录页……")
            body = await wait_for_text(ws_url, ("请输入设备编号登录",), 40)
            ok = "请输入设备编号登录" in body
            check("B1 pad 旧 token→回登录页", ok, f"页面正文含登录表单={ok}")
            await cdp_screenshot(ws_url, ART / "m54_pad_401_login.png")

            # B2 family：退房后家属 UI 登录 → 首页空态
            print("  [B2] 导航 family 页并登录……")
            await cdp_eval(ws_url, "sessionStorage.clear(); localStorage.clear(); 'cleared'")
            await cdp_eval(ws_url, f"location.href = {json.dumps(FAMILY_PAGE)}")
            await asyncio.sleep(3)
            fill = (
                "const set=(sel,val)=>{const el=document.querySelector(sel);"
                "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')"
                ".set.call(el,val);el.dispatchEvent(new Event('input',{bubbles:true}));};"
                f"set('#family-phone',{json.dumps(M54_GUARDIAN_PHONE)});"
                f"set('#family-password',{json.dumps(M54_GUARDIAN_PASSWORD)});"
                "document.querySelector('button[type=\"submit\"]').click();'clicked'"
            )
            await cdp_eval(ws_url, fill)
            body = await wait_for_text(
                ws_url, ("您还没有绑定长者", "未绑定长者", "手机号或密码不正确"), 25
            )
            ok = "您还没有绑定长者" in body or "未绑定长者" in body
            check("B2 家属登录→空态", ok, f"页面正文含空态文案={ok}")
            await cdp_screenshot(ws_url, ART / "m54_family_empty.png")
        finally:
            proc.kill()


async def main(skip_browser: bool = False) -> int:
    print("== M5.4 退房被动链路 · 前端断言实测 ==")
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
        health = await client.get("/health")
        if health.status_code != 200:
            print("先启动后端 8000")
            return 1

        # 复用 dryrun 同款造数（幂等），保证可重复复跑
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.core.config import get_settings

        engine = create_async_engine(get_settings().database_url)
        await seed_m54(engine)
        await engine.dispose()
        print("  [SEED] M54 固定造数完成（长者/设备/管理员复用 dryrun UUID，家属独立账号）")

        # A1 退房前设备会话
        dev = await client.post(
            "/api/device-sessions",
            json={"device_id": str(DEVICE_ID), "device_secret": DEVICE_SECRET},
        )
        check("A1 设备会话签发（退房前）", dev.status_code == 200, f"status={dev.status_code}")
        token = dev.json()["access_token"]
        device_json = json.dumps(dev.json()["device"], separators=(",", ":"))

        # 登录：家属 + admin
        glogin = await client.post(
            "/api/sessions",
            json={"phone": M54_GUARDIAN_PHONE, "password": M54_GUARDIAN_PASSWORD},
        )
        check("家属登录", glogin.status_code == 200, f"status={glogin.status_code}")
        gh = {"Authorization": f"Bearer {glogin.json()['access_token']}"}
        alogin = await client.post(
            "/api/sessions", json={"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD}
        )
        ah = {"Authorization": f"Bearer {alogin.json()['access_token']}"}

        # A2 退房主链路
        checkout = await client.post(
            "/api/lifecycle/checkout",
            json={"elder_id": str(ELDER_ID), "confirm": True},
            headers=ah,
        )
        check("A2 checkout 200", checkout.status_code == 200, f"status={checkout.status_code}")

        # A3 旧设备 token 失效（后端侧）
        replay = await client.get(
            "/api/sessions/current", headers={"Authorization": f"Bearer {token}"}
        )
        check(
            "A3 旧设备 token→401 TOKEN_REVOKED",
            replay.status_code == 401 and replay.json()["code"] == "TOKEN_REVOKED",
            f"status={replay.status_code} code={replay.json().get('code')}",
        )

        # A4 家属 F0 清单空（页面空态数据源）
        f0 = await client.get("/api/family/elders", headers=gh)
        body = f0.json() if f0.status_code == 200 else {}
        items = body.get("elders") or body.get("items") or []
        check(
            "A4 家属 F0 清单不含已退房长者",
            f0.status_code == 200 and str(ELDER_ID) not in f0.text,
            f"status={f0.status_code} items={len(items)}",
        )

        # A5 家属查该长者日报 → 403+审计
        fr = await client.get(
            f"/api/family/elders/{ELDER_ID}/reports",
            headers=gh,
        )
        check(
            "A5 家属查已退房长者日报→403 SCOPE_FORBIDDEN",
            fr.status_code == 403 and fr.json()["code"] == "SCOPE_FORBIDDEN",
            f"status={fr.status_code} code={fr.json().get('code')}",
        )

        ART.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — 证据目录一次性创建
        (ART / "m54_state.json").write_text(
            json.dumps(
                {
                    "device_token_revoked": token,
                    "device": dev.json()["device"],
                    "guardian_phone": M54_GUARDIAN_PHONE,
                    "checked_out_elder": str(ELDER_ID),
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    if not skip_browser:
        await phase_browser(token, device_json)
    print(f"\n== 结果：{len(PASS)} PASS / {len(FAIL)} FAIL ==")
    for f in FAIL:
        print(f"  FAIL -> {f}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    import argparse

    _ap = argparse.ArgumentParser()
    _ap.add_argument("--skip-browser", action="store_true", help="只跑 A 段 API 断言")
    _args = _ap.parse_args()
    sys.exit(asyncio.run(main(skip_browser=_args.skip_browser)))
