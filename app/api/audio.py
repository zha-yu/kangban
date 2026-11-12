"""按住说话降级链路 REST 路由 —— `POST /api/audio/utterance`（契约 C3，Day 3 A5 冻结）。

分层硬约束（AGENTS §2）：
- 本模块**禁止 import ORM / services**，只做三件事：参数校验 → 调 ASR/TTS 适配器 → 序列化。
- `elder_id` 只从设备 JWT 绑定关系取（禁区 9），客户端传参一律忽略。
- 错误统一走 `ErrorResponse`（`{detail, code, trace_id}`），由 app/main.py 全局 handler 兜底。

ASR/TTS 适配器统一走 A1 的 `build_asr()` / `build_tts()` 工厂，**不由本模块自造 Mock**。
配置 `ASR_PROVIDER` / `TTS_PROVIDER` 为空或未知时，A1 工厂自动降级为 Mock 实现
（断网可跑，符合 PRD Mock 边界与 AGENTS §1 "真实 key 不进仓库"的要求）。
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
import uuid
import wave
from collections.abc import AsyncIterator
from typing import Annotated, TypedDict

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.api.schemas.auth import ErrorResponse
from app.audio.asr import (
    FRAME_BYTES,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    ASRError,
    ASREventKind,
    ASRProvider,
    build_asr,
)
from app.audio.tts import TTSProvider, TTSUnavailable, build_tts
from app.core.auth import current_device
from app.core.config import get_settings
from app.core.context import get_trace_id
from app.core.runtime_context import Identity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audio", tags=["audio"])


# ---------------------------------------------------------------------------
# C3 · 降级 REST 契约（冻结人：A5，消费方：A4 前端降级模式）
# ---------------------------------------------------------------------------


class UtteranceReply(TypedDict):
    """端点成功响应里的 reply 对象。"""

    text: str  # 回复话术（Day 9 F15：与 WS FINAL 同一条编排入口产出，不再写死模板）
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
    "llm_timeout": ("抱歉，我正在思考，您可以稍后再试或联系工作人员。"),
    "tool_breaker": ("抱歉，当前请求处理超时，我会尽快通知工作人员。"),
    # Day 9 F15：编排入口未装配（裸 app / 装配异常）时诚实降级，不再写死假回复
    "orchestrator_unavailable": ("抱歉，我现在没法回应，请稍后再试或联系工作人员。"),
}

# PRD line 247（FR-UI-01）：降级模式单次录音最长 120 秒。Day 3 曾写 30s，Day 9 F15
# 依 PRD 数值修正（历史注释引用非基线文档为依据，违反 AGENTS 前置铁律，已删）。
MAX_AUDIO_SECONDS = 120


# ---------------------------------------------------------------------------
# FastAPI 依赖函数 —— 默认走 A1 build_asr/build_tts，可被 dependency_overrides 替换
# ---------------------------------------------------------------------------


async def _get_default_asr() -> ASRProvider:
    """默认 ASR 依赖：A1 build_asr()。配置 ASR_PROVIDER 为空时自动走 MockASRProvider。"""
    return build_asr()


async def _get_default_tts() -> TTSProvider:
    """默认 TTS 依赖：A1 build_tts()。配置 TTS_PROVIDER 为空时自动走 MockTTSProvider。"""
    return build_tts()


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _wav_duration_ms(audio: bytes) -> int | None:
    """从 WAV 头计算时长（ms）；头部解析失败返回 None（调用方拒收 422，a0-F-02）。

    此前失败静默归 0：垃圾字节通过 ≤30s 校验漏到 ASR 层炸成 503，客户端
    拿不到「格式不对」的明确信号。现在格式校验前移到入口，非 WAV 一律 422。
    """
    try:
        with wave.open(io.BytesIO(audio), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            if rate <= 0:
                return None
            return int(frames * 1000 / rate)
    except wave.Error:
        logger.warning("wav_duration: invalid wav header (len=%d)", len(audio))
        return None


async def _read_upload_file(file: UploadFile) -> bytes:
    """读 multipart UploadFile 到 bytes（用 await，符合 FastAPI 异步规范）。"""
    return await file.read()


async def _frame_iter(wav_bytes: bytes) -> AsyncIterator[bytes]:
    """WAV 解码 → 校验 16kHz/单声道/16bit → 切 60ms/1920B 帧 → 逐帧 yield。

    A1 的 ASRProvider.stream() 契约（C1 冻结）要求入参是 AsyncIterator[bytes]
    （PCM 帧流），本函数就是把客户端上传的 WAV 字节适配成那个形状。
    非标准 WAV 头会抛 ASRError，由上层 utterance() 捕获走 503 降级。
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            rate = w.getframerate()
            channels = w.getnchannels()
            sampwidth = w.getsampwidth()

            if rate != PCM_SAMPLE_RATE or channels != PCM_CHANNELS or sampwidth != PCM_SAMPLE_WIDTH:
                raise ASRError(
                    f"wav format mismatch: got {rate}Hz/{channels}ch/{sampwidth * 8}bit, "
                    f"need {PCM_SAMPLE_RATE}Hz/{PCM_CHANNELS}ch/{PCM_SAMPLE_WIDTH * 8}bit"
                )

            pcm = w.readframes(w.getnframes())
    except wave.Error as exc:
        raise ASRError(f"invalid wav header: {exc}") from exc

    for i in range(0, len(pcm), FRAME_BYTES):
        chunk = pcm[i : i + FRAME_BYTES]
        # 尾帧不足 FRAME_BYTES 补零到完整帧——A1 MockASRProvider 的 drain 会吞掉所有帧，
        # FunASR 连接也能正确收尾；不补零会让尾帧被丢弃导致识别截断。
        if len(chunk) < FRAME_BYTES:
            chunk = chunk + b"\x00" * (FRAME_BYTES - len(chunk))
        yield chunk


