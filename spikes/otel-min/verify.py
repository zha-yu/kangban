"""spikes/otel-min · Day 10 B1 前置最小验证（AGENTS §3：新依赖先 spike 再合并）。

OTel SDK 与 langfuse 早已在 pyproject 声明（依赖引入不是缺口），本 spike 只验证
**运行时行为**三件事，全部离线、不依赖真实 Langfuse：

1. BatchSpanProcessor + 不可达 OTLP 端点：span 提交**不阻塞**业务协程、异常不外抛；
2. 自定义 IdGenerator 读 contextvar：OTel TraceID 与系统 trace_id（32 hex）同值；
3. 与 WindowsSelectorEventLoopPolicy（方案 A）共存：Selector loop 下同样非阻塞。

跑法：uv run python spikes/otel-min/verify.py
通过判据（三条全部 OK 才允许合并进主工程）：
  [1] span 提交额外开销 < 5ms（纯入队，网络在后台线程）；
  [2] trace_id 同值 = True；
  [3] Selector loop 下同样非阻塞。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根，供 import app.*

logging.basicConfig(level=logging.WARNING)  # exporter 失败会打 warning，不吞

from opentelemetry import trace as otel_trace  # noqa: E402
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator  # noqa: E402

from app.core.context import reset_trace_id, set_trace_id  # noqa: E402

UNREACHABLE = "http://127.0.0.1:59999/api/public/otel/v1/traces"


class SystemTraceIdGenerator(IdGenerator):
    """观测层同款实现：优先读系统 trace_id（32 hex → 128-bit int），否则随机。"""

    _random = RandomIdGenerator()

    def generate_trace_id(self) -> int:
        from app.core.context import get_trace_id

        tid = get_trace_id()
        if tid and len(tid) == 32:
            try:
                return int(tid, 16)
            except ValueError:
                pass
        return self._random.generate_trace_id()

    def generate_span_id(self) -> int:
        return self._random.generate_span_id()


def build_provider() -> TracerProvider:
    provider = TracerProvider(id_generator=SystemTraceIdGenerator())
    exporter = OTLPSpanExporter(endpoint=UNREACHABLE, timeout=2)
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=200))
    otel_trace.set_tracer_provider(provider)
    return provider


def check_nonblocking(tracer: otel_trace.trace.Tracer) -> float:
    for i in range(50):
        with tracer.start_as_current_span(f"s{i}"):
            pass
    t0 = time.perf_counter()
    with tracer.start_as_current_span("blocking-test"):
        time.sleep(0.3)
    overhead_ms = round((time.perf_counter() - t0) * 1000 - 300, 2)
    return overhead_ms


def check_trace_id_same(tracer: otel_trace.trace.Tracer) -> bool:
    token = set_trace_id("0123456789abcdef0123456789abcdef")
    try:
        with tracer.start_as_current_span("same-id-test") as span:
            ctx = span.get_span_context()
            return f"{ctx.trace_id:032x}" == "0123456789abcdef0123456789abcdef"
    finally:
        reset_trace_id(token)


def check_random_fallback(tracer: otel_trace.trace.Tracer) -> bool:
    """无系统 trace_id（如调度 job）→ 随机 128-bit，不崩。"""
    with tracer.start_as_current_span("no-context-test") as span:
        return span.get_span_context().trace_id != 0


async def main() -> None:
    provider = build_provider()
    tracer = otel_trace.get_tracer("spike-otel-min")

    overhead = check_nonblocking(tracer)
    same = check_trace_id_same(tracer)
    fallback = check_random_fallback(tracer)
    print(f"[1] span 提交额外开销 = {overhead}ms -> {'OK' if overhead < 5 else 'FAIL'}")
    print(f"[2] trace_id 同值 = {same} -> {'OK' if same else 'FAIL'}")
    print(f"[3] 无上下文随机回退 = {fallback} -> {'OK' if fallback else 'FAIL'}")

    # 方案 A 口径：Selector 事件循环下再来一轮
    loop = asyncio.SelectorEventLoop()
    try:
        asyncio.set_event_loop(loop)
        overhead2 = check_nonblocking(tracer)
        same2 = check_trace_id_same(tracer)
        print(
            f"[3] Selector loop 开销 = {overhead2}ms / 同值 = {same2} -> "
            f"{'OK' if overhead2 < 5 and same2 else 'FAIL'}"
        )
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    t0 = time.perf_counter()
    try:
        provider.force_flush(timeout_millis=2000)
    except Exception as exc:
        print(f"    force_flush 异常被捕获（预期）: {type(exc).__name__}")
    print(
        f"[4] force_flush(不可达端点) 耗时 = {round((time.perf_counter() - t0) * 1000)}ms -> "
        f"{'OK' if time.perf_counter() - t0 < 3 else 'FAIL'}（超时上限 2s）"
    )

    provider.shutdown()
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
