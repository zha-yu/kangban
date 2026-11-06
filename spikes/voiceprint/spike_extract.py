"""Day 4 A1 · CAM++ Spike（v2：加 VAD 预处理）

验证 3D-Speaker CAM++ 声纹模型（192 维）在本环境能否正常加载、提取 embedding，
并实测同/异人样本的 cosine 相似度分布。v2 版加 TEN-VAD 切纯语音段（与
参考项目 sherpa_gate.py 一致），并扫描多个阈值的可分性。

模型：3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx（sherpa-onnx 加载）
VAD：ten-vad.onnx
样本：speaker1_a / speaker1_b / speaker2_a（参考项目 examples/ 下已存在）

运行：
    uv run --with sherpa-onnx --with numpy spikes/voiceprint/spike_extract.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = (
    REPO_ROOT
    / "spikes"
    / "reference"
    / "asr"
    / "funasr-runtime-resources"
    / "models"
    / "sherpa-onnx"
)
MODEL_PATH = MODEL_DIR / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
VAD_MODEL = MODEL_DIR / "ten-vad.onnx"
EXAMPLES_DIR = REPO_ROOT / "spikes" / "reference" / "asr" / "examples"

RATE = 16000


def load_wav_mono_16k(path: Path) -> np.ndarray:
    """加载 WAV 文件为 float32 单声道 16k 采样率。"""
    import wave

    with wave.open(str(path), "rb") as wav:
        n_channels = wav.getnchannels()
        framerate = wav.getframerate()
        n_frames = wav.getnframes()
        raw = wav.readframes(n_frames)

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels == 2:
        audio = audio[0::2] + audio[1::2]
        audio = audio / 2.0
    if framerate != RATE:
        ratio = RATE / framerate
        n_out = int(len(audio) * ratio)
        audio = np.interp(
            np.linspace(0, len(audio) - 1, n_out), np.arange(len(audio)), audio
        )
    return audio.astype(np.float32)


def normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    norm = np.linalg.norm(v)
    if not np.isfinite(v).all() or norm < 1e-8:
        raise ValueError("Invalid speaker embedding")
    return v / norm


def speech_only_with_vad(audio: np.ndarray, vad_model_path: Path) -> np.ndarray:
    """用 sherpa-onnx TEN-VAD 切出纯语音段，避免静音/尾部噪声干扰 embedding。"""
    import sherpa_onnx

    c = sherpa_onnx.VadModelConfig()
    c.ten_vad.model = str(vad_model_path)
    c.ten_vad.min_silence_duration = 0.6  # 静音 0.6s 切段
    c.ten_vad.max_speech_duration = 20.0
    c.sample_rate = RATE
    vad = sherpa_onnx.VoiceActivityDetector(c, buffer_size_in_seconds=60)

    segments: list[np.ndarray] = []
    chunk_size = 256  # sherpa-onnx 官方推荐分块大小
    for i in range(0, len(audio), chunk_size):
        vad.accept_waveform(audio[i : i + chunk_size])
        while not vad.empty():
            segments.append(np.array(vad.front.samples, dtype=np.float32))
            vad.pop()
    vad.flush()
    while not vad.empty():
        segments.append(np.array(vad.front.samples, dtype=np.float32))
        vad.pop()

    if not segments:
        raise RuntimeError("VAD 未检出任何语音段，可能整段都是静音")

    speech = np.concatenate(segments)
    min_speech_len = 2.0 * RATE  # 最少 2 秒有效语音
    if len(speech) < min_speech_len:
        raise RuntimeError(f"有效语音太短：{len(speech)/RATE:.1f}s < 2s")

    return speech


def extract_embedding(extractor, audio_16k: np.ndarray, label: str) -> np.ndarray:
    """从 16k float32 音频提取归一化 embedding。"""
    stream = extractor.create_stream()
    stream.accept_waveform(RATE, np.ascontiguousarray(audio_16k, dtype=np.float32))
    stream.input_finished()
    if not extractor.is_ready(stream):
        raise RuntimeError(f"{label}: 音频太短 / 无有效语音")
    return normalize(extractor.compute(stream))


def main() -> int:
    print("=" * 64)
    print("CAM++ Voiceprint Spike v2 (Day 4 A1 · with TEN-VAD)")
    print("=" * 64)

    # --- 前置检查 ---
    for f, desc in [
        (MODEL_PATH, "CAM++ 模型"),
        (VAD_MODEL, "TEN-VAD 模型"),
    ]:
        if not f.is_file():
            print(f"  ❌ {desc} 不存在：{f}")
            return 1
        print(f"  ✅ {desc} 就位：{f.name}")

    try:
        import sherpa_onnx
    except ImportError:
        print("  ❌ sherpa-onnx 未安装。运行：uv run --with sherpa-onnx --with numpy ...")
        return 2

    # --- 加载模型 ---
    print(f"\n[1/3] 加载 SpeakerEmbeddingExtractor...")
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL_PATH), num_threads=1)
    )
    print(f"  ✅ 模型加载成功")

    # --- 加载样本 + VAD 切段 + 提取 ---
    print(f"\n[2/3] 样本 VAD 切段 + embedding 提取...")

    sample_files = {
        "spk1_a": EXAMPLES_DIR / "speaker1_a_cn_16k.wav",
        "spk1_b": EXAMPLES_DIR / "speaker1_b_cn_16k.wav",
        "spk2_a": EXAMPLES_DIR / "speaker2_a_cn_16k.wav",
    }

    embeddings: dict[str, np.ndarray] = {}
    raw_durs: dict[str, float] = {}
    vad_durs: dict[str, float] = {}

    for label, path in sample_files.items():
        if not path.is_file():
            print(f"  ⚠️ 跳过（缺失）：{path.name}")
            continue

        raw = load_wav_mono_16k(path)
        raw_durs[label] = len(raw) / RATE
        print(f"  {label}: 原始 {raw_durs[label]:.2f}s → VAD 切段...", end=" ")

        try:
            speech = speech_only_with_vad(raw, VAD_MODEL)
        except RuntimeError as exc:
            print(f"❌ {exc}")
            continue

        vad_durs[label] = len(speech) / RATE
        print(f"有效语音 {vad_durs[label]:.2f}s → 提取...", end=" ")

        try:
            emb = extract_embedding(extractor, speech, label)
        except RuntimeError as exc:
            print(f"❌ {exc}")
            continue

        embeddings[label] = emb
        print(f"✅ dim={emb.shape[0]}, norm={np.linalg.norm(emb):.4f}")

    if len(embeddings) < 2:
        print("\n  ❌ 有效样本不足")
        return 4

    dim = list(embeddings.values())[0].shape[0]

    # --- cosine 矩阵 ---
    print(f"\n[3/3] cosine 相似度矩阵 + 阈值扫描...")

    labels = list(embeddings.keys())
    n = len(labels)
    matrix = np.zeros((n, n))
    for i, li in enumerate(labels):
        for j, lj in enumerate(labels):
            matrix[i, j] = float(np.dot(embeddings[li], embeddings[lj]))

    header = " " * 10 + "".join(f"{l:>9}" for l in labels)
    print(f"\n  相似度矩阵（已归一化，对角线 = 1.0）：")
    print(f"  {header}")
    print(f"  {'-' * (10 + 9 * n)}")
    for i, li in enumerate(labels):
        row = f"  {li:>8} " + "".join(f"{matrix[i, j]:>9.4f}" for j in range(n))
        print(row)

    # 同人对（spk1_a ↔ spk1_b）和异人对（spk1 ↔ spk2）
    same_pair = ("spk1_a", "spk1_b")
    cross_pairs = [
        ("spk1_a", "spk2_a"),
        ("spk1_b", "spk2_a"),
    ]

    same_score = float(np.dot(embeddings[same_pair[0]], embeddings[same_pair[1]]))
    cross_scores = [
        float(np.dot(embeddings[a], embeddings[b])) for a, b in cross_pairs
    ]
    cross_max = max(cross_scores)

    print(f"\n  同人对: {same_pair[0]} ↔ {same_pair[1]} = {same_score:.4f}")
    print(f"  异人对 max: {cross_max:.4f}")
    print(f"  gap（同人 - 异人max）= {same_score - cross_max:.4f}")

    # 阈值扫描
    thresholds = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9]
    print(f"\n  阈值扫描（同人 ≥ T ✅ / 异人 < T ✅）：")
    print(f"  {'阈值':>6}  {'同人≥T':>8}  {'异人<T':>8}  {'结论':>10}")
    print(f"  {'-' * 38}")
    for t in thresholds:
        same_ok = same_score >= t
        cross_ok = cross_max < t
        verdict = "可行" if (same_ok and cross_ok) else "不可行"
        print(f"  {t:>6.2f}  {'✅' if same_ok else '❌':>6}  {'✅' if cross_ok else '❌':>6}  {verdict:>10}")

    print(f"\n{'=' * 64}")
    print(f"SPIKE 结论")
    print(f"{'=' * 64}")
    print(f"  模型:      CAM++ 3D-Speaker")
    print(f"  维度:      {dim}")
    print(f"  VAD:       TEN-VAD 切纯语音段 ✅")
    print(f"  同人 cosine: {same_score:.4f}  |  异人 max cosine: {cross_max:.4f}")

    # 与 PRD 裁决阈值 0.85 对比
    prd_threshold = 0.85
    same_ok = same_score >= prd_threshold
    cross_ok = cross_max < prd_threshold
    prd_ok = same_ok and cross_ok

    print(f"\n  PRD/AGENTS 裁决阈值 0.85:")
    print(f"    同人 ≥ 0.85? {'✅ YES' if same_ok else '❌ NO (只有 %.4f)' % same_score}")
    print(f"    异人 < 0.85?  {'✅ YES' if cross_ok else '❌ NO (最大 %.4f)' % cross_max}")
    print(f"    综合结论:     {'✅ 0.85 阈值可行' if prd_ok else '⚠️ 0.85 阈值偏高，CAM++ 同人样本仅 %.4f' % same_score}")

    if not prd_ok:
        # 计算实际可分阈值范围
        best_t = (same_score + cross_max) / 2
        print(f"\n  💡 CAM++ 实际可分阈值区间: ({cross_max:.4f}, {same_score:.4f})")
        print(f"     推荐阈值 = ({cross_max:.4f} + {same_score:.4f}) / 2 = {best_t:.4f}")
        print(f"     需要主理人裁决：是调整阈值还是换模型？")

    return 0


if __name__ == "__main__":
    sys.exit(main())
