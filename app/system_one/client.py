"""System One HTTP 客户端（Day 7 · A2）：快路径 200ms / shadow 400ms / 熔断降级。

**这个类的唯一承诺：永不抛异常。** 任何失败——连不上、超时、5xx、响应 schema 不合规——
都返回 `degraded_result()`。理由很实际：Router 挂在长者说话的必经路径上，
分类器抖动不该让长者听到「出错了」；降级后 Agent 还在，对话照常。

两个入口（2026-11-10 主理人裁决，见计划 §10-10）：

- `decide_fast()`：只问 `intent`，客户端 200ms 硬预算（PRD §5.3）。实测单问 p95 167ms。
- `decide_shadow()`：只问 `is_sensitive`，独立 400ms 预算，**有果才用、没果当没问过**。
  两问 285ms、四问 668ms 均超 200ms，所以敏感信号只能走 shadow 并行。

熔断（连续 3 次失败/30s 打开、半开探测 1 次）：打开期间**立刻**返回降级结果，不再等 200ms。
快路径与 shadow **各用一个熔断器**：shadow 的 400ms 超时不该把快路径一起打成 fallback。

隐私纪律：日志只有 `trace_id` + `text_len` + 文本 sha256 前 8 位，**不记原文**。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any, Final

import httpx
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.system_one.backend import text_fingerprint
from app.system_one.questions import build_fast_questions, build_shadow_questions, build_state
from app.system_one.schemas import (
    SystemOneRequest,
    SystemOneResponse,
    SystemOneResult,
    degraded_result,
)

logger = logging.getLogger("app.system_one.client")

#: 熔断：连续失败次数阈值（契约口径，别按场景改）
BREAKER_FAILURE_THRESHOLD: Final[int] = 3
#: 熔断打开后的冷却窗口（秒）；到期进入半开，只放 1 个探测请求
BREAKER_OPEN_WINDOW_S: Final[float] = 30.0

#: 连接阶段单独预算（秒）。`localhost` 在 Windows 上先试 `::1` 再回 `127.0.0.1`，
#: sidecar 没起时第一跳会挂满 connect 预算——实测 340ms（超 250ms 降级门槛）。
#: 环回连接 50ms 足够，超了就是没起，快点失败比"等满 200ms"对长者更友好。
CONNECT_TIMEOUT_S: Final[float] = 0.05

#: 降级原因（进日志，不进库；排障时靠它区分「模型慢」与「HTTP 挂了」）
REASON_DISABLED: Final[str] = "disabled"
REASON_BREAKER_OPEN: Final[str] = "breaker_open"
REASON_TIMEOUT: Final[str] = "timeout"
REASON_HTTP_STATUS: Final[str] = "http_status"
REASON_TRANSPORT: Final[str] = "transport"
REASON_SCHEMA: Final[str] = "schema"


class _CircuitBreaker:
    """极简熔断器：closed → (3 次失败) → open → (30s 后) → half_open(1 次探测) → closed。

    用**注入的 now()**（默认 `time.monotonic`）判冷却窗口，测试才能不 sleep 就把
    30 秒「快进」过去。
    """

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def allow(self) -> bool:
        """是否放行本次请求。打开且未到冷却窗口 → False（调用方立刻降级）。"""
        if self._opened_at is None:
            return True
        if self._now() - self._opened_at < BREAKER_OPEN_WINDOW_S:
            return False
        if self._probe_in_flight:  # 半开只放一个
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        if self._probe_in_flight:  # 半开探测失败 → 立刻重新打开，重置冷却窗口
            self._probe_in_flight = False
            self._opened_at = self._now()
            return
        self._failures += 1
        if self._failures >= BREAKER_FAILURE_THRESHOLD:
            self._opened_at = self._now()


class SystemOneClient:
    """`POST /v1/decide` 的异步客户端（进程内单例，装配层构造一次）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], float] = time.monotonic,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._base_url = (self._settings.system_one_base_url or "").rstrip("/")
        self._api_key = (self._settings.system_one_api_key or "").strip()
        self._fast_timeout = max(0.001, self._settings.system_one_timeout_ms / 1000)
        self._shadow_timeout = max(0.001, self._settings.system_one_shadow_timeout_ms / 1000)
        self._model_version = self._settings.system_one_model_version
        self._transport = transport
        self._now = now
        self._client = client
        self._owns_client = client is None
        self._client_lock = asyncio.Lock()
        # 两条路径分开熔断：shadow 慢不该拖垮快路径
        self._breakers: dict[str, _CircuitBreaker] = {
            "fast": _CircuitBreaker(now=now),
            "shadow": _CircuitBreaker(now=now),
        }

    # ---------------------------------------------------------------- 公开入口

    async def decide_fast(
        self,
        text: str,
        *,
        trace_id: str | None = None,
        state: str | dict[str, Any] | list[Any] | None = None,
    ) -> SystemOneResult:
        """只问 `intent`（200ms）。失败/熔断 → 降级结果。"""
        return await self._decide(
            text,
            trace_id=trace_id,
            state=state,
            questions=build_fast_questions(),
            budget_s=self._fast_timeout,
            path="fast",
        )

    async def decide_shadow(
        self,
        text: str,
        *,
        trace_id: str | None = None,
        state: str | dict[str, Any] | list[Any] | None = None,
    ) -> SystemOneResult:
        """只问 `is_sensitive`（400ms）。失败即「当没问过」——降级结果的 `is_sensitive=False`。"""
        return await self._decide(
            text,
            trace_id=trace_id,
            state=state,
            questions=build_shadow_questions(),
            budget_s=self._shadow_timeout,
            path="shadow",
        )

    async def aclose(self) -> None:
        """关闭底层连接池（lifespan 收尾用）。外部注入的 client 不归本类关闭。"""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- 内部实现

    async def _decide(
        self,
        text: str,
        *,
        trace_id: str | None,
        state: str | dict[str, Any] | list[Any] | None,
        questions: dict[str, Any],
        budget_s: float,
        path: str,
    ) -> SystemOneResult:
        text_len, digest = text_fingerprint(text)
        started = time.perf_counter()

        if not self._settings.system_one_enabled:
            return self._degraded(started, REASON_DISABLED, trace_id, text_len, digest, path)

        breaker = self._breakers[path]
        if not breaker.allow():
            # 打开期间不等超时：直接降级（这是「熔断」的全部意义）
            return self._degraded(started, REASON_BREAKER_OPEN, trace_id, text_len, digest, path)

        try:
            payload = SystemOneRequest(
                request_id=uuid.uuid4().hex,
                trace_id=trace_id,
                state=state if state is not None else build_state(text),
                questions=questions,
                model_version=self._model_version,
            ).model_dump(mode="json")
        except ValidationError:
            # 题面/state 形状不合契约：这是调用方的编程错误，但也不能抛进对话链路
            breaker.record_failure()
            return self._degraded(started, REASON_SCHEMA, trace_id, text_len, digest, path)

        try:
            http = await self._http()
            response = await http.post(
                 f"{self._base_url}/v1/decide", json=payload, timeout=budget_s
            )
        except (httpx.TimeoutException, TimeoutError):
            breaker.record_failure()
            return self._degraded(started, REASON_TIMEOUT, trace_id, text_len, digest, path)
        except Exception:  # 传输层任何异常（含 DNS/URL 非法）都统一降级，绝不外抛
            breaker.record_failure()
            return self._degraded(started, REASON_TRANSPORT, trace_id, text_len, digest, path)

        if response.status_code != httpx.codes.OK:
            breaker.record_failure()
            return self._degraded(
                started,
                f"{REASON_HTTP_STATUS}:{response.status_code}",
                trace_id,
                text_len,
                digest,
                path,
            )

        try:
            parsed = SystemOneResponse.model_validate(response.json())
        except (ValidationError, ValueError):
            # schema 不合规与「模型没回答」等价：宁可降级，也不把可疑字段塞给路由
            breaker.record_failure()
            return self._degraded(started, REASON_SCHEMA, trace_id, text_len, digest, path)

        breaker.record_success()
        result = parsed.result
        logger.info(
            "system_one_%s_ok trace_id=%s text_len=%d text_sha8=%s intent=%s conf=%.3f",
            path,
            trace_id or "-",
            text_len,
            digest,
            result.intent,
            result.confidence,
        )
        return result

    async def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                headers: dict[str, str] = {}
                if self._api_key:
                    headers["Authorization"] = f"Bearer {self._api_key}"
                self._client = httpx.AsyncClient(
                    headers=headers,
                    transport=self._transport,
                    timeout=self._fast_timeout,
                    # **必须关掉环境代理**：sidecar 是本机回环服务，不该经任何代理。
                    # 踩过的坑（2026-11-10 实测）：开发机常设 `HTTP_PROXY=127.0.0.1:65489`，
                    # httpx 默认 trust_env=True 会把发往 127.0.0.1:7001 的请求也交给代理，
                    # 结果是「10 次只成功 1 次、p50 181ms」，看起来像 195ms 预算不够，
                    # 实为代理在中间加了一次跳转并破坏 keep-alive 复用。
                    # 关掉后同一台机 10/10 成功、p50 131ms。**离线自检同样适用**。
                    trust_env=False,
                )
            return self._client

    def _degraded(
        self,
        started: float,
        reason: str,
        trace_id: str | None,
        text_len: int,
        digest: str,
        path: str,
    ) -> SystemOneResult:
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.warning(
            "system_one_%s_degraded reason=%s trace_id=%s text_len=%d text_sha8=%s latency_ms=%d",
            path,
            reason,
            trace_id or "-",
            text_len,
            digest,
            latency_ms,
        )
        return degraded_result(latency_ms=latency_ms, model_version=self._model_version)


__all__ = [
    "BREAKER_FAILURE_THRESHOLD",
    "BREAKER_OPEN_WINDOW_S",
    "CONNECT_TIMEOUT_S",
    "SystemOneClient",
]
