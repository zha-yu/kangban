"""Minimax OpenAI 兼容 LLM 适配器（Day04 A2 · FR-DLG-05）。

边界（`docs/plans/day04/a2-impl-信令与对话.md` N02）：

- 只用 `httpx` 直连 `POST {llm_base_url}/chat/completions`，**不引 SDK**、非流式；
- model / key / temperature / timeout / max_retries 全部来自注入的 `Settings`；
- 首次请求后再重试**至多 2 次**（总请求 ≤3）；仅瞬态错误（超时/连接/429/5xx）退避；
- 任何失败都返回固定长者可读模板并标 `degraded=True`，不把内部异常回显给长者；
- 日志只含 trace_id / 尝试序号 / 错误分类 / HTTP 状态，不含 key、请求头或响应体；
- 客户端生命周期明确：外部注入的客户端不关闭，自建客户端由 `aclose()` 关闭。

本模块不做对话编排、不读写 Redis、不发 WebSocket、不调 TTS。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx

from app.core.config import Settings
from app.core.context import get_trace_id

logger = logging.getLogger("app.services.llm_service")

# FR-DLG-05：LLM 故障时的固定降级话术（长者可读、不暴露内部细节）
DEGRADED_REPLY_TEXT: Final[str] = "我这会儿暂时回答不上来，您过一会儿再问我，或者找护工帮帮忙。"
# 首次请求 + 至多 2 次重试 = 总请求至多 3 次（硬上限，不随 Settings 放宽）
MAX_RETRIES_AFTER_FIRST: Final[int] = 2
# 退避基数（秒）：第 1 次重试 0.25s、第 2 次 0.5s；测试 monkeypatch 为 0
RETRY_BACKOFF_SECONDS: Final[float] = 0.25

_REQUEST_PATH: Final[str] = "/chat/completions"


@dataclass(frozen=True, slots=True)
class LLMReply:
    """一次 LLM 调用的结果；对话层只消费这两个字段。"""

    text: str
    degraded: bool


class LLMService:
    """Minimax OpenAI 兼容适配器。

    `client` 可注入（测试用 `httpx.MockTransport`；生产由装配层持有）；
    未注入时本类自建客户端，并在 `aclose()` 中关闭，避免每条消息泄漏连接。
    """

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client: httpx.AsyncClient | None = client or httpx.AsyncClient(
            timeout=settings.llm_timeout_seconds
        )

    @property
    def owns_client(self) -> bool:
        return self._owns_client

    async def aclose(self) -> None:
        """只关闭自己创建的客户端；注入客户端由注入方关闭。"""
        if not self._owns_client or self._client is None:
            return
        await self._client.aclose()
        self._client = None

    async def chat(self, messages: Sequence[Mapping[str, str]]) -> LLMReply:
        """调用 OpenAI 兼容端点；失败返回固定模板 + degraded=True。"""
        api_key = self._settings.llm_api_key.strip()
        base_url = self._settings.llm_base_url.strip().rstrip("/")
        if not api_key or not base_url:
            logger.warning(
                "llm_missing_config",
                extra={"trace_id": get_trace_id() or "-", "provider": self._settings.llm_provider},
            )
            return LLMReply(text=DEGRADED_REPLY_TEXT, degraded=True)

        client = self._client
        if client is None:
            logger.warning(
                "llm_client_closed",
                extra={"trace_id": get_trace_id() or "-", "provider": self._settings.llm_provider},
            )
            return LLMReply(text=DEGRADED_REPLY_TEXT, degraded=True)

        url = f"{base_url}{_REQUEST_PATH}"
        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": [dict(message) for message in messages],
            "temperature": self._settings.llm_temperature,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        max_retries = max(0, min(self._settings.llm_max_retries, MAX_RETRIES_AFTER_FIRST))
        total_attempts = max_retries + 1

        for attempt in range(1, total_attempts + 1):
            error_type: str | None = None
            status_code: int | None = None
            transient = False
            started = asyncio.get_running_loop().time()
            logger.info(
                "llm_request_started",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "provider": self._settings.llm_provider,
                    "attempt": attempt,
                    "message_count": len(payload["messages"]),
                    "prompt_chars": sum(len(m.get("content", "")) for m in payload["messages"]),
                },
            )
            try:
                response = await client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self._settings.llm_timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                error_type = type(exc).__name__
                transient = True
            except httpx.TransportError as exc:
                error_type = type(exc).__name__
                transient = True
            else:
                status_code = response.status_code
                if status_code == 429 or 500 <= status_code < 600:
                    error_type = "http_status"
                    transient = True
                elif status_code >= 300:
                    error_type = "http_status"
                else:
                    parsed = _parse_content(response)
                    if parsed is not None:
                        logger.info(
                            "llm_request_completed",
                            extra={
                                "trace_id": get_trace_id() or "-",
                                "provider": self._settings.llm_provider,
                                "attempt": attempt,
                                "status_code": status_code,
                                "elapsed_ms": round(
                                    (asyncio.get_running_loop().time() - started) * 1000, 1
                                ),
                                "reply_chars": len(parsed),
                            },
                        )
                        return LLMReply(text=parsed, degraded=False)
                    error_type = "invalid_response"

            logger.warning(
                "llm_request_failed",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "provider": self._settings.llm_provider,
                    "attempt": attempt,
                    "total_attempts": total_attempts,
                    "error_type": error_type,
                    "status_code": status_code,
                    "elapsed_ms": round(
                        (asyncio.get_running_loop().time() - started) * 1000, 1
                    ),
                },
            )
            if not transient or attempt >= total_attempts:
                break
            await _backoff(attempt)

        logger.warning(
            "llm_degraded",
            extra={
                "trace_id": get_trace_id() or "-",
                "provider": self._settings.llm_provider,
                "attempts": total_attempts,
            },
        )
        return LLMReply(text=DEGRADED_REPLY_TEXT, degraded=True)


async def _backoff(attempt: int) -> None:
    """第 attempt 次失败后的退避；测试可 monkeypatch 本函数记录次数而不真等。"""
    await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)


def _parse_content(response: httpx.Response) -> str | None:
    """解析 OpenAI 兼容响应；结构不合法或 content 为空时返回 None。"""
    try:
        payload: Any = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    content = content.strip()
    # 推理型模型（MiniMax M 系等）会在 content 内联 <think>…</think> 思考块，
    # 不剥离会进 TTS 播报；max_tokens 截断时可能残留未闭合的 <think> 头，一并去掉。
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    content = re.sub(r"^<think>.*$", "", content, flags=re.DOTALL).strip()
    return content or None


__all__ = [
    "DEGRADED_REPLY_TEXT",
    "MAX_RETRIES_AFTER_FIRST",
    "RETRY_BACKOFF_SECONDS",
    "LLMReply",
    "LLMService",
]
