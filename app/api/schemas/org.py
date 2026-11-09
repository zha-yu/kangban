"""admin 房间/床位管理请求/响应模型（计划 v1.5 §3）。

只放 Pydantic v2 模型，不做业务判断：
- 空值/非法枚举/显式 `occupied` 一律由模型层 422（`VALIDATION_ERROR`）；
- `extra="forbid"` 让客户端无法夹带 `status` 派生字段（如 `elder_id`）；
- 唯一性、删除受阻、批量占用等业务冲突由 `app/services/org_service.py` 判 409。
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RoomStatus = Literal["available", "partial", "full", "maintenance"]
BedStatus = Literal["available", "occupied", "maintenance", "disabled"]
#: PATCH / 批量可写的床位状态；`occupied` 只能由 elder_id 派生，故不在白名单
BedMutableStatus = Literal["available", "maintenance", "disabled"]


class RoomCreateRequest(BaseModel):
    """新建房间：房号必填，服务层再做去空白与唯一校验。"""

    model_config = ConfigDict(extra="forbid")

    room_no: str = Field(min_length=1, max_length=64, description="房号（如 A-301），全局唯一")


class RoomUpdateRequest(BaseModel):
    """改房号：只允许房号一个字段，不允许借此改房间状态。"""

    model_config = ConfigDict(extra="forbid")

    room_no: str = Field(min_length=1, max_length=64, description="新-房号，全局唯一")


class BedCreateRequest(BaseModel):
    """新建床位：所属房间必填，床号在同一房间内唯一。"""

    model_config = ConfigDict(extra="forbid")

    room_id: uuid.UUID
    bed_no: str = Field(min_length=1, max_length=32, description="床号（如 1 号床）")


class BedUpdateRequest(BaseModel):
    """改床号/状态：字段可空表示不改；显式 `occupied` 在模型层即 422。"""

    model_config = ConfigDict(extra="forbid")

    bed_no: str | None = Field(default=None, min_length=1, max_length=32)
    status: BedMutableStatus | None = None

    @field_validator("bed_no", "status")
    @classmethod
    def _reject_explicit_null(cls, value: str | None) -> str:
        """两个字段都不是可空列：显式传 null 要 422，不能落到服务层当「未传」。"""
        if value is None:
            raise ValueError("must not be null")
        return value


class BedBatchStatusRequest(BaseModel):
    """批量改状态：空数组、`occupied` 与非法枚举在模型层被拒。"""

    model_config = ConfigDict(extra="forbid")

    bed_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    status: BedMutableStatus


class BedBatchDeleteRequest(BaseModel):
    """批量删空床：只收 id 列表。"""

    model_config = ConfigDict(extra="forbid")

    bed_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class BedSummaryResponse(BaseModel):
    """房间列表里的床位摘要（含空床）。"""

    bed_id: uuid.UUID
    bed_no: str
    status: BedStatus
    elder_id: uuid.UUID | None


class RoomItemResponse(BaseModel):
    """房间列表项：房间 + 全量床位。"""

    room_id: uuid.UUID
    room_no: str
    status: RoomStatus
    beds: list[BedSummaryResponse]


class RoomResponse(BaseModel):
    """房间写操作响应（新建/改房号）。"""

    room_id: uuid.UUID
    room_no: str
    status: RoomStatus


class RoomListResponse(BaseModel):
    items: list[RoomItemResponse]
    total: int
    page: int
    page_size: int


class BedItemResponse(BaseModel):
    """床位列表/写操作响应：`room_no` 由服务层 JOIN 得出。"""

    bed_id: uuid.UUID
    room_id: uuid.UUID
    room_no: str
    bed_no: str
    status: BedStatus
    elder_id: uuid.UUID | None


class BedListResponse(BaseModel):
    items: list[BedItemResponse]
    total: int
    page: int
    page_size: int


class BedBatchResponse(BaseModel):
    """批量操作结果：实际生效的床位条数（重复 id 已去重）。"""

    affected: int


__all__ = [
    "BedBatchDeleteRequest",
    "BedBatchResponse",
    "BedBatchStatusRequest",
    "BedCreateRequest",
    "BedItemResponse",
    "BedListResponse",
    "BedMutableStatus",
    "BedStatus",
    "BedSummaryResponse",
    "BedUpdateRequest",
    "RoomCreateRequest",
    "RoomItemResponse",
    "RoomListResponse",
    "RoomResponse",
    "RoomStatus",
    "RoomUpdateRequest",
]
