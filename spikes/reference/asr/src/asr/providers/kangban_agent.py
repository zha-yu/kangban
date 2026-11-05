"""康伴数字人 WebRTC 整合：Provider 可插拔架构"""

import asyncio
import fractions
import json
import logging
import secrets
import time
from pathlib import Path

import numpy as np
from aiortc import MediaStreamTrack, RTCPeerConnection
from aiortc.contrib.media import MediaBlackhole, MediaRelay
from aiortc.mediastreams import AudioFrame

from asr.providers import ASRProvider, LLMProvider, TTSProvider, VoiceprintProvider
from asr.providers.edge_tts import EdgeTTSProvider
from asr.providers.funasr import FunASRWSProvider
from asr.providers.mock_llm import MockLLMProvider
from asr.providers.voiceprint import SherpaVoiceprintProvider

logger = logging.getLogger(__name__)


# ============================== Token 管理 ==============================


class WebRTCSessionTokens:
    def __init__(self, ttl_seconds=300):
        self.ttl_seconds = ttl_seconds
        self._items = {}

    def issue(self, uid: str, profile_revision: str):
        now = time.time()
        token = secrets.token_urlsafe(32)
        expires_at = now + self.ttl_seconds
        self._items[token] = (expires_at, uid, profile_revision)
        return token, expires_at

    def validate(self, token: str, uid: str, profile_revision: str):
        now = time.time()
        item = self._items.get(token)
        if (
            item is None
            or item[0] <= now
            or item[1] != uid
            or not secrets.compare_digest(item[2], profile_revision)
        ):
            self._items.pop(token, None)
            return None
        return item[0]

    def revoke(self, token: str):
        self._items.pop(token, None)

    def clear_expired(self):
        now = time.time()
        for token, (expires_at, _, _) in list(self._items.items()):
            if expires_at <= now:
                self._items.pop(token, None)


# ============================== 音频转换 ==============================


class AudioConverter:
    @staticmethod
    def webrtc_to_16k(pcm_48k: bytes) -> np.ndarray:
        """48kHz int16 -> 16kHz float32"""
        samples = np.frombuffer(pcm_48k, dtype=np.int16)
        samples_16k = samples[::3]  # 简单3倍抽取
        return samples_16k.astype(np.float32) / 32768.0

    @staticmethod
    def to_48k(pcm_16k: bytes) -> bytes:
        """16kHz int16 -> 48kHz int16"""
        samples = np.frombuffer(pcm_16k, dtype=np.int16)
        samples_48k = np.repeat(samples, 3)  # 简单3倍插值
        return samples_48k.astype(np.int16).tobytes()


# ============================== Provider 门控 Track ==============================


class ProviderGateTrack(MediaStreamTrack):
    """Provider 驱动的声纹门控：可插拔替换"""

    kind = "audio"

    def __init__(
        self,
        source_track,
        voiceprint: VoiceprintProvider,
        tokens: WebRTCSessionTokens,
        uid: str,
        token: str | None = None,
    ):
        super().__init__()
        self.source = source_track
        self.voiceprint = voiceprint
        self.tokens = tokens
        self.uid = uid
        self.token = token
        self.buffer = []
        self.verified = False

        # Token 免验证
        if token:
            revision = voiceprint.profile_revision()
            if tokens.validate(token, uid, revision):
                self.verified = True
                logger.info(f"Token 有效，用户 {uid} 免验证进入")

    async def recv(self):
        frame = await self.source.recv()

        if not self.verified:
            pcm_48k = frame.planes[0].to_bytes()
            self.buffer.append(pcm_48k)

            if len(self.buffer) >= 100:  # 2秒
                audio_16k = AudioConverter.webrtc_to_16k(b"".join(self.buffer))
                pcm_bytes = (audio_16k * 32767).astype(np.int16).tobytes()

                passed, score, user_id = await self.voiceprint.verify(pcm_bytes)

                if passed:
                    self.verified = True
                    revision = self.voiceprint.profile_revision()
                    self.token, _ = self.tokens.issue(self.uid, revision)
                    logger.info(
                        f"声纹验证通过 {user_id}，得分 {score:.3f}，已下发 token"
                    )
                else:
                    logger.warning(f"声纹验证失败，得分 {score:.3f}")
                    silence = np.zeros(frame.samples, dtype=np.int16)
                    frame.planes[0].update(silence.tobytes())

                self.buffer.clear()

        return frame


# ============================== TTS 推送 Track ==============================


class ProviderTTSTrack(MediaStreamTrack):
    """Provider 驱动的 TTS 推送轨"""

    kind = "audio"

    def __init__(self, tts: TTSProvider, sample_rate=48000):
        super().__init__()
        self.tts = tts
        self.sample_rate = sample_rate
        self.chunk = int(sample_rate * 0.02)
        self.queue = asyncio.Queue()
        self.pts = 0
        self._silence = np.zeros(self.chunk, dtype=np.int16).tobytes()

    async def recv(self):
        try:
            pcm_data = await asyncio.wait_for(self.queue.get(), timeout=0.02)
        except TimeoutError:
            pcm_data = self._silence

        frame = AudioFrame(format="s16", layout="mono", samples=self.chunk)
        frame.planes[0].update(pcm_data)
        frame.sample_rate = self.sample_rate
        frame.pts = self.pts
        frame.time_base = fractions.Fraction(1, self.sample_rate)
        self.pts += self.chunk
        return frame

    async def speak(self, text: str, emotion: str = "neutral"):
        """调用 TTS Provider 合成并推送"""
        try:
            pcm_16k = await self.tts.synthesize(text, emotion)
            pcm_48k = AudioConverter.to_48k(pcm_16k)

            # 20ms 分块
            for i in range(0, len(pcm_48k), self.chunk * 2):
                chunk = pcm_48k[i : i + self.chunk * 2]
                if len(chunk) == self.chunk * 2:
                    await self.queue.put(chunk)
        except Exception as e:
            logger.error(f"TTS 合成失败: {e}")


