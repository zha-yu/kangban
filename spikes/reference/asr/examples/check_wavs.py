"""快速检查三个 WAV 文件的基本参数（采样率、时长、RMS 能量）。"""
import wave
from pathlib import Path
import numpy as np

EX = Path(__file__).resolve().parents[1] / "examples"

for name in ["speaker1_a_cn_16k.wav", "speaker1_b_cn_16k.wav", "speaker2_a_cn_16k.wav"]:
    p = EX / name
    with wave.open(str(p), "rb") as w:
        n_ch = w.getnchannels()
        sw = w.getsampwidth()
        fr = w.getframerate()
        nf = w.getnframes()
        raw = w.readframes(nf)
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_ch == 2:
        audio = audio[0::2] + audio[1::2]
        audio = audio / 2.0
    rms = np.sqrt(np.mean(audio ** 2))
    peak = np.max(np.abs(audio))
    print(f"{name}: {n_ch}ch, {sw*8}bit, {fr}Hz, {nf/fr:.2f}s, RMS={rms:.4f}, peak={peak:.4f}, file={p.stat().st_size//1024}KB")
