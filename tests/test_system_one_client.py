"""A2 · `SystemOneClient` 六类行为 + 降级不变量（`httpx.MockTransport`，不打真网）。

为什么要这 6 条：客户端挂在长者说话路径上，**它的失败模式就是对话的失败模式**。
每条用例对应一种线上真的会发生的事故：

1. 正常（含 Bearer 与题面形状）——防止「字段名改了没人发现」；
2. 超时——sidecar 卡住时 200ms 必须放手；
3. 5xx——模型崩了不该 500 往上冒；
4. schema 非法——半成品 JSON 比没有答案更危险；
5. 熔断打开后不再发请求——连续失败时不该每次都等满 200ms；
6. 半开恢复——30s 后放一个探测请求，成功即闭合。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.system_one.client import SystemOneClient
from app.system_one.schemas import SystemOneResult, degraded_result


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        # 显式钉住：仓库 .env 里可能仍有 LAYA_ENABLED=false，兼容映射会覆盖默认值
        "system_one_enabled": True,
        "system_one_base_url": "http://sidecar.test",
        "system_one_api_key": "",
        "system_one_timeout_ms": 200,
        "system_one_shadow_timeout_ms": 400,
        "system_one_model_version": "laya:multilingual:q1",
        "system_one_provider": "laya",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _ok_payload(request_id: str, *, intent: str = "weather", confidence: float = 0.91) -> dict:
    return {
        "request_id": request_id,
        "result": {
            "intent": intent,
            "confidence": confidence,
            "is_sensitive": False,
            "sensitive_probability": 0.02,
            "privacy_scope": "none",
            "is_out_of_domain": False,
            "degraded": False,
            "latency_ms": 150,
            "model_version": "laya:multilingual:q1",
            "multi_intent": False,
            "sub_intents": [],
        },
    }


class _Clock:
    """可推进的单调时钟：熔断窗口不需要真的 sleep 30 秒。"""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


async def test_decide_fast_parses_result_and_sends_contract_shape() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(200, json=_ok_payload(body["request_id"]))

    client = SystemOneClient(
        _settings(system_one_api_key="secret"),
        transport=httpx.MockTransport(handler),
    )
    result = await client.decide_fast("今天天气怎么样", trace_id="t-1")

    assert result.intent == "weather"
    assert result.confidence == pytest.approx(0.91)
    assert result.degraded is False
    # 快路径只问 intent（两问 285ms 超预算，见计划 §10-10）
    assert list(seen[0]["questions"]) == ["intent"]
    assert seen[0]["state"]["asr_final"] == "今天天气怎么样"
    await client.aclose()


async def test_decide_fast_timeout_returns_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sidecar too slow", request=request)

    client = SystemOneClient(_settings(), transport=httpx.MockTransport(handler))
    result = await client.decide_fast("今天天气怎么样", trace_id="t-2")

    assert result.degraded is True
    assert result.intent == "other"
    assert result.confidence == 0.0
    assert result.is_out_of_domain is True
    await client.aclose()


async def test_decide_fast_5xx_returns_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "timeout"})

    client = SystemOneClient(_settings(), transport=httpx.MockTransport(handler))
    result = await client.decide_fast("今天天气怎么样")

    assert result.degraded is True
    assert result.model_version == "laya:multilingual:q1"
    await client.aclose()


async def test_decide_fast_invalid_schema_returns_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # intent 不在七类里 → 响应 schema 不合规，必须降级而不是把脏值塞给路由
        return httpx.Response(200, json=_ok_payload("r-1", intent="not_an_intent", confidence=1.7))

    client = SystemOneClient(_settings(), transport=httpx.MockTransport(handler))
    result = await client.decide_fast("随便说点什么")

    assert result.degraded is True
    await client.aclose()


async def test_breaker_opens_after_three_failures_and_stops_calling() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"status": "inference_failed"})

    clock = _Clock()
    client = SystemOneClient(_settings(), transport=httpx.MockTransport(handler), now=clock)

    for _ in range(3):
        assert (await client.decide_fast("你好")).degraded is True
    assert len(calls) == 3

    # 第 4 次：熔断已打开 → 立刻降级，一个请求都不发
    result = await client.decide_fast("你好")
    assert result.degraded is True
    assert len(calls) == 3
    await client.aclose()


async def test_breaker_half_open_probe_closes_on_success() -> None:
    clock = _Clock()
    state = {"fail": True, "calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["fail"]:
            return httpx.Response(500, json={"status": "inference_failed"})
        body = json.loads(request.content)
        return httpx.Response(200, json=_ok_payload(body["request_id"]))

    client = SystemOneClient(_settings(), transport=httpx.MockTransport(handler), now=clock)
    for _ in range(3):
        await client.decide_fast("你好")
    assert state["calls"] == 3

    clock.advance(31)  # 冷却窗口过去 → 半开
    state["fail"] = False
    probe = await client.decide_fast("你好")
    assert probe.degraded is False
    assert probe.intent == "weather"
    assert state["calls"] == 4

    # 探测成功后闭合：后续请求照常发送
    state["calls"] = 0
    again = await client.decide_fast("你好")
    assert again.degraded is False
    assert state["calls"] == 1
    await client.aclose()


async def test_decide_shadow_failure_means_not_asked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sidecar down", request=request)

    client = SystemOneClient(_settings(), transport=httpx.MockTransport(handler))
    result = await client.decide_shadow("我胸口有点闷")

    assert result.degraded is True
    # shadow 失败 = 「当没问过」：不得因此把长者判成敏感，安全仍由 G0 兜底
    assert result.is_sensitive is False
    assert result.sensitive_probability is None
    await client.aclose()


async def test_decide_shadow_sensitive_signal_is_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # Day 10 B3：shadow 扩为两问（is_sensitive + privacy_scope），仍是旁路
        assert list(body["questions"]) == ["is_sensitive", "privacy_scope"]
        payload = _ok_payload(body["request_id"], intent="other", confidence=0.0)
        payload["result"].update(
            {"is_sensitive": True, "sensitive_probability": 0.93, "privacy_scope": "personal"}
        )
        return httpx.Response(200, json=payload)

    client = SystemOneClient(_settings(), transport=httpx.MockTransport(handler))
    result = await client.decide_shadow("我胸口疼得厉害")

    assert result.is_sensitive is True
    assert result.sensitive_probability == pytest.approx(0.93)
    assert result.privacy_scope == "personal"
    await client.aclose()


async def test_disabled_switch_skips_network() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=_ok_payload("r"))

    client = SystemOneClient(
        _settings(system_one_enabled=False), transport=httpx.MockTransport(handler)
    )
    result = await client.decide_fast("今天天气怎么样")

    assert result.degraded is True
    assert calls == []
    await client.aclose()


def test_degraded_result_holds_three_invariants() -> None:
    result = degraded_result(latency_ms=3, model_version="laya:multilingual:q1")

    assert isinstance(result, SystemOneResult)
    assert result.intent == "other"
    assert result.confidence == 0.0
    assert result.is_out_of_domain is True
    assert result.is_sensitive is False
    assert result.sub_intents == ()


async def test_http_client_ignores_system_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接池必须 `trust_env=False`：本机回环不该经任何代理。

    实测事故（2026-11-10，开发机自带 `HTTP_PROXY=http://127.0.0.1:65489`）：
    httpx 默认 trust_env=True 会把发往 `127.0.0.1:7001` 的请求也交给代理，
    结果是 **10 次只成功 1 次、成功那次 p50 181ms**，看起来像「195ms 预算不够」，
    实际是代理多一跳并破坏 keep-alive 复用。关掉后同一台机 **10/10 成功、p50 131ms**。

    这条断言不依赖私有属性：直接看构造 AsyncClient 时传了什么。
    """
    captured: dict[str, object] = {}
    real_async_client = httpx.AsyncClient

    def spy(*args: object, **kwargs: object) -> httpx.AsyncClient:
        captured.update(kwargs)
        return real_async_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", spy)

    client = SystemOneClient(_settings())
    try:
        await client._http()  # 触发连接池创建
    finally:
        await client.aclose()

    assert captured.get("trust_env") is False, "连接池必须信任环境变量=False，否则会走系统代理"
