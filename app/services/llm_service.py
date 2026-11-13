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
from app.core.metrics import record_llm_cache
from app.core.observability import emit_event_span

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
    """一次 LLM 调用的结果。

    `prompt_tokens` / `completion_tokens` 来自 OpenAI 兼容响应的 `usage` 段：
    缺字段、非整数或降级路径一律 ``None``（**不补零、不估算**，dev-C 的
    `/api/metrics` 据此判 `available`）。两者都不影响主回复文本与 degraded 语义。
    """

    text: str
    degraded: bool
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


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
                    parsed, prompt_tokens, completion_tokens = _parse_completion(response)
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
                                "prompt_tokens": prompt_tokens,
                                "completion_tokens": completion_tokens,
                            },
                        )
                        # P15：token 用量只走「日志 + 进程内计数」——DB 落点（metrics.stage
                        # 增 llm_tokens）尚未迁移，这里不写库、不塞 duration_ms。
                        # 观测旁路失败不得影响主回复。
                        try:
                            from app.services.metrics_service import record_llm_usage

                            record_llm_usage(
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                provider=self._settings.llm_provider,
                                model=self._settings.llm_model,
                            )
                        except Exception as exc:  # pragma: no cover - 防御性
                            logger.warning(
                                "llm_usage_record_failed",
                                extra={
                                    "trace_id": get_trace_id() or "-",
                                    "error_type": type(exc).__name__,
                                },
                            )
                        # B2（D-1 裁决）：prompt cache 计数（B 写域内进程计数）+
                        # llm_usage event span（明细跨重启可查，归 trace 职责）。
                        # 观测旁路失败不得影响主回复。
                        try:
                            cache_hit, cache_miss = _parse_cache_tokens(response.json())
                            record_llm_cache(hit_tokens=cache_hit, miss_tokens=cache_miss)
                            emit_event_span(
                                "llm_usage",
                                {
                                    "provider": self._settings.llm_provider,
                                    "model": self._settings.llm_model,
                                    "prompt_tokens": prompt_tokens,
                                    "completion_tokens": completion_tokens,
                                    "cache_hit_tokens": cache_hit,
                                    "cache_miss_tokens": cache_miss,
                                },
                            )
                        except Exception as exc:  # pragma: no cover - 防御性
                            logger.warning(
                                "llm_cache_record_failed",
                                extra={
                                    "trace_id": get_trace_id() or "-",
                                    "error_type": type(exc).__name__,
                                },
                            )
                        return LLMReply(
                            text=parsed,
                            degraded=False,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                        )
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
                    "elapsed_ms": round((asyncio.get_running_loop().time() - started) * 1000, 1),
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


def _parse_usage(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    """解析 `usage.prompt_tokens` / `usage.completion_tokens`；缺字段或非整数 → None。

    - 供应商未返回 usage（部分兼容实现只在流式/特定模型给）→ 双 None，不影响回复；
    - **不补零、不估算**：0 与「不知道」在统计上语义完全不同；
    - `bool` 是 `int` 子类，必须显式排除；负数同样按缺失处理。
    """

    def _pick(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if value >= 0 else None

    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None
    return _pick(usage.get("prompt_tokens")), _pick(usage.get("completion_tokens"))


def _parse_cache_tokens(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    """解析 prompt cache 命中/未命中 token（B2 · D-1 裁决：明细进 llm_usage span）。

    兼容两种供应商口径，取「第一个明确给出的命中值」：

    - DeepSeek：``usage.prompt_cache_hit_tokens`` / ``usage.prompt_cache_miss_tokens``
      （其语义恒满足 prompt_tokens = hit + miss）；
    - OpenAI 兼容（Qwen/通义、OpenAI 本家）：``usage.prompt_tokens_details.cached_tokens``
      （cached_tokens ⊆ prompt_tokens）——miss 未给时按 ``prompt_tokens - hit`` 推导
      （两值都已确认才推，属「确认后相减」而非估算）；推导不出则 None，不编造。

    返回 ``(hit_tokens, miss_tokens)``，缺即 None，不补零。
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None
    hit: int | None = None
    miss: int | None = None

    def _pick(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if value >= 0 else None

    hit = _pick(usage.get("prompt_cache_hit_tokens"))
    miss = _pick(usage.get("prompt_cache_miss_tokens"))
    if hit is None:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            hit = _pick(details.get("cached_tokens"))
    if miss is None and hit is not None:
        prompt = _pick(usage.get("prompt_tokens"))
        if prompt is not None and prompt >= hit:
            miss = prompt - hit
    return hit, miss


def _parse_completion(response: httpx.Response) -> tuple[str | None, int | None, int | None]:
    """解析 OpenAI 兼容响应：返回 (content, prompt_tokens, completion_tokens)。

    结构不合法或 content 为空时 content 为 None（调用方按 invalid_response 处理）；
    usage 解析与 content 解耦——内容合法但 usage 缺失时仍正常回复，只是 token 为 None。
    """
    try:
        payload: Any = response.json()
    except (json.JSONDecodeError, ValueError):
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, None, None
    first = choices[0]
    if not isinstance(first, dict):
        return None, None, None
    message = first.get("message")
    if not isinstance(message, dict):
        return None, None, None
    content = message.get("content")
    if not isinstance(content, str):
        return None, None, None
    content = content.strip()
    # 推理型模型（MiniMax M 系等）会在 content 内联 <think>…</think> 思考块，
    # 不剥离会进 TTS 播报；max_tokens 截断时可能残留未闭合的 <think> 头（可在
    # 任意位置，不只在开头），一并去掉——从 <think> 到字符串末尾按截断残片处理。
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    content = re.sub(r"<think>.*\Z", "", content, flags=re.DOTALL).strip()
    if not content:
        return None, None, None
    prompt_tokens, completion_tokens = _parse_usage(payload)
    return content, prompt_tokens, completion_tokens


def _parse_content(response: httpx.Response) -> str | None:
    """只取文本内容的兼容薄壳（既有调用/测试面不变）。"""
    return _parse_completion(response)[0]


__all__ = [
    "DEGRADED_REPLY_TEXT",
    "MAX_RETRIES_AFTER_FIRST",
    "RETRY_BACKOFF_SECONDS",
    "LLMReply",
    "LLMService",
]
