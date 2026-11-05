"""C1 · ASR/TTS 适配层契约类型（冻结人 A1，消费方 A2/A3/A5）。

本文件当前只落 **契约类型**，不实现任何 Provider、工厂或外部连接：

- `ASRResult` / `ASREventKind` / `ASREvent` / `ASRError` 两分类：A3 只消费；
- `ASRProvider.stream()`：A3 以 60 ms PCM 帧喂入，按 `async for` 消费事件；
- `TTSProvider.synthesize()`：A3 只接收 16 kHz/16 bit/mono 原始 PCM 并写轨。

约束：
- 不复制第二套协议到 `webrtc.py`；
- `confidence` 拿不到就是 `None`，不伪造 `0.0`；
- 本文件不 import `app.audio.signaling` / `webrtc`，避免反向依赖。

状态：按 `docs/team_tasks_day03/contracts.md` 的 C1 原文落类型；Provider 实现与契约冻结
仍归 A1，A3 只负责让下游可 import。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = [
    "ASRError",
    "ASREvent",
    "ASREventKind",
    "ASRProtocolError",
    "ASRProvider",
    "ASRResult",
    "ASRUnavailable",
    "TTSProvider",
]


@dataclass(frozen=True, slots=True)
class ASRResult:
    """一次语音识别的结果。"""

    text: str
    is_final: bool
    # 引擎提供时取 0.0~1.0；FunASR 协议层拿不到 → None，不造 0.0 假值。
    confidence: float | None


class ASREventKind(StrEnum):
    """ASR 事件类型。"""

    PARTIAL = "partial"
    FINAL = "final"
    ERROR = "error"


class ASRError(Exception):
    """ASR 错误基类；message 面向日志，不直接透出给长者。"""


class ASRUnavailable(ASRError):
    """服务不可达/超时/容器未启动 → 可退避重试（≤2 次），耗尽后走降级话术。"""


class ASRProtocolError(ASRError):
    """协议错帧/握手失败/响应非法 → 不可重试，直接降级话术并打 trace 标记。"""


@dataclass(frozen=True, slots=True)
class ASREvent:
    """ASR 事件：partial/final 携带结果，error 携带异常。"""

    kind: ASREventKind
    result: ASRResult | None
    error: ASRError | None


class ASRProvider(Protocol):
    """ASR 适配器协议；实现方（FunASR/Mock/Cloud）由 A1 负责。

    `stream()` 实现应为 async generator：调用后直接 `async for` 消费，
    不需要先 await 一次拿到迭代器。
    """

    def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[ASREvent]:
        """输入 16 kHz/16 bit/单声道 60 ms PCM 帧流，输出事件流。"""
        ...


class TTSProvider(Protocol):
    """TTS 适配器协议；Day 3 为非流式整段合成。"""

    async def synthesize(self, text: str) -> bytes:
        """输出 16 kHz/16 bit/单声道原始 PCM（无 WAV/RIFF 头）。"""
        ...
