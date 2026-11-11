# owner=D3 · 依据 PRD FR-ALT-06 / AC-26
"""pad 一键求助 REST（Day 8 D3，FR-ALT-06）。

口径：

1. **只认设备凭证**：`current_device` 先把非 `kind=device` 的主体判成 401；
   `elder_id` 只从 `devices` 绑定表读出（AGENTS §4.9），请求体不收任何身份字段。
2. **不经语音路由**：点击即调 `sos_service.trigger_sos` 生成红色事件＋护工工单
   ＋护工通知，与 ASR/路由/LLM 完全解耦。
3. **幂等**：service 在长者存在未关闭 sos 事件时复用原事件/工单，响应 `created=false`；
   连续点击不会重复建单。
4. **未绑定 → 403 `device_not_bound`**：与 `pad.py` 的 avatar-config 同口径，
   不返回 200 让长者误以为求助已发出。

响应体 `{event_id, order_id, created}` 为 **Day 8 自裁口径**（Q-D7 撤销/工单契约
尚未冻结，无冻结 schema 可依）。冻结后若字段变化，按契约修订记录适配，不静默改。

分层：本文件只做校验 + 调 `app/services/sos_service.py`，不拼查询、不写库。
路由经 `app/api/__init__.py` 的 `ROUTERS` 挂载，不改 `main.py`。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.core.auth import current_device
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import sos_service

router = APIRouter(prefix="/api/pad", tags=["pad"], route_class=ContractAPIRoute)


class SosTriggerResponse(BaseModel):
    """一键求助结果：事件/工单 ID + 是否新建（重复点击复用原单时为 false）。"""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    order_id: uuid.UUID | None
    created: bool


@router.post(
    "/sos",
    response_model=SosTriggerResponse,
    summary="pad 一键求助：生成红色事件＋护工工单（设备未绑定长者返回 403）",
)
async def trigger_sos(
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SosTriggerResponse | JSONResponse:
    """按下 SOS 常显按钮 → 落红色事件与护工工单，不经过语音路由。"""

    if identity.elder_id is None:
        return _error_response(403, "device_not_bound", "device is not bound to an elder")

    # Identity.__post_init__ 已规范成小写连字 UUID 文本；转换失败说明身份被手改坏，
    # 让它冒 500 比按「没绑定」降级更容易被发现（与 pad.py 同口径）。
    result = await sos_service.trigger_sos(session, elder_id=uuid.UUID(identity.elder_id))
    return SosTriggerResponse(
        event_id=result.event_id,
        order_id=result.order_id,
        created=result.created,
    )


__all__ = ["router"]
