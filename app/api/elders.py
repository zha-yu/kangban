"""长者档案 REST 薄层（接口契约 E5–E8）。

只做四件事：Pydantic 校验、接收当前主体、调用 `app/services/elder_service.py`、
把领域异常映射成契约 §7 的响应体。**不拼查询、不做范围过滤**（AGENTS §2）。

契约要求的错误体是 `{detail, code, trace_id}`（§7），而 task-4 只产出带 `code` 的领域异常，
`app/core/errors.py` 尚不存在且不在本卡写作用域内。因此这里用 `ContractAPIRoute`
在路由内统一映射：它在依赖解析与响应生成的同一层捕获异常，`get_session` 的
rollback/commit 语义不受影响；等 task-7 挂载统一错误处理器后可整体撤掉。
同一路由类还按 §7 区分 400（路径参数格式错 / 非法 JSON）与 422（字段校验失败）。

`ContractAPIRoute` 与 `_error_response` 也被 `app/api/devices.py` 复用 —— 本卡只允许改五个
文件，不新建 `app/api/_errors.py`，所以共享件落在这里。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.core.auth import AuthError, RoleForbiddenError, get_current_principal
from app.core.context import get_trace_id
from app.core.runtime_context import Identity
from app.db import get_session
from app.services import elder_service
from app.services.device_service import (
    DeviceConflictError,
    DeviceNotFoundError,
    DeviceScopeForbiddenError,
)
from app.services.elder_service import (
    ElderConflictError,
    ElderDetail,
    ElderNotFoundError,
    ElderScopeForbiddenError,
    ElderValidationError,
)

ElderStatus = Literal["active", "away", "transferred", "archived"]


def _error_response(status_code: int, code: str, detail: str) -> JSONResponse:
    """契约 §7 的错误体；`trace_id` 由中间件写入 contextvar，这里只负责带出。"""
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": code, "trace_id": get_trace_id() or "-"},
    )


def _is_request_format_error(exc: RequestValidationError) -> bool:
    """区分「请求格式坏」与「字段校验失败」。

    契约 §7 把**路径参数格式错**与**请求体不是合法 JSON** 归为 400 `BAD_REQUEST`，
    其余 Pydantic 字段问题才是 422 `VALIDATION_ERROR`。FastAPI 默认把两者都塞进
    `RequestValidationError`，只能按 `loc` 与 `type` 分辨：

    - `loc[0] == "path"`：路径参数被 Pydantic 解析失败（如 UUID 写成 `not-a-uuid`）；
    - `type == "json_invalid"`：请求体根本不是合法 JSON（不是字段值不合法）。
    """
    for error in exc.errors():
        if error.get("type") == "json_invalid":
            return True
        location = error.get("loc") or ()
        if location[:1] == ("path",):
            return True
    return False


class ContractAPIRoute(APIRoute):
    """把领域异常映射成契约错误体的路由类。

    为什么用路由类而不是 `app.add_exception_handler`：本卡不允许改 `app/main.py`，
    而错误映射必须随 router 一起被 task-7 挂载，不能要求调用方再补一层配置。
    捕获点在 `original_handler` 之外，因此 `get_session` 已经按异常路径完成 rollback。
    """

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original_handler = super().get_route_handler()

        async def contract_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except RoleForbiddenError:
                return _error_response(403, "ROLE_FORBIDDEN", "forbidden")
            except AuthError as exc:
                # 凭证缺失/失效/被撤销一律 401；码保留 task-4 的分类（TOKEN_*）。
                return _error_response(401, exc.code, "credential rejected")
            except (ElderScopeForbiddenError, DeviceScopeForbiddenError):
                return _error_response(403, "SCOPE_FORBIDDEN", "forbidden")
            except (ElderNotFoundError, DeviceNotFoundError):
                return _error_response(404, "NOT_FOUND", "not found")
            except DeviceConflictError:
                return _error_response(409, "CONFLICT", "conflict")
            except ElderConflictError:
                return _error_response(409, "CONFLICT", "conflict")
            except ElderValidationError:
                return _error_response(422, "VALIDATION_ERROR", "invalid request")
            except RequestValidationError as exc:
                if _is_request_format_error(exc):
                    return _error_response(400, "BAD_REQUEST", "malformed request")
                return _error_response(422, "VALIDATION_ERROR", "request validation failed")

        return contract_handler


router = APIRouter(prefix="/api/elders", tags=["elders"], route_class=ContractAPIRoute)


# ------------------------------------------------------------------ 请求 / 响应模型


class ElderCreateRequest(BaseModel):
    """E5 请求体。`name` 不唯一，允许重名；`persona_id` 可空。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    birth_date: date | None = None
    gender: str | None = Field(default=None, max_length=20)
    persona_id: uuid.UUID | None = None


