"""task-5 端点手动验证脚本。"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid

import httpx

BASE = "http://127.0.0.1:8000"
PASS = "test123"
DEV_SECRET = "device-long-secret-plain-abc123"


def _show(label: str, resp: httpx.Response) -> None:
    body = resp.json()
    print(f"\n=== {label} ===")
    print(f"HTTP {resp.status_code}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE) as c:
        # 1. health
        r = await c.get("/health")
        _show("GET /health", r)

        # 2. staff login
        r = await c.post(
            "/api/sessions",
            json={"phone": "13800000001", "password": PASS},
        )
        _show("POST /api/sessions (staff)", r)
        if r.status_code != 200:
            print("❌ 登录失败，后续无法验证")
            sys.exit(1)
        login = r.json()
        access = login["access_token"]
        refresh = login["refresh_token"]

        # 3. guardian login
        r = await c.post(
            "/api/sessions",
            json={"phone": "13900000001", "password": PASS},
        )
        _show("POST /api/sessions (guardian)", r)

        # 4. wrong password → 401
        r = await c.post(
            "/api/sessions",
            json={"phone": "13800000001", "password": "wrong"},
        )
        _show("POST /api/sessions (wrong pwd → 401)", r)

        # 5. GET /current with staff token
        r = await c.get(
            "/api/sessions/current",
            headers={"Authorization": f"Bearer {access}"},
        )
        _show("GET /api/sessions/current (staff)", r)

        # 6. fake token → 401
        r = await c.get(
            "/api/sessions/current",
            headers={"Authorization": "Bearer fake.fake.fake"},
        )
        _show("GET /api/sessions/current (fake token → 401)", r)

        # 7. refresh
        r = await c.post(
            "/api/sessions/refresh",
            json={"refresh_token": refresh},
        )
        _show("POST /api/sessions/refresh", r)

        # 8. device session — 查 DB 里的 device_id
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool
        from app.core.config import get_settings

        async def _get_device_id():
            eng = create_async_engine(get_settings().database_url, poolclass=NullPool)
            async with eng.begin() as conn:
                row = (await conn.execute(text("SELECT device_id FROM devices LIMIT 1"))).fetchone()
            await eng.dispose()
            return row[0] if row else None

        dev_id = await _get_device_id()
        if dev_id:
            r = await c.post(
                "/api/device-sessions",
                json={
                    "device_id": str(dev_id),
                    "device_secret": DEV_SECRET,
                },
            )
            _show("POST /api/device-sessions", r)

            # 9. device wrong secret → 401
            r = await c.post(
                "/api/device-sessions",
                json={
                    "device_id": str(dev_id),
                    "device_secret": "wrong",
                },
            )
            _show("POST /api/device-sessions (wrong secret → 401)", r)

        print("\n✅ 全部端点验证完成")


if __name__ == "__main__":
    asyncio.run(main())