async def _asr_recognize(asr: ASRProvider, audio_bytes: bytes) -> str:
    """统一 ASR 调用：遍历 stream 直到 FINAL，遇到 ERROR 抛 ASRError。

    MockASRProvider 和 FunASRWSProvider 都走同一 ASRProvider.stream() 协议。
    """
    final_text: str | None = None
    try:
        events = await asr.stream(_frame_iter(audio_bytes))
        async for event in events:
            if event.kind is ASREventKind.FINAL and event.result is not None:
                final_text = event.result.text
            elif event.kind is ASREventKind.ERROR and event.error is not None:
                raise event.error
    except ASRError:
        raise  # 让上层 utterance() 捕获并映射到 503
    except Exception as exc:
        # A1 P1-1 修补：兜底——任何非 ASRError 的异常都包装成 ASRError 再抛
        # （避免 ast.parse SyntaxError 等裸异常冒泡成裸 500）
        logger.exception("utterance: unexpected error in asr.stream()")
        raise ASRError(f"asr internal error: {exc}") from exc

    if final_text is None:
        # MockASRProvider 默认脚本一定吐 FINAL；真 FunASR 理论上也有。
        # 没有 FINAL 文本就当作"ASR 不可用"处理。
        raise ASRError("asr returned no final result")
    return final_text


