"""T1 spike vs T4 容器 embedding 一致性验证。

同一段 speaker1_a WAV，分别用 spike（本地 sherpa-onnx + io.BytesIO）
和容器 deploy/voiceprint/app.py（同一套代码逻辑）提取 embedding，
计算 cosine 相似度应该 ≥ 0.99（完全一致）。

容器必须先起：docker compose up -d voiceprint
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
WAV_PATH = REPO_ROOT / "spikes/reference/asr/examples/speaker1_a_cn_16k.wav"
CONTAINER_URL = "http://localhost:7003"
DIM = 192


def normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = np.linalg.norm(v)
    if n < 1e-8:
        raise ValueError("zero norm")
    return v / n


def extract_via_spike(wav_bytes: bytes) -> np.ndarray:
    """和 deploy/voiceprint/app.py 完全相同的 WAV → embedding 流程。"""
    import io
    import wave
    import sherpa_onnx

    model_path = REPO_ROOT / "spikes/reference/asr/funasr-runtime-resources/models/sherpa-onnx/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model_path), num_threads=1)
    )

    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        n_ch = w.getnchannels()
        raw = w.readframes(w.getnframes())

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_ch == 2:
        audio = audio[0::2] + audio[1::2]
        audio = audio / 2.0

    stream = extractor.create_stream()
    stream.accept_waveform(16000, np.ascontiguousarray(audio, dtype=np.float32))
    stream.input_finished()
    assert extractor.is_ready(stream), "音频太短"
    return normalize(extractor.compute(stream))


def extract_via_container(wav_bytes: bytes) -> np.ndarray:
    """POST /extract 拿 embedding。"""
    resp = httpx.post(
        f"{CONTAINER_URL}/extract",
        files={"audio": ("test.wav", wav_bytes, "audio/wav")},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    assert "embedding" in data
    emb = np.asarray(data["embedding"], dtype=np.float32)
    assert emb.shape[0] == DIM, f"维度不对：{emb.shape[0]}"
    return normalize(emb)


def main() -> int:
    print("=" * 60)
    print("T1 vs T4 Embedding 一致性验证")
    print("=" * 60)

    wav_bytes = WAV_PATH.read_bytes()
    print(f"\n样本: {WAV_PATH.name} ({len(wav_bytes)} bytes)")

    # 容器 /health 验证
    health = httpx.get(f"{CONTAINER_URL}/health", timeout=5.0).json()
    print(f"容器 /health: {health}")

    # T1（本地 spike 同逻辑）
    emb1 = extract_via_spike(wav_bytes)
    print(f"\nT1 spike  embedding: dim={emb1.shape[0]}, norm={np.linalg.norm(emb1):.4f}")

    # T4（容器 /extract）
    emb2 = extract_via_container(wav_bytes)
    print(f"T4 容器   embedding: dim={emb2.shape[0]}, norm={np.linalg.norm(emb2):.4f}")

    cosine = float(np.dot(emb1, emb2))
    diff = np.max(np.abs(emb1 - emb2))
    print(f"\ncosine 相似度: {cosine:.6f}")
    print(f"max 绝对差:   {diff:.6e}")

    verdict = "✅ 一致" if cosine > 0.9999 else ("⚠️ 高度接近" if cosine > 0.99 else "❌ 差异大")
    print(f"\n结论: {verdict}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
