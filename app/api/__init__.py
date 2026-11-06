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

from app.api import audio, auth, devices, elders, health, pad

#: (路由器, 挂载前缀) —— 顺序与 `main.py` 原先手写的 6 条保持一致
ROUTERS: tuple[tuple[APIRouter, str], ...] = (
    (health.router, ""),  # /health
    (auth.router, "/api"),  # 路由内只写 /sessions 等，需补 /api
    (elders.router, ""),  # 自带 /api/elders 前缀
    (devices.router, ""),  # 自带 /api/devices 前缀
    (audio.router, ""),  # 自带 /api/audio 前缀（C3）
    (pad.router, ""),  # 自带 /api/pad 前缀（C7）
)

__all__ = ["ROUTERS"]
