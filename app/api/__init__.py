"""REST 路由层：只做参数校验 + 权限范围过滤 + 调 `app.services`，不写业务逻辑、不碰 ORM 写库。

## 路由注册表（T0 · 2026-11-06）

`app/main.py` 不再逐条手写 `include_router`，而是遍历本文件的 `ROUTERS`。
这样每个新路由卡只需在自己的分支里**末尾追加一行**，不必改 `main.py` 那块公共代码，
多人并行时不会互相冲突（详见 `docs/plans/adhoc-20261106-建号接口-DAG.md` §6）。

约定：
- 元组顺序即注册顺序，`.` 前缀写 `""` 表示不加前缀（路由内部自带）。
- **只放 REST 路由**。`app/audio/signaling.py` 的 WS 路由留在 `main.py`，
  因为它是 `api → audio` 的反向依赖，放进来会破坏 AGENTS §2 的单向依赖约束。
- 新增路由必须保持「模块自带 prefix 就少加、不自带就补 `/api`」的既有口径。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    admin_org,
    alerts,
    audio,
    auth,
    devices,
    elders,
    family,
    guardians,
    health,
    health_records,
    medication,
    pad,
    reports,
    rooms,
    staff,
    voiceprints,
    workorders,
)

#: (路由器, 挂载前缀) —— 顺序与 `main.py` 原先手写的 6 条保持一致
ROUTERS: tuple[tuple[APIRouter, str], ...] = (
    (health.router, ""),  # /health
    (auth.router, "/api"),  # 路由内只写 /sessions 等，需补 /api
    (elders.router, ""),  # 自带 /api/elders 前缀
    (devices.router, ""),  # 自带 /api/devices 前缀
    (audio.router, ""),  # 自带 /api/audio 前缀（C3）
    (pad.router, ""),  # 自带 /api/pad 前缀（C7）
    # ↓ 2026-11-06 建号接口：以下只增不改，追加新行即可
    (staff.router, ""),  # 自带 /api/staff 前缀（E12）
    (guardians.router, ""),  # 自带 /api/guardians 与 /api/elders/{id}/guardians（G1/E13）
    (voiceprints.router, "/api"),  # 路由内只写 /voiceprints/*，需补 /api（C4）
    (rooms.router, ""),  # 自带 /api/rooms 前缀（E18，契约 v1.8）
    (admin_org.router, ""),  # 自带 /api/admin 前缀（房间/床位管理，计划 v1.5）
    (workorders.router, ""),  # 自带 /api/work-orders 前缀（f2-D1 console 看板，2026-11-10 提前件）
    (family.router, ""),  # 自带 /api/family 前缀（N1/N4 家属通知，Day 8 P8 提前件）
    (alerts.router, ""),  # 自带 /api 前缀（N-1/N-2/N-3 员工告警域，Q-D1 批准新增）
    (reports.router, ""),  # 自带 /api/family 前缀（F2 家属日报详情，Day 8 D1）
    (health_records.router, ""),  # 自带 /api/health-records 前缀（C1 健康域，Day 8）
    (medication.router, ""),  # 自带 /api/medication-plans 前缀（Day 8 dev-A H+1 药单管理 REST）
)

__all__ = ["ROUTERS"]
