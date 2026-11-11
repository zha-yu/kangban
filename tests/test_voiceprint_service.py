"""Day 4 task-01 · T6 — voiceprint_service 单元测试。

覆盖计划 T6 第 6 条验收矩阵：
    1. Fernet 加解密往返 / NaN 拒入 / 维度不符 / 密钥错误
    2. 推理容器 HTTP 客户端：超时、不可达、503、4xx、格式异常、维度异常、正常归一化
    3. 三段注册编排（none → partial → active）、重复样本 409、容器不可用 503
    4. 库内 embedding 为密文、原始音频取完即弃（AC-36，断言无临时文件残留）
    5. 进程内 cosine 比对：同人命中（≥阈值）、异人不命中、无样本、阈值取自 Settings
    6. 删除：行数返回 + 状态回退 none

方案 C 口径（2026-11-06，待主理人裁决阈值终值）：**阈值不依赖 config 默认值**，
测试统一经 env 覆写 `VOICEPRINT_SIMILARITY_THRESHOLD=0.55`（CAM++ spike 实测可行区间，
见 spikes/voiceprint/README.md），裁决落地后只需改默认值，测试本身不动。

DB 用例沿用事务回滚模式；引擎统一走 conftest 的 module 级共享引擎
（阶段 B 夹具治理，跨事件循环投毒 P-03 已根治）；库不可达时按 conftest
口径 skip 并说明原因，不静默通过。
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import numpy as np
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
)

from app.core.config import Settings, get_settings
from app.models import Elder
from app.models.voiceprint import Voiceprint
from app.services import voiceprint_service as vs
from app.services.voiceprint_service import (
    VoiceprintDuplicateSample,
    VoiceprintError,
    VoiceprintExtractUnavailable,
    VoiceprintInvalidAudio,
    VoiceprintStatus,
    cosine_compare_against_elder,
    decrypt_embedding,
    delete_all_for_elder,
    encrypt_embedding,
    extract_embedding_from_container,
    get_status,
    register_sample,
)

# 测试专用 Fernet key（一次性生成，仅进本进程 env，不落任何文件）
TEST_FERNET_KEY = Fernet.generate_key().decode("ascii")

ELDER_ID = "11111111-2222-3333-4444-555555555555"
ELDER_UUID = uuid.UUID(ELDER_ID)

# 方案 C：测试显式钉住阈值，不消费 config 默认的 0.85（待裁决，见文件头说明）
TEST_THRESHOLD = 0.55

DIM = 192  # CAM++ 固定维度（spike 实测，config.voiceprint_embedding_dimension 默认值）


# ============================================================
# 夹具
# ============================================================


@pytest.fixture
def vp_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """注入测试用 VOICEPRINT_* 配置并返回生效的 Settings。

    yield 后必须再 `cache_clear()` 一次，否则带测试 key 的 Settings 会留在
    lru_cache 里泄漏给同进程后续用例（与 test_security.py jwt_env 同契约）。
    """
    monkeypatch.setenv("VOICEPRINT_FERNET_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("VOICEPRINT_BASE_URL", "http://vp-test:7003")
    monkeypatch.setenv("VOICEPRINT_SIMILARITY_THRESHOLD", str(TEST_THRESHOLD))
    monkeypatch.setenv("VOICEPRINT_EXTRACT_TIMEOUT_SECONDS", "1.0")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
async def vp_engine(vp_env: Settings, db_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """复用 conftest 的 module 级共享引擎（阶段 B 夹具治理，P-03 根治）；
    vp_env 仅负责注入 VOICEPRINT_* 测试配置（database_url 不受其影响）。"""
    yield db_engine


@pytest.fixture
async def vp_db(vp_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """每用例一个事务、结束回滚；连不上库就 skip 并说明原因。"""
    try:
        conn = await vp_engine.connect()
    except Exception as exc:  # 任何连接失败都算「无库」
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")

    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


def _db_session(conn: AsyncConnection) -> AsyncSession:
    """把夹具那条未提交的事务借给被测代码；残留由夹具回滚。"""
    return AsyncSession(bind=conn, expire_on_commit=False)


async def _seed_elder(conn: AsyncConnection) -> None:
    """voiceprints.elder_id 的外键目标；占位姓名，不写真实长者数据（AGENTS §4.3）。"""
    await conn.execute(Elder.__table__.insert().values(elder_id=ELDER_UUID, name="测试长者"))


def _emb_onehot(index: int) -> np.ndarray:
    """192 维 one-hot 单位向量：相同 index cosine=1.0，不同 index cosine=0。"""
    vec = np.zeros(DIM, dtype=np.float32)
    vec[index] = 1.0
    return vec


def _mock_extract(returning: np.ndarray):
    """替换推理容器调用，返回固定 embedding（不依赖真实容器/模型）。"""

    async def _fake(audio_bytes: bytes) -> np.ndarray:
        return returning

    return _fake


# ============================================================
# 1. Fernet 加解密
# ============================================================


def test_encrypt_decrypt_roundtrip_restores_embedding(vp_env: Settings) -> None:
    emb = _emb_onehot(3)
    restored = decrypt_embedding(encrypt_embedding(emb))
    assert isinstance(restored, np.ndarray)
    assert restored.shape == (DIM,)
    np.testing.assert_allclose(restored, emb, atol=1e-6)


def test_encrypt_rejects_non_finite_embedding(vp_env: Settings) -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        emb = _emb_onehot(0)
        emb[1] = bad
        with pytest.raises(VoiceprintInvalidAudio):
            encrypt_embedding(emb)


def test_decrypt_rejects_wrong_key(vp_env: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    ciphertext = encrypt_embedding(_emb_onehot(0))
    # 换一把 key：解密必须失败，而不是解出乱码（否则会比出一个随机 cosine）
    monkeypatch.setenv("VOICEPRINT_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    try:
        with pytest.raises(VoiceprintError, match="解密失败"):
            decrypt_embedding(ciphertext)
    finally:
        get_settings.cache_clear()


def test_decrypt_rejects_dimension_mismatch(vp_env: Settings) -> None:
    ciphertext = encrypt_embedding(np.ones(8, dtype=np.float32))  # 8 维 ≠ 192
    with pytest.raises(VoiceprintError, match="维度"):
        decrypt_embedding(ciphertext)


def test_ciphertext_is_not_plaintext_float_bytes(vp_env: Settings) -> None:
    """加密落库的前提：密文不得等于/包含原始 float32 字节串。"""
    emb = _emb_onehot(7)
    ciphertext = encrypt_embedding(emb)
    assert ciphertext != emb.tobytes()
    assert emb.tobytes() not in ciphertext


# ============================================================
# 2. 推理容器 HTTP 客户端（MockTransport，不起真实容器）
# ============================================================


def _patch_http(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """用 MockTransport 替换 vs 模块内的 httpx.AsyncClient 构造。

    必须先存住真实的 AsyncClient：patch 后 `vs.httpx` 就是 httpx 模块本身，
    在工厂里再写 `httpx.AsyncClient` 会递归调回工厂（keyword 冲突 TypeError）。
    """
    real_async_client = httpx.AsyncClient

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(vs.httpx, "AsyncClient", _factory)


async def test_extract_returns_normalized_embedding(
    vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/extract"
        return httpx.Response(200, json={"embedding": [3.0, 4.0] + [0.0] * (DIM - 2)})

    _patch_http(monkeypatch, handler)
    emb = await extract_embedding_from_container(b"fake-wav-bytes")
    assert emb.shape == (DIM,)
    assert np.linalg.norm(emb) == pytest.approx(1.0, abs=1e-6)


async def test_extract_timeout_maps_to_unavailable(
    vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    _patch_http(monkeypatch, handler)
    with pytest.raises(VoiceprintExtractUnavailable):
        await extract_embedding_from_container(b"audio")


async def test_extract_connect_error_maps_to_unavailable(
    vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("container down", request=request)

    _patch_http(monkeypatch, handler)
    with pytest.raises(VoiceprintExtractUnavailable):
        await extract_embedding_from_container(b"audio")


async def test_extract_container_503_maps_to_unavailable(
    vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_http(monkeypatch, lambda req: httpx.Response(503, json={"detail": "模型未加载"}))
    with pytest.raises(VoiceprintExtractUnavailable, match="模型未加载"):
        await extract_embedding_from_container(b"audio")


async def test_extract_client_error_maps_to_invalid_audio(
    vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_http(monkeypatch, lambda req: httpx.Response(422, text="not a wav"))
    with pytest.raises(VoiceprintInvalidAudio):
        await extract_embedding_from_container(b"garbage")


async def test_extract_malformed_response_maps_to_unavailable(
    vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_http(monkeypatch, lambda req: httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(VoiceprintExtractUnavailable):
        await extract_embedding_from_container(b"audio")


async def test_extract_wrong_dimension_maps_to_unavailable(
    vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_http(monkeypatch, lambda req: httpx.Response(200, json={"embedding": [1.0, 2.0]}))
    with pytest.raises(VoiceprintExtractUnavailable, match="维度"):
        await extract_embedding_from_container(b"audio")


async def test_extract_zero_norm_maps_to_unavailable(
    vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_http(monkeypatch, lambda req: httpx.Response(200, json={"embedding": [0.0] * DIM}))
    with pytest.raises(VoiceprintExtractUnavailable):
        await extract_embedding_from_container(b"audio")


# ============================================================
# 3. VoiceprintStatus 纯函数（无库）
# ============================================================


def test_status_from_empty_rows_is_none() -> None:
    status = VoiceprintStatus.from_rows([])
    assert status.status == "none"
    assert status.registered == []
    assert status.version == 1


# ============================================================
# 4. 注册 / 状态 / 删除（真库，事务回滚；extract 全部 mock）
# ============================================================


async def test_register_three_samples_reaches_active(
    vp_db: AsyncConnection, vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """三段注册编排：none → partial → partial → active，version 恒 1。"""
    await _seed_elder(vp_db)
    monkeypatch.setattr(vs, "extract_embedding_from_container", _mock_extract(_emb_onehot(0)))

    async with _db_session(vp_db) as session:
        s1 = await register_sample(session, ELDER_UUID, 1, b"wav-1")
        assert (s1.status, s1.registered, s1.version) == ("partial", [1], 1)
        s2 = await register_sample(session, ELDER_UUID, 2, b"wav-2")
        assert (s2.status, s2.registered) == ("partial", [1, 2])
        s3 = await register_sample(session, ELDER_UUID, 3, b"wav-3")
        assert (s3.status, s3.registered) == ("active", [1, 2, 3])


async def test_register_duplicate_sample_index_raises_409_error(
    vp_db: AsyncConnection, vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_elder(vp_db)
    monkeypatch.setattr(vs, "extract_embedding_from_container", _mock_extract(_emb_onehot(0)))

    async with _db_session(vp_db) as session:
        await register_sample(session, ELDER_UUID, 1, b"wav-1")
        with pytest.raises(VoiceprintDuplicateSample):
            await register_sample(session, ELDER_UUID, 1, b"wav-1-again")


async def test_register_failure_leaves_no_row(
    vp_db: AsyncConnection, vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """容器不可用 → 503 类异常，且库里零写入（不能落半条注册）。"""

    async def _down(audio_bytes: bytes) -> np.ndarray:
        raise VoiceprintExtractUnavailable("container down")

    await _seed_elder(vp_db)
    monkeypatch.setattr(vs, "extract_embedding_from_container", _down)

    async with _db_session(vp_db) as session:
        with pytest.raises(VoiceprintExtractUnavailable):
            await register_sample(session, ELDER_UUID, 1, b"wav-1")
        count = (
            (await session.execute(select(Voiceprint).where(Voiceprint.elder_id == ELDER_UUID)))
            .scalars()
            .all()
        )
        assert count == []


async def test_register_stores_ciphertext_and_discards_raw_audio(
    vp_db: AsyncConnection, vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-36：库内 embedding 是密文；原始音频取完即弃，不留临时文件。"""
    await _seed_elder(vp_db)
    monkeypatch.setattr(vs, "extract_embedding_from_container", _mock_extract(_emb_onehot(5)))

    tmp = tempfile.gettempdir()
    before = set(os.listdir(tmp))
    raw_audio = b"RIFF-raw-audio-must-not-persist"

    async with _db_session(vp_db) as session:
        await register_sample(session, ELDER_UUID, 1, raw_audio)
        row = (
            await session.execute(select(Voiceprint).where(Voiceprint.elder_id == ELDER_UUID))
        ).scalar_one()
        assert row.embedding != _emb_onehot(5).tobytes(), "库内必须是 Fernet 密文"
        assert raw_audio not in row.embedding
        np.testing.assert_allclose(decrypt_embedding(row.embedding), _emb_onehot(5), atol=1e-6)

    after = set(os.listdir(tmp))
    assert after == before, f"注册过程不得产生临时文件，多出：{after - before}"


