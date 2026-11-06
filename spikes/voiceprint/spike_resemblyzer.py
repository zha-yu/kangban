"""Day 4 A1 · Resemblyzer Spike

与 CAM++ 对比：验证 resemblyzer（256 维）在同一样本上的 cosine 分布。
AGENTS §7 硬性阈值 0.85 很可能是为 resemblyzer 定的，CAM++ 原生阈值只有 0.55。

运行：
    uv run --with torch --with resemblyzer --with numpy spikes/voiceprint/spike_resemblyzer.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "spikes" / "reference" / "asr" / "examples"
RATE = 16000


def load_wav(path: Path) -> np.ndarray:
    import wave

    with wave.open(str(path), "rb") as w:
        n_ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_ch == 2:
        audio = audio[0::2] + audio[1::2]
        audio = audio / 2.0
    return audio


def normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = np.linalg.norm(v)
    return v / n if n > 1e-8 else v


def main() -> int:
    print("=" * 64)
    print("Resemblyzer Spike vs CAM++ (Day 4 A1)")
    print("=" * 64)

    try:
        import torch  # noqa: F401
        from resemblyzer import VoiceEncoder, preprocess_wav
    except ImportError as exc:
        print(f"  ❌ 依赖缺失：{exc}")
        print("  运行：uv run --with torch --with resemblyzer --with numpy ...")
        return 1

    print(f"\n[1/3] 加载 VoiceEncoder...")
    encoder = VoiceEncoder()
    print(f"  ✅ 加载成功（device={encoder.device}）")

    print(f"\n[2/3] 提取 embedding...")
    files = {
        "spk1_a": EXAMPLES_DIR / "speaker1_a_cn_16k.wav",
        "spk1_b": EXAMPLES_DIR / "speaker1_b_cn_16k.wav",
        "spk2_a": EXAMPLES_DIR / "speaker2_a_cn_16k.wav",
    }

    embeddings: dict[str, np.ndarray] = {}
    for label, path in files.items():
        if not path.is_file():
            continue
        wav = preprocess_wav(str(path))  # resemblyzer 自带预处理
        emb = normalize(encoder.embed_utterance(wav).numpy())
        embeddings[label] = emb
        print(f"  {label}: dim={emb.shape[0]}, norm={np.linalg.norm(emb):.4f}")

    dim = list(embeddings.values())[0].shape[0]

    print(f"\n[3/3] cosine 相似度矩阵...")
    labels = list(embeddings.keys())
    n = len(labels)
    matrix = np.zeros((n, n))
    for i, li in enumerate(labels):
        for j, lj in enumerate(labels):
            matrix[i, j] = float(np.dot(embeddings[li], embeddings[lj]))

    header = " " * 10 + "".join(f"{l:>9}" for l in labels)
    print(f"\n  相似度矩阵（256 维）：")
    print(f"  {header}")
    print(f"  {'-' * (10 + 9 * n)}")
    for i, li in enumerate(labels):
        row = f"  {li:>8} " + "".join(f"{matrix[i, j]:>9.4f}" for j in range(n))
        print(row)

    # 统计
    same = float(np.dot(embeddings["spk1_a"], embeddings["spk1_b"]))
    cross_max = max(
        float(np.dot(embeddings["spk1_a"], embeddings["spk2_a"])),
        float(np.dot(embeddings["spk1_b"], embeddings["spk2_a"])),
    )

    prd = 0.85
    print(f"\n  同人 cosine: {same:.4f}")
    print(f"  异人 max cosine: {cross_max:.4f}")
    print(f"  gap = {same - cross_max:.4f}")
    print(f"\n  AGENTS §7 硬性阈值 0.85:")
    print(f"    同人 ≥ 0.85? {'✅ YES' if same >= prd else f'❌ NO ({same:.4f})'}")
    print(f"    异人 < 0.85?  {'✅ YES' if cross_max < prd else '❌ NO'}")
    print(f"    综合: {'✅ 0.85 阈值可行' if (same >= prd and cross_max < prd) else '❌ 0.85 不可行'}")

    print(f"\n  模型维度: {dim}（若 OK，迁移 0005 写 vector({dim})）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