async def _tts_synthesize(tts: TTSProvider, text: str) -> str | None:
    """统一 TTS 调用：synthesize() → PCM16 bytes → 包装 WAV → 存 static → 返回 URL。

    A1 MockTTSProvider / EdgeTTSProvider / CloudTTSProvider 都返回 PCM16 原始字节。
    """
    pcm_bytes = await tts.synthesize(text)

    # 包装成 WAV 容器（16k / mono / s16，和 PCM 口径一致）
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(16_000)
        w.writeframes(pcm_bytes)

    # 存到 static/mock/ 目录（相对项目根）——to_thread 包住同步文件 I/O，别堵事件循环
    static_dir = os.path.join(os.getcwd(), "static", "mock")
    await asyncio.to_thread(os.makedirs, static_dir, exist_ok=True)
    filename = f"tts_{int(time.time() * 1000)}_{os.getpid()}.wav"
    filepath = os.path.join(static_dir, filename)
    wav_bytes = wav_buf.getvalue()

    def _write_file() -> None:
        with open(filepath, "wb") as f:
            f.write(wav_bytes)

    await asyncio.to_thread(_write_file)

    # 返回可访问 URL（假设 nginx 把 static/ 挂到 /static/）
    return f"/static/mock/{filename}"


# ---------------------------------------------------------------------------
# POST /api/audio/utterance
# ---------------------------------------------------------------------------


