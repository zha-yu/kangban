# owner=dev-D2 · 依据 PRD FR-MED-06 / FR-UI-10 / PRD:336（rtc_failed）｜Day 9 F17（不得裁减项）
"""反向 WebRTC 外呼管理器 —— 服务端主动发起 offer，pad 自动接听建连。

方案（已批准 2026-11-12，B1 组合，登记于 docs/plans/day09/dev-d-day09-工作推进计划.md §6.2）：

- **不新建信令服务**（AGENTS §1）：offer 经既有信令 `signaling.deliver` 下发，
  pad 的 answer / ice 走既有 `/ws/signal` 上行（webrtc.py `handle_signal_message`
  新增 `answer` 分支）；
- **B1：复用设备活跃会话、单次握手**。会话注册表以 device_id 为键
  （webrtc.py `_signaling_registry()`），**已存在会话即视为「会话中」→ 跳过外呼**，
  调用方降级页面播报（PRD:575 口径）——把「会话中外呼占用冲突」边界直接消掉；
- **失败归因**：设备不在线（`no_connection`）、会话中（`in_session`）、应答超时 /
  SDP 非法 / ICE 失败（统一 `rtc_failed`）。`failure_reason='rtc_failed'` 的
  **落库由调用方 service 层完成**（X8 枚举已在库；本模块不 import services，
  AGENTS §2 分层）；
- 外呼**成功后不播报**：本模块只负责建连，触达播报走既有
  `webrtc.speak_for_device` / `enqueue_tts_for_device`（单一职责）。

接线状态（骨架期自验 + 联调段对齐 B4）：

- 信令侧 `install_signaling_handler`（webrtc.py，应用启动已装）已覆盖 answer 分支，
  **无需改 main.py**；
- 触发方：B4 用药触达链在发起时调 `initiate_outbound_call`（联调 D11 10:00）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.core.context import new_trace_id

logger = logging.getLogger(__name__)

# 呼叫失败归因（调用方据此落 failure_reason；'rtc_failed' 与 PRD:336 / X8 枚举逐字对齐）
FAILURE_NO_CONNECTION = "no_connection"  # 设备不在线 / 信令未鉴权
FAILURE_IN_SESSION = "in_session"  # 会话中跳过外呼（B1 策略），调用方走降级页面播报
FAILURE_RTC_FAILED = "rtc_failed"  # 应答超时 / SDP 非法 / ICE 失败

# 等待 pad answer 的默认超时（秒）。ICE 协商失败的判定归 MediaSession 的
# ice watchdog（webrtc_ice_timeout_seconds，默认 5s），本超时只守「answer 帧回来没有」。
_DEFAULT_ANSWER_TIMEOUT_S = 10.0

# 等待中的外呼：device_id → (answer 到达事件, sdp)。device_id 由 webrtc 的
# answer 分支经 notify_outbound_answer 唤醒。
_pending_answers: dict[str, asyncio.Event] = {}
_answer_sdps: dict[str, str] = {}


@dataclass(frozen=True)
class OutboundCallResult:
    """一次外呼尝试的终态。connected=False 时 failure_reason 必非 None。"""

    device_id: str
    connected: bool
    failure_reason: str | None
    elapsed_ms: int


def notify_outbound_answer(device_id: str, sdp: str) -> None:
    """webrtc 的 C→S answer 分支回调：唤醒等待方并暂存 SDP（内部接口）。"""
    event = _pending_answers.get(device_id)
    if event is None:
        return  # 无等待方（重复 answer / 会话已超时回收）→ 静默，媒体层已拒绝
    _answer_sdps[device_id] = sdp
    event.set()


async def initiate_outbound_call(
    device_id: str,
    *,
    answer_timeout_s: float = _DEFAULT_ANSWER_TIMEOUT_S,
) -> OutboundCallResult:
    """向指定设备发起一次反向 WebRTC 外呼（FR-MED-06 / FR-UI-10）。

    流程：占位等待槽 → 建会话（复用 webrtc 装配回调）→ 服务端 offer → 信令下发
    → 等 pad answer（answer 帧由 webrtc 分支收口握手并唤醒本等待）→ 终态返回。

    失败不抛异常（与 speak_for_device 同口径）：调用方按 `failure_reason`
    决定落 `rtc_failed` 还是走降级页面播报（PRD:575）。
    """
    from app.audio.webrtc import (
        MediaError,
        MediaFormatError,
        MediaStateError,
        _asr_callback,
        _deliver,
        _signaling_registry,
        _vad_callback,
    )

    started = time.monotonic()

    def _elapsed_ms() -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    # --- ① 占位等待槽（先占位再发 offer，answer 竞态不丢）---
    if device_id in _pending_answers:
        return OutboundCallResult(
            device_id=device_id,
            connected=False,
            failure_reason=FAILURE_RTC_FAILED,  # 并发外呼视同失败，不静默重入
            elapsed_ms=_elapsed_ms(),
        )

    registry = _signaling_registry()
    if registry.get_session(device_id) is not None:
        # B1 策略：会话中跳过外呼 → 调用方直接走降级页面播报（PRD:575）
        logger.info(
            "outbound_call_skipped_in_session",
            extra={"device_id": device_id, "reason": FAILURE_IN_SESSION},
        )
        return OutboundCallResult(
            device_id=device_id,
            connected=False,
            failure_reason=FAILURE_IN_SESSION,
            elapsed_ms=_elapsed_ms(),
        )

    trace_id = new_trace_id()

    # --- ② 建会话（参数与 webrtc._handle_offer 同构；失败回调额外唤醒等待方）---
    media_failed = asyncio.Event()

    async def _on_media_failed(
        connection_id: str, reason: Any, detail: str
    ) -> None:
        # 镜像 webrtc._failure_callback：清语音段缓存、关会话；再加外呼等待唤醒
        from app.audio import webrtc as _webrtc

        _webrtc._latest_segments.pop(device_id, None)
        with contextlib.suppress(MediaError):
            await registry.close_session(device_id, reason=str(getattr(reason, "value", reason)))
        media_failed.set()
        logger.warning(
            "outbound_call_media_failed",
            extra={
                "trace_id": trace_id,
                "device_id": device_id,
                "reason": str(getattr(reason, "value", reason)),
                "detail": detail,
            },
        )

    try:
        session = registry.create_session(
            device_id,
            trace_id,
            on_asr_event=_asr_callback(device_id, trace_id),
            on_media_failed=_on_media_failed,
            on_vad_event=_vad_callback(trace_id),
        )
    except MediaError as exc:
        logger.warning(
            "outbound_call_session_failed",
            extra={"trace_id": trace_id, "device_id": device_id,
                   "error_type": type(exc).__name__},
        )
        return OutboundCallResult(
            device_id=device_id,
            connected=False,
            failure_reason=FAILURE_RTC_FAILED,
            elapsed_ms=_elapsed_ms(),
        )

    # --- ③ 服务端 offer → 信令下发 ---
    try:
        offer_sdp = await session.create_outbound_offer()
    except (MediaStateError, MediaFormatError) as exc:
        logger.warning(
            "outbound_call_offer_failed",
            extra={"trace_id": trace_id, "device_id": device_id,
                   "error_type": type(exc).__name__},
        )
        await _safe_close(registry, device_id, "outbound_offer_failed")
        return OutboundCallResult(
            device_id=device_id,
            connected=False,
            failure_reason=FAILURE_RTC_FAILED,
            elapsed_ms=_elapsed_ms(),
        )

    answer_event = asyncio.Event()
    _pending_answers[device_id] = answer_event
    try:
        if not _deliver(device_id, {"type": "offer", "sdp": offer_sdp}):
            # 设备不在线 / 未鉴权（信令 deliver False 口径）
            logger.info(
                "outbound_call_no_connection",
                extra={"trace_id": trace_id, "device_id": device_id},
            )
            await _safe_close(registry, device_id, "outbound_no_connection")
            return OutboundCallResult(
                device_id=device_id,
                connected=False,
                failure_reason=FAILURE_NO_CONNECTION,
                elapsed_ms=_elapsed_ms(),
            )

        # --- ④ 等 pad answer（或媒体失败）---
        wait_done = asyncio.create_task(answer_event.wait())
        fail_done = asyncio.create_task(media_failed.wait())
        try:
            _done, _pending = await asyncio.wait(
                {wait_done, fail_done},
                timeout=answer_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (wait_done, fail_done):
                if not task.done():
                    task.cancel()

        if answer_event.is_set():
            # 握手已由 webrtc answer 分支收口（setRemote + ICE 冲刷 + watchdog）；
            # ICE 是否最终连通由媒体层 watchdog 裁决，这里以 answer 到达为建连成功口径
            logger.info(
                "outbound_call_answered",
                extra={"trace_id": trace_id, "device_id": device_id,
                       "elapsed_ms": _elapsed_ms()},
            )
            return OutboundCallResult(
                device_id=device_id,
                connected=True,
                failure_reason=None,
                elapsed_ms=_elapsed_ms(),
            )

        reason = FAILURE_RTC_FAILED  # 超时或媒体失败，统一 rtc_failed 归因
        logger.warning(
            "outbound_call_failed",
            extra={"trace_id": trace_id, "device_id": device_id,
                   "reason": "media_failed" if media_failed.is_set() else "answer_timeout",
                   "elapsed_ms": _elapsed_ms()},
        )
        await _safe_close(registry, device_id, "outbound_failed")
        return OutboundCallResult(
            device_id=device_id,
            connected=False,
            failure_reason=reason,
            elapsed_ms=_elapsed_ms(),
        )
    finally:
        _pending_answers.pop(device_id, None)
        _answer_sdps.pop(device_id, None)


async def _safe_close(registry: Any, device_id: str, reason: str) -> None:
    """关会话兜底：不存在 / 已关不再抛（失败路径不互相掩盖）。"""
    from app.audio.webrtc import MediaError

    with contextlib.suppress(MediaError):
        await registry.close_session(device_id, reason=reason)


__all__ = [
    "FAILURE_IN_SESSION",
    "FAILURE_NO_CONNECTION",
    "FAILURE_RTC_FAILED",
    "OutboundCallResult",
    "initiate_outbound_call",
    "notify_outbound_answer",
]
