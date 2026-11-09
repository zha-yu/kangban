"""全链路打点位单点收口（AC-42 ≤1.5s 数据底座）。

口径（day-04 计划 §S5 → day-05 计划 S2）：Day 4 = **日志级 + trace 字段**；
**Day 5 升级为双轨**——结构化日志照旧输出，同时经 ``app/services/metrics_service``
（裁决 B6/D2 建表的唯一写库入口）异步批量入 ``metrics`` 表。不接 OTel（Day 10），
届时只改本文件，调用方一行不动（与 ``app/audio/_observe.py`` 同款单点收口模式）。

打点位（唤醒→建连→ASR→路由→LLM→TTS→前端播放，PRD AC-42 分段口径）：

- ``wakeup``：端侧 KWS 命中 ``wake_ts`` 随 auth 首帧带入（day03/contracts.md v1.5）；
  后端兜底口径 = WS accept，``source`` 区分两者；
- ``connection``：WebRTC offer 收到 → answer 产出（建连耗时）；
- ``stage_ms``：ASR/TTS 走 ``app/audio/_observe.py`` 的 record_asr/record_tts
  （两函数体已同步双轨），其余阶段（路由/LLM/TTS 触发处/playback 代打）直接调
  ``record_stage``；``route`` 段待 Day 6 路由接线后补（当前无测量点，缺口登记）；
- identity 三点位（C5 裁决 5）：仅日志轨，不进 metrics 表（非七值 stage 枚举）。

纪律（AGENTS §4 + day-03 裁决）：每行日志带 ``trace_id``（``JsonFormatter`` 自动注入）；
入 ``metrics`` 表的行由 ``metrics_service`` 校验（trace_id 缺失不入库，自裁 Z4）；
只写耗时 / 计数 / 文本长度等指标字段，**不写转写全文、不写长者隐私**。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.core.context import get_trace_id
from app.core.runtime_context import get_identity
from app.services.metrics_service import get_metrics_recorder

logger = logging.getLogger("app.core.metrics")

#: wakeup 段耗时上限（ms）：端侧时钟偏移防护——超过视为 skew 异常值截断（自裁登记随代码提交）
WAKEUP_DURATION_MS_CAP = 300_000.0


def _record_db(
    stage: str,
    duration_ms: float,
    *,
    elder_id: str | None = None,
    source: str | None = None,
) -> None:
    """metrics 表轨：组装行数据交给唯一写库入口（校验/丢弃/降级在 service 内）。

    trace_id 取自请求上下文；取不到的行不入库、只留日志轨（自裁 Z4）。
    elder_id 兜底：调用点未显式传时（asr / tts_first_packet 在引擎层无身份入参），
    从 runtime_context 读服务端注入的身份（AGENTS 红线 9）——WS 握手已 set_identity，
    打点协程均自该上下文派生；无身份上下文或 staff/guardian 身份则保持 None。
    """
    if elder_id is None:
        identity = get_identity()
        if identity is not None:
            elder_id = identity.elder_id
    get_metrics_recorder().record_stage(get_trace_id(), elder_id, stage, duration_ms, source)


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


def record_stage(
    stage: str,
    elapsed_ms: float,
    *,
    elder_id: str | None = None,
    source: str | None = None,
) -> None:
    """分段耗时打点（LLM/TTS 触发处 / playback 代打；ASR 走 _observe.record_asr 聚合）。

    双轨：``stage_ms`` 日志行照旧 + ``metrics`` 表一行（``elder_id`` 可空）。
    """
    record_event("stage_ms", stage=stage, elapsed_ms=elapsed_ms)
    _record_db(stage, elapsed_ms, elder_id=elder_id, source=source)


# ---------------------------------------------------------------------------
# 唤醒 / 建连
# ---------------------------------------------------------------------------


def record_wakeup(
    *,
    source: str,
    wake_ts: float | None = None,
    connection_ms: float | None = None,
    elder_id: str | None = None,
) -> None:
    """唤醒打点（AC-42 链路起点）。

    source 取值：``client``（端侧 KWS 命中，wake_ts 随 auth 首帧带入）
    / ``backend``（后端兜底口径 = WS accept 时刻，wake_ts 为 None）。

    双轨：日志行照旧 + ``metrics`` 表 ``wakeup`` 行——duration = accept−wake_ts
    （client 口径，负值截 0、超 ``WAKEUP_DURATION_MS_CAP`` 视为时钟偏移截断），
    backend 口径 duration=0（KWS 起点未知，仅占位表可查）。
    ``elder_id``：auth 身份解析结果（服务端注入，AGENTS 红线 9），供按长者口径分析。
    """
    if source not in ("client", "backend"):
        raise ValueError(f"wakeup source must be 'client' or 'backend', got {source!r}")
    record_event("wakeup", source=source, wake_ts=wake_ts, connection_ms=connection_ms)
    if wake_ts is not None:
        duration_ms = min(max((time.time() - wake_ts) * 1000.0, 0.0), WAKEUP_DURATION_MS_CAP)
    else:
        duration_ms = 0.0
    _record_db("wakeup", duration_ms, elder_id=elder_id, source=source)


def record_connection(
    *, connection_id: str, elapsed_ms: float, elder_id: str | None = None
) -> None:
    """建连打点：offer 收到 → answer 产出的耗时（webrtc `_handle_offer` 接线）。"""
    record_event("connection", connection_id=connection_id, elapsed_ms=elapsed_ms)
    _record_db("connection", elapsed_ms, elder_id=elder_id)


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
