"""OTel SDK + Langfuse（Span Exporter）一体接入（Day 10 · B1 · 主理人裁决 ⑤/Q10）。

口径（上游依据，事实只引 PRD/TD）：
- FR-TC-04 / AC-31：每次主控 Agent 执行 = 1 条 Trace；规则路由 / 分类器 / 模型 /
  工具调用 / TTS 各为 Span；记录输入输出摘要、耗时、异常、模型版本；**脱敏后上传**；
- TD §1:205：观测选型 = Langfuse + OpenTelemetry Traces，「Langfuse 可作 OTel
  exporter」——本模块即该选型的落地：OTel API 发射 Span，OTLP/HTTP exporter 把
  span 批量推给 Langfuse 的 ``/api/public/otel/v1/traces`` 端点（**OTLP 端点
  Langfuse v3.22+ 才引入**——Q4 初裁 v2 与该前提冲突，2026-11-13 改判为 v4
  全家桶共享实例；另附 ``x-langfuse-ingestion-version: 4`` header 使 v4 实时
  摄入，缺省最多延迟 10 分钟）；
- M6.2 增补判据：OTel TraceID 与系统 ``trace_id``（``app/core/context.py`` 32 位
  hex）**同值可互查**——由 :class:`SystemTraceIdGenerator` 保证。

设计约束（对齐任务书 v1.1 §0.1/§0.3 与本计划 S1 方案）：

1. **单点收口**：业务代码不直接 import 本模块的 span API；``app/core/metrics.py``
   与 ``app/audio/_observe.py`` 的 hook 函数体改接本模块（兑现 day03/day04
   「Day 10 只改一个函数」承诺），调用方一行不动；
2. **三轨并存**：hook 改接后结构化日志轨与 ``metrics`` 表轨原样保留；
3. **降级不白屏**：``langfuse_enabled=False``（或 ``otel_enabled=False``）时不建
   Provider，全部 emit 函数 no-op——业务零影响（本模块自带开关自检测试）；
4. **凭据零硬编码**：``LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST`` 只从 Settings 读
   （Y2 契约：``.env`` 注入）；
5. **导出失败不阻塞业务**：BatchSpanProcessor 在独立后台线程重试/丢弃，异常只落
   日志（spikes/otel-min 已验证：提交开销 <3ms，flush 有界）。

不使用 opentelemetry 全局 Provider（``trace.set_tracer_provider``）：所有 span
经本模块 tracer 发射，测试可用 InMemory exporter 逐用例隔离（``init_observability``
接受自定义 processor）。

**HTTP 服务端 span 采用自建纯 ASGI 中间件**（:class:`RequestSpanMiddleware`），
不用 ``opentelemetry-instrumentation-fastapi``——Step 2 实测：该插件通过重写
``build_middleware_stack`` 把 OTel 中间件包在**最外层**，服务端 span 创建先于
``TraceIdMiddleware`` 写 contextvar，TraceID 无法同值（S1-4 预案实测落档）。
自建中间件在 ``add_middleware(TraceIdMiddleware)`` 之前注册（后添加者在外层 →
它位于最内层），span 创建时系统 trace_id 已就绪，同值有保证；未启用时逐请求
直通（一次判空，可忽略）。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.trace import Span, Tracer
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.context import get_trace_id
from app.core.redact import redact_obj
from app.core.runtime_context import get_identity

logger = logging.getLogger("app.core.observability")

#: Langfuse OTLP 端点路径（自托管共享实例 = Langfuse v4，Q4 改判；host 来自
#: ``langfuse_host``，Y2 契约）。v3.22+ 才有此端点，v2 系列不支持。
_OTLP_PATH = "/api/public/otel/v1/traces"

_TRACER_NAME = "app.observability"

#: 模块级状态：None = 未启用（全部 emit no-op）
_provider: TracerProvider | None = None
_tracer: Tracer | None = None


class SystemTraceIdGenerator(IdGenerator):
    """OTel TraceID 与系统 trace_id 同值（M6.2 增补判据）。

    - 系统 trace_id 为 32 位 hex（``uuid.uuid4().hex``）→ 直接映射 128-bit int；
    - 无上下文（调度 job）或非 32 hex（测试桩如 ``trace-metrics-1``）→ 随机，
      不伪造同值（诚实口径：该请求在 Langfuse 中以随机 id 呈现）。
    """

    _random = RandomIdGenerator()

    def generate_trace_id(self) -> int:
        tid = get_trace_id()
        if tid and len(tid) == 32:
            try:
                return int(tid, 16)
            except ValueError:
                pass
        return self._random.generate_trace_id()

    def generate_span_id(self) -> int:
        return self._random.generate_span_id()


def _safe_attr(value: Any) -> str | bool | int | float | None:
    """把任意字段值收敛为 OTel 可接受的标量属性（超范围一律 JSON 字符串化）。"""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    try:
        return json.dumps(redact_obj(value), ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _clean_attrs(fields: Mapping[str, Any]) -> dict[str, str | bool | int | float]:
    """字段脱敏（复用 ``app/core/redact.py``，day09 A1 唯一实现）+ 标量化。"""
    cleaned: dict[str, str | bool | int | float] = {}
    for key, value in redact_obj(dict(fields)).items():
        converted = _safe_attr(value)
        if converted is not None:
            cleaned[key] = converted
    return cleaned


def _identity_attrs() -> dict[str, str]:
    """Langfuse 用户维度（方案 B，主理人 2026-11-14 批准）。

    AGENTS §4.9 合规口径：身份只从服务端 ``runtime_context`` 读取，调用方/客户端
    传入的 user 值一律丢弃（合并时身份键覆盖调用方同名键）。主体 id 按优先级
    ``elder_id > staff_id > guardian_id``；无身份（调度 job / 登录前请求）或身份
    不携带任何主体 id（未绑定设备）返回空 dict，不伪造。
    红线测试 ``test_span_never_carries_elder_id`` 仍成立——原始 ``elder_id`` 键
    不进 attrs，身份只经 ``langfuse.user.id`` 官方映射键上报。
    """
    identity = get_identity()
    if identity is None:
        return {}
    subject = identity.elder_id or identity.staff_id or identity.guardian_id
    return {"langfuse.user.id": subject} if subject else {}


def is_enabled() -> bool:
    """观测是否已启用（测试与降级自检用）。"""
    return _provider is not None and _tracer is not None


def init_observability(settings: Settings, *, processor: SpanProcessor | None = None) -> bool:
    """初始化 TracerProvider + OTLP/Langfuse exporter；返回是否启用。

    - 开关：``langfuse_enabled`` 且 ``otel_enabled`` 双闸（任一关闭即 no-op）；
    - endpoint：``{langfuse_host}/api/public/otel/v1/traces``（Y2：复用同一组变量）；
    - 凭据：Basic Auth（public_key:secret_key），只从 Settings 读；附
      ``x-langfuse-ingestion-version: 4``（v4 实时摄入）；
    - 采样：沿用 ``langfuse_sample_rate``（ParentBased，根 span 才掷骰）；
    - ``processor`` 参数仅供测试注入（如 InMemorySpanExporter），生产不传。
    """
    global _provider, _tracer

    if not (settings.langfuse_enabled and settings.otel_enabled):
        logger.info(
            "observability disabled (langfuse_enabled=%s otel_enabled=%s)",
            settings.langfuse_enabled,
            settings.otel_enabled,
        )
        return False

    host = (settings.langfuse_host or "").rstrip("/")
    if not host:
        logger.warning("observability enabled but langfuse_host empty; staying disabled")
        return False

    exporter = OTLPSpanExporter(
        endpoint=f"{host}{_OTLP_PATH}",
        headers={
            "Authorization": "Basic "
            + base64.b64encode(
                f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
            ).decode(),
            # v4 直发 OTLP 若缺此 header，数据入库 UI 呈现最多延迟 10 分钟（官方文档）
            "x-langfuse-ingestion-version": "4",
        },
        timeout=5,
    )
    resource = Resource.create({"service.name": settings.otel_service_name})
    _provider = TracerProvider(
        resource=resource,
        id_generator=SystemTraceIdGenerator(),
        sampler=_build_sampler(settings.langfuse_sample_rate),
    )
    _provider.add_span_processor(
        processor if processor is not None else BatchSpanProcessor(exporter)
    )
    _tracer = _provider.get_tracer(_TRACER_NAME)
    logger.info(
        "observability enabled: otel->langfuse endpoint=%s sample_rate=%s service=%s",
        f"{host}{_OTLP_PATH}",
        settings.langfuse_sample_rate,
        settings.otel_service_name,
    )
    return True


def _build_sampler(sample_rate: float):
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    clamped = min(max(sample_rate, 0.0), 1.0)
    return ParentBased(TraceIdRatioBased(clamped))


class RequestSpanMiddleware:
    """HTTP 服务端 span（纯 ASGI，自建；替代 FastAPIInstrumentor，见模块 docstring）。

    注册顺序：**在 ``add_middleware(TraceIdMiddleware)`` 之前** add（后添加者在
    外层 → 本中间件位于最内层），span 创建时系统 trace_id 已写入 contextvar，
    ``SystemTraceIdGenerator`` 保证服务端 span 与响应头 ``X-Trace-Id`` 同值。
    未启用时逐请求直通（一次判空），业务零影响；失败只打点不改变响应行为。
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _tracer is None:
            await self._app(scope, receive, send)
            return

        method = scope.get("method") or ""
        path = scope.get("path") or ""
        span: Span = _tracer.start_span(
            f"{method} {path}",
            attributes={
                "http.method": method,
                "http.path": path,
                "source": "asgi",
                **_identity_attrs(),
            },
        )
        status_code = 0

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status") or 0
            await send(message)

        try:
            await self._app(scope, receive, send_with_status)
        except Exception as exc:
            span.record_exception(exc)
            raise
        finally:
            if status_code:
                span.set_attribute("http.status_code", status_code)
            span.end()