@router.post(
    "/utterance",
    response_model=UtteranceResponse,
    summary="按住说话降级链路：整段上传 WAV → ASR + 编排回复（与 WS FINAL 同路）",
    responses={
        401: {"model": ErrorResponse, "description": "device token 失效/伪造/过期"},
        413: {"model": ErrorResponse, "description": "音频超 120s 拒收（PRD:247）"},
        422: {"model": ErrorResponse, "description": "表单字段缺失/类型错误"},
        503: {"model": ErrorResponse, "description": "ASR/编排不可用（降级话术）"},
    },
)
async def utterance(
    request: Request,
    identity: Annotated[Identity, Depends(current_device)],
    asr: Annotated[ASRProvider, Depends(_get_default_asr)],
    tts: Annotated[TTSProvider, Depends(_get_default_tts)],
    audio: Annotated[UploadFile, File(...)],
    duration_ms: int | None = Form(default=None),
) -> UtteranceResponse:
    """按住说话降级端点 —— **WebRTC 主链路不可用时的唯一回退**。

    流程（Day 9 F15 收口，依据 PRD line 247 FR-UI-01）：
        ① 鉴权 → Depends(current_device) 已完成；elder_id 从库绑定取（禁区 9）
        ② 读 WAV 文件头，校验 ≤ 120s（PRD line 247 单次最长 120 秒）
        ③ A1 ASR（build_asr）→ 识别文本；不可用时走降级话术（503）
        ④ 编排：调 `app.state.dialogue_orchestrator.handle_asr_final`——与 WS FINAL
          （main.py `_on_dialogue_final` → main.py:592）**同一条编排入口**（任务书
          F15「不得另造第二条编排入口」）；安全一票否决（AC-04）由编排入口内的
          safety 层天然生效，本层不写第二套判定。audio_sink=None：REST 降级不经
          WS 分段下发，回复文本随响应体返回。
        ⑤ A1 TTS（build_tts）→ 合成 PCM → 存 static → audio_url；
           不可用时 audio_url=null + 降级话术（200 不白屏）
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
    if real_duration_ms is None:
        # a0-F-02：非法 WAV 头（垃圾字节）在入口拒收 422，不再漏到 ASR 层炸 503
        logger.warning(
            "utterance rejected: not a valid wav file (len=%d, trace_id=%s)",
            len(audio_bytes),
            trace_id,
        )
        raise HTTPException(status_code=422, detail="audio is not a valid wav file")

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

    # --- ③ ASR（A1 build_asr → stream FINAL；ERROR → 503 降级）---
    try:
        asr_text = await _asr_recognize(asr, audio_bytes)
    except ASRError as exc:
        logger.warning(
            "utterance: asr unavailable → 503 degrade (trace_id=%s, err=%s)",
            trace_id,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail=DEGRADE_TEMPLATES["asr_unavailable"],
        ) from None

    # --- ④ 编排（Day 9 F15：与 WS FINAL 同一条编排入口；无写死回复）---
    orchestrator = getattr(request.app.state, "dialogue_orchestrator", None)
    if orchestrator is None:
        # 裸 app（单测）或装配异常：诚实降级，不伪造回复
        logger.error(
            "utterance: dialogue orchestrator unavailable → 503 degrade (trace_id=%s)",
            trace_id,
        )
        raise HTTPException(
            status_code=503,
            detail=DEGRADE_TEMPLATES["orchestrator_unavailable"],
        ) from None

    try:
        reply = await orchestrator.handle_asr_final(
            device_id=device_id,
            trace_id=trace_id,
            # 降级路径每次上传独立 turn（编排器按 (device_id, turn_id) 去重）；
            # 服务端生成，客户端无从传入（身份铁律）
            turn_id=uuid.uuid4().hex,
            text=asr_text,
            audio_bytes=audio_bytes,
            audio_sink=None,
        )
    except Exception:
        # 编排异常不白屏：诚实降级话术 + 200（与 ASR/TTS 降级同策略）
        logger.exception("utterance: orchestration failed → degrade (trace_id=%s)", trace_id)
        reply = None

    if reply is None or not reply.text:
        # 空文本 / 重复 turn / 编排异常 → 降级话术（不编造回复，FR-UI-05 口径）
        reply_text = DEGRADE_TEMPLATES["llm_timeout"]
        logger.warning(
            "utterance: orchestration returned empty → degrade template (trace_id=%s)",
            trace_id,
        )
    else:
        reply_text = reply.text

    # --- ⑤ TTS（A1 build_tts → synthesize PCM → 存 static；不可用时 audio_url=null + 话术前缀）---
    try:
        audio_url = await _tts_synthesize(tts, reply_text)
    except TTSUnavailable:
        logger.warning("utterance: tts unavailable → audio_url=null (trace_id=%s)", trace_id)
        audio_url = None
        reply_text = DEGRADE_TEMPLATES["tts_unavailable"] + reply_text

    # --- ⑥ 组装响应（TypedDict 直接返回，FastAPI 会自动序列化）---
    return UtteranceResponse(
        text=asr_text,
        reply=UtteranceReply(text=reply_text, audio_url=audio_url),
    )


# ---------------------------------------------------------------------------
# GET /api/audio/ice-config（Day 9 F29）
# ---------------------------------------------------------------------------


class IceConfigResponse(TypedDict):
    """`GET /api/audio/ice-config` 的 200 响应体。

    值全部来自 config（环境注入）；**响应体含 TURN 凭据但不落日志**（AGENTS §4.2：
    凭据不出现在前端源码与日志——前端经本端点动态获取，不进构建产物）。coturn
    服务本体归 Day 10 compose，本端点只做配置下发。
    """

    stun_url: str
    turn_url: str
    turn_username: str
    turn_credential: str
    ttl_seconds: int


@router.get(
    "/ice-config",
    response_model=IceConfigResponse,
    summary="WebRTC ICE 配置下发（设备鉴权；TURN 凭据环境注入，不进前端源码与日志）",
    responses={
        401: {"model": ErrorResponse, "description": "device token 失效/伪造/过期"},
    },
)
async def ice_config(
    identity: Annotated[Identity, Depends(current_device)],
) -> IceConfigResponse:
    """F29：pad 建连前从本端点取 iceServers（含 TURN）。

    `RTCPeerConnection` 不再无 iceServers 裸连；获取失败由前端回落空配置
    （本地 host candidate 仍可用，不阻塞主链路）。elder_id/device_id 服务端
    注入（禁区 9），本端点不收任何身份入参。
    """
    settings = get_settings()
    logger.info(
        "ice_config issued (device=%s, turn_url_set=%s)",
        identity.device_id or "-",
        bool(settings.turn_url),
    )
    return IceConfigResponse(
        stun_url=settings.stun_url,
        turn_url=settings.turn_url,
        turn_username=settings.turn_username,
        turn_credential=settings.turn_credential,
        ttl_seconds=settings.turn_credential_ttl_seconds,
    )
