# owner=A3 · 依据 C7 v1.0 · PRD FR-3D-01/02（前端渲染所需的前置数据）
"""长者端（pad）渲染前置数据 REST 薄层 —— C7 `GET /api/pad/avatar-config`。

Day 8 追加（§P-1）：`POST /api/pad/health-records` 长者端每日健康采集 5 问的
答案回传落库口（设备令牌鉴权，elder_id / source 服务端解出，前端不传）。

本文件只做四件事：取设备主体、调 `app/services/persona_service.py`、按 C7 序列化四键、
把「设备没绑长者」映射成 403。**不拼查询、不读配置、不读 persona 表**（AGENTS §2）。

三条口径：

1. **只认设备凭证**：`current_device` 会先把非 `kind=device` 的主体判成 401 —— 拿错了凭证
   体系（员工令牌 / 家属令牌）时，403 会误导客户端「换个更高级的账号就能过」，
   而真实动作是「用平板自己的设备令牌重登」（契约 §7 的四类拒绝）。
2. **未绑定 → 403 `device_not_bound`**：C7 把 403 的错误体定为 `{detail, code, trace_id}`。
   这里不能返回 200 + `avatar_url=None` 糊过去 —— 那个 200 的语义是「后端没配形象，
   前端用内置兜底」，而「设备没绑长者」需要人工去照护台重新绑定，是两种不同的处置。
3. **不挂载**：路由注册留在 `app/main.py`（T6 负责），本卡不碰装配文件，避免与其他人冲突。

`_error_response` 与 `ContractAPIRoute` 复用 `app/api/elders.py`：契约 §7 的错误体目前只有
那一处实现（`app/core/errors.py` 尚未建立，也不在本卡写作用域内）。复制一份错误体形状
迟早与 elders/devices 分叉，而这正是「同一个错误有两种 body」的典型来源；等统一错误处理器
落地后，三个 API 模块一起撤掉即可。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.elders import ContractAPIRoute, _error_response
from app.api.health_records import _FOLLOW_UP, HealthRecordCreated
from app.core.auth import current_device
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import health_service, persona_service

router = APIRouter(prefix="/api/pad", tags=["pad"], route_class=ContractAPIRoute)


def _voice_source_for_now() -> Literal["voice_scheduled", "voice_active"]:
    """按当前上海时间判定 `health_records.source` 的 voice_ 前缀取值。

    **自裁口径**（PRD/TD 未定义 pad 直接回传的 source 判定，AGENTS 前置铁律 6）：
    采集窗口内（`health_checkup_start`–`end`，上海时间）提交视为定时采集
    `voice_scheduled`；窗口外提交视为主动补测 `voice_active`。只影响记录来源
    标签与展示，不参与分级；调整需同步 docs/team_tasks/day08/contracts.md §P-1。
    """
    from app.core.config import get_settings

    settings = get_settings()

    def _to_min(hhmm: str) -> int:
        hour_s, minute_s = hhmm.split(":")
        return int(hour_s) * 60 + int(minute_s)

    now_min = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
    minutes = now_min.hour * 60 + now_min.minute
    if _to_min(settings.health_checkup_start) <= minutes < _to_min(settings.health_checkup_end):
        return "voice_scheduled"
    return "voice_active"


class PadHealthRecordSubmit(BaseModel):
    """§P-1 请求体：五项采集指标任一非空；elder_id / source **不收**。

    值域照 H-2 契约口径（systolic/diastolic 0–300、sleep_hours 0–24、
    mood_score 1–5）；complaint 即第 5 问主诉文本（「没有」在前端解析为 null）。
    """

    model_config = ConfigDict(extra="forbid")

    systolic: int | None = Field(default=None, ge=0, le=300)
    diastolic: int | None = Field(default=None, ge=0, le=300)
    sleep_hours: float | None = Field(default=None, ge=0, le=24)
    mood_score: int | None = Field(default=None, ge=1, le=5)
    complaint: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _at_least_one_metric(self) -> PadHealthRecordSubmit:
        if all(
            value is None
            for value in (
                self.systolic,
                self.diastolic,
                self.sleep_hours,
                self.mood_score,
                self.complaint,
            )
        ):
            raise ValueError("至少提交一项健康指标（血压/睡眠/情绪/主诉）")
        return self


class AvatarConfigResponse(BaseModel):
    """C7 的 200 响应体：四个键**严格**为这四项。

    显式声明响应模型而不是直接返回服务层的 TypedDict，是为了让「契约形状」在 FastAPI 的
    响应校验里被强制一次：服务层将来多出一个字段，也不会悄悄漏给前端。
    """

    avatar_url: str | None
    fallback_image_url: str | None
    rotation_y_offset: float
    version: int


@router.get(
    "/avatar-config",
    response_model=AvatarConfigResponse,
    summary="长者端数字人形象配置（C7；设备未绑定长者返回 403）",
)
async def read_avatar_config(
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AvatarConfigResponse | JSONResponse:
    """返回平板当前绑定长者该渲染的数字人配置（只读，无写库、无副作用）。"""

    if identity.elder_id is None:
        # 403 而不是空配置，理由见模块 docstring 第 2 条；code 按 C7 用下划线命名。
        return _error_response(403, "device_not_bound", "device is not bound to an elder")

    # `Identity.__post_init__` 已把 elder_id 规范成小写连字 UUID 文本，这次转换不可能失败；
    # 真失败说明身份被手改坏了，让它冒成 500 比悄悄按「没绑定」降级更容易被发现。
    elder_id = uuid.UUID(identity.elder_id)
    config = await persona_service.get_avatar_config(session, elder_id=elder_id)
    return AvatarConfigResponse.model_validate(config)


@router.post(
    "/health-records",
    response_model=HealthRecordCreated,
    status_code=200,
    summary="长者端健康采集答案回传（§P-1；设备未绑定长者返回 403）",
)
async def submit_pad_health_record(
    body: PadHealthRecordSubmit,
    identity: Annotated[Identity, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HealthRecordCreated | JSONResponse:
    """pad 每日健康采集 5 问的答案落库口（§P-1）。

    - **只认设备凭证**（口径同 C7 avatar-config）：拿错凭证体系 → 401；
    - elder_id 从设备 Identity 解出，客户端不传（AGENTS §4.9）；
    - source 服务端按当前时刻定（窗口内 voice_scheduled / 窗口外 voice_active），
      客户端不可信任何来源字段；
    - 分级、risk_events / work_order 联动全部复用 `create_health_record`
      （voice_* 时 operator_id=None 合法），不建第二条写库路径。
    """
    if identity.elder_id is None:
        return _error_response(403, "device_not_bound", "device is not bound to an elder")

    elder_id = uuid.UUID(identity.elder_id)
    ref = await health_service.create_health_record(
        session,
        elder_id=elder_id,
        source=_voice_source_for_now(),
        systolic=body.systolic,
        diastolic=body.diastolic,
        sleep_hours=body.sleep_hours,
        mood_score=body.mood_score,
        mood_note=None,
        complaint=body.complaint,
        operator_id=None,
    )
    row = await health_service.get_record_row(session, record_id=ref.record_id)
    assert row is not None  # 同事务刚写入
    return HealthRecordCreated(
        record_id=ref.record_id,
        severity=ref.severity,
        rule_version=row.record.rule_version,
        recorded_at=row.record.recorded_at,
        follow_up=_FOLLOW_UP[ref.severity],
    )


__all__ = [
    "router",
]
