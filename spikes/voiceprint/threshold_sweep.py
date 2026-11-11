"""声纹阈值扫描（O3 声纹调优 · Day 8 Step 9 / H13）。

用途：
    对现场补录的互测样本（真人录音，不涉真实长者，原始音频不入库）批量
    提取 CAM++ 声纹向量，计算同人/异人 cosine 配对分数，扫描阈值区间输出
    FAR/FRR 表 + 误识/拒识 case 清单，为阈值定稿（PRD §3.2，当前基线
    cosine ≥ 0.55，PRD v2.8 裁决）提供数据。

扫描口径（主理人裁决 2026-11-11）：
    主区间 0.45–0.70（步长 0.01，含 0.55 基线），另附 0.80/0.85/0.90 三个
    反证点（H13 原文写 0.80–0.90，实测同人对分数达不到，报告留证）。

样本命名规范：
    <speaker>_<take>.wav，如 spk1_1.wav / spk1_2.wav / spk2_1.wav
    （speaker = 最后一个下划线前缀；take 序号随意）
    录音规范：16kHz 单声道 WAV（Windows 录音机默认输出 m4a 需先转格式），
    每人 ≥3 段、每段 ≥5s 正常说话；段间间隔重录，不要连续连读。
    原始录音放 spikes/voiceprint/samples/（.gitignore，不入库可随时删除）。

运行：
    uv run python spikes/voiceprint/threshold_sweep.py [--samples-dir DIR]
    提取向量走声纹容器 POST /extract（docker compose up -d voiceprint），
    结果缓存到 --cache 指定 JSON（默认 samples 目录旁 threshold_sweep_cache.json，
    基于文件内容 sha256，重跑不重复推理）。
    明细输出写 eval/reports/voiceprint_sweep_raw.json（评测报告的数据底稿）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import sys
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

import numpy as np

VOICEPRINT_URL = "http://127.0.0.1:7003/extract"
THRESHOLDS_MAIN = [round(0.45 + 0.01 * i, 2) for i in range(26)]  # 0.45–0.70
THRESHOLDS_REF = [0.80, 0.85, 0.90]  # H13 原文区间反证点


def _post_extract(wav_bytes: bytes, name: str, timeout: float = 30.0) -> list[float]:
    """multipart 上传 WAV 给容器 /extract，返回 192 维向量。"""
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(name)[0] or "audio/wav"
    body = (
        f"--{boundary}\r\n".encode()
        + f'Content-Disposition: form-data; name="audio"; filename="{name}"\r\n'.encode()
        + f"Content-Type: {mime}\r\n\r\n".encode()
        + wav_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        VOICEPRINT_URL,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    embedding = payload.get("embedding")
    if not embedding or len(embedding) != 192:
        raise RuntimeError(f"/extract 返回异常：dim={len(embedding) if embedding else 0}")
    return embedding


def _wav_info(path: Path) -> str:
    """读 WAV 头，提示不符合 16k 单声道规范的样本（不拦截，只提醒）。"""
    try:
        with wave.open(str(path), "rb") as w:
            rate, ch = w.getframerate(), w.getnchannels()
            return f"{rate}Hz {'mono' if ch == 1 else f'{ch}ch'}"
    except Exception as exc:
        return f"无法解析 WAV 头：{exc!r}"


def extract_embeddings(samples_dir: Path, cache_path: Path) -> dict[str, list[float]]:
    """对样本目录全部 .wav 提取向量，带 sha256 缓存。"""
    wavs = sorted(samples_dir.glob("*.wav"))
    if not wavs:
        print(f"[错误] 样本目录没有 .wav：{samples_dir}")
        sys.exit(2)

    cache: dict[str, list[float]] = {}
    if cache_path.is_file():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    embeddings: dict[str, list[float]] = {}
    print(f"样本 {len(wavs)} 个，容器 {VOICEPRINT_URL}，缓存命中规则=文件 sha256")
    for wav in wavs:
        data = wav.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest in cache:
            embeddings[wav.stem] = cache[digest]
            print(f"  {wav.name}: 缓存命中")
            continue
        print(f"  {wav.name}: 提取中（{_wav_info(wav)}）...")
        try:
            vec = _post_extract(data, wav.name)
        except (urllib.error.URLError, RuntimeError) as exc:
            print(f"  [失败] {wav.name}: {exc!r}（容器未起？docker compose up -d voiceprint）")
            sys.exit(3)
        cache[digest] = vec
        embeddings[wav.stem] = vec

    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    return embeddings


def _speaker_of(stem: str) -> str:
    return stem.rsplit("_", 1)[0]


def pair_scores(embeddings: dict[str, list[float]]) -> tuple[list[tuple[str, str, float]], list[tuple[str, str, float]]]:
    """返回 (同人对列表, 异人对列表)，元素 (speaker_a, speaker_b, cosine)。"""
    names = sorted(embeddings)
    vecs = {n: np.asarray(embeddings[n], dtype=np.float64) for n in names}
    vecs = {n: v / np.linalg.norm(v) for n, v in vecs.items()}  # 归一化后点积=cosine

    same, cross = [], []
    speakers = sorted({_speaker_of(n) for n in names})
    for i, sa in enumerate(speakers):
        for sb in speakers[i + 1 :]:
            for na in (n for n in names if _speaker_of(n) == sa):
                for nb in (n for n in names if _speaker_of(n) == sb):
                    score = float(np.dot(vecs[na], vecs[nb]))
                    cross.append((na, nb, score))
    for spk in speakers:
        group = sorted(n for n in names if _speaker_of(n) == spk)
        for i, na in enumerate(group):
            for nb in group[i + 1 :]:
                score = float(np.dot(vecs[na], vecs[nb]))
                same.append((na, nb, score))
    return same, cross


def sweep(
    same: list[tuple[str, str, float]],
    cross: list[tuple[str, str, float]],
) -> list[dict]:
    """逐阈值算 FAR/FRR + case 明细。"""
    results = []
    for t in THRESHOLDS_MAIN + THRESHOLDS_REF:
        fa = [(a, b, s) for a, b, s in cross if s >= t]
        fr = [(a, b, s) for a, b, s in same if s < t]
        results.append(
            {
                "threshold": t,
                "far": round(len(fa) / len(cross), 4) if cross else None,
                "frr": round(len(fr) / len(same), 4) if same else None,
                "false_accept_cases": [
                    {"a": a, "b": b, "cosine": round(s, 4)} for a, b, s in fa
                ],
                "false_reject_cases": [
                    {"a": a, "b": b, "cosine": round(s, 4)} for a, b, s in fr
                ],
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="声纹阈值扫描（O3/H13）")
    parser.add_argument(
        "--samples-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "samples",
        help="样本目录（<speaker>_<take>.wav）",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="向量缓存 JSON（默认 samples 目录旁 threshold_sweep_cache.json）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("eval/reports/voiceprint_sweep_raw.json"),
        help="明细输出 JSON（评测报告数据底稿）",
    )
    args = parser.parse_args()

    cache_path = args.cache or (args.samples_dir / "threshold_sweep_cache.json")
    embeddings = extract_embeddings(args.samples_dir, cache_path)
    same, cross = pair_scores(embeddings)
    results = sweep(same, cross)

    speakers = sorted({_speaker_of(n) for n in embeddings})
    same_scores = sorted(s for _, _, s in same)
    cross_scores = sorted(s for _, _, s in cross)
    print("\n== 分数分布 ==")
    print(f"说话人 {len(speakers)} 人 {len(embeddings)} 段；同人对 {len(same)}，异人对 {len(cross)}")
    if same:
        print(
            f"同人 cosine: min={same_scores[0]:.4f} mean={np.mean(same_scores):.4f} "
            f"max={same_scores[-1]:.4f}"
        )
    if cross:
        print(
            f"异人 cosine: min={cross_scores[0]:.4f} mean={np.mean(cross_scores):.4f} "
            f"max={cross_scores[-1]:.4f}"
        )

    print("\n== FAR/FRR 扫描 ==")
    print("| 阈值 | FAR | FRR | 误识 case | 拒识 case |")
    print("| --- | --- | --- | --- | --- |")
    for r in results:
        print(
            f"| {r['threshold']:.2f} | {r['far']:.4f} | {r['frr']:.4f} "
            f"| {len(r['false_accept_cases'])} | {len(r['false_reject_cases'])} |"
        )

    payload = {
        "samples_dir": str(args.samples_dir),
        "voiceprint_url": VOICEPRINT_URL,
        "speaker_count": len(speakers),
        "sample_count": len(embeddings),
        "same_pairs": len(same),
        "cross_pairs": len(cross),
        "same_scores": [round(s, 4) for s in same_scores],
        "cross_scores": [round(s, 4) for s in cross_scores],
        "sweep": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写 {args.out}（评测报告数据底稿）")


if __name__ == "__main__":
    main()
