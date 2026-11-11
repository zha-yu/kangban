"""Day 5 S2 · 打点双轨测试（裁决 B6/D2，day-05 计划 §3.3 自检）。

覆盖三件事：
1. ``app/core/metrics.record_*`` 双轨一致性——日志轨字段照旧（caplog），
   metrics 轨组装行（trace_id / stage / duration_ms / source）正确交给
   ``metrics_service``（stub 替换单例，不连库）；
2. ``wakeup`` 两口径的 duration 计算（client：accept−wake_ts 时钟偏移防护；
   backend：duration=0 占位）；
3. client_diag ``playback`` 代打（裁决 R3）——合法帧代打 record_stage(source=client)，
   duration_ms 缺失/非法丢弃并记 warning，不触发 vad 兜底、不回执。
"""

from __future__ import annotations

import logging
import time
import types
from typing import Any

import pytest

from app.audio import signaling
from app.core.context import reset_trace_id, set_trace_id
from app.core.metrics import (
    record_connection,
    record_stage,
    record_wakeup,
)

METRICS_LOGGER = "app.core.metrics"


class _StubRecorder:
    """收集 record_stage 组装行的假单例（service 校验逻辑已在 test_metrics_service 覆盖）。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record_stage(
        self,
        trace_id: str | None,
        elder_id: str | None,
        stage: str,
        duration_ms: float,
        source: str | None,
    ) -> bool:
        self.rows.append(
            {
                "trace_id": trace_id,
                "elder_id": elder_id,
                "stage": stage,
                "duration_ms": duration_ms,
                "source": source,
            }
        )
        return True


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _StubRecorder:
    stub_recorder = _StubRecorder()
    monkeypatch.setattr("app.core.metrics.get_metrics_recorder", lambda: stub_recorder)
    return stub_recorder


@pytest.fixture
def trace():
    token = set_trace_id("trace-db-track-1")
    yield
    reset_trace_id(token)


class TestDbTrack:
    def test_record_stage_dual_track(self, stub: _StubRecorder, caplog, trace) -> None:  # type: ignore[no-untyped-def]
        with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
            record_stage("llm_first_token", 733.0, elder_id=None, source="non_stream")

        # 日志轨照旧
        assert caplog.records[0].__dict__["stage"] == "llm_first_token"
        assert caplog.records[0].__dict__["elapsed_ms"] == 733.0
        # metrics 轨组装行
        assert stub.rows == [
            {
                "trace_id": "trace-db-track-1",
                "elder_id": None,
                "stage": "llm_first_token",
                "duration_ms": 733.0,
                "source": "non_stream",
            }
        ]

    def test_wakeup_client_duration_from_wake_ts(self, stub: _StubRecorder, trace) -> None:  # type: ignore[no-untyped-def]
        wake_ts = time.time() - 0.5  # 500ms 前命中
        record_wakeup(source="client", wake_ts=wake_ts, elder_id="elder-1")
        row = stub.rows[0]
        assert row["stage"] == "wakeup"
        assert row["source"] == "client"
        assert row["elder_id"] == "elder-1"  # auth 身份解析随行入表（A-2 接线补全）
        assert 0.0 < row["duration_ms"] <= 300_000.0  # 时钟偏移防护上界

    def test_wakeup_negative_clamped_to_zero(self, stub: _StubRecorder, trace) -> None:  # type: ignore[no-untyped-def]
        """wake_ts 在未来（时钟偏移）：负值截 0，不出负行。"""
        record_wakeup(source="client", wake_ts=time.time() + 10.0)
        assert stub.rows[0]["duration_ms"] == 0.0

    def test_wakeup_backend_placeholder(self, stub: _StubRecorder, trace) -> None:  # type: ignore[no-untyped-def]
        """兜底口径：duration=0 占位（KWS 起点未知），source=backend。"""
        record_wakeup(source="backend")
        row = stub.rows[0]
        assert row["stage"] == "wakeup"
        assert row["source"] == "backend"
        assert row["duration_ms"] == 0.0

    def test_connection_dual_track(self, stub: _StubRecorder, trace) -> None:  # type: ignore[no-untyped-def]
        record_connection(connection_id="dev-1", elapsed_ms=88.8, elder_id="elder-2")
        assert stub.rows == [
            {
                "trace_id": "trace-db-track-1",
                "elder_id": "elder-2",
                "stage": "connection",
                "duration_ms": 88.8,
                "source": None,
            }
        ]

    def test_no_trace_id_passes_none(self, stub: _StubRecorder) -> None:
        """自裁 Z4：无 trace 上下文组装行 trace_id=None（service 层丢弃不入库）。"""
        record_stage("asr", 100.0, source="funasr")
        assert stub.rows[0]["trace_id"] is None


class TestElderIdFallback:
    """_record_db 兜底口径：调用点未显式传 elder_id 时从 runtime_context 身份取。

    覆盖 asr / tts_first_packet 两段（引擎层无身份入参，AGENTS 红线 9 的
    服务端注入口径）；显式传参（wakeup/connection/playback/llm_first_token）优先。
    """

    _DEVICE_ID = "11111111-1111-1111-1111-111111111111"
    _ELDER_ID = "22222222-2222-2222-2222-222222222222"

    def test_falls_back_to_context_identity(
        self,
        stub: _StubRecorder,
        trace,  # type: ignore[no-untyped-def]
    ) -> None:
        from app.core.runtime_context import Identity, identity_scope

        identity = Identity(kind="device", device_id=self._DEVICE_ID, elder_id=self._ELDER_ID)
        with identity_scope(identity):
            record_stage("asr", 100.0, source="funasr")
        assert stub.rows[0]["elder_id"] == self._ELDER_ID

    def test_explicit_elder_id_wins(
        self,
        stub: _StubRecorder,
        trace,  # type: ignore[no-untyped-def]
    ) -> None:
        from app.core.runtime_context import Identity, identity_scope

        identity = Identity(kind="device", device_id=self._DEVICE_ID, elder_id=self._ELDER_ID)
        with identity_scope(identity):
            record_stage("llm_first_token", 733.0, elder_id="elder-explicit", source="non_stream")
        assert stub.rows[0]["elder_id"] == "elder-explicit"

    def test_staff_identity_keeps_none(
        self,
        stub: _StubRecorder,
        trace,  # type: ignore[no-untyped-def]
    ) -> None:
        from app.core.runtime_context import Identity, identity_scope

        identity = Identity(
            kind="staff",
            staff_id="33333333-3333-3333-3333-333333333333",
            role="care_worker",
        )
        with identity_scope(identity):
            record_stage("tts_first_packet", 200.0, source="non_stream")
        assert stub.rows[0]["elder_id"] is None


class TestClientDiagPlayback:
    """裁决 R3：client_diag stage=playback 由服务端代打入 metrics。"""

    @staticmethod
    def _fake_conn() -> Any:
        return types.SimpleNamespace(
            trace_id="trace-diag-1", device_id="dev-1", bound_elder_id="elder-9"
        )

    async def test_playback_forwarded_to_metrics(
        self, stub: _StubRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        # 生产中 WS 建连时 set_trace_id 与 conn.trace_id 同值；测试里对齐 contextvar
        token = set_trace_id("trace-diag-1")
        try:
            conn = self._fake_conn()
            await signaling._handle_client_diag(
                conn,
                {
                    "type": "client_diag",
                    "stage": "playback",
                    "ok": True,
                    "error": "",
                    "duration_ms": 1500,
                },
            )
        finally:
            reset_trace_id(token)
        assert stub.rows == [
            {
                "trace_id": "trace-diag-1",
                "elder_id": "elder-9",
                "stage": "playback",
                "duration_ms": 1500.0,
                "source": "client",
            }
        ]

    async def test_playback_missing_duration_dropped(self, stub: _StubRecorder, caplog) -> None:  # type: ignore[no-untyped-def]
        conn = self._fake_conn()
        with caplog.at_level(logging.WARNING, logger="app.audio.signaling"):
            await signaling._handle_client_diag(
                conn,
                {"type": "client_diag", "stage": "playback", "ok": True, "error": ""},
            )
        assert stub.rows == []
        assert any(r.getMessage() == "ws_bad_message" for r in caplog.records)

    async def test_playback_negative_duration_dropped(self, stub: _StubRecorder) -> None:  # type: ignore[no-untyped-def]
        conn = self._fake_conn()
        await signaling._handle_client_diag(
            conn,
            {
                "type": "client_diag",
                "stage": "playback",
                "ok": True,
                "error": "",
                "duration_ms": -1,
            },
        )
        assert stub.rows == []
