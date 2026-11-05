"""按住说话降级链路 REST 路由 —— `POST /api/audio/utterance`（契约 C3，Day 3 A5 冻结）。

分层硬约束（AGENTS §2）：
- 本模块**禁止 import ORM / services**，只做三件事：参数校验 → 调 ASR/TTS 适配器 → 序列化。
- `elder_id` 只从设备 JWT 绑定关系取（禁区 9），客户端传参一律忽略。
- 错误统一走 `ErrorResponse`（`{detail, code, trace_id}`），由 app/main.py 全局 handler 兜底。
"""

from __future__ import annotations

import io
import logging
import wave
from collections.abc import Callable
from typing import Annotated, TypedDict

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.api.schemas.auth import ErrorResponse
from app.core.auth import current_device
from app.core.context import get_trace_id
from app.core.runtime_context import Identity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audio", tags=["audio"])


# ---------------------------------------------------------------------------
# C3 · 降级 REST 契约（冻结人：A5，消费方：A4 前端降级模式）
# ---------------------------------------------------------------------------


class UtteranceReply(TypedDict):
    """端点成功响应里的 reply 对象。"""

    text: str  # 回复话术（Day 3 为写死模板，Day 6 接真 LLM 后替换）
    audio_url: str | None  # 服务端合成音频可取地址；无 TTS 时为 null


class UtteranceResponse(TypedDict):
    """`POST /api/audio/utterance` 的 200 响应体。"""

    text: str  # ASR 终稿文本
    reply: UtteranceReply


# ---------------------------------------------------------------------------
# 常量：降级话术 + 音频上限（PRD 硬性数值，不得私自调整）
# ---------------------------------------------------------------------------

DEGRADE_TEMPLATES: dict[str, str] = {
    "asr_unavailable": ("抱歉，语音识别暂时不可用。您可以直接输入文字，或稍后再试。"),
    "tts_unavailable": ("抱歉，语音播报暂时不可用，但我会用文字回复您。"),
    # 以下两个 Day 6 接真 LLM / Tool 熔断机制后才会被触发（Day 3 当前不可达）
    "llm_timeout": ("抱歉，我正在思考，您可以稍后再试或联系工作人员。"),
    "tool_breaker": ("抱歉，当前请求处理超时，我会尽快通知工作人员。"),
}

MAX_AUDIO_SECONDS = 30  # PRD 硬性上限


# ---------------------------------------------------------------------------
# Mock ASR / TTS 工厂（Day 3 不接真链路，Block 3 收口后替换为 A1 真实现）
# ---------------------------------------------------------------------------


class ASRUnavailable(RuntimeError):
    """Mock ASR 故意不可用时抛出；真实链路会有更细的异常分类。"""


class TTSUnavailable(RuntimeError):
    """Mock TTS 故意不可用时抛出。"""


def mock_asr_factory(
    text: str = "Mock ASR result", *, unavailable: bool = False
) -> Callable[[bytes], str]:
    """返回一个 Mock ASR 函数；`unavailable=True` 时抛 ASRUnavailable。

    测试里用它控制 Mock ASR 的成功/失败，不引入 patch（项目约定：Mock 只作注入，
    不用 unittest.mock 打补丁）。
    """

    def _recognize(audio: bytes) -> str:
        if unavailable:
            raise ASRUnavailable("mock asr container down")
        logger.debug("mock asr: audio_bytes=%d → text=%s", len(audio), text)
        return text

    return _recognize


def mock_tts_factory(
    audio_url: str | None = None, *, unavailable: bool = False
) -> Callable[[str], str | None]:
    """返回一个 Mock TTS 函数；`unavailable=True` 时抛 TTSUnavailable。"""

    def _synthesize(text: str) -> str | None:
        if unavailable:
            raise TTSUnavailable("mock tts service down")
        return audio_url

    return _synthesize


# ---------------------------------------------------------------------------
# FastAPI 依赖函数 —— 默认工厂，可被 dependency_overrides 替换（测试控制）
# ---------------------------------------------------------------------------


async def _get_default_asr() -> Callable[[bytes], str]:
    """默认 ASR 依赖：Mock 成功路径。测试 override 成 unavailable 版本。"""
    return mock_asr_factory()