class ElderPatchRequest(BaseModel):
    """E8 请求体：**只允许**白名单字段；`extra="forbid"` 让 session_seq 等直接 422。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=50)
    birth_date: date | None = None
    gender: str | None = Field(default=None, max_length=20)
    persona_id: uuid.UUID | None = None
    status: ElderStatus | None = None

    @field_validator("name", "status")
    @classmethod
    def _reject_explicit_null(cls, value: str | None) -> str:
        """`name` / `status` 是 NOT NULL 列：显式传 null 要在 422，不能落到 500。"""
        if value is None:
            raise ValueError("must not be null")
        return value


class ElderCreateResponse(BaseModel):
    """E5 响应：契约只列这四个字段。"""

    elder_id: uuid.UUID
    status: str
    session_seq: int
    created_at: datetime


class ElderDetailResponse(BaseModel):
    """E6 响应：R4 全字段 + `room_no / bed_no`（S-05）。"""

    elder_id: uuid.UUID
    name: str
    birth_date: date | None
    gender: str | None
    persona_id: uuid.UUID | None
    status: str
    session_seq: int
    created_at: datetime
    room_no: str | None
    bed_no: str | None


class ElderListItemResponse(BaseModel):
    """E7 列表项：不返 `birth_date / gender / persona_id`（S-07）。"""

    elder_id: uuid.UUID
    name: str
    room_no: str | None
    bed_no: str | None
    status: str


class ElderListResponse(BaseModel):
    items: list[ElderListItemResponse]
    total: int
    page: int
    page_size: int


class ElderBedAssignRequest(BaseModel):
    """E19 请求体：目标床位。"""

    model_config = ConfigDict(extra="forbid")

    bed_id: uuid.UUID


class ElderBedAssignResponse(BaseModel):
    """E19 响应：入床后的定位三字段。"""

    elder_id: uuid.UUID
    bed_id: uuid.UUID
    room_id: uuid.UUID


def _detail_response(detail: ElderDetail) -> ElderDetailResponse:
    return ElderDetailResponse(
        elder_id=detail.elder_id,
        name=detail.name,
        birth_date=detail.birth_date,
        gender=detail.gender,
        persona_id=detail.persona_id,
        status=detail.status,
        session_seq=detail.session_seq,
        created_at=detail.created_at,
        room_no=detail.room_no,
        bed_no=detail.bed_no,
    )


# ------------------------------------------------------------------ 端点


@router.post(
    "",
    response_model=ElderCreateResponse,
    status_code=201,
    summary="长者建档（admin/care_worker，契约 v1.8）",
)
async def create_elder(
    payload: ElderCreateRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ElderCreateResponse:
    detail = await elder_service.create_elder(
        session,
        identity,
        name=payload.name,
        birth_date=payload.birth_date,
        gender=payload.gender,
        persona_id=payload.persona_id,
    )
    return ElderCreateResponse(
        elder_id=detail.elder_id,
        status=detail.status,
        session_seq=detail.session_seq,
        created_at=detail.created_at,
    )


@router.get(
    "/{elder_id}",
    response_model=ElderDetailResponse,
    summary="长者详情（admin 全量；护工/医生限本人 relation；家属/设备 403）",
)
async def read_elder(
    elder_id: uuid.UUID,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ElderDetailResponse:
    detail = await elder_service.get_elder(session, identity, elder_id)
    return _detail_response(detail)


@router.get("", response_model=ElderListResponse, summary="长者列表（按角色范围过滤）")
async def list_elders(
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    page: Annotated[int, Query(ge=1, description="页码，默认 1")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，默认 20，上限 100")] = 20,
    status: Annotated[ElderStatus | None, Query(description="按长者状态过滤")] = None,
) -> ElderListResponse:
    result = await elder_service.list_elders(
        session, identity, page=page, page_size=page_size, status=status
    )
    return ElderListResponse(
        items=[
            ElderListItemResponse(
                elder_id=item.elder_id,
                name=item.name,
                room_no=item.room_no,
                bed_no=item.bed_no,
                status=item.status,
            )
            for item in result.items
        ],
        total=result.total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/{elder_id}/bed",
    response_model=ElderBedAssignResponse,
    summary="长者入床（admin/care_worker；床位被占 409，换床先腾旧床）",
)
async def assign_elder_bed(
    elder_id: uuid.UUID,
    payload: ElderBedAssignRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ElderBedAssignResponse:
    assignment = await elder_service.assign_bed(session, identity, elder_id, bed_id=payload.bed_id)
    return ElderBedAssignResponse(
        elder_id=assignment.elder_id,
        bed_id=assignment.bed_id,
        room_id=assignment.room_id,
    )


@router.patch(
    "/{elder_id}",
    response_model=ElderDetailResponse,
    summary="长者档案修改（仅 admin；archived 只允许改状态）",
)
async def update_elder(
    elder_id: uuid.UUID,
    payload: ElderPatchRequest,
    identity: Annotated[Identity, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ElderDetailResponse:
    changes: dict[str, Any] = payload.model_dump(exclude_unset=True)
    detail = await elder_service.update_elder(session, identity, elder_id, changes)
    return _detail_response(detail)


__all__ = [
    "ContractAPIRoute",
    "router",
]