# ============================== 康伴数字人 Agent ==============================


class KangbanDigitalHuman:
    """可插拔 Provider 架构的数字人 Agent"""

    def __init__(
        self,
        voiceprint: VoiceprintProvider,
        asr: ASRProvider,
        llm: LLMProvider,
        tts: TTSProvider,
        token_ttl: int = 300,
    ):
        self.voiceprint = voiceprint
        self.asr = asr
        self.llm = llm
        self.tts = tts
        self.tokens = WebRTCSessionTokens(ttl_seconds=token_ttl)
        self.tts_track = ProviderTTSTrack(tts)
        self.pc = None
        self._processing = False

    async def handle_call(self, offer, sender_uid, ws, call_token=None):
        """处理呼叫，建立完整通话链路"""
        self.pc = RTCPeerConnection()
        relay = MediaRelay()

        @self.pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                # 声纹门控（Provider 驱动）
                gated = ProviderGateTrack(
                    track, self.voiceprint, self.tokens, sender_uid, call_token
                )
                sink = MediaBlackhole()
                sink.addTrack(relay.subscribe(gated))
                asyncio.create_task(sink.start())

                # ASR 处理协程
                asyncio.create_task(self._process_asr(gated))

        # TTS 推送轨
        self.pc.addTrack(self.tts_track)

        await self.pc.setRemoteDescription(offer)
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)

        await ws.send(
            json.dumps(
                {
                    "type": "answer",
                    "sdp": answer.sdp,
                }
            )
        )

    async def _process_asr(self, track):
        """处理 ASR 识别 → LLM → TTS 完整链路"""
        await self.asr.start_session()

        async def feed_audio():
            try:
                while True:
                    frame = await track.recv()
                    pcm_48k = frame.planes[0].to_bytes()
                    audio_16k = AudioConverter.webrtc_to_16k(pcm_48k)
                    pcm_bytes = (audio_16k * 32767).astype(np.int16).tobytes()
                    await self.asr.feed_audio(pcm_bytes)
            except Exception as e:
                logger.info(f"音频喂入结束: {e}")
                await self.asr.finish()

        feed_task = asyncio.create_task(feed_audio())

        try:
            async for result in self.asr.results():
                if not result.is_final or "offline" not in result.mode:
                    continue

                text = result.text.strip()
                if not text:
                    continue

                logger.info(f"ASR 识别: {text}")

                # 防重入
                if self._processing:
                    continue
                self._processing = True

                try:
                    # LLM 生成回复
                    response = await self.llm.chat(text)
                    logger.info(f"LLM 回复: {response.text}")

                    # TTS 播报
                    await self.tts_track.speak(response.text, response.emotion)
                finally:
                    self._processing = False

        except Exception as e:
            logger.error(f"ASR 处理异常: {e}")
        finally:
            feed_task.cancel()
            await self.asr.close()

    async def close(self):
        if self.pc:
            await self.pc.close()
            self.pc = None


# ============================== 工厂函数 ==============================


def create_default_agent() -> KangbanDigitalHuman:
    """创建默认配置的数字人 Agent"""

    # 模型路径：相对于项目根目录（asr/）
    model_dir = (
        Path(__file__).resolve().parents[3]
        / "funasr-runtime-resources/models/sherpa-onnx"
    )
    voiceprint = SherpaVoiceprintProvider(model_dir=model_dir, threshold=0.55)
    asr = FunASRWSProvider(url="wss://127.0.0.1:10096", mode="2pass")
    llm = MockLLMProvider(delay_ms=(300, 800))
    tts = EdgeTTSProvider(voice="zh-CN-XiaoxiaoNeural")

    return KangbanDigitalHuman(
        voiceprint=voiceprint,
        asr=asr,
        llm=llm,
        tts=tts,
        token_ttl=300,
    )


def create_mock_agent() -> KangbanDigitalHuman:
    """创建全 Mock 配置（无外部依赖，用于开发调试）"""
    from asr.providers.funasr import MockASRProvider
    from asr.providers.mock_llm import MockLLMProvider

    class MockVoiceprint(VoiceprintProvider):
        async def verify(self, audio_16k):
            return True, 0.99, "mock_user"

        async def enroll(self, name, audio_files):
            pass

        def profile_revision(self):
            return "mock_v1"

    voiceprint = MockVoiceprint()
    asr = MockASRProvider()
    llm = MockLLMProvider(delay_ms=(100, 300))
    tts = EdgeTTSProvider(voice="zh-CN-XiaoxiaoNeural")

    return KangbanDigitalHuman(
        voiceprint=voiceprint,
        asr=asr,
        llm=llm,
        tts=tts,
        token_ttl=300,
    )
