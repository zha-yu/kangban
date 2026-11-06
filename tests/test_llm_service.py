"""Day 4 A2 · Minimax OpenAI 兼容 LLM 适配器测试（A2-07）。

全部用 `httpx.MockTransport`，不访问真实 Minimax；密钥只用测试占位字符串。
覆盖：成功、缺 key、超时、429、连接失败、畸形响应、重试上限、退避次数、客户端所有权、
日志不含密钥。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.context import new_trace_id, reset_trace_id, set_trace_id
from app.services import llm_service
from app.services.llm_service import (
    DEGRADED_REPLY_TEXT,
    MAX_RETRIES_AFTER_FIRST,
    LLMReply,
    LLMService,
)

_TEST_KEY = "test-minimax-key-not-real"
_MESSAGES = [
    {"role": "system", "content": "固定前缀"},
    {"role": "user", "content": "今天天气怎么样"},
]


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_provider": "minimax",
        "llm_base_url": "https://minimax.example/v1",
        "llm_api_key": _TEST_KEY,
        "llm_model": "minimax-chat-test",
        "llm_timeout_seconds": 2,
        "llm_max_retries": 2,
        "llm_temperature": 0.5,
    }
    values.update(overrides)
    return Settings(**values)


def _ok_response(content: str = "今天晴，适合出门走走。") -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
    )


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def trace_context() -> Any:
    token = set_trace_id(new_trace_id())
    try:
        yield
    finally:
        reset_trace_id(token)


async def test_success_parses_content_and_keeps_injected_client_open() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return _ok_response("  你好呀，今天想聊点什么？  ")

    client = _client(handler)
    service = LLMService(_settings(), client=client)
    reply = await service.chat(_MESSAGES)

    assert reply == LLMReply(text="你好呀，今天想聊点什么？", degraded=False)
    assert captured["url"] == "https://minimax.example/v1/chat/completions"
    assert captured["body"]["stream"] is False
    assert captured["body"]["model"] == "minimax-chat-test"
    assert captured["body"]["temperature"] == 0.5
    assert captured["body"]["messages"] == _MESSAGES
    assert captured["authorization"] == f"Bearer {_TEST_KEY}"

    # 注入客户端由注入方负责；service.aclose() 不得关闭它
    assert service.owns_client is False
    await service.aclose()
    assert client.is_closed is False
    await client.aclose()


async def test_missing_api_key_returns_degraded_without_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _ok_response()

    client = _client(handler)
    service = LLMService(_settings(llm_api_key=""), client=client)
    reply = await service.chat(_MESSAGES)

    assert reply == LLMReply(text=DEGRADED_REPLY_TEXT, degraded=True)
    assert calls == 0
    await client.aclose()


async def test_timeout_retries_at_most_two_times(
    monkeypatch: pytest.MonkeyPatch, trace_context: Any, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0
    backoffs: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    async def fake_backoff(attempt: int) -> None:
        backoffs.append(attempt)

    monkeypatch.setattr(llm_service, "_backoff", fake_backoff)
    client = _client(handler)
    service = LLMService(_settings(), client=client)

    with caplog.at_level(logging.WARNING, logger="app.services.llm_service"):
        reply = await service.chat(_MESSAGES)

    assert reply == LLMReply(text=DEGRADED_REPLY_TEXT, degraded=True)
    assert calls == 3  # 首次 + 2 次重试
    assert backoffs == [1, 2]
    assert [r.attempt for r in caplog.records if r.getMessage() == "llm_request_failed"] == [
        1,
        2,
        3,
    ]
    assert all(r.trace_id for r in caplog.records)
    assert _TEST_KEY not in " ".join(r.getMessage() for r in caplog.records)
    await client.aclose()


async def test_429_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    backoffs: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return _ok_response("好的，我记住了。")

    async def fake_backoff(attempt: int) -> None:
        backoffs.append(attempt)

    monkeypatch.setattr(llm_service, "_backoff", fake_backoff)
    client = _client(handler)
    service = LLMService(_settings(), client=client)

    reply = await service.chat(_MESSAGES)
    assert reply == LLMReply(text="好的，我记住了。", degraded=False)
    assert calls == 2
    assert backoffs == [1]
    await client.aclose()


async def test_429_exhausts_retries_and_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    backoffs: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": "rate limited"})

    async def fake_backoff(attempt: int) -> None:
        backoffs.append(attempt)

    monkeypatch.setattr(llm_service, "_backoff", fake_backoff)
    client = _client(handler)
    service = LLMService(_settings(), client=client)

    reply = await service.chat(_MESSAGES)
    assert reply == LLMReply(text=DEGRADED_REPLY_TEXT, degraded=True)
    assert calls == 3
    assert backoffs == [1, 2]
    await client.aclose()


async def test_connection_error_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    backoffs: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused", request=request)

    async def fake_backoff(attempt: int) -> None:
        backoffs.append(attempt)

    monkeypatch.setattr(llm_service, "_backoff", fake_backoff)
    client = _client(handler)
    service = LLMService(_settings(), client=client)

    reply = await service.chat(_MESSAGES)
    assert reply.degraded is True
    assert reply.text == DEGRADED_REPLY_TEXT
    assert calls == 3
    assert backoffs == [1, 2]
    await client.aclose()


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": 123}}]},
        [],
        "not-an-object",
    ],
)
async def test_malformed_response_is_not_retried(body: Any) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    client = _client(handler)
    service = LLMService(_settings(), client=client)

    reply = await service.chat(_MESSAGES)
    assert reply == LLMReply(text=DEGRADED_REPLY_TEXT, degraded=True)
    assert calls == 1  # 畸形响应不是瞬态错误，不重试
    await client.aclose()


async def test_non_retryable_http_error_degrades_immediately() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "invalid key"})

    client = _client(handler)
    service = LLMService(_settings(), client=client)

    reply = await service.chat(_MESSAGES)
    assert reply.degraded is True
    assert calls == 1
    await client.aclose()


async def test_settings_retries_capped_at_two(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    backoffs: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "unavailable"})

    async def fake_backoff(attempt: int) -> None:
        backoffs.append(attempt)

    monkeypatch.setattr(llm_service, "_backoff", fake_backoff)
    client = _client(handler)
    service = LLMService(_settings(llm_max_retries=5), client=client)

    reply = await service.chat(_MESSAGES)
    assert reply.degraded is True
    assert calls == MAX_RETRIES_AFTER_FIRST + 1  # 3，不随 Settings 放宽
    assert backoffs == [1, 2]
    await client.aclose()


async def test_owned_client_is_closed_by_aclose() -> None:
    service = LLMService(_settings())
    assert service.owns_client is True
    await service.aclose()
    assert service._client is None


async def test_logs_never_contain_api_key(
    trace_context: Any, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    client = _client(handler)
    service = LLMService(_settings(), client=client)
    with caplog.at_level(logging.INFO, logger="app.services.llm_service"):
        await service.chat(_MESSAGES)

    combined = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert _TEST_KEY not in combined
    assert _TEST_KEY not in caplog.text
    await client.aclose()