async def test_get_status_without_rows_returns_none(
    vp_db: AsyncConnection, vp_env: Settings
) -> None:
    await _seed_elder(vp_db)
    async with _db_session(vp_db) as session:
        status = await get_status(session, ELDER_UUID)
    assert status.status == "none"
    assert status.registered == []


async def test_delete_all_removes_rows_and_reports_count(
    vp_db: AsyncConnection, vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_elder(vp_db)
    monkeypatch.setattr(vs, "extract_embedding_from_container", _mock_extract(_emb_onehot(0)))

    async with _db_session(vp_db) as session:
        for idx in (1, 2, 3):
            await register_sample(session, ELDER_UUID, idx, f"wav-{idx}".encode())
        deleted = await delete_all_for_elder(session, ELDER_UUID, actor="device:test")
        assert deleted == 3
        assert (await get_status(session, ELDER_UUID)).status == "none"
        # 重复删除幂等：0 行、不报错
        assert await delete_all_for_elder(session, ELDER_UUID, actor="device:test") == 0


# ============================================================
# 5. 进程内 cosine 比对（候选集 1v1，阈值取自 Settings）
# ============================================================


async def test_cosine_same_speaker_hits_threshold(
    vp_db: AsyncConnection, vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同人（查询向量与注册样本同向）→ cosine=1.0 ≥ 阈值 → matched。"""
    await _seed_elder(vp_db)
    monkeypatch.setattr(vs, "extract_embedding_from_container", _mock_extract(_emb_onehot(0)))

    async with _db_session(vp_db) as session:
        await register_sample(session, ELDER_UUID, 1, b"enroll")
        result = await cosine_compare_against_elder(session, ELDER_UUID, b"query")

    assert result.matched is True
    assert result.score == pytest.approx(1.0, abs=1e-5)
    assert result.threshold == pytest.approx(TEST_THRESHOLD)
    assert result.elder_id == ELDER_UUID


async def test_cosine_different_speaker_misses(
    vp_db: AsyncConnection, vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异人（查询向量与注册样本正交）→ cosine=0 < 阈值 → 不命中且不回填 elder_id。"""
    await _seed_elder(vp_db)
    register_emb = _emb_onehot(0)
    query_emb = _emb_onehot(1)
    monkeypatch.setattr(vs, "extract_embedding_from_container", _mock_extract(register_emb))

    async with _db_session(vp_db) as session:
        await register_sample(session, ELDER_UUID, 1, b"enroll")
        # 比对阶段的「新音频」提取结果与注册不同
        monkeypatch.setattr(vs, "extract_embedding_from_container", _mock_extract(query_emb))
        result = await cosine_compare_against_elder(session, ELDER_UUID, b"query")

    assert result.matched is False
    assert result.score == pytest.approx(0.0, abs=1e-5)
    assert result.elder_id is None


async def test_cosine_no_enrolled_samples_returns_unmatched(
    vp_db: AsyncConnection, vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未注册过的长者：matched=False / score=0，不臆造命中。"""
    await _seed_elder(vp_db)
    monkeypatch.setattr(vs, "extract_embedding_from_container", _mock_extract(_emb_onehot(0)))

    async with _db_session(vp_db) as session:
        result = await cosine_compare_against_elder(session, ELDER_UUID, b"query")

    assert result.matched is False
    assert result.score == 0.0
    assert result.elder_id is None


async def test_cosine_best_score_wins_across_samples(
    vp_db: AsyncConnection, vp_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多样本取最高分：三段里只要有一段达到阈值即命中。"""
    await _seed_elder(vp_db)
    monkeypatch.setattr(vs, "extract_embedding_from_container", _mock_extract(_emb_onehot(0)))

    async with _db_session(vp_db) as session:
        for idx in (1, 2, 3):
            await register_sample(session, ELDER_UUID, idx, f"enroll-{idx}".encode())
        # 查询向量 = onehot(0) 与 onehot(1) 的 45° 混合：与三段(onehot0)的 cosine≈0.707 ≥ 0.55
        mixed = np.zeros(DIM, dtype=np.float32)
        mixed[0] = 1.0
        mixed[1] = 1.0
        mixed /= np.linalg.norm(mixed)
        monkeypatch.setattr(vs, "extract_embedding_from_container", _mock_extract(mixed))
        result = await cosine_compare_against_elder(session, ELDER_UUID, b"query")

    assert result.matched is True
    assert result.score == pytest.approx(float(np.sqrt(0.5)), abs=1e-5)