def shutdown_observability(*, flush_timeout_ms: int = 2000) -> None:
    """flush + 关停（lifespan finally 调用）；全部异常吞掉并落日志，不阻塞退出。"""
    global _provider, _tracer
    provider, _provider = _provider, None
    _tracer = None
    if provider is None:
        return
    try:
        provider.force_flush(timeout_millis=flush_timeout_ms)
    except Exception as exc:
        logger.warning("observability force_flush failed: %s", type(exc).__name__)
    try:
        provider.shutdown()
    except Exception as exc:
        logger.warning("observability shutdown failed: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# emit API（仅 hook 函数体调用；未启用时 no-op）
# ---------------------------------------------------------------------------


def emit_stage_span(
    stage: str,
    elapsed_ms: float,
    *,
    attrs: Mapping[str, Any] | None = None,
) -> None:
    """发射一个带真实耗时的 stage span（asr / route / llm_first_token / ...）。

    span 名 = stage 值（AC-31 链段语义）；duration = elapsed_ms（时间戳回放）；
    attrs 经 :func:`_clean_attrs` 脱敏。父 span 取当前上下文：请求链路下挂服务端
    span / 前序 hook span，无上下文时以系统 trace_id 为根（IdGenerator 同值）。
    """
    if _tracer is None:
        return
    now_ns = time.time_ns()
    duration_ns = max(int(elapsed_ms * 1_000_000), 0)
    span: Span = _tracer.start_span(
        stage,
        attributes={**_clean_attrs(attrs or {}), **_identity_attrs()},
        start_time=now_ns - duration_ns,
    )
    try:
        if elapsed_ms < 0:
            span.record_exception(ValueError(f"negative elapsed_ms: {elapsed_ms}"))
    finally:
        span.end(end_time=now_ns)


def emit_event_span(name: str, fields: Mapping[str, Any]) -> None:
    """发射一个瞬时事件 span（tool_started / tool_succeeded / laya_client / ...）。

    fields 中的 ``latency_ms / elapsed_ms / first_packet_ms / connection_ms`` 会
    同时作为 span 真实耗时回放（链路树更可读）；其余字段脱敏后平铺为属性。
    """
    if _tracer is None:
        return
    attrs = {**_clean_attrs(fields), **_identity_attrs()}
    duration_key = next(
        (
            k
            for k in ("latency_ms", "elapsed_ms", "first_packet_ms", "connection_ms")
            if k in fields
        ),
        None,
    )
    now_ns = time.time_ns()
    start_ns = now_ns
    if duration_key is not None:
        try:
            start_ns = now_ns - max(int(float(fields[duration_key]) * 1_000_000), 0)
        except (TypeError, ValueError):
            start_ns = now_ns
    span = _tracer.start_span(name, attributes=attrs, start_time=start_ns)
    span.end(end_time=now_ns)


__all__ = [
    "RequestSpanMiddleware",
    "SystemTraceIdGenerator",
    "emit_event_span",
    "emit_stage_span",
    "init_observability",
    "is_enabled",
    "shutdown_observability",
]
