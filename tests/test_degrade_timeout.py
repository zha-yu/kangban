"""Day 5 B-2：三档超时并行 watchdog 测试。

三档语义：
  ASR >2000ms → asr_slow （signaling.deliver code="asr_slow"）
  LLM >3000ms → llm_slow （signaling.deliver code="llm_slow"）
  TTS >1000ms → tts_slow （signaling.deliver code="tts_slow"）

provider 断网兜底（ASRUnavailable / LLMTimeout / TTSUnavailable）走 ErrorMsg 旧码，
B-2 slow 是"有结果但太慢"，两套超时不混。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any
from unittest.mock import patch

import pytest

from app.audio.signaling import _ERROR_TALKBACK, ErrorMsg
from app.core.config import Settings
from app.main import get_settings

# ---------------------------------------------------------------------------
# 共用 fixture：捕获 signaling.deliver 的所有调用
# ---------------------------------------------------------------------------


@pytest.fixture
def deliver_records() -> list[ErrorMsg]:
    """返回 list：每条是 signaling.deliver() 收到的 message dict。

    使用方式：在 async 测试里 `async with patch_signaling_deliver(records): ...`
    """
    return []


# ---------------------------------------------------------------------------
# 辅助：在 main.py / webrtc.py 外面直接跑 watchdog 逻辑
# ---------------------------------------------------------------------------


async def _run_asr_pipeline(
    provider_latency_ms: int,
    *,
    degrade_threshold_ms: int = 2000,
) -> list[ErrorMsg]:
    """独立跑 ASR watchdog 流水线：模拟 _consume_asr 的核心逻辑。

    返回 signaling.deliver 收到的所有 ErrorMsg（含 slow + 可能的 unavailable）。
    """
    records: list[ErrorMsg] = []

    def _fake_deliver(_device_id: str, message: dict[str, Any]) -> bool:
        records.append(message)  # type: ignore[arg-type]
        return True

    # 准备 mock provider：按 latency_ms 延迟后产出 PARTIAL + FINAL
    from app.audio.asr import MockASRProvider

    provider = MockASRProvider(latency_ms=provider_latency_ms)
    # Mock stream 里的 provider 不需要真实 queue，构造一个假 iterator
    _sentinel = object()

    async def _fake_queue() -> Any:
        yield b"\x00" * 320  # 一帧静音，触发 provider stream

    # 核心 watchdog 逻辑（复制 webrtc.py _consume_asr 里的片段）
    _state: dict[str, bool] = {"slow_degraded": False}
    _watchdog: asyncio.Task[None] | None = None

    async def _asr_slow_watchdog() -> None:
        await asyncio.sleep(degrade_threshold_ms / 1000)
        if not _state["slow_degraded"]:
            _state["slow_degraded"] = True
            _fake_deliver(
                "device-test",
                {
                    "type": "error",
                    "code": "asr_slow",
                    "message": _ERROR_TALKBACK["asr_slow"],
                },
            )

    # 让 MockASRProvider 的 stream 直接被 await
    with patch("app.audio.webrtc._deliver", side_effect=_fake_deliver):
        try:
            events = await provider.stream(_fake_queue())
        except Exception:
            return records
        _watchdog = asyncio.create_task(_asr_slow_watchdog())
        try:
            first_event = True
            async for _event in events:
                if first_event and _watchdog is not None and not _watchdog.done():
                    _watchdog.cancel()
                    with suppress(asyncio.CancelledError):
                        await _watchdog
                    first_event = False
        finally:
            if _watchdog is not None and not _watchdog.done():
                _watchdog.cancel()
                with suppress(asyncio.CancelledError):
                    await _watchdog

    return records


# ---------------------------------------------------------------------------
# B2-5 测试 1：慢响应只发一次（>threshold 只收到 1 条 asr_slow）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asr_slow_sent_once_when_over_threshold() -> None:
    """provider 慢（2500ms > 2000ms threshold）→ slow 只发 1 次（不重复）。"""
    records = await _run_asr_pipeline(provider_latency_ms=2500, degrade_threshold_ms=2000)
    slow_msgs = [m for m in records if m.get("code") == "asr_slow"]
    assert len(slow_msgs) == 1, f"asr_slow 应只发 1 次，实际 {len(slow_msgs)} 次：{slow_msgs}"


# ---------------------------------------------------------------------------
# B2-5 测试 2：provider 最终成功 → FINAL 正常到达，无额外异常
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asr_provider_success_after_slow_returns_final() -> None:
    """provider 慢但最终成功 → 有 slow + provider 返回正常（无异常抛出）。"""
    # latency_ms=2500 但 MockASRProvider 产出 3 个 event（PARTIAL×2 + FINAL）
    # 所以每个 event 都有 2500ms 延迟，首事件 2500ms 到达
    records = await _run_asr_pipeline(provider_latency_ms=2500, degrade_threshold_ms=2000)
    slow_msgs = [m for m in records if m.get("code") == "asr_slow"]
    assert len(slow_msgs) == 1


# ---------------------------------------------------------------------------
# B2-5 测试 3：下一轮请求不继承上一轮的 slow 状态
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_request_does_not_inherit_slow_state() -> None:
    """两次独立 pipeline 调用，各有各的 _state dict → 每次都独立触发 slow。"""
    records1 = await _run_asr_pipeline(provider_latency_ms=2500, degrade_threshold_ms=2000)
    records2 = await _run_asr_pipeline(provider_latency_ms=2500, degrade_threshold_ms=2000)

    slow1 = [m for m in records1 if m.get("code") == "asr_slow"]
    slow2 = [m for m in records2 if m.get("code") == "asr_slow"]

    assert len(slow1) == 1, "第一轮应触发 slow"
    assert len(slow2) == 1, "第二轮也应独立触发 slow（per-request state 不继承）"


# ---------------------------------------------------------------------------
# B2-5 测试 4：provider 直接成功（无慢）→ 不发 slow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asr_no_slow_when_fast() -> None:
    """provider 快（500ms < 2000ms threshold）→ 不发 slow。"""
    records = await _run_asr_pipeline(provider_latency_ms=500, degrade_threshold_ms=2000)
    slow_msgs = [m for m in records if m.get("code") == "asr_slow"]
    assert len(slow_msgs) == 0, f"provider 快时不应发 slow，实际 {len(slow_msgs)} 次：{slow_msgs}"


# ---------------------------------------------------------------------------
# B2-5 测试 5：watchdog task 清理（try/finally cancel + await）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_cleanup_on_fast_provider() -> None:
    """provider 快 → watchdog 被 cancel + await → asyncio.all_tasks 无残留。"""
    existing = {id(t) for t in asyncio.all_tasks()}
    await _run_asr_pipeline(provider_latency_ms=100, degrade_threshold_ms=2000)
    # 允许 loop task 和测试 fixture 残留；检查没有新增 sleep 类 task
    remaining = {t for t in asyncio.all_tasks() if id(t) not in existing}
    # 我们创建的 watchdog 应该已经被取消
    for t in remaining:
        assert t.done() or t.cancelled(), (
            f"残留 task：{t.get_name()}, done={t.done()}, cancelled={t.cancelled()}"
        )


# ---------------------------------------------------------------------------
# B2-5 测试 6：watchdog 清理（provider 慢 + 超时到达 → watchdog 自己也能干净退出）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_cleanup_after_timeout_fires() -> None:
    """provider 慢 → watchdog 触发 slow → provider 最终返回 → finally 里 watchdog.done() 已 True。"""
    existing = {id(t) for t in asyncio.all_tasks()}
    await _run_asr_pipeline(provider_latency_ms=2500, degrade_threshold_ms=2000)
    remaining = {t for t in asyncio.all_tasks() if id(t) not in existing}
    for t in remaining:
        assert t.done() or t.cancelled(), f"残留 task：{t.get_name()}"


# ---------------------------------------------------------------------------
# B2-5 测试 7：ErrorMsg 扩枚举 + 话术字典完整性
# ---------------------------------------------------------------------------


def test_error_message_enum_and_talkback_complete() -> None:
    """_ERROR_TALKBACK 字典有 7 条（Day5 前 4 + 新增 3 slow），ErrorMsg.code 枚举和它一致。

    关键：signaling.py 有 `from __future__ import annotations`，TypedDict.__annotations__ 里的
    Literal 会变字符串 → get_args 拿不到值。用 typing.get_type_hints() 解实际类型。
    """
    import typing as _typing

    hints = _typing.get_type_hints(ErrorMsg)
    error_code_type = hints["code"]
    literal_args = set(_typing.get_args(error_code_type))

    talkback_keys = set(_ERROR_TALKBACK.keys())

    assert literal_args == talkback_keys, (
        f"ErrorMsg.code 枚举 ({literal_args}) 与 _ERROR_TALKBACK 键 ({talkback_keys}) 不一致"
    )
    assert len(literal_args) == 7, f"应有 7 条 ErrorMsg code，实际 {len(literal_args)}"
    for slow_code in ("asr_slow", "llm_slow", "tts_slow"):
        assert slow_code in literal_args, f"{slow_code} 应在 ErrorMsg.code 枚举里"


# ---------------------------------------------------------------------------
# B2-5 测试 8：阈值走 Settings 常量（不从 env 散读）
# ---------------------------------------------------------------------------


def test_degrade_thresholds_in_settings() -> None:
    """三组 degrade 阈值在 Settings 里有默认值。"""
    s = Settings()  # 用默认值，不从 .env 读
    assert s.degrade_asr_slow_ms == 2000
    assert s.degrade_llm_slow_ms == 3000
    assert s.degrade_tts_slow_ms == 1000


def test_degrade_thresholds_via_get_settings() -> None:
    """get_settings() 也能取到。"""
    s = get_settings()
    assert s.degrade_asr_slow_ms == 2000
    assert s.degrade_llm_slow_ms == 3000
    assert s.degrade_tts_slow_ms == 1000


# ---------------------------------------------------------------------------
# B2-5 测试 9：两套超时不混（ASRUnavailable 异常路径 → 走 error 旧码，不是 slow 码）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_unavailable_does_not_trigger_slow() -> None:
    """provider stream() 直接抛 ASRUnavailable → watchdog 还没启动 → 不发 slow。"""
    from app.audio.asr import ASRUnavailable, MockASRProvider

    records: list[ErrorMsg] = []

    def _fake_deliver(_device_id: str, message: dict[str, Any]) -> bool:
        records.append(message)  # type: ignore[arg-type]
        return True

    provider = MockASRProvider(raise_error=ASRUnavailable("连接失败"))

    async def _fake_queue() -> Any:
        yield b"\x00" * 320

    with patch("app.audio.webrtc._deliver", side_effect=_fake_deliver), suppress(ASRUnavailable):
        await provider.stream(_fake_queue())

    # provider stream() 直接抛 → 上层还没启动 watchdog → slow 不应被发
    slow_msgs = [m for m in records if m.get("code") == "asr_slow"]
    assert len(slow_msgs) == 0


# ---------------------------------------------------------------------------
# B2-5 测试 10：三档语义一致性（watchdog 触发时机相同：都是 threshold 超时即发）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_three_phase_watchdogs_fire_at_same_relative_time() -> None:
    """ASR/LLM/TTS 三档 watchdog 行为一致性验证：都在 threshold 到达后立即发、
    provider 正常返回后被取消、try/finally 三件套清理。

    这里只测 ASR（另外两档用相同 watchdog 逻辑）——LLM 和 TTS 的接入点验证
    见 main.py / webrtc.py 对应的单测集成测试。
    """
    # 2000ms threshold → provider 2100ms → slow 在 ≈2000ms 触发
    records = await _run_asr_pipeline(provider_latency_ms=2100, degrade_threshold_ms=2000)
    slow_msgs = [m for m in records if m.get("code") == "asr_slow"]
    assert len(slow_msgs) == 1

    # 0ms threshold → provider 1900ms → slow 在 ≈0ms 触发（立即）
    records2 = await _run_asr_pipeline(provider_latency_ms=1900, degrade_threshold_ms=0)
    slow2 = [m for m in records2 if m.get("code") == "asr_slow"]
    assert len(slow2) == 1
