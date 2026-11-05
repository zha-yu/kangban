"""Provider 编排器：替代原 voice.py 中 SpeakerGate.run() 的复杂编排逻辑"""

import base64
import io
import json
import logging
import wave
from pathlib import Path

import numpy as np

from asr.providers import ASRProvider, VoiceprintProvider

logger = logging.getLogger(__name__)

RATE = 16000


class VoiceSessionOrchestrator:
    """声纹验证 → ASR 识别 的完整会话编排（Provider 驱动）"""

    def __init__(
        self,
        voiceprint: VoiceprintProvider,
        asr: ASRProvider,
        profile_path: str | Path = "data/speaker.json",
    ):
        self.voiceprint = voiceprint
        self.asr = asr
        self.profile_path = Path(profile_path)

    async def run(
        self,
        audio_blocks,
        emit,
        *,
        endpointing="server",
        authorized=False,
        skip_verify=False,
    ):
        """
        编排声纹验证 + ASR 识别
        audio_blocks: async iterator of float32 numpy arrays (16kHz)
        emit: async callback for events
        """
        if endpointing not in ("server", "client"):
            raise ValueError("Invalid endpointing mode")

        # 加载 profile
        record = None
        if not (authorized or skip_verify):
            if not self.profile_path.exists():
                raise ValueError("请先录入声纹")
            record = json.loads(self.profile_path.read_text(encoding="utf-8"))

        verified = authorized or skip_verify
        buffered = []
        audio_blocks_for_replay = []
        ws = None

        async def open_asr():
            nonlocal ws
            await self.asr.start_session()
            ws = True  # 标记已启动

        if not (authorized or skip_verify):
            await emit({"type": "state", "state": "verifying"})

        try:
            if authorized or skip_verify:
                await emit(
                    {
                        "type": "verification",
                        "accepted": True,
                        "cached": authorized,
                        "name": record["name"] if record else None,
                        "skip_verify": skip_verify,
                    }
                )
                await open_asr()
                await emit({"type": "state", "state": "recognizing"})

            speech_buffer = []
            total_samples = 0
            max_samples = 30 * RATE  # 30秒限制

            async for block in audio_blocks:
                block = np.asarray(block, dtype=np.float32).reshape(-1)
                if not len(block) or len(block) > RATE:
                    continue

                total_samples += len(block)
                audio_blocks_for_replay.append(block.copy())

                if not verified:
                    # 声纹验证阶段：收集音频
                    buffered.append(block.copy())
                    speech_buffer.append(block.copy())

                    # 攒够 2 秒做验证
                    if sum(len(b) for b in speech_buffer) >= 2 * RATE:
                        audio_bytes = b"".join(
                            (np.clip(b, -1, 1) * 32767).astype("<i2").tobytes()
                            for b in speech_buffer
                        )
                        passed, score, name = await self.voiceprint.verify(audio_bytes)

                        await emit(
                            {
                                "type": "verification",
                                "accepted": passed,
                                "score": score,
                                "name": name,
                            }
                        )

                        if not passed:
                            return

                        verified = True
                        await open_asr()
                        # 补发缓冲的音频
                        for saved in buffered:
                            pcm = (
                                (np.clip(saved, -1, 1) * 32767).astype("<i2").tobytes()
                            )
                            await self.asr.feed_audio(pcm)
                        buffered.clear()
                        await emit({"type": "state", "state": "recognizing"})
                else:
                    # ASR 识别阶段
                    pcm = (np.clip(block, -1, 1) * 32767).astype("<i2").tobytes()
                    await self.asr.feed_audio(pcm)

                if total_samples >= max_samples:
                    break

            # 结束音频流
            if verified:
                await self.asr.finish()

            # 接收 ASR 结果
            async for result in self.asr.results():
                await emit(
                    {
                        "type": "transcript",
                        "text": result.text,
                        "is_final": result.is_final,
                        "mode": result.mode,
                    }
                )
                if result.is_final and "offline" in result.mode:
                    break

        finally:
            # 音频回放
            if audio_blocks_for_replay:
                try:
                    audio = np.concatenate(audio_blocks_for_replay)
                    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
                    buf = io.BytesIO()
                    with wave.open(buf, "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(RATE)
                        wf.writeframes(pcm.tobytes())
                    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                    await emit(
                        {
                            "type": "audio",
                            "mime": "audio/wav",
                            "duration_s": round(len(audio) / RATE, 2),
                            "data": b64,
                        }
                    )
                except Exception as e:
                    logger.error(f"音频回放失败: {e}")

            await self.asr.close()
            await emit({"type": "state", "state": "idle"})
