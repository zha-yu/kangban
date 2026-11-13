"""app/core/metrics.py 单元测试（S5 · AC-42 打点位数据底座）。

覆盖 day-04 计划 §S5 验收：
- record 函数输出结构化字段断言（caplog + JsonFormatter）；
- 每行带 trace_id；
- 不写转写全文、不写隐私（只断言指标字段，无文本内容字段）。
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.context import reset_trace_id, set_trace_id
from app.core.logging import JsonFormatter
from app.core.metrics import (
    record_connection,
    record_event,
    record_identity_ask_sent,
    record_identity_confirm_received,
    record_identity_result_sent,
    record_llm_cache,
    record_stage,
    record_wakeup,
    reset_llm_cache,
    snapshot_llm_cache,
    stopwatch,
)

METRICS_LOGGER = "app.core.metrics"


def _formatted(record: logging.LogRecord) -> dict:
    """用真实 JsonFormatter 渲染，断言最终 JSON 输出（对齐生产口径）。"""
    return json.loads(JsonFormatter().format(record))


@pytest.fixture
def _trace():
    token = set_trace_id("trace-metrics-1")
    yield
    reset_trace_id(token)


class TestRecordEvent:
    def test_record_event_flat_fields_with_trace(
        self, caplog: pytest.LogCaptureFixture, _trace
    ) -> None:
        """record_event 平铺字段 + trace_id 进最终 JSON。"""
        with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
            record_event("custom", foo=1, bar="x")

        payload = _formatted(caplog.records[0])
        assert payload["metric"] == "custom"
        assert payload["foo"] == 1
        assert payload["bar"] == "x"
        assert payload["trace_id"] == "trace-metrics-1"

    def test_record_stage_logs_stage_and_elapsed(self, caplog: pytest.LogCaptureFixture) -> None:
        """record_stage：stage + elapsed_ms 字段（AC-42 分段口径）。"""
        with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
            record_stage("router", 123.45)

        payload = _formatted(caplog.records[0])
        assert payload["metric"] == "stage_ms"
        assert payload["stage"] == "router"
        assert payload["elapsed_ms"] == 123.45

    def test_stopwatch_measures_elapsed(self) -> None:
        """stopwatch 可反复取耗时且单调不减。"""
        sw = stopwatch()
        first = sw.elapsed_ms()
        second = sw.elapsed_ms()
        assert first >= 0.0
        assert second >= first


class TestWakeupConnection:
    def test_wakeup_client_source_with_wake_ts(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
            record_wakeup(source="client", wake_ts=1_799_000_000.5, connection_ms=120.0)

        payload = _formatted(caplog.records[0])
        assert payload["metric"] == "wakeup"
        assert payload["source"] == "client"
        assert payload["wake_ts"] == 1_799_000_000.5
        assert payload["connection_ms"] == 120.0

    def test_wakeup_backend_fallback_no_wake_ts(self, caplog: pytest.LogCaptureFixture) -> None:
        """后端兜底口径：WS accept 时刻起算，wake_ts=None。"""
        with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
            record_wakeup(source="backend")

        payload = _formatted(caplog.records[0])
        assert payload["source"] == "backend"
        assert payload["wake_ts"] is None

    def test_wakeup_rejects_unknown_source(self) -> None:
        with pytest.raises(ValueError, match="source"):
            record_wakeup(source="kws")

    def test_connection_logs_id_and_elapsed(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
            record_connection(connection_id="device-1", elapsed_ms=88.8)

        payload = _formatted(caplog.records[0])
        assert payload["metric"] == "connection"
        assert payload["connection_id"] == "device-1"
        assert payload["elapsed_ms"] == 88.8


class TestIdentityPoints:
    """C5 裁决 5 三打点位：A2 signaling 消费，签名即契约。"""

    def test_identity_ask_sent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
            record_identity_ask_sent(ask_id="ask-1", reason="voice_mismatch", elapsed_ms=5.0)

        payload = _formatted(caplog.records[0])
        assert payload["metric"] == "identity_ask_sent"
        assert payload["ask_id"] == "ask-1"
        assert payload["reason"] == "voice_mismatch"
        assert payload["elapsed_ms"] == 5.0

    def test_identity_confirm_received(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
            record_identity_confirm_received(ask_id="ask-1", elapsed_ms=1500.0)

        payload = _formatted(caplog.records[0])
        assert payload["metric"] == "identity_confirm_received"
        assert payload["ask_id"] == "ask-1"
        assert payload["elapsed_ms"] == 1500.0

    def test_identity_result_sent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
            record_identity_result_sent(ask_id="ask-1", elapsed_ms=1800.0)

        payload = _formatted(caplog.records[0])
        assert payload["metric"] == "identity_result_sent"
        assert payload["ask_id"] == "ask-1"
        assert payload["elapsed_ms"] == 1800.0

    def test_no_privacy_fields_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """脱敏红线：打点只含指标字段，不允许出现文本/隐私载荷。"""
        with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
            record_identity_ask_sent(ask_id="ask-2", reason="no_face")
            record_stage("llm", 300.0)
            record_connection(connection_id="device-2", elapsed_ms=10.0)

        forbidden = {"text", "transcript", "content", "audio", "elder_id", "phone", "address"}
        for record in caplog.records:
            payload = _formatted(record)
            leaked = forbidden & payload.keys()
            assert not leaked, f"打点出现隐私/载荷字段: {leaked}"


# ---------------------------------------------------------------------------
# Day 10 B2 · D-1 裁决：prompt cache 进程内计数（方案①）
# ---------------------------------------------------------------------------


class TestLLMCacheCounters:
    """record_llm_cache / snapshot_llm_cache / reset_llm_cache。"""

    def test_none_means_no_record(self) -> None:
        """双 None（供应商未返回 cache 字段）→ 不记、available 不翻转。"""
        reset_llm_cache()
        record_llm_cache(hit_tokens=None, miss_tokens=None)
        snap = snapshot_llm_cache()
        assert snap["calls_with_cache_info"] == 0
        assert snap["cache_info_available"] is False
        assert snap["hit_rate"] is None

    def test_full_info_accumulates_and_hit_rate_recomputable(self) -> None:
        """两侧齐：累加可复算 hit_rate = hit/(hit+miss)。"""
        reset_llm_cache()
        record_llm_cache(hit_tokens=1000, miss_tokens=250)
        record_llm_cache(hit_tokens=500, miss_tokens=250)
        snap = snapshot_llm_cache()
        assert snap["cache_hit_tokens"] == 1500
        assert snap["cache_miss_tokens"] == 500
        assert snap["calls_with_cache_info"] == 2
        assert snap["hit_rate"] == 0.75

    def test_partial_info_keeps_known_side_only(self) -> None:
        """只有一侧已知：已知侧照记，hit_rate 不可复算（None），不编造。"""
        reset_llm_cache()
        record_llm_cache(hit_tokens=300, miss_tokens=None)
        snap = snapshot_llm_cache()
        assert snap["cache_hit_tokens"] == 300
        assert snap["cache_miss_tokens"] == 0
        assert snap["hit_rate"] is None  # miss 未知 → 不复算

    def test_reset_clears(self) -> None:
        record_llm_cache(hit_tokens=10, miss_tokens=10)
        reset_llm_cache()
        snap = snapshot_llm_cache()
        assert snap["cache_info_available"] is False and snap["cache_hit_tokens"] == 0
