"""Edge-TTS 实现：微软免费 TTS，无需 API Key"""

import asyncio
import io
import logging

import edge_tts

from . import TTSProvider

logger = logging.getLogger(__name__)


class EdgeTTSProvider(TTSProvider):
    """Edge-TTS 语音合成"""

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+0%"):
        self.voice = voice
        self.rate = rate
        self._sample_rate = 16000

    def sample_rate(self) -> int:
        return self._sample_rate

    async def synthesize(self, text: str, emotion: str = "neutral") -> bytes:
        # Edge-TTS 输出 24kHz MP3，需要转 16kHz PCM
        mp3_buffer = io.BytesIO()
        communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)

        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_buffer.write(chunk["data"])

        mp3_buffer.seek(0)
        return await asyncio.to_thread(self._mp3_to_pcm16k, mp3_buffer.read())

    def _mp3_to_pcm16k(self, mp3_bytes: bytes) -> bytes:
        """MP3 -> 16kHz mono int16 PCM"""
        try:
            from pydub import AudioSegment
        except ImportError:
            raise RuntimeError("需要安装 pydub: uv add pydub")

        audio = AudioSegment.from_mp3(io.BytesIO(mp3_bytes))
        audio = (
            audio.set_frame_rate(self._sample_rate).set_channels(1).set_sample_width(2)
        )
        return audio.raw_data

    @classmethod
    def list_voices(cls) -> list[str]:
        """列出可用声音"""
        return [
            "zh-CN-XiaoxiaoNeural",  # 小晓，女声
            "zh-CN-YunxiNeural",  # 云希，男声
            "zh-CN-YunjianNeural",  # 云健，男声
            "zh-CN-XiaoyiNeural",  # 小艺，女声
            "zh-CN-liaoning-XiaobeiNeural",  # 东北话
            "zh-CN-shaanxi-XiaoniNeural",  # 陕西话
        ]


# 备用：无 pydub 时的简易实现（需要 ffmpeg）
class EdgeTTSRawProvider(TTSProvider):
    """Edge-TTS 原始输出（24kHz），由调用方处理重采样"""

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural"):
        self.voice = voice

    def sample_rate(self) -> int:
        return 24000  # Edge-TTS 原生输出

    async def synthesize(self, text: str, emotion: str = "neutral") -> bytes:
        mp3_buffer = io.BytesIO()
        communicate = edge_tts.Communicate(text, self.voice)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_buffer.write(chunk["data"])
        mp3_buffer.seek(0)
        return mp3_buffer.read()  # 返回 MP3，调用方需解码
