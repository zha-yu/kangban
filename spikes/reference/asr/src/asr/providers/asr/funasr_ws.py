"""FunASR WebSocket ASR Provider：独立实现，不依赖 voice.py"""

import asyncio
import json
import logging
import ssl
from collections.abc import AsyncIterator

import websockets

from asr.providers import ASRProvider, ASRResult

logger = logging.getLogger(__name__)


class FunASRWSProvider(ASRProvider):
    """FunASR WebSocket 远程识别（独立实现）"""

    def __init__(
        self,
        url: str = "wss://127.0.0.1:10096",
        mode: str = "2pass",
        chunk_size: list | None = None,
        insecure: bool = True,
    ):
        self.url = url
        self.mode = mode
        self.chunk_size = chunk_size or [5, 10, 5]
        self.insecure = insecure
        self.ws = None
        self._results: asyncio.Queue[ASRResult] = asyncio.Queue()
        self._closed = False
        self._receiver_task = None

    async def start_session(self) -> None:
        ssl_ctx = None
        if self.url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if self.insecure:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

        self.ws = await websockets.connect(
            self.url,
            ssl=ssl_ctx,
            subprotocols=["binary"],
            ping_interval=None,
            max_size=2**20,
        )
        await self.ws.send(
            json.dumps(
                {
                    "mode": self.mode,
                    "chunk_size": self.chunk_size,
                    "chunk_interval": 10,
                    "encoder_chunk_look_back": 4,
                    "decoder_chunk_look_back": 1,
                    "audio_fs": 16000,
                    "wav_format": "pcm",
                    "wav_name": "provider-session",
                    "is_speaking": True,
                    "itn": True,
                }
            )
        )
        self._receiver_task = asyncio.create_task(self._receive_loop())

    async def feed_audio(self, audio_16k: bytes) -> None:
        if self.ws and not self._closed:
            await self.ws.send(audio_16k)

    async def finish(self) -> None:
        if self.ws:
            await self.ws.send(json.dumps({"is_speaking": False, "is_end": True}))

    async def _receive_loop(self) -> None:
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                if data.get("error"):
                    raise RuntimeError(str(data["error"]))

                result = ASRResult(
                    text=data.get("text", ""),
                    is_final=data.get("is_final", False),
                    mode=data.get("mode", ""),
                    confidence=data.get("confidence", 0.0),
                )
                await self._results.put(result)

                # 2pass-offline final 是最终结果
                if result.is_final and "offline" in result.mode:
                    break
        except Exception as e:
            logger.error(f"FunASR 接收异常: {e}")
        finally:
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
        if self._receiver_task:
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.close()
