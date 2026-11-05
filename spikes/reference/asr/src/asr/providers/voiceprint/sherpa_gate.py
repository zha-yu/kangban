"""Sherpa-ONNX 声纹识别 Provider：独立实现，不依赖 voice.py"""

import hashlib
import json
from pathlib import Path

import numpy as np
import sherpa_onnx

from asr.providers import VoiceprintProvider

RATE = 16000


def normalize(vector):
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = np.linalg.norm(vector)
    if not np.isfinite(vector).all() or norm < 1e-8:
        raise ValueError("Invalid speaker embedding")
    return vector / norm


class SherpaVoiceprintProvider(VoiceprintProvider):
    """基于 sherpa-onnx 的本地声纹识别"""

    def __init__(
        self, model_dir: Path, threshold: float = 0.55, verify_seconds: float = 2.0
    ):
        self.model_dir = Path(model_dir)
        self.threshold = threshold
        self.verify_seconds = verify_seconds

        model_path = (
            self.model_dir / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
        )
        if not model_path.is_file():
            raise FileNotFoundError(model_path)

        self.fingerprint = hashlib.sha256(model_path.read_bytes()).hexdigest()
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(model_path), num_threads=1
            )
        )

    def _vad(self, silence_seconds=0.8, max_seconds=30.0):
        c = sherpa_onnx.VadModelConfig()
        c.ten_vad.model = str(self.model_dir / "ten-vad.onnx")
        c.ten_vad.min_silence_duration = silence_seconds
        c.ten_vad.max_speech_duration = max_seconds + 1
        c.sample_rate = RATE
        return sherpa_onnx.VoiceActivityDetector(c, buffer_size_in_seconds=60)

    def embedding(self, audio: np.ndarray) -> np.ndarray:
        stream = self.extractor.create_stream()
        stream.accept_waveform(RATE, np.ascontiguousarray(audio, dtype=np.float32))
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            raise ValueError("Not enough speech for speaker verification")
        return normalize(self.extractor.compute(stream))

    def speech_only(self, audio: np.ndarray) -> np.ndarray:
        vad = self._vad()
        segments = []
        for i in range(0, len(audio), 256):
            vad.accept_waveform(audio[i : i + 256])
            while not vad.empty():
                segments.append(np.array(vad.front.samples))
                vad.pop()
        vad.flush()
        while not vad.empty():
            segments.append(np.array(vad.front.samples))
            vad.pop()
        if not segments:
            raise ValueError("No speech detected")
        speech = np.concatenate(segments)
        if len(speech) < self.verify_seconds * RATE:
            raise ValueError("Speech too short; record at least 3-5 seconds")
        return speech

    async def verify(self, audio_16k: bytes) -> tuple[bool, float, str]:
        """验证声纹，返回 (是否通过, 得分, 用户标识)"""
        audio = np.frombuffer(audio_16k, dtype=np.int16).astype(np.float32) / 32768.0

        try:
            embedding = self.embedding(self.speech_only(audio))
            profile_path = Path("data/speaker.json")
            if not profile_path.exists():
                return False, 0.0, "unknown"

            record = json.loads(profile_path.read_text(encoding="utf-8"))
            if record.get("model_sha256") != self.fingerprint:
                return False, 0.0, "unknown"

            reference = np.array(record["embedding"], dtype=np.float32)
            score = float(np.dot(reference, embedding))
            passed = score >= self.threshold
            return passed, score, record.get("name", "unknown")
        except Exception:
            return False, 0.0, "unknown"

    async def enroll(self, name: str, audio_files: list[bytes]) -> None:
        """录入声纹"""
        import tempfile
        import wave

        embeddings = []
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, audio_bytes in enumerate(audio_files):
                path = Path(tmpdir) / f"{i}.wav"
                with wave.open(str(path), "wb") as f:
                    f.setnchannels(1)
                    f.setsampwidth(2)
                    f.setframerate(RATE)
                    f.writeframes(audio_bytes)

                with wave.open(str(path), "rb") as wav:
                    audio = (
                        np.frombuffer(wav.readframes(wav.getnframes()), "<i2").astype(
                            np.float32
                        )
                        / 32768
                    )
                embeddings.append(self.embedding(self.speech_only(audio)))

        record = {
            "name": name,
            "model_sha256": self.fingerprint,
            "embedding": normalize(np.mean(embeddings, axis=0)).tolist(),
        }
        dest = Path("data/speaker.json")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    def profile_revision(self) -> str:
        profile_path = Path("data/speaker.json")
        if not profile_path.exists():
            return ""
        record = json.loads(profile_path.read_text(encoding="utf-8"))
        return record.get("model_sha256", "")
