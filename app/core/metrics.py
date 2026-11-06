"""全链路打点位单点收口（AC-42 ≤1.5s 数据底座）。

口径（day-04 计划 §S5）：Day 4 = **日志级 + trace 字段**——不建 metrics 表、不接
OTel（metrics 表在 Day 5、OTel 在 Day 10，技术设计文档 §8）。Day 5 换 ``/metrics``、
Day 10 换 OTel 时**只改本文件**，调用方一行不动（与 ``app/audio/_observe.py``
同款单点收口模式）。

打点位（唤醒→建连→ASR→路由→LLM→TTS，PRD AC-42 分段口径）：

- ``wakeup``：端侧 KWS 命中 ``wake_ts`` 随 auth 首帧 / client_diag 带入
  （晨会对齐口径）；后端兜底口径 = WS accept 时刻，``source`` 区分两者；
- ``connection``：WebRTC offer 收到 → answer 产出（建连耗时）；
- ``stage_ms``：ASR/TTS 已有 ``app/audio/_observe.py`` 的 record_asr/record_tts，
  其余阶段（路由/LLM/TTS 触发处）直接调 ``record_stage``；
- identity 三点位（C5 裁决 5）：``identity_ask_sent`` / ``identity_confirm_received``
  / ``identity_result_sent``——由 A2 的 signaling 改动调用，A4 只提供 API 不改
  signaling.py。

纪律（AGENTS §4 + day-03 裁决）：每行带 ``trace_id``（``JsonFormatter`` 自动注入）；
只写耗时 / 计数 / 文本长度等指标字段，**不写转写全文、不写长者隐私**。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("app.core.metrics")


class Stopwatch:
    """毫秒计时器；``elapsed_ms()`` 可反复调用（对齐 _observe.Stopwatch 形状）。"""

    __slots__ = ("_t0",)

    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000.0, 2)


def stopwatch() -> Stopwatch:
    """开始一段计时（offer 到达、阶段入口等处调用）。"""
    return Stopwatch()


def record_event(name: str, **fields: Any) -> None:
    """通用打点：结构化 JSON 日志一行（trace_id 由 JsonFormatter 自动注入）。

    Day 5 /metrics、Day 10 OTel 只改本函数体。字段名不得与 LogRecord 保留属性
    冲突（如 ``name``/``msg``/``module``）。
    """
    logger.info("metric %s", name, extra={"metric": name, **fields})


def record_stage(stage: str, elapsed_ms: float) -> None:
    """分段耗时打点（路由 / LLM / TTS 触发处；ASR 走 _observe.record_asr 聚合）。"""
    record_event("stage_ms", stage=stage, elapsed_ms=elapsed_ms)


# ---------------------------------------------------------------------------
# 唤醒 / 建连
# ---------------------------------------------------------------------------


def record_wakeup(
    *,
    source: str,
    wake_ts: float | None = None,
    connection_ms: float | None = None,
) -> None:
    """唤醒打点（AC-42 链路起点）。

    source 取值：``client``（端侧 KWS 命中，wake_ts 随 auth 首帧 / client_diag 带入）
    / ``backend``（后端兜底口径 = WS accept 时刻，wake_ts 为 None）。
    """
    if source not in ("client", "backend"):
        raise ValueError(f"wakeup source must be 'client' or 'backend', got {source!r}")
    record_event("wakeup", source=source, wake_ts=wake_ts, connection_ms=connection_ms)


def record_connection(*, connection_id: str, elapsed_ms: float) -> None:
    """建连打点：offer 收到 → answer 产出的耗时（webrtc `_handle_offer` 接线）。"""
    record_event("connection", connection_id=connection_id, elapsed_ms=elapsed_ms)


# ---------------------------------------------------------------------------
# identity 三打点位（C5 裁决 5 · A2 消费，A4 只提供 API）
# ---------------------------------------------------------------------------


def record_identity_ask_sent(*, ask_id: str, reason: str, elapsed_ms: float | None = None) -> None:
    """identity 询问下发（A2 发出 identity_ask 消息时调用）。"""
    record_event("identity_ask_sent", ask_id=ask_id, reason=reason, elapsed_ms=elapsed_ms)


def record_identity_confirm_received(*, ask_id: str, elapsed_ms: float | None = None) -> None:
    """identity 确认收到（A2 收到 identity_confirm 消息时调用；elapsed=下发→确认）。"""
    record_event("identity_confirm_received", ask_id=ask_id, elapsed_ms=elapsed_ms)


def record_identity_result_sent(*, ask_id: str, elapsed_ms: float | None = None) -> None:
    """identity 结果下发（A2 发出 identity_result 消息时调用）。"""
    record_event("identity_result_sent", ask_id=ask_id, elapsed_ms=elapsed_ms)
