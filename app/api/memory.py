"""记忆管理 API（Day 9 · C4 · contracts X5）：查看 / 纠正 / 删除。

**薄路由**：三个端点直调 dev-B 的 `memory_admin_service` 三函数（X5 冻结签名），
**隔离与审计全部在 B 的 service 层**（``chunk_id`` 不属于该长者 → 403＋审计，
闸门②/AC-39）——本文件**不重复实现隔离逻辑**（契约 X5「禁止」行）。

- **admin-only**：console 记忆管理页消费（PRD line 352「长者或管理员」——长者侧走
  对话链路，不经本 REST；越权口径联调期与 B2 对齐后如需放宽只改 require_roles 一行）；
- **骨架先行**：`memory_admin_service`（B2 · X5）未交付时返回 **503 X5_NOT_DELIVERED**，
  不让 import 失败炸掉整个 app（对齐 tts.py 惰性 import 口径）；
- 纠正语义：旧值标 ``superseded``、新值生效，不物理删旧行（FR-MEM-03/06）；
- 删除语义：立即不再被召回（FR-MEM-05/08）。
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import require_roles
from app.core.runtime_context import Identity
from app.db import get_session

try:  # 骨架先行：B2（X5）交付前保持可 import，调用时 503
    from app.services import memory_admin_service
except ImportError:  # pragma: no cover —— B2 落地后此分支消失
    memory_admin_service = None  # type: ignore[assignment]

router = APIRouter(
    prefix="/api/memory",
    tags=["memory"],
    route_class=ContractAPIRoute,
)

_X5_FUNCTIONS: tuple[str, ...] = ("list_memories", "correct_memory", "delete_memory")


def _require_x5() -> Any:
    """X5 未交付（模块缺函数）→ 503，不编造响应。"""
    svc = memory_admin_service
    if svc is None or not all(hasattr(svc, name) for name in _X5_FUNCTIONS):
        return None
    return svc


def _serialize(obj: object) -> dict[str, Any]:
    """X5 返回值（MemoryView / MemoryRef dataclass）→ JSON dict；未知形态如实透传。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return jsonable_encoder(dataclasses.asdict(obj))  # type: ignore[arg-type]
    return jsonable_encoder(obj)


class MemoryCorrectIn(BaseModel):
    """X5 纠正入参（契约：``correct_memory(…, new_text: str)``）。"""

    model_config = ConfigDict(extra="forbid")

    new_text: str


@router.get("/{elder_id}/memories", response_model=dict)
async def list_memories(
    elder_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _staff: Annotated[Identity, Depends(require_roles("admin"))],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """查看某长者记忆（X5 ``list_memories``；隔离在 service 强制 ``WHERE elder_id``）。"""
    svc = _require_x5()
    if svc is None:
        return _error_response(503, "X5_NOT_DELIVERED", "memory_admin_service is not delivered yet")
    views = await svc.list_memories(session, elder_id=elder_id, limit=limit, offset=offset)
    return {"items": [_serialize(v) for v in views], "limit": limit, "offset": offset}


@router.patch("/{elder_id}/memories/{chunk_id}", response_model=dict)
async def correct_memory(
    elder_id: uuid.UUID,
    chunk_id: uuid.UUID,
    body: MemoryCorrectIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    _staff: Annotated[Identity, Depends(require_roles("admin"))],
) -> dict[str, object]:
    """纠正一条记忆（X5 ``correct_memory``：旧值 superseded、新值生效，FR-MEM-03/06）。"""
    svc = _require_x5()
    if svc is None:
        return _error_response(503, "X5_NOT_DELIVERED", "memory_admin_service is not delivered yet")
    ref = await svc.correct_memory(
        session, elder_id=elder_id, chunk_id=chunk_id, new_text=body.new_text
    )
    return _serialize(ref)


@router.delete("/{elder_id}/memories/{chunk_id}", response_model=dict)
async def delete_memory(
    elder_id: uuid.UUID,
    chunk_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _staff: Annotated[Identity, Depends(require_roles("admin"))],
) -> dict[str, object]:
    """删除一条记忆（X5 ``delete_memory``：立即不再被召回，FR-MEM-05/08）。"""
    svc = _require_x5()
    if svc is None:
        return _error_response(503, "X5_NOT_DELIVERED", "memory_admin_service is not delivered yet")
    ref = await svc.delete_memory(session, elder_id=elder_id, chunk_id=chunk_id)
    return _serialize(ref)
