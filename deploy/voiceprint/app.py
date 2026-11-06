"""CAM++ 声纹推理容器 — 只做 /extract，比对在主服务进程内 numpy 完成。

端点：
    POST /extract  multipart audio=WAV bytes → {"embedding": [float, ...]}（192 维）
    GET  /health   → {"status": "ok", "model": "<model_name>"}

关键口径：
- 不装 torch（主服务 pyproject 也不装 torch，重依赖隔离在容器里）
- 用 sherpa-onnx 加载 CAM++ .onnx 模型
- 输入 WAV → numpy float32 → CAM++ extractor → normalize → JSON list[float]
- 推理失败返回 503 + 可读错误；超时保护（app.py 内 asyncio.wait_for）
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

# ---- 日志 ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("voiceprint")

# ---- 模型路径 ----
MODEL_DIR = Path("/app/models")
MODEL_PATH = MODEL_DIR / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
RATE = 16000
DIM = 192  # CAM++ 固定 192 维（spikes/voiceprint/ 已验证）

# ---- Lazy 加载 sherpa-onnx（容器冷启动一次） ----
_extractor = None
_model_fingerprint = ""


def _get_extractor():
    """首次调用时加载 sherpa-onnx SpeakerEmbeddingExtractor。"""
    global _extractor, _model_fingerprint
    if _extractor is not None:
        return _extractor

    if not MODEL_PATH.is_file():
        raise RuntimeError(
            f"CAM++ 模型不存在：{MODEL_PATH}。"
            f"请把 .onnx 文件放入 deploy/voiceprint/models/ 目录（容器 /app/models/ 挂载点）。"
        )

    import sherpa_onnx  # 容器内依赖

    _model_fingerprint = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()[:16]
    logger.info("加载 CAM++ 模型：%s (sha256=%s)", MODEL_PATH.name, _model_fingerprint)

    _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(MODEL_PATH), num_threads=1
        )
    )
    logger.info("✅ CAM++ 模型加载成功（192 维）")
    return _extractor


def _normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    norm = np.linalg.norm(v)
    if not np.isfinite(v).all() or norm < 1e-8:
        raise ValueError("Invalid speaker embedding (NaN or zero-norm)")
    return v / norm


def _extract_embedding(wav_bytes: bytes) -> list[float]:
    """从 WAV bytes 提取归一化 192 维 embedding。"""
    import wave

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        n_ch = wav.getnchannels()
        raw = wav.readframes(wav.getnframes())

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_ch == 2:
        audio = audio[0::2] + audio[1::2]
        audio = audio / 2.0

    extractor = _get_extractor()
    stream = extractor.create_stream()
    stream.accept_waveform(RATE, np.ascontiguousarray(audio, dtype=np.float32))
    stream.input_finished()

    if not extractor.is_ready(stream):
        raise ValueError("音频太短，无法提取声纹（建议 ≥ 2s 清晰语音）")

    emb = _normalize(extractor.compute(stream))
    if emb.shape[0] != DIM:
        raise ValueError(f"维度异常：期望 {DIM}，实际 {emb.shape[0]}")

    return emb.tolist()


# ---- FastAPI App ----
app = FastAPI(title="SilverMate Voiceprint Extractor", version="1.0")


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        _get_extractor()
        return {"status": "ok", "model": MODEL_PATH.name, "dim": DIM, "fp": _model_fingerprint}
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "detail": str(exc)},
        )


@app.post("/extract")
async def extract(audio: UploadFile = File(...)) -> dict[str, Any]:
    """提取声纹 embedding（multipart/form-data，audio=WAV bytes）。

    返回：{"embedding": [192 floats]}
    失败：503 + 可读错误（模型未加载 / 音频非法 / 推理超时）
    """
    try:
        data = await asyncio.wait_for(audio.read(), timeout=10.0)
    except TimeoutError:
        raise HTTPException(status_code=413, detail="上传超时（音频超过 10MB 或网络卡顿）") from None

    if len(data) > 10 * 1024 * 1024:  # 10MB
        raise HTTPException(status_code=413, detail="音频超过 10MB")

    try:
        emb = await asyncio.wait_for(
            asyncio.to_thread(_extract_embedding, data),
            timeout=5.0,
        )
    except TimeoutError:
        logger.warning("推理超时：5s 内未返回")
        raise HTTPException(status_code=503, detail="声纹推理超时，请稍后重试") from None
    except Exception as exc:
        logger.warning("推理失败：%s", exc)
        raise HTTPException(status_code=503, detail=f"声纹推理失败：{exc}") from exc

    logger.info("extract ok (dim=%d)", len(emb))
    return {"embedding": emb}
