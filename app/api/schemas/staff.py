"""建号接口请求/响应模型（T2）。

owner=建号接口（adhoc-20261106）· 依据 docs/plans/adhoc-20261106-员工与家属建号接口.md

只放 Pydantic v2 模型，不做业务判断；校验失败由 FastAPI 统一转 422（契约 §7）。
手机号统一 11 位中国大陆号码；响应里的手机号一律脱敏，不回传完整号码。
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

StaffRole = Literal["admin", "care_worker", "doctor"]
StaffStatus = Literal["active", "disabled"]

_PHONE_PATTERN = r"^1\d{10}$"


class StaffCreateRequest(BaseModel):
    """建员工账号（E12）。`elder_ids` 为空表示暂不分配长者。"""

    name: str = Field(min_length=1, max_length=64, description="姓名")
    role: StaffRole = Field(description="admin / care_worker / doctor")
    phone: str = Field(pattern=_PHONE_PATTERN, description="11 位手机号，全局唯一（跨员工与家属）")
    password: str = Field(min_length=8, max_length=128, description="口令明文，服务端只存哈希")
    elder_ids: list[uuid.UUID] = Field(
        default_factory=list, description="要绑定的长者；admin 不接受该项"
    )


class StaffUpdateRequest(BaseModel):
    """改角色或停用（E12 PATCH）。两个字段都可空，同时给时一起生效。"""

    role: StaffRole | None = None
    status: StaffStatus | None = None


class StaffResponse(BaseModel):
    """员工账号的对外视图（不含口令哈希、不含完整手机号）。"""

    staff_id: uuid.UUID
    name: str
    role: str
    phone_masked: str
    status: str


class GuardianCreateRequest(BaseModel):
    """建家属账号（G1，契约缺口补齐）。"""

    name: str = Field(min_length=1, max_length=64, description="姓名")
    phone: str = Field(pattern=_PHONE_PATTERN, description="11 位手机号，全局唯一")
    password: str = Field(min_length=8, max_length=128, description="口令明文，服务端只存哈希")


class GuardianCreateResponse(BaseModel):
    """家属账号的对外视图。"""

    guardian_id: uuid.UUID
    name: str
    phone_masked: str
    status: str


class GuardianBindRequest(BaseModel):
    """把已有家属账号绑定到某位长者（E13）。"""

    guardian_id: uuid.UUID
    relation: str = Field(min_length=1, max_length=32, description="与长者关系：子/女/孙辈/配偶…")


class GuardianBindResponse(BaseModel):
    """绑定结果。"""

    elder_id: uuid.UUID
    guardian_id: uuid.UUID
    relation: str
    status: str


__all__ = [
    "GuardianBindRequest",
    "GuardianBindResponse",
    "GuardianCreateRequest",
    "GuardianCreateResponse",
    "StaffCreateRequest",
    "StaffResponse",
    "StaffRole",
    "StaffStatus",
    "StaffUpdateRequest",
]
