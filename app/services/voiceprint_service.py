"""Day 4 · 声纹领域服务 — voiceprint_service.py

职责（仅业务逻辑 + 唯一写库层）：
    1. 调独立推理容器（deploy/voiceprint/ · 端口 7003）的 POST /extract 端点拿 embedding
    2. Fernet 加密 embedding → voiceprints.embedding（bytea）落库
    3. 进程内 numpy 做 cosine 比对（解密 → normalize → dot）
    4. 状态查询 / 删除

禁区（AGENTS §2）：
    - 不直接 import 推理容器的 sherpa-onnx（torch 重依赖隔离在容器里）
    - 原始音频 bytes 取完即弃，不写临时文件/日志（AC-36）
    - 不 import conversation_threads（那是 identity_service 的事）
    - 不 import FastAPI（路由层在 app/api/voiceprints.py）
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Literal

import httpx
import numpy as np
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import get_trace_id
from app.models.voiceprint import Voiceprint

logger = logging.getLogger("voiceprint_service")

SampleIndex = Literal[1, 2, 3]


# ============================================================
# 自定义异常（REST 层将映射为 HTTP 状态码）
# ============================================================


class VoiceprintError(Exception):
    """声纹服务通用异常基类。REST 层映射为 4xx/5xx。"""


class VoiceprintExtractUnavailable(VoiceprintError):
    """推理容器不可用 / 超时 / 模型未加载 → 503。"""


class VoiceprintDuplicateSample(VoiceprintError):
    """(elder_id, version, sample_index) 唯一约束冲突 → 409。"""


class VoiceprintInvalidAudio(VoiceprintError):
    """WAV 非法 / 维度异常 → 422。"""


# ============================================================
# Fernet 加解密
# ============================================================


def _get_fernet() -> Fernet:
    settings = get_settings()
    return Fernet(settings.voiceprint_fernet_key.encode("utf-8"))


def encrypt_embedding(embedding: np.ndarray) -> bytes:
    """归一化 embedding 数组 → bytes → Fernet 加密。"""
    emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if not np.isfinite(emb).all():
        raise VoiceprintInvalidAudio("embedding 包含 NaN/Inf")
    return _get_fernet().encrypt(emb.tobytes())


def decrypt_embedding(ciphertext: bytes) -> np.ndarray:
    """Fernet 解密 bytes → float32 数组。维度由 Settings 配置校验。"""
    settings = get_settings()
    try:
        raw = _get_fernet().decrypt(ciphertext)
    except InvalidToken as exc:
        raise VoiceprintError(f"Fernet 解密失败：密钥不匹配或密文损坏（{exc}）") from exc
    arr = np.frombuffer(raw, dtype=np.float32)
    expected = settings.voiceprint_embedding_dimension
    if arr.shape[0] != expected:
        raise VoiceprintError(f"解密后 embedding 维度 {arr.shape[0]} ≠ 预期 {expected}")
    return arr


# ============================================================
# 推理容器 HTTP 客户端
# ============================================================


async def extract_embedding_from_container(audio_bytes: bytes) -> np.ndarray:
    """调推理容器 POST /extract，返回归一化 float32 数组。

    失败映射：
        超时 / 网络错误 / 容器 503 → VoiceprintExtractUnavailable (503)
        4xx 客户端错误 → VoiceprintInvalidAudio (422)
        响应 JSON 缺 embedding / 维度不对 → VoiceprintExtractUnavailable (503)
    """
    settings = get_settings()
    url = f"{settings.voiceprint_base_url.rstrip('/')}/extract"
    timeout = httpx.Timeout(settings.voiceprint_extract_timeout_seconds)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                files={"audio": ("sample.wav", audio_bytes, "audio/wav")},
            )
    except httpx.TimeoutException as exc:
        logger.warning("推理容器超时：%s", exc)
        raise VoiceprintExtractUnavailable(f"声纹推理超时：{exc}") from exc
    except httpx.ConnectError as exc:
        logger.warning("推理容器不可达：%s", exc)
        raise VoiceprintExtractUnavailable(f"声纹推理容器不可用：{exc}") from exc

    if resp.status_code >= 500:
        # 5xx（502/503/504）都属于服务端临时错误 → 503 降级
        detail = resp.text[:200] if resp.content else "推理容器暂不可用"
        raise VoiceprintExtractUnavailable(f"推理容器返回 {resp.status_code}: {detail}")
    if resp.status_code >= 400:
        # 4xx 客户端错误（格式、参数）→ 422 validation_error
        detail = resp.text[:200]
        raise VoiceprintInvalidAudio(f"推理容器返回 {resp.status_code}: {detail}")

    try:
        data = resp.json()
        emb_list = data["embedding"]
    except (KeyError, ValueError) as exc:
        logger.warning("推理容器响应格式异常：%s — %s", resp.status_code, resp.text[:200])
        raise VoiceprintExtractUnavailable("推理容器响应格式异常") from exc

    expected = settings.voiceprint_embedding_dimension
    if len(emb_list) != expected:
        raise VoiceprintExtractUnavailable(f"推理容器返回维度 {len(emb_list)} ≠ 预期 {expected}")

    arr = np.asarray(emb_list, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm < 1e-8 or not np.isfinite(arr).all():
        raise VoiceprintExtractUnavailable("推理容器返回了无效 embedding（零范数或含 NaN）")
    return arr / norm


# ============================================================
# 注册 · 状态 · 删除
# ============================================================


@dataclass
class VoiceprintStatus:
    """声纹注册状态（REST GET /status 返回）。"""

    registered: list[int]
    version: int
    status: Literal["none", "partial", "active"]

    @classmethod
    def from_rows(cls, rows: list[Voiceprint]) -> VoiceprintStatus:
        if not rows:
            return cls(registered=[], version=1, status="none")
        version = max(r.version for r in rows)
        current = [r for r in rows if r.version == version and r.status == "active"]
        registered = sorted(r.sample_index for r in current)
        if len(registered) == 3:
            return cls(registered=registered, version=version, status="active")
        elif registered:
            return cls(registered=registered, version=version, status="partial")
        return cls(registered=[], version=version, status="partial")


async def register_sample(
    session: AsyncSession,
    elder_id: uuid.UUID,
    sample_index: SampleIndex,
    audio_bytes: bytes,
) -> VoiceprintStatus:
    """注册一段声纹样本（三段齐自动置 status='active'）。

    写库事务：INSERT voiceprints，唯一约束冲突抛 VoiceprintDuplicateSample。
    原始 audio_bytes 本函数内用完即弃，不存不写日志。
    """
    # 1. 推理容器 → embedding（异常往上抛，REST 层映射）
    emb = await extract_embedding_from_container(audio_bytes)
    # audio_bytes 用完即弃（AC-36）——函数内无任何持久化操作

    # 2. 查询当前版本的已注册样本
    existing = (
        (
            await session.execute(
                select(Voiceprint).where(
                    Voiceprint.elder_id == elder_id,
                    Voiceprint.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    version = max((r.version for r in existing), default=1)

    # 3. INSERT（唯一约束冲突 → 409）
    # 先检查是否已有同 (elder_id, version, sample_index)
    dup = (
        await session.execute(
            select(Voiceprint).where(
                Voiceprint.elder_id == elder_id,
                Voiceprint.version == version,
                Voiceprint.sample_index == sample_index,
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise VoiceprintDuplicateSample(
            f"elder={elder_id}, version={version}, sample_index={sample_index} 已存在"
        )

    encrypted = encrypt_embedding(emb)
    row = Voiceprint(
        elder_id=elder_id,
        version=version,
        sample_index=sample_index,
        embedding=encrypted,
        status="active",
    )
    session.add(row)

    # 4. 检查三段是否齐了
    existing.append(row)
    current_samples = {
        r.sample_index for r in existing if r.status == "active" and r.version == version
    }
    if current_samples == {1, 2, 3}:
        # 三段齐了 → 不做额外操作（Voiceprint 默认 status=active）
        logger.info("elder=%s 声纹注册完成（version=%d）", elder_id, version)

    await session.commit()
    return VoiceprintStatus.from_rows(existing)


async def get_status(
    session: AsyncSession,
    elder_id: uuid.UUID,
) -> VoiceprintStatus:
    rows = (
        (
            await session.execute(
                select(Voiceprint).where(
                    Voiceprint.elder_id == elder_id,
                    Voiceprint.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    return VoiceprintStatus.from_rows(rows)


async def delete_all_for_elder(
    session: AsyncSession,
    elder_id: uuid.UUID,
    actor: str,
) -> int:
    """删除该长者全部 voiceprints，返回删除行数。

    actor 由调用方（REST 层）传入，格式 "device:<uuid>" / "admin:<uuid>" 等，
    用于结构化审计日志（Day 4 只写日志，Day 9 回填 audit_logs 表）。
    """
    result = await session.execute(delete(Voiceprint).where(Voiceprint.elder_id == elder_id))
    await session.commit()
    count = result.rowcount or 0
    logger.info(
        "voiceprint delete",
        extra={
            "trace_id": get_trace_id() or "-",
            "audit_action": "voiceprint_delete",
            "actor": actor,
            "target": f"elder:{elder_id}",
            "result": f"deleted:{count}",
        },
    )
    return count


# ============================================================
# 进程内 cosine 比对（供 identity_service 调用）
# ============================================================


@dataclass
class CosineMatch:
    matched: bool
    score: float
    threshold: float
    elder_id: uuid.UUID | None  # matched=True 时填谁的样本命中


async def cosine_compare_against_elder(
    session: AsyncSession,
    elder_id: uuid.UUID,
    audio_bytes: bytes,
) -> CosineMatch:
    """比对新音频 vs 某长者全部已注册声纹样本。返回最高分。

    候选集 1 位（裁决：设备绑定长者 1v1，恒 1 位），精确扫描 ≤3 行。
    """
    settings = get_settings()
    threshold = settings.voiceprint_similarity_threshold

    # 推理容器 → 新 embedding
    query_emb = await extract_embedding_from_container(audio_bytes)

    # 取该长者全部 active 样本
    rows = (
        (
            await session.execute(
                select(Voiceprint).where(
                    Voiceprint.elder_id == elder_id,
                    Voiceprint.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return CosineMatch(matched=False, score=0.0, threshold=threshold, elder_id=None)

    # 解密 + 比对
    best = 0.0
    for row in rows:
        ref = decrypt_embedding(row.embedding)
        ref_norm = np.linalg.norm(ref)
        if ref_norm < 1e-8:
            continue
        score = float(np.dot(query_emb, ref / ref_norm))
        best = max(best, score)

    matched = best >= threshold
    return CosineMatch(
        matched=matched,
        score=best,
        threshold=threshold,
        elder_id=elder_id if matched else None,
    )
