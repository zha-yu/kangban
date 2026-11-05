"""FunASR Provider：基于 FunASRWSProvider 封装（兼容层）"""

import asyncio
from collections.abc import AsyncIterator

from asr.providers import ASRProvider, ASRResult

from .asr.funasr_ws import FunASRWSProvider

# 兼容旧名称
FunASRProvider = FunASRWSProvider


class MockASRProvider(ASRProvider):
    """Mock ASR：用于无 FunASR 环境开发"""

    def __init__(self):
        self._results: asyncio.Queue[ASRResult] = asyncio.Queue()
        self._closed = False
        self._audio_buffer = []

    async def start_session(self) -> None:
        pass

    async def feed_audio(self, audio_16k: bytes) -> None:
        self._audio_buffer.append(audio_16k)
        if len(self._audio_buffer) % 5 == 0:
            await self._results.put(
                ASRResult(
                    text="[模拟识别中...]",
                    is_final=False,
                    mode="online",
                )
            )

    async def finish(self) -> None:
        await self._results.put(
            ASRResult(
                text="这是模拟的语音识别结果",
                is_final=True,
                mode="2pass-offline",
            )
        )
        self._closed = True

    async def results(self) -> AsyncIterator[ASRResult]:
        while not self._closed or not self._results.empty():
            try:
                result = await asyncio.wait_for(self._results.get(), timeout=1.0)
                yield result
            except TimeoutError:
                continue

    async def close(self) -> None:
        self._closed = True