async def _get_default_tts() -> Callable[[str], str | None]:
    """默认 TTS 依赖：Mock 成功路径，audio_url 指向 static。"""
    return mock_tts_factory(audio_url="http://static.example.com/mock.mp3")


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _wav_duration_ms(audio: bytes) -> int:
    """从 WAV 头计算时长（ms）。失败返回 0，不抛异常。"""
    try:
        with wave.open(io.BytesIO(audio), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            if rate <= 0:
                return 0
            return int(frames * 1000 / rate)
    except wave.Error:
        logger.warning("wav_duration: invalid wav header (len=%d)", len(audio))
        return 0


async def _read_upload_file(file: UploadFile) -> bytes:
    """读 multipart UploadFile 到 bytes（用 await，符合 FastAPI 异步规范）。"""
    return await file.read()


# ---------------------------------------------------------------------------
# POST /api/audio/utterance
# ---------------------------------------------------------------------------


@router.post(
    "/utterance",
    response_model=UtteranceResponse,
    summary="按住说话降级链路：整段上传 WAV → ASR + 模板回复",
    responses={
        401: {"model": ErrorResponse, "description": "device token 失效/伪造/过期"},
        413: {"model": ErrorResponse, "description": "音频超 30s 拒收"},
        422: {"model": ErrorResponse, "description": "表单字段缺失/类型错误"},
        503: {"model": ErrorResponse, "description": "ASR 不可用（降级话术）"},
    },
)
async def utterance(
    identity: Annotated[Identity, Depends(current_device)],
    asr: Annotated[Callable[[bytes], str], Depends(_get_default_asr)],
    tts: Annotated[Callable[[str], str | None], Depends(_get_default_tts)],
    audio: Annotated[UploadFile, File(...)],
    duration_ms: int | None = Form(default=None),
) -> UtteranceResponse:
    """按住说话降级端点 —— **WebRTC 主链路不可用时的唯一回退**。

    流程（Block 3 Day 3 冻结，后续 Block 不改变契约）：
        ① 鉴权 → Depends(current_device) 已完成；elder_id 从库绑定取（禁区 9）
        ② 读 WAV 文件头，校验 ≤ 30s
        ③ Mock ASR → 识别文本；不可用时走降级话术（503）
        ④ 组装 Day 3 写死回复话术："您说的是『{asr_text}』，我记下了。"
        ⑤ Mock TTS → 合成音频；不可用时 audio_url=null + 降级话术（200 不白屏）

    Day 6 接真 LLM 后，④ 会换成真 Agent 回复；本契约不动。
    """
    trace_id = get_trace_id() or "-"
    device_id = identity.device_id or "-"
    elder_id = identity.elder_id or "unbound"

    logger.info(
        "utterance start (device=%s, elder=%s, trace_id=%s)",
        device_id,
        elder_id,
        trace_id,
    )

    # --- ② 参数校验 ---
    audio_bytes = await _read_upload_file(audio)
    if not audio_bytes:
        raise HTTPException(status_code=422, detail="audio file is empty")

    # 用 WAV 头算真实时长；不信任客户端传的 duration_ms
    real_duration_ms = _wav_duration_ms(audio_bytes)
    if real_duration_ms > MAX_AUDIO_SECONDS * 1000:
        logger.warning(
            "utterance rejected: audio too long (real=%dms, client=%sms, trace_id=%s)",
            real_duration_ms,
            duration_ms,
            trace_id,
        )
        raise HTTPException(
            status_code=413,
            detail=f"audio exceeds {MAX_AUDIO_SECONDS}s limit",
        )

    logger.debug(
        "utterance audio accepted: len=%d, real_duration_ms=%d, client_duration_ms=%s",
        len(audio_bytes),
        real_duration_ms,
        duration_ms,
    )

    # --- ③ ASR（依赖注入：默认 mock 成功；测试 override 成 unavailable → 503）---
    try:
        asr_text = asr(audio_bytes)
    except ASRUnavailable:
        logger.warning("utterance: asr unavailable → 503 degrade (trace_id=%s)", trace_id)
        raise HTTPException(
            status_code=503,
            detail=DEGRADE_TEMPLATES["asr_unavailable"],
        ) from None

    # --- ④ Day 3 写死回复话术（大纲课时 3.4 明确）---
    reply_text = f"您说的是『{asr_text}』，我记下了。"

    # --- ⑤ TTS（依赖注入：默认 mock 成功；override unavailable → audio_url=null + 话术前缀）---
    try:
        audio_url = tts(reply_text)
    except TTSUnavailable:
        logger.warning("utterance: tts unavailable → audio_url=null (trace_id=%s)", trace_id)
        audio_url = None
        reply_text = DEGRADE_TEMPLATES["tts_unavailable"] + reply_text

    # --- ⑥ 组装响应（TypedDict 直接返回，FastAPI 会自动序列化）---
    return UtteranceResponse(
        text=asr_text,
        reply=UtteranceReply(text=reply_text, audio_url=audio_url),
    )
