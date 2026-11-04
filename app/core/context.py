"""请求级上下文：`trace_id` 的载体。

为什么用 `contextvars` 而不是参数透传：日志格式化器与中间件不在同一条调用链上，
只有 contextvar 能让「任何一行日志」自动拿到当前请求的 trace_id
（技术文档 §1.2：全链路统一 trace_id）。

`runtime_context`（elder_id / 角色注入）属于 Day 2 鉴权，本课只放 trace_id。
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token

# 对外约定：请求头与响应头同名
TRACE_HEADER = "X-Trace-Id"

# 只接受 8~64 位安全字符，防止把换行/引号塞进日志（日志注入）
_TRACE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

_trace_id_ctx: ContextVar[str | None] = ContextVar("trace_id", default=None)


def new_trace_id() -> str:
    """生成新 trace_id（32 位十六进制，与 OTel trace_id 长度同量级）。"""
    return uuid.uuid4().hex


def get_trace_id() -> str | None:
    return _trace_id_ctx.get()


def set_trace_id(trace_id: str) -> Token[str | None]:
    return _trace_id_ctx.set(trace_id)


def reset_trace_id(token: Token[str | None]) -> None:
    _trace_id_ctx.reset(token)


def is_valid_trace_id(value: str | None) -> bool:
    """上游透传的 trace_id 是否可信；不可信一律重新生成，不报错。"""
    # 显式判空而非 bool(value) 短路：mypy 无法借布尔短路收窄 value，原写法报 union-attr
    if not value:
        return False
    return bool(_TRACE_PATTERN.match(value.strip()))
