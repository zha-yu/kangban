"""身份认证相关的 Pydantic 请求/响应 DTO（契约 §4 E1–E4）。

这些模型**只**负责 HTTP 层的入参校验和出参序列化，不承载业务逻辑、
也不 import ORM（分层硬约束：AGENTS §2）。

所有 `expires_in` 取 Settings 字段的计算值，不写字面量（契约 S-12 口径）。
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# E1：POST /api/sessions — 人账号登录（契约 §4）
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """手机号 + 密码登录请求。staff 与 guardian 共用此端点。"""

    phone: str = Field(..., min_length=1, max_length=32, description="手机号")
    password: str = Field(..., min_length=1, description="明文密码（只传，不存）")


class PrincipalInfo(BaseModel):
    """登录返回的主体基本信息。role 对 guardian 为 None。"""

    kind: Literal["staff", "guardian"]
    id: str
    role: str | None = None
    name: str


class LoginResponse(BaseModel):
    """登录成功响应：2h Access + 7d Refresh（PRD 180）。"""

    access_token: str
    refresh_token: str
    token_type: str = Field(default="bearer", description="OAuth 2.0 Bearer 方案")
    expires_in: int
    principal: PrincipalInfo


# ---------------------------------------------------------------------------
# E2：POST /api/sessions/refresh
# ---------------------------------------------------------------------------


class RefreshRequest(BaseModel):
    """刷新请求 —— 只需要 refresh_token。"""

    refresh_token: str = Field(..., min_length=1)


class RefreshResponse(BaseModel):
    """刷新成功响应：只返回新的 Access（Refresh 不轮换）。"""

    access_token: str
    expires_in: int


# ---------------------------------------------------------------------------
# E3：GET /api/sessions/current
# ---------------------------------------------------------------------------


class DeviceInfo(BaseModel):
    """设备当前身份里的设备字段（E4 响应 + E3 device 主体共用）。"""

    id: str
    room_id: str
    elder_id: str | None = None


class CurrentIdentityResponse(BaseModel):
    """当前身份响应（契约 E3）。

    scope_elder_ids 的取值规则：
    - admin → 空列表（全量权限）
    - care_worker / doctor → elder_staff_assignments (active) 的 elder_id 列表
    - guardian → elder_guardians (active) 的 elder_id 列表
    - device → devices.elder_id 或按 room 推导（由 auth_service 负责）
    """

    kind: Literal["staff", "guardian", "device", "staff_card"]
    id: str
    role: str | None = None
    name: str | None = None
    scope_elder_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# E4：POST /api/device-sessions — 设备换会话
# ---------------------------------------------------------------------------


class DeviceLoginRequest(BaseModel):
    """设备换会话请求：长期令牌明文（只在换会话这一次走明文）。"""

    device_id: uuid.UUID
    device_secret: str = Field(..., min_length=1)


class DeviceLoginResponse(BaseModel):
    """设备换会话成功：24h 设备会话（PRD 180）。"""

    access_token: str
    expires_in: int
    device: DeviceInfo


# ---------------------------------------------------------------------------
# 通用错误响应（契约 §7）
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """统一错误响应格式。trace_id 必带（AGENTS §8）。"""

    detail: str
    code: str
    trace_id: str
