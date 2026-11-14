"""app/core/observability.py 单元测试（Day 10 · B1 · 裁决 ⑤）。

全部离线（InMemorySpanExporter + SimpleSpanProcessor，不依赖真实 Langfuse），
覆盖任务书 §1 自检判据的可单测部分：

1. 降级证明：``langfuse_enabled=False`` 时观测整体 no-op，业务打点函数照常工作；
2. stage span：``record_stage`` 发射带真实耗时的 OTel span（含 ASR 补充属性），
   且不再额外发 ``stage_ms`` span（无双 span）；
3. trace_id 同值：OTel TraceID 与系统 trace_id（32 hex）同值（M6.2 增补判据）；
4. 脱敏证明：span attrs 经 ``redact.py``——手机号掩码、密钥整值抹除；
5. HTTP 服务端 span：经 nginx 同款装配顺序（instrument 在 add_middleware 之前）
   服务端 span 的 trace_id 与响应头 ``X-Trace-Id`` 同值（装配顺序自证）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.config import Settings
from app.core.context import reset_trace_id, set_trace_id
from app.core.metrics import record_event, record_stage
from app.core.observability import (
    RequestSpanMiddleware,
    emit_stage_span,
    init_observability,
    is_enabled,
    shutdown_observability,
)
from app.core.runtime_context import Identity, identity_scope
from app.core.trace import TraceIdMiddleware

_HEX32 = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def obs_on() -> Iterator[InMemorySpanExporter]:
    """启用观测（不可达端点 + SimpleSpanProcessor 内存导出，逐用例隔离）。"""
    exporter = InMemorySpanExporter()
    settings = Settings(
        langfuse_enabled=True,
        otel_enabled=True,
        langfuse_host="http://127.0.0.1:59999",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        langfuse_sample_rate=1.0,
    )
    assert init_observability(settings, processor=SimpleSpanProcessor(exporter)) is True
    yield exporter
    shutdown_observability()


@pytest.fixture
def obs_off() -> Iterator[None]:
    assert init_observability(Settings(langfuse_enabled=False)) is False
    yield
    shutdown_observability()


def _trace(token_text: str = _HEX32):
    token = set_trace_id(token_text)
    return token


class TestDisabled:
    def test_disabled_noop(self, obs_off) -> None:
        """降级不白屏：关闭后 is_enabled=False，打点函数照常工作、不抛异常。"""
        assert is_enabled() is False
        emit_stage_span("asr", 1.0, attrs={"asr_confidence": 0.9})
        record_event("tool_succeeded", tool_name="weather", latency_ms=1.0)
        record_stage("route", 2.0, source="router")

    def test_disabled_otel_gate(self) -> None:
        """otel_enabled=False 同样关闭（双闸）。"""
        settings = Settings(langfuse_enabled=True, otel_enabled=False)
        assert init_observability(settings) is False
        shutdown_observability()


class TestStageSpan:
    def test_stage_span_with_duration_and_attrs(self, obs_on: InMemorySpanExporter) -> None:
        token = _trace()
        try:
            record_stage(
                "asr",
                100.0,
                source="provider",
                attrs={"asr_confidence": 0.93, "asr_text_len": 7},
            )
        finally:
            reset_trace_id(token)

        spans = obs_on.get_finished_spans()
        names = {s.name for s in spans}
        assert "asr" in names
        assert "stage_ms" not in names  # 无双 span

        asr = next(s for s in spans if s.name == "asr")
        attrs = dict(asr.attributes)
        assert attrs["source"] == "provider"
        assert attrs["asr_confidence"] == pytest.approx(0.93)
        assert attrs["asr_text_len"] == 7
        duration_ms = (asr.end_time - asr.start_time) / 1_000_000
        assert duration_ms == pytest.approx(100.0, rel=1e-6)

    def test_trace_id_same_value(self, obs_on: InMemorySpanExporter) -> None:
        """M6.2 增补判据：OTel TraceID 与系统 trace_id 同值可互查。"""
        token = _trace()
        try:
            record_stage("route", 5.0, source="router")
        finally:
            reset_trace_id(token)

        span = next(s for s in obs_on.get_finished_spans() if s.name == "route")
        assert f"{span.get_span_context().trace_id:032x}" == _HEX32

    def test_random_fallback_without_context(self, obs_on: InMemorySpanExporter) -> None:
        """无系统 trace_id（调度 job 等）→ 随机 128-bit，不伪造同值、不崩。"""
        emit_stage_span("route", 5.0)
        span = obs_on.get_finished_spans()[-1]
        assert span.get_span_context().trace_id != 0

    def test_llm_first_token_span(self, obs_on: InMemorySpanExporter) -> None:
        """既有调用点（companion/dialogue）经 record_stage 自动获得模型 span。"""
        token = _trace()
        try:
            record_stage("llm_first_token", 800.0, source="non_stream")
        finally:
            reset_trace_id(token)
        assert any(s.name == "llm_first_token" for s in obs_on.get_finished_spans())


class TestRedaction:
    def test_event_span_redacts_pii_and_secrets(self, obs_on: InMemorySpanExporter) -> None:
        token = _trace()
        try:
            record_event(
                "tool_succeeded",
                tool_name="weather",
                latency_ms=12.0,
                note="我手机号是13812345678",
                api_key="sk-abcdef1234567890",
            )
        finally:
            reset_trace_id(token)

        span = next(s for s in obs_on.get_finished_spans() if s.name == "tool_succeeded")
        attrs = dict(span.attributes)
        assert attrs["note"] == "我手机号是138****5678"  # redact_text 掩码
        assert attrs["api_key"] == "***"  # 字段级密钥整值抹除
        # latency_ms 同时回放为真实耗时
        duration_ms = (span.end_time - span.start_time) / 1_000_000
        assert duration_ms == pytest.approx(12.0, rel=1e-6)

    def test_span_never_carries_elder_id(self, obs_on: InMemorySpanExporter) -> None:
        """红线：span attrs 不携带 elder_id（身份只走服务端注入，不进观测载荷）。"""
        token = _trace()
        try:
            record_stage("response_segment", 10.0, elder_id="elder-uuid", source="server")
        finally:
            reset_trace_id(token)
        span = next(s for s in obs_on.get_finished_spans() if s.name == "response_segment")
        assert "elder_id" not in dict(span.attributes)


class TestUserIdentity:
    """方案 B（主理人 2026-11-14 批准）：Langfuse 用户维度只从服务端
    runtime_context 注入（AGENTS §4.9 合规路径），键名 ``langfuse.user.id``。"""

    def test_span_carries_user_id_from_runtime_context(self, obs_on: InMemorySpanExporter) -> None:
        """device 身份 → ``langfuse.user.id`` = elder_id。"""
        elder = str(uuid.uuid4())
        identity = Identity(kind="device", device_id=str(uuid.uuid4()), elder_id=elder)
        token = _trace()
        try:
            with identity_scope(identity):
                record_stage("route", 5.0, source="router")
        finally:
            reset_trace_id(token)
        span = next(s for s in obs_on.get_finished_spans() if s.name == "route")
        assert dict(span.attributes)["langfuse.user.id"] == elder

    def test_no_identity_no_user_attr(self, obs_on: InMemorySpanExporter) -> None:
        """无身份上下文（调度 job / 登录前请求）→ 不上报 user 维度，不伪造。"""
        token = _trace()
        try:
            record_stage("route", 5.0, source="router")
        finally:
            reset_trace_id(token)
        span = next(s for s in obs_on.get_finished_spans() if s.name == "route")
        assert "langfuse.user.id" not in dict(span.attributes)

    def test_identity_wins_over_caller_attr(self, obs_on: InMemorySpanExporter) -> None:
        """调用方传入的 ``langfuse.user.id`` 被服务端身份覆盖（客户端传入值一律丢弃）。"""
        elder = str(uuid.uuid4())
        identity = Identity(kind="device", device_id=str(uuid.uuid4()), elder_id=elder)
        token = _trace()
        try:
            with identity_scope(identity):
                emit_stage_span("route", 5.0, attrs={"langfuse.user.id": "spoofed"})
        finally:
            reset_trace_id(token)
        span = next(s for s in obs_on.get_finished_spans() if s.name == "route")
        assert dict(span.attributes)["langfuse.user.id"] == elder


class TestFastAPIServerSpan:
    def test_server_span_same_trace_id_as_header(self, obs_on: InMemorySpanExporter) -> None:
        """装配顺序自证：RequestSpanMiddleware 在 add_middleware(TraceId) 之前注册
        （最内层）→ 服务端 span trace_id 与响应头 X-Trace-Id 同值（与 main.py
        create_app 同款顺序）。"""
        app = FastAPI()

        @app.get("/ping")
        async def ping() -> dict[str, str]:
            return {"ok": "1"}

        app.add_middleware(RequestSpanMiddleware)
        app.add_middleware(TraceIdMiddleware)

        with TestClient(app) as client:
            resp = client.get("/ping")
        header_trace_id = resp.headers["X-Trace-Id"]
        assert header_trace_id

        server_span = next(
            s
            for s in obs_on.get_finished_spans()
            if s.name in ("GET /ping", "GET /ping/") or s.name.endswith("/ping")
        )
        assert f"{server_span.get_span_context().trace_id:032x}" == header_trace_id

    def test_request_middleware_noop_when_disabled(self, obs_off) -> None:
        app = FastAPI()

        @app.get("/ping")
        async def ping() -> dict[str, str]:
            return {"ok": "1"}

        app.add_middleware(RequestSpanMiddleware)
        app.add_middleware(TraceIdMiddleware)
        with TestClient(app) as client:
            resp = client.get("/ping")
        assert resp.status_code == 200
        assert resp.headers["X-Trace-Id"]  # 链路头照常回写
