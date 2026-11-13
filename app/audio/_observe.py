"""A1 观测收口：ASR/TTS 的置信度与首包耗时统一从这里进 trace。

Day 3 的验收口径是「**ASR 置信度进 trace**」（技术设计文档 §8 Day 3 技术验收）与
「分段耗时（ASR/路由/LLM/TTS）都要保存」（PRD §5.3），不是「现在就把 OTel 接上」——
OTel/Langfuse 排在 Day 10（技术设计文档 §8 Day 10 落地清单）。

所以本模块做两件事：
1. 用现成的 `trace_id`（`app/core/context.py` 中间件写入）+ JSON 结构化日志承载观测字段，
   字段名已对齐 `decision_logs.asr_confidence` 与 PRD §5.3 的分段耗时口径；
2. **Day 5 双轨**（day-05 计划 S2）：日志轨照旧 + 经 `app/core/metrics.record_stage`
   把 `asr`（total_ms 口径）与 `tts_first_packet`（非流式口径，自裁 Z2）两段异步批量入
   `metrics` 表（唯一写库入口 `metrics_service`）；Day 10 接 OTel 时仍只改本文件
   两个 `record_*` 的函数体，调用方一行都不用动。

纪律（AGENTS §4 + day-03 计划 §3 裁决）：
- 置信度拿不到就写 ``null``，**禁止伪造 0.0**；
- 只写指标与文本长度，**不写转写全文、不写长者隐私**（日志脱敏硬要求）。
"""

from __future__ import annotations

import logging
import time

from app.core.metrics import record_stage

logger = logging.getLogger("app.audio.observe")


class Stopwatch:
    """毫秒计时器；`elapsed_ms()` 可反复调用（拿首包再拿总耗时）。"""

    __slots__ = ("_t0",)

    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000.0, 2)


def start() -> Stopwatch:
    """开始计时（一般在收到第一帧音频 / 进入 synthesize 时调用）。"""
    return Stopwatch()


def record_asr(
    *,
    provider: str,
    source: str,
    text_len: int,
    is_final: bool,
    confidence: float | None,
    first_packet_ms: float | None,
    total_ms: float | None,
) -> None:
    """记录一次 ASR 观测。

    source 取值：``provider``（引擎直给）/ ``funasr``（协议层拿不到，confidence 恒 None）/ ``mock``。

    Day 5 双轨：日志行照旧 + ``metrics`` 表 ``asr`` 行（total_ms 口径；
    source 透传 provider 口径，供 A-3 FunASR 基线归档区分；终稿才入表）。
    """
    logger.info(
        "asr observation",
        extra={
            "asr_provider": provider,
            "asr_source": source,
            "asr_confidence": confidence,
            "asr_text_len": text_len,
            "asr_is_final": is_final,
            "asr_first_packet_ms": first_packet_ms,
            "asr_total_ms": total_ms,
        },
    )
    if is_final and total_ms is not None:
        record_stage(
            "asr",
            total_ms,
            source=source,
            attrs={
                "asr_provider": provider,
                "asr_source": source,
                "asr_confidence": confidence,
                "asr_text_len": text_len,
                "asr_is_final": is_final,
            },
        )


def record_tts(
    *,
    provider: str,
    voice: str,
    text_len: int,
    pcm_bytes: int,
    first_packet_ms: float | None,
    total_ms: float | None,
) -> None:
    """记录一次 TTS 观测（Day 3 非流式：首包 = 整段可用时刻）。

    Day 5 双轨：日志行照旧 + ``metrics`` 表 ``tts_first_packet`` 行
    （自裁 Z2：非流式口径 source="non_stream"；first_packet 缺失不入表）。
    """
    logger.info(
        "tts observation",
        extra={
            "tts_provider": provider,
            "tts_voice": voice,
            "tts_text_len": text_len,
            "tts_pcm_bytes": pcm_bytes,
            "tts_first_packet_ms": first_packet_ms,
            "tts_total_ms": total_ms,
        },
    )
    if first_packet_ms is not None:
        record_stage(
            "tts_first_packet",
            first_packet_ms,
            source="non_stream",
            attrs={
                "tts_provider": provider,
                "tts_voice": voice,
                "tts_text_len": text_len,
                "tts_pcm_bytes": pcm_bytes,
            },
        )
