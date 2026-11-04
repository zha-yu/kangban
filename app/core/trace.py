"""trace_id 中间件：进出请求各打一行日志，贯穿整个调用链。

为什么是纯 ASGI 而不是 `BaseHTTPMiddleware`：后者在 Starlette 里以任务组包装下游应用，
对 SSE / WebRTC 信令这类长连接与流式响应有已知副作用（Day 5 起就要用）。纯 ASGI 只多
十几行，没有这层风险。

行为约定：
1. 请求带合法 `X-Trace-Id` 时透传（便于前端/网关串联），否则新生成；
2. 响应头一定回写 `X-Trace-Id`，前端可据此上报问题；
3. 无论成功失败都 reset contextvar，避免协程复用时串号。
"""

from __future__ import annotations

import logging
import time

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import (
    TRACE_HEADER,
    is_valid_trace_id,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
)

logger = logging.getLogger("app.http")


class TraceIdMiddleware:
    """纯 ASGI 中间件：注入 trace_id + 访问日志。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        incoming = headers.get(TRACE_HEADER)
        trace_id = incoming.strip() if is_valid_trace_id(incoming) else new_trace_id()

        token = set_trace_id(trace_id)
        scope.setdefault("state", {})["trace_id"] = trace_id
        started = time.perf_counter()
        status_code = 500

        async def send_with_trace(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[TRACE_HEADER] = trace_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "request finished",
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status_code,
                    "elapsed_ms": elapsed_ms,
                    "trace_id_from_client": bool(incoming and is_valid_trace_id(incoming)),
                },
            )
            reset_trace_id(token)
