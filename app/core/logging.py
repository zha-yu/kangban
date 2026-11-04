"""结构化日志：一行一个 JSON，每行必带 `trace_id`。

大纲 1.4 验收原文：「日志每行有 trace_id」。因此 trace_id 不是可选字段 ——
没有值时写 `"-"`，保证 grep 与采集侧字段恒定（技术文档 §1.2 全链路统一 trace_id）。

不引入 structlog：AGENTS.md §4 禁区 12「不擅自引入新框架」，标准库足够。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.context import get_trace_id

# LogRecord 自带的属性，打日志时不当成业务字段
_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        # uvicorn 自带：ANSI 着色的重复消息，进 JSON 只会污染日志
        "color_message",
    }
)

_SERVICE_FIELD = "service"


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON；业务字段用 `logger.info("...", extra={...})` 传入。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": get_trace_id() or "-",
        }

        # extra={...} 带进来的业务字段（不许覆盖保留字段）
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and key not in payload and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", *, service: str = "konban", force: bool = False) -> None:
    """幂等配置根日志器；重复调用不会叠加 handler（uvicorn --reload 会重复 import）。"""
    root = logging.getLogger()
    if getattr(root, "_konban_configured", False) and not force:
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())

    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn 自带 handler 会绕过 JSON 格式，这里收编到根日志器
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    root._konban_configured = True  # type: ignore[attr-defined]

    logging.getLogger(__name__).info(
        "logging configured", extra={_SERVICE_FIELD: service, "log_level": level.upper()}
    )
