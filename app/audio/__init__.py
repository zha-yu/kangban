"""实时语音层。

Day 3 提供 ASR / TTS 两个适配层（A1）；Day 5 补齐 WebRTC 建连与媒体（aiortc）、
WS 信令、反向外呼、按住说话降级、回执与断线检测（A2 / A3 / A5）。

下游（A2 信令、A3 媒体、A5 上传降级）**唯一入口**：

    from app.audio import build_asr, build_tts, ASREvent, ASREventKind, ASRResult

消费约定（硬约束，C1 契约不为此让步）：

1. ``ASRProvider.stream(audio)`` 必须 ``async for`` 读到**自然结束**。提前 ``break``
   会把连接关闭与首包埋点推迟到 GC，导致 ws 连接泄漏、trace 缺一条记录；确实要提前
   退出时用 ``contextlib.aclosing`` 包住：``async with aclosing(provider.stream(a)) as it``。
2. ``FINAL`` / ``ERROR`` 恒为**最后一个事件**，读到它就可以停。
3. ``ASREvent.result`` / ``ASREvent.error`` **故意不设默认值**，构造点一律显式传参——
   默认值会让漏传静默变成一个"合法"事件，比报错更危险。
4. ``ASRResult.confidence`` 在 FunASR 链路恒为 ``None``（供应商不给），只作观测指标
   进 trace，**不参与任何业务决策**；禁止用 0.0 之类的假值填坑。
5. 身份（``elder_id``）由调用方从 ``runtime_context`` 注入，**不接受客户端或模型传入**。

实现分派由配置驱动（``ASR_PROVIDER`` / ``TTS_PROVIDER``）：

- ASR: ``mock`` | ``funasr``（本地基线，默认 ``ws://localhost:7002``） | ``cloud``（骨架）
- TTS: ``mock`` | ``edge``（宿主机 ffmpeg 兜底） | ``cloud``（骨架，生产主路）
"""

from __future__ import annotations

from app.audio.asr import (
    ASRError,
    ASREvent,
    ASREventKind,
    ASRProtocolError,
    ASRProvider,
    ASRResult,
    ASRUnavailable,
    FunASRWSProvider,
    MockASRProvider,
    TTSProvider,
    build_asr,
)
from app.audio.tts import (
    CloudTTSProvider,
    EdgeTTSProvider,
    MockTTSProvider,
    TTSUnavailable,
    build_tts,
)

__all__ = [
    "ASRError",
    "ASREvent",
    "ASREventKind",
    "ASRProtocolError",
    "ASRProvider",
    "ASRResult",
    "ASRUnavailable",
    "CloudTTSProvider",
    "EdgeTTSProvider",
    "FunASRWSProvider",
    "MockASRProvider",
    "MockTTSProvider",
    "TTSProvider",
    "TTSUnavailable",
    "build_asr",
    "build_tts",
]
