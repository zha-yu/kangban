"""WS /ws/signal —— WebRTC 信令通道：首帧鉴权、心跳保活、信令转发与断线清理。

A2 单元（docs/plans/task-a2-信令WS与鉴权.md）的四件事：**连接、鉴权、转发、清理**。

边界与口径：

- 传输：文本帧 JSON，每消息一个对象；消息类型以本文件顶部的 C2 契约块为准
  （从 docs/team_tasks_day03/contracts.md C2 **原样拷贝**，改动必须走契约变更流程）。
- 鉴权：首帧必须 `auth{token}`（300ms 内）；复用 `app/core/auth.py` 的
  `resolve_principal`（组装 Bearer 头），不新造第二条鉴权入口；任务卡写的
  `verify_device_token` 是口径简称，实际函数名以代码为准。**不改 core/auth* 一行**。
- 身份：`elder_id` 只从服务端设备绑定（Identity）读，客户端传的一律丢弃（AGENTS 禁区 9）。
- 分层：`app/audio` 不写库（AGENTS §2）；room_id 经 `device_service.list_elder_candidates`
  只读复用（audio → services 单向依赖），不 import ORM。
- trace：TraceIdMiddleware 对 websocket scope 不注入（`app/core/trace.py` 的
  `scope["type"] != "http"` 直通分支），本层 accept 时自补 new_trace_id，连接 finally 里 reset。
- 审计：按裁决 A-02/S-08 只落结构化日志（trace_id / audit_action / actor / target / result），
  `audit_logs` 表 Day 9 建后回填；日志**不回显 token**（S-23 ④）。
- 心跳：单向 C→S ping / S→C pong；45s 无任何帧判死（契约会裁决①）。
- 对 A3/A1 只暴露两个公开面：**C2 消息类型（TypedDict）+ 投递接口**
  （`deliver` / `deliver_asr_event` / `set_media_handler`）；A3 不得触碰
  WebSocket 实例、注册表与连接内部状态。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal, NotRequired, TypedDict
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio.asr import ASREvent, ASREventKind
from app.core.auth import (
    KIND_DEVICE,
    AuthError,
    CredentialExpiredError,
    resolve_principal,
)
from app.core.context import get_trace_id, new_trace_id, reset_trace_id, set_trace_id
from app.core.metrics import (
    record_identity_ask_sent,
    record_identity_confirm_received,
    record_identity_result_sent,
    record_stage,
    record_wakeup,
)
from app.core.runtime_context import Identity, reset_identity, set_identity
from app.db import get_session
from app.services.device_service import list_elder_candidates

logger = logging.getLogger("app.audio.signaling")

# ------------------------------------------------------------------ 模块常量
# Day 3 config.py 只读（A1 域），心跳/超时参数先落这里为模块常量；
# 初值出处 adhoc-20261105 §六，实测后回填并同步 A5（README 端点表）。

# auth 首帧预算（毫秒）：accept 后 300ms 内必须收到 auth{token}
WS_AUTH_TIMEOUT_MS = 300
# 客户端 ping 周期（契约裁决①：单向心跳，C→S）；服务端只回 pong，不做主动探测
WS_PING_INTERVAL_S = 20
# 45s 无任何帧（含业务帧）判死清理
WS_DEAD_AFTER_S = 45
# 判死检查轮询间隔（实现细节，测试 monkeypatch 用）
_DEAD_CHECK_INTERVAL_S = 1.0

# C5 身份确认：契约 timeout_s 恒 30；内部计时器单独命名，测试用受控时钟 monkeypatch
IDENTITY_ASK_TIMEOUT_S = 30
_IDENTITY_TIMEOUT_TEST_S = float(IDENTITY_ASK_TIMEOUT_S)
# C5 client_diag 允许的阶段（day03/contracts.md v1.0 四阶段 + v1.5 追加 playback，
# 主理人裁决 R3：playback 由端侧经 diag 上报、服务端代打 metrics）
_CLIENT_DIAG_STAGES = frozenset({"vrm_load", "webrtc", "vad", "asr", "playback"})

# 自定义关闭码（4000–4999 为应用私有区间）
WS_CLOSE_AUTH_FAILED = 4401  # 鉴权失败（契约定值）
WS_CLOSE_SUPERSEDED = 4002  # 同设备踢旧留新（契约会裁决③）

router = APIRouter()

# ------------------------------------------------------------------ C2 契约（contracts.md 原样拷贝，冻结 v1.0）
# 方向标记：C→S 客户端发，S→C 服务端发。


class AuthMsg(TypedDict):  # C→S，连接后 300ms 内必须首发
    type: Literal["auth"]
    token: str  # 设备会话 access_token（E4 签发）；token 不进 URL
    wake_ts: NotRequired[float]  # v1.5 可选：端侧 KWS 命中时刻（epoch 秒）；未命中不填走兜底


class AuthOkMsg(TypedDict):  # S→C
    type: Literal["auth_ok"]
    elder_id: str | None  # 展示用；业务归属一律服务端裁决
    room_id: str


class AuthFailMsg(TypedDict):
    """S→C，随后以 4401 关闭。"""

    type: Literal["auth_fail"]
    code: Literal["timeout", "invalid", "expired"]


__all__ = [
    "AnswerMsg",
    "AsrEndMsg",
    "AsrFinalMsg",
    "AsrPartialMsg",
    "AsrStartMsg",
    "AuthFailMsg",
    "AuthMsg",
    "AuthOkMsg",
    "ClientDiagMsg",
    "DialogueFinalHandler",
    "ErrorMsg",
    "IceMsg",
    "IdentityAskMsg",
    "IdentityConfirmHandler",
    "IdentityConfirmMsg",
    "IdentityResultMsg",
    "IdentityState",
    "IdentityTimeoutHandler",
    "OfferMsg",
    "PingMsg",
    "PongMsg",
    "TtsTextMsg",
    "apply_generic_identity",
    "apply_voice_identity",
    "begin_identity_recheck",
    "dispatch_asr_event",
    "request_identity_confirmation",
    "set_dialogue_final_handler",
    "set_identity_confirm_handler",
    "set_identity_timeout_handler",
]


class OfferMsg(TypedDict):
    """C→S，WebRTC 客户端发起。"""

    type: Literal["offer"]
    sdp: str


class AnswerMsg(TypedDict):
    """S→C。"""

    type: Literal["answer"]
    sdp: str


class IceMsg(TypedDict):
    """双向；candidate 为 ICE candidate JSON 字符串。"""

    type: Literal["ice"]
    candidate: str


class PingMsg(TypedDict):
    """C→S，20s 周期。"""

    type: Literal["ping"]


class PongMsg(TypedDict):
    """S→C；45s 无任何帧 → 服务端判死清理。"""

    type: Literal["pong"]


class AsrStartMsg(TypedDict):
    """C→S，TEN-VAD speechstart（preRoll 帧随其后补发）。"""

    type: Literal["asr_start"]


class AsrEndMsg(TypedDict):
    """C→S，speechend（0.8s 静音）。"""

    type: Literal["asr_end"]


class AsrPartialMsg(TypedDict):
    """S→C，字幕预显。"""

    type: Literal["asr_partial"]
    text: str


class AsrFinalMsg(TypedDict):
    """S→C，终稿字幕。"""

    type: Literal["asr_final"]
    text: str


class TtsTextMsg(TypedDict):
    """S→C，大字幕渲染数字人回复（打断时前端立即停播）。

    `degraded` 为 C6 的向后兼容扩展字段：True = LLM 降级模板话术，缺省 False。
    """

    type: Literal["tts_text"]
    text: str
    degraded: NotRequired[bool]


class ErrorMsg(TypedDict):  # S→C，服务端降级/异常通知
    type: Literal["error"]
    code: Literal[
        "asr_unavailable",
        "llm_timeout",
        "tool_breaker",
        "internal",
        "asr_slow",  # Day 5 B-2：ASR 有结果但太慢（>2s）
        "llm_slow",  # Day 5 B-2：LLM 有结果但太慢（>3s）
        "tts_slow",  # Day 5 B-2：TTS 有结果但太慢（>1s）
    ]
    message: str  # 已脱敏的长者可读话术


# ------------------------------------------------------------------ C5 契约追加（contracts.md v1.0 原样拷贝，属 C2 消息表增量）
# 方向标记：C→S 客户端发，S→C 服务端发。


class IdentityAskMsg(TypedDict):  # S→C，低置信/服务不可用时主动确认
    type: Literal["identity_ask"]
    ask_id: str  # 本次确认的唯一 id；confirm 必须回带
    reason: Literal["low_confidence", "vp_unavailable"]
    candidates: list[dict[str, str]]  # 契约 list[dict]；恒 1 位（设备绑定长者 1v1）
    timeout_s: int  # 恒 30


class IdentityConfirmMsg(TypedDict):  # C→S，pad 大按钮点选
    type: Literal["identity_confirm"]
    ask_id: str
    elder_id: str  # 必须等于 candidates[0].elder_id；否则按未确认处理


class IdentityResultMsg(TypedDict):  # S→C，告知最终身份模式
    type: Literal["identity_result"]
    source: Literal["voice", "confirm", "generic"]
    elder_id: str | None  # generic 时为 None


class ClientDiagMsg(TypedDict):  # C→S，仅日志（3D/WebRTC/链路诊断），服务端不回执
    type: Literal["client_diag"]
    stage: Literal["vrm_load", "webrtc", "vad", "asr"]
    ok: bool
    error: str  # 已脱敏


class IdentityState(StrEnum):
    """连接级身份状态（C5 状态机；每轮判定只允许一个最终结果）。"""

    UNKNOWN = "unknown"  # 已鉴权、身份未定
    ASKING = "asking"  # 已发 identity_ask，等待 confirm 或 30s 超时
    IDENTIFIED = "identified"  # voice/confirm 已确定
    GENERIC = "generic"  # 超时或无法确认，active_elder_id 恒 None
    CLOSED = "closed"  # 断线/被新连接替代，计时已取消


# ------------------------------------------------------------------ 连接上下文与注册表


@dataclass(slots=True)
class _TrackedFrame:
    """下行帧 + 实际发送完成事件（C5 的 30s 计时必须等真实 send_text 成功）。

    只有 identity_ask 需要回执；C2 普通下行仍是裸 dict，`deliver()` 的对外语义不变。
    """

    payload: dict[str, Any]
    sent: asyncio.Event


@dataclass
class ConnectionContext:
    """一条 WS 连接的全部服务端状态。

    `websocket` 是执行计划 §3 W1 字段清单之外的实现必需项：踢旧留新（裁决③）要拿到
    旧连接的 WebSocket 才能发 close 帧。对 A3 仍不暴露（只读上层字段即可）。

    身份字段拆成两个：`bound_elder_id` 是设备绑定（来自鉴权），`active_elder_id`
    是本轮确认的说话人（voice/confirm 才有值）。**generic 不得清空绑定关系**。
    """

    trace_id: str
    websocket: WebSocket
    device_id: str | None = None
    bound_elder_id: str | None = None
    active_elder_id: str | None = None
    elder_display_name: str | None = None
    room_id: str | None = None
    authed: bool = False
    last_frame_at: float = field(default_factory=time.monotonic)
    downstream: asyncio.Queue[dict[str, Any] | _TrackedFrame] = field(default_factory=asyncio.Queue)
    disconnect_callbacks: list[Callable[[], object]] = field(default_factory=list)
    # C5 状态机
    identity_state: IdentityState = IdentityState.UNKNOWN
    pending_ask_id: str | None = None
    pending_ask_sent: asyncio.Event | None = None
    identity_task: asyncio.Task[None] | None = None
    identity_result_sent: bool = False

    @property
    def elder_id(self) -> str | None:
        """C2 兼容读法：`auth_ok.elder_id` 展示的是**设备绑定长者**，不是本轮说话人。"""
        return self.bound_elder_id


# 进程内连接注册表：device_id → 连接上下文（契约会裁决③：同设备踢旧留新，操作加锁）
_registry: dict[str, ConnectionContext] = {}
_registry_lock = asyncio.Lock()

# A3 注册的上行处理回调（offer / ice / asr_start / asr_end 的消费者）
UplinkHandler = Callable[[ConnectionContext, dict[str, Any]], Awaitable[None]]
_media_handler: UplinkHandler | None = None


def set_media_handler(handler: UplinkHandler | None) -> None:
    """A3 注册上行回调；传 None 注销。联调前为 None，此时上行信令回 error internal。"""
    global _media_handler
    _media_handler = handler


def _log_extra(conn: ConnectionContext, **fields: object) -> dict[str, object]:
    """结构化日志公共字段：每一行都带 trace_id（AGENTS §8）。"""
    extra: dict[str, object] = {
        "trace_id": conn.trace_id,
        "device_id": conn.device_id or "-",
    }
    extra.update(fields)
    return extra


def _audit_extra(conn: ConnectionContext, *, result: str, actor: str) -> dict[str, object]:
    """审计日志五字段（Day 2 服务层审计模式，A-02/S-08：只落日志不写库）。"""
    return {
        "trace_id": conn.trace_id,
        "audit_action": "ws_auth_failed",
        "actor": actor,
        "target": "/ws/signal",
        "result": result,
    }


# ------------------------------------------------------------------ 下行投递接口（A3/A1 唯一公开面）

# error.code → 已脱敏的长者可读话术（C2 ErrorMsg.message 口径：不透出内部细节）
_ERROR_TALKBACK: dict[str, str] = {
    "asr_unavailable": "我这会儿听不太清，请稍后再说一遍。",
    "llm_timeout": "我稍微想一想，请等我一下下。",
    "tool_breaker": "这个功能暂时不可用，稍后再试试。",
    "internal": "出了点小状况，请再说一遍。",
    # Day 5 B-2 新增：并行 watchdog 慢响应话术（provider 还在跑但超过阈值就先发）
    "asr_slow": "我正在听您说，请稍等一下下。",
    "llm_slow": "我正在想，请稍等一下下。",
    "tts_slow": "我马上说，请稍等一下下。",
}


def deliver(device_id: str, message: Mapping[str, Any]) -> bool:
    """下行投递接口：把一条 C2 消息放进目标连接的发送队列。

    放不进（设备不在线 / 未鉴权）返回 False，调用方自行决定降级；本接口不抛异常。
    线程安全口径：仅允许在事件循环内调用；队列无界，用 put_nowait（同步上下文
    可直接调用，无需 await）。
    """
    conn = _registry.get(device_id)
    if conn is None or not conn.authed:
        return False
    conn.downstream.put_nowait(dict(message))
    return True


def deliver_asr_event(device_id: str, event: ASREvent) -> bool:
    """C1 `ASREvent` → C2 下行消息映射（A1/A3 消费）。

    C1 已冻结落地（`app/audio/asr.py`），正式 import 消费。
    `ERROR` 只映射固定脱敏话术，不透出 `ASRError.message` 内部细节。
    """
    if event.kind is ASREventKind.PARTIAL:
        return deliver(
            device_id, {"type": "asr_partial", "text": event.result.text if event.result else ""}
        )
    if event.kind is ASREventKind.FINAL:
        return deliver(
            device_id, {"type": "asr_final", "text": event.result.text if event.result else ""}
        )
    if event.kind is ASREventKind.ERROR:
        return deliver(
            device_id,
            {
                "type": "error",
                "code": "asr_unavailable",
                "message": _ERROR_TALKBACK["asr_unavailable"],
            },
        )
    return False


# A3 媒体层在 FINAL 时 await 的 A2 编排入口；由 app 装配层注册，未注册时行为不变。
DialogueFinalHandler = Callable[[str, str, str, str, bytes | None], Awaitable[None]]
_dialogue_final_handler: DialogueFinalHandler | None = None


def set_dialogue_final_handler(handler: DialogueFinalHandler | None) -> None:
    """注册 FINAL → A2 对话编排的唯一 awaitable 回调；传 None 注销（测试/关闭用）。"""
    global _dialogue_final_handler
    _dialogue_final_handler = handler


async def dispatch_asr_event(
    device_id: str,
    turn_id: str,
    trace_id: str,
    event: ASREvent,
    audio_bytes: bytes | None = None,
) -> bool:
    """A3 ASR 事件统一出口：字幕先投递一次，FINAL 再 await A2 编排。

    - PARTIAL/ERROR 只走字幕/错误映射，不触发 LLM；
    - FINAL 字幕投递成功后调用注册的 `DialogueFinalHandler`，异常只记录不反抛，
      避免破坏 C2 媒体回调；同一 FINAL 由 A2 内部按 (device_id, turn_id) 去重；
    - `audio_bytes` 是 A3 VAD 切出的本 turn 语音段（WAV），仅供服务端声纹使用，
      不落盘、不下发。
    """
    delivered = deliver_asr_event(device_id, event)
    if event.kind is not ASREventKind.FINAL or not delivered:
        return delivered

    handler = _dialogue_final_handler
    if handler is None:
        return delivered

    text = event.result.text if event.result else ""
    try:
        await handler(device_id, turn_id, trace_id, text, audio_bytes)
    except Exception as exc:
        # 只记录异常类型，不把 handler 内部消息/堆栈写进日志，避免敏感内容外泄
        logger.error(
            "dialogue_final_handler_failed",
            extra={
                "trace_id": trace_id,
                "device_id": device_id,
                "turn_id": turn_id,
                "error_type": type(exc).__name__,
            },
        )
    return delivered


# ------------------------------------------------------------------ 内部实现


def _parse_message(raw: str) -> dict[str, Any] | None:
    """文本帧 → 消息对象；非 JSON / 非对象 / 缺 type → None（按非法消息处理，裁决④）。"""
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str):
        return None
    return parsed


async def _direct_send(websocket: WebSocket, message: Mapping[str, Any]) -> None:
    """绕过下行队列的直发（仅鉴权阶段 / 关闭前使用；连接级消息必须先于 close 落地）。"""
    await websocket.send_text(json.dumps(dict(message), ensure_ascii=False))


async def _reject(conn: ConnectionContext, *, result: str) -> None:
    """鉴权失败统一出口：审计日志 → auth_fail → 4401 关闭。"""
    logger.warning("audit", extra=_audit_extra(conn, result=result, actor="unknown"))
    with contextlib.suppress(Exception):
        await _direct_send(conn.websocket, {"type": "auth_fail", "code": result})
    with contextlib.suppress(Exception):
        await conn.websocket.close(code=WS_CLOSE_AUTH_FAILED)


async def _authenticate(conn: ConnectionContext, session: AsyncSession) -> Identity | None:
    """首帧鉴权（W2）。返回 None 表示连接已被 auth_fail + 4401 关闭。

    失败 actor 一律记 `unknown`：失败路径无法可靠取得「已验证」的主体 ID，
    宁缺毋错（不回显 token 内容，S-23 ④）。
    """
    websocket = conn.websocket
    try:
        first_raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_MS / 1000
        )
    except TimeoutError:  # = asyncio.TimeoutError（3.11+ 别名），ruff UP041
        await _reject(conn, result="timeout")
        return None
    except WebSocketDisconnect:
        raise

    first = _parse_message(first_raw)
    if first is None or first.get("type") != "auth" or not isinstance(first.get("token"), str):
        # auth 前收到其他任何消息 / 非法帧 → invalid（C2 语义约定）
        await _reject(conn, result="invalid")
        return None

    try:
        identity = await resolve_principal(f"Bearer {first['token']}", session)
    except CredentialExpiredError:
        await _reject(conn, result="expired")
        return None
    except AuthError:
        await _reject(conn, result="invalid")
        return None

    # 本端点只收设备会话（C2 AuthMsg.token 口径）：其余三类主体按 invalid 拒
    if identity.kind != KIND_DEVICE or identity.device_id is None:
        await _reject(conn, result="invalid")
        return None

    conn.device_id = identity.device_id
    conn.bound_elder_id = identity.elder_id
    conn.last_frame_at = time.monotonic()
    # Day 5 唤醒段打点（裁决 R2，day03/contracts.md v1.5）：auth 首帧带 wake_ts 用端侧
    # 口径（source=client，duration=accept−wake_ts，时钟偏移防护在 record_wakeup 内）；
    # 未带走 WS accept 兜底（source=backend，duration=0 占位）。elder_id 随行入表
    # （服务端注入口径，AGENTS 红线 9；asr/tts 行的 elder_id 由复算侧按 trace_id JOIN 本行补全）。
    wake_ts = first.get("wake_ts")
    elder_id = identity.elder_id
    if isinstance(wake_ts, (int, float)) and wake_ts > 0:
        record_wakeup(source="client", wake_ts=float(wake_ts), elder_id=elder_id)
    else:
        record_wakeup(source="backend", elder_id=elder_id)
    return identity


async def _register(conn: ConnectionContext) -> None:
    """接管注册表槽位；同设备旧连接踢下线（裁决③：踢旧留新，注册表操作加锁）。"""
    assert conn.device_id is not None  # _authenticate 通过后必非空
    async with _registry_lock:
        old = _registry.get(conn.device_id)
        _registry[conn.device_id] = conn
    if old is not None:
        _cancel_identity_timer(old)
        old.identity_state = IdentityState.CLOSED
        logger.warning(
            "ws_superseded",
            extra=_log_extra(old, reason="same_device_reconnect"),
        )
        with contextlib.suppress(Exception):
            await old.websocket.close(code=WS_CLOSE_SUPERSEDED)


async def _writer(conn: ConnectionContext) -> None:
    """单一 writer 协程：排空下行队列逐帧发送（绑定本连接所在 loop，P1 风险 A2 侧落点）。"""
    try:
        while True:
            item = await conn.downstream.get()
            if isinstance(item, _TrackedFrame):
                await conn.websocket.send_text(json.dumps(item.payload, ensure_ascii=False))
                # 只有真实 send_text 成功后才置位：C5 的 30s 计时从这里起算
                item.sent.set()
            else:
                await conn.websocket.send_text(json.dumps(item, ensure_ascii=False))
    except asyncio.CancelledError:
        raise
    except Exception:
        # 发送失败 = 连接已死：关掉 WebSocket 让主循环尽快感知（断线三来源之一）
        logger.info("ws_writer_failed", extra=_log_extra(conn))
        with contextlib.suppress(Exception):
            await conn.websocket.close()


async def _dead_watcher(conn: ConnectionContext) -> None:
    """判死任务（W3）：周期检查 45s 无任何帧 → 正常 close 并走断线清理。"""
    try:
        while True:
            await asyncio.sleep(_DEAD_CHECK_INTERVAL_S)
            if time.monotonic() - conn.last_frame_at > WS_DEAD_AFTER_S:
                logger.info(
                    "ws_dead",
                    extra=_log_extra(
                        conn,
                        idle_seconds=round(time.monotonic() - conn.last_frame_at, 3),
                    ),
                )
                await conn.websocket.close()
                return
    except asyncio.CancelledError:
        raise


async def _send_error(conn: ConnectionContext, code: str) -> None:
    """error 下行（裁决④：脱敏长者可读话术，不关连接）。"""
    conn.downstream.put_nowait(
        {
            "type": "error",
            "code": code,
            "message": _ERROR_TALKBACK.get(code, _ERROR_TALKBACK["internal"]),
        }
    )


# ------------------------------------------------------------------ C5 身份状态机


IdentityAskReason = Literal["low_confidence", "vp_unavailable"]
IdentitySource = Literal["voice", "confirm", "generic"]
_IDENTITY_CANDIDATE_FALLBACK_NAME = "本人"

# 服务端 C5 → A1 identity_service 的接线回调；由 app 装配层注册。
IdentityConfirmHandler = Callable[[str, str, str], Awaitable[None]]  # device, trace, elder
IdentityTimeoutHandler = Callable[[str, str], Awaitable[None]]  # device, trace
_identity_confirm_handler: IdentityConfirmHandler | None = None
_identity_timeout_handler: IdentityTimeoutHandler | None = None


def set_identity_confirm_handler(handler: IdentityConfirmHandler | None) -> None:
    """注册合法 identity_confirm 的 A1 thread 创建回调；传 None 注销。"""
    global _identity_confirm_handler
    _identity_confirm_handler = handler


def set_identity_timeout_handler(handler: IdentityTimeoutHandler | None) -> None:
    """注册 30s 超时 generic 的 A1 thread 创建回调；传 None 注销。"""
    global _identity_timeout_handler
    _identity_timeout_handler = handler


def _identity_candidates(conn: ConnectionContext) -> list[dict[str, str]]:
    """C5 候选集：恒为设备绑定长者 1 位，客户端/模型输入不参与。"""
    if conn.bound_elder_id is None:
        return []
    return [
        {
            "elder_id": conn.bound_elder_id,
            "display_name": conn.elder_display_name or _IDENTITY_CANDIDATE_FALLBACK_NAME,
        }
    ]


def _cancel_identity_timer(conn: ConnectionContext) -> None:
    """取消 30s 计时；断线、被替代、合法 confirm/voice 都走这里。"""
    task = conn.identity_task
    conn.identity_task = None
    conn.pending_ask_sent = None
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()


def _send_identity_result(
    conn: ConnectionContext,
    *,
    source: IdentitySource,
    elder_id: str | None,
    ask_id: str | None = None,
) -> bool:
    """每会话每轮判定只发一次最终结果；generic 强制 elder_id=None。"""
    if conn.identity_result_sent:
        logger.warning(
            "identity_result_ignored",
            extra=_log_extra(conn, reason="duplicate_result", source=source),
        )
        return False

    conn.identity_result_sent = True
    if source == "generic":
        # FR-VPR-03：通用模式不伪造默认长者，也不清空设备绑定关系
        elder_id = None
        conn.active_elder_id = None
        conn.identity_state = IdentityState.GENERIC
    else:
        conn.active_elder_id = elder_id
        conn.identity_state = IdentityState.IDENTIFIED

    conn.downstream.put_nowait({"type": "identity_result", "source": source, "elder_id": elder_id})
    logger.info(
        "identity_result",
        extra=_log_extra(conn, source=source, elder_id=elder_id or "-"),
    )
    record_identity_result_sent(ask_id=ask_id or "")
    return True


async def _identity_ask_timeout(
    conn: ConnectionContext,
    ask_id: str,
    sent: asyncio.Event,
) -> None:
    """30s 从 ask **实际发送成功**起算；超时只发一次 generic。"""
    try:
        await sent.wait()
        logger.info("identity_ask_sent", extra=_log_extra(conn, ask_id=ask_id))
        await asyncio.sleep(_IDENTITY_TIMEOUT_TEST_S)
    except asyncio.CancelledError:
        raise

    if (
        conn.pending_ask_id != ask_id
        or conn.identity_state is not IdentityState.ASKING
        or conn.identity_result_sent
    ):
        return
    conn.pending_ask_id = None
    conn.pending_ask_sent = None
    conn.identity_task = None
    logger.info("identity_ask_timeout", extra=_log_extra(conn, ask_id=ask_id))

    handler = _identity_timeout_handler
    if handler is not None and conn.device_id is not None:
        try:
            await handler(conn.device_id, conn.trace_id)
        except Exception as exc:
            logger.error(
                "identity_timeout_handler_failed",
                extra=_log_extra(conn, error_type=type(exc).__name__),
            )
    _send_identity_result(conn, source="generic", elder_id=None, ask_id=ask_id)


async def request_identity_confirmation(
    device_id: str,
    *,
    reason: IdentityAskReason,
) -> bool:
    """A1 声纹级联的服务端入口：低置信/声纹不可用时下发 identity_ask。

    只接受服务端 device_id 与本轮判定结果；客户端与模型不能调用此入口。
    返回 True 表示 ask 已入队（30s 计时在真实发送成功后启动）。
    """
    conn = _registry.get(device_id)
    if conn is None or not conn.authed:
        return False
    if reason not in ("low_confidence", "vp_unavailable"):
        logger.warning("identity_ask_ignored", extra=_log_extra(conn, reason="invalid_reason"))
        return False
    if conn.identity_result_sent or conn.identity_state in (
        IdentityState.ASKING,
        IdentityState.IDENTIFIED,
        IdentityState.GENERIC,
    ):
        logger.warning(
            "identity_ask_ignored",
            extra=_log_extra(conn, reason="identity_already_determined"),
        )
        return False

    candidates = _identity_candidates(conn)
    if not candidates:
        # 无设备绑定长者时无法构造 1v1 候选集：直接通用模式，不伪造默认长者
        logger.warning("identity_ask_ignored", extra=_log_extra(conn, reason="no_bound_elder"))
        _send_identity_result(conn, source="generic", elder_id=None)
        return False

    ask_id = uuid4().hex
    sent = asyncio.Event()
    conn.pending_ask_id = ask_id
    conn.pending_ask_sent = sent
    conn.identity_state = IdentityState.ASKING
    conn.downstream.put_nowait(
        _TrackedFrame(
            payload={
                "type": "identity_ask",
                "ask_id": ask_id,
                "reason": reason,
                "candidates": candidates,
                "timeout_s": IDENTITY_ASK_TIMEOUT_S,
            },
            sent=sent,
        )
    )
    conn.identity_task = asyncio.create_task(_identity_ask_timeout(conn, ask_id, sent))
    logger.info(
        "identity_ask_enqueued",
        extra=_log_extra(conn, reason=reason, ask_id=ask_id),
    )
    record_identity_ask_sent(ask_id=ask_id, reason=reason)
    return True


async def apply_voice_identity(device_id: str, *, elder_id: str) -> bool:
    """A1 声纹命中的服务端入口：elder_id 必须等于设备绑定长者，否则忽略。

    声纹计算与 cosine 阈值属于 A1；本层只消费"已判定命中"这一事实，不接收相似度。
    """
    conn = _registry.get(device_id)
    if conn is None or not conn.authed:
        return False
    if conn.identity_result_sent or conn.identity_state in (
        IdentityState.IDENTIFIED,
        IdentityState.GENERIC,
    ):
        logger.warning(
            "identity_result_ignored",
            extra=_log_extra(conn, reason="duplicate_voice_result"),
        )
        return False
    if conn.bound_elder_id is None or elder_id != conn.bound_elder_id:
        logger.warning(
            "identity_result_ignored",
            extra=_log_extra(conn, reason="voice_candidate_mismatch"),
        )
        return False

    _cancel_identity_timer(conn)
    conn.pending_ask_id = None
    _send_identity_result(conn, source="voice", elder_id=conn.bound_elder_id)
    return True


async def apply_generic_identity(device_id: str) -> bool:
    """A1 返回 generic 终态时，向客户端下发一次 identity_result(generic, null)。"""
    conn = _registry.get(device_id)
    if conn is None or not conn.authed:
        return False
    if conn.identity_result_sent or conn.identity_state in (
        IdentityState.IDENTIFIED,
        IdentityState.GENERIC,
    ):
        logger.warning(
            "identity_result_ignored",
            extra=_log_extra(conn, reason="duplicate_generic_result"),
        )
        return False

    _cancel_identity_timer(conn)
    conn.pending_ask_id = None
    return _send_identity_result(conn, source="generic", elder_id=None)


async def begin_identity_recheck(device_id: str) -> bool:
    """换人/复检前复位 C5 轮次：保留设备绑定，清空本轮结果，允许再次下发。

    A1 `recheck_session_identity` 返回新决策后调用；A2 不自行创建 thread。
    """
    conn = _registry.get(device_id)
    if conn is None or not conn.authed:
        return False
    if conn.identity_state is IdentityState.CLOSED:
        return False

    _cancel_identity_timer(conn)
    conn.pending_ask_id = None
    conn.active_elder_id = None
    conn.identity_result_sent = False
    conn.identity_state = IdentityState.UNKNOWN
    logger.info("identity_recheck_reset", extra=_log_extra(conn))
    return True


async def _handle_identity_confirm(conn: ConnectionContext, message: Mapping[str, Any]) -> None:
    """C→S identity_confirm：错配/重复/候选外一律 warn + 忽略，不发 error、不关连接。"""
    ask_id = message.get("ask_id")
    elder_id = message.get("elder_id")
    if not isinstance(ask_id, str) or not isinstance(elder_id, str):
        logger.warning(
            "ws_bad_message",
            extra=_log_extra(conn, reason="identity_confirm_invalid"),
        )
        await _send_error(conn, "internal")
        return

    record_identity_confirm_received(ask_id=ask_id)

    if (
        conn.identity_state is not IdentityState.ASKING
        or conn.pending_ask_id is None
        or ask_id != conn.pending_ask_id
    ):
        logger.warning(
            "identity_confirm_ignored",
            extra=_log_extra(conn, reason="ask_mismatch_or_not_asking"),
        )
        return

    if conn.bound_elder_id is None or elder_id != conn.bound_elder_id:
        # 候选集外：保持 ASKING，继续等待合法确认或 30s 超时
        logger.warning(
            "identity_confirm_ignored",
            extra=_log_extra(conn, reason="candidate_out_of_set"),
        )
        return

    _cancel_identity_timer(conn)
    conn.pending_ask_id = None

    handler = _identity_confirm_handler
    if handler is not None and conn.device_id is not None:
        try:
            await handler(conn.device_id, conn.trace_id, conn.bound_elder_id)
        except Exception as exc:
            logger.error(
                "identity_confirm_handler_failed",
                extra=_log_extra(conn, error_type=type(exc).__name__),
            )
    _send_identity_result(conn, source="confirm", elder_id=conn.bound_elder_id, ask_id=ask_id)


async def _handle_client_diag(conn: ConnectionContext, message: Mapping[str, Any]) -> None:
    """C→S client_diag：只写结构化安全字段，不回执；仅 playback 经 metrics 代打入库。

    v1.5（裁决 R3）：stage=playback 由端侧上报端侧 TTS 播放耗时（duration_ms 必填），
    服务端代打 record_stage(stage="playback", source="client")；其余 stage 仅日志观测
    （vad 另有兜底激活语义）。wakeup 不走本路径（统一 C2 auth 首帧 wake_ts）。
    """
    stage = message.get("stage")
    ok = message.get("ok")
    error = message.get("error")
    if (
        not isinstance(stage, str)
        or stage not in _CLIENT_DIAG_STAGES
        or not isinstance(ok, bool)
        or not isinstance(error, str)
    ):
        logger.warning(
            "ws_bad_message",
            extra=_log_extra(conn, reason="client_diag_invalid"),
        )
        await _send_error(conn, "internal")
        return

    # 默认不记录客户端 error 原文（可能含敏感信息）；只记录长度与布尔状态
    logger.info(
        "client_diag",
        extra=_log_extra(conn, stage=stage, ok=ok, error_len=len(error)),
    )

    # v1.5（裁决 R3）：playback 段由服务端代打入 metrics（duration_ms 必填校验）
    if stage == "playback":
        duration_ms = message.get("duration_ms")
        if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
            record_stage(
                "playback",
                float(duration_ms),
                elder_id=conn.bound_elder_id,
                source="client",
            )
        else:
            logger.warning(
                "ws_bad_message",
                extra=_log_extra(conn, reason="client_diag_playback_duration_missing"),
            )
        return

    # W4：stage=vad 的端侧自检结果驱动服务端 VAD 兜底（ok=false → 激活）
    if stage == "vad" and conn.device_id is not None:
        from app.audio import webrtc

        try:
            applied = webrtc.set_vad_fallback_for_device(conn.device_id, not ok)
        except Exception as exc:
            logger.warning(
                "client_diag_vad_failed",
                extra=_log_extra(conn, error_type=type(exc).__name__),
            )
            applied = False
        logger.info(
            "client_diag_vad",
            extra=_log_extra(conn, ok=ok, active=not ok, applied=applied),
        )


async def _read_loop(conn: ConnectionContext) -> None:
    """auth 后的消息主循环（W4）：任何合法帧都刷新 last_frame_at（W3：业务帧同样续命）。"""
    websocket = conn.websocket
    while True:
        raw = await websocket.receive_text()
        conn.last_frame_at = time.monotonic()
        message = _parse_message(raw)
        if message is None:
            logger.warning(
                "ws_bad_message",
                extra=_log_extra(conn, reason="malformed_frame"),
            )
            await _send_error(conn, "internal")
            continue

        msg_type = message["type"]
        if msg_type == "ping":
            conn.downstream.put_nowait({"type": "pong"})
            continue
        if msg_type == "identity_confirm":
            await _handle_identity_confirm(conn, message)
            continue
        if msg_type == "client_diag":
            await _handle_client_diag(conn, message)
            continue
        if msg_type == "auth":
            # 裁决②：连接内重复 auth 按非法凭证处理（防连接内换 token 旁路）
            logger.warning(
                "audit",
                extra=_audit_extra(conn, result="invalid", actor=f"device:{conn.device_id}"),
            )
            await _direct_send(websocket, {"type": "auth_fail", "code": "invalid"})
            await websocket.close(code=WS_CLOSE_AUTH_FAILED)
            return
        if msg_type in ("offer", "ice", "asr_start", "asr_end"):
            if msg_type == "offer" and not isinstance(message.get("sdp"), str):
                logger.warning("ws_bad_message", extra=_log_extra(conn, reason="offer_sdp_not_str"))
                await _send_error(conn, "internal")
                continue
            handler = _media_handler
            if handler is None:
                logger.warning("ws_no_media_handler", extra=_log_extra(conn, msg_type=msg_type))
                await _send_error(conn, "internal")
                continue
            await handler(conn, message)
            continue

        # 未知消息类型（裁决④）：不关连接
        logger.warning("ws_bad_message", extra=_log_extra(conn, reason=f"unknown_type:{msg_type}"))
        await _send_error(conn, "internal")


async def _cleanup(conn: ConnectionContext) -> None:
    """断线清理统一收敛（W5）：三来源（客户端主动断 / 心跳判死 / 服务端异常）都走这里。

    只清 A2 域：注册表条目、disconnect_callbacks（A3 借此关 PeerConnection / ASR 会话 /
    TTS 在途任务 / 帧缓冲）。**不释放 PeerConnection 与音频轨**（A3 域，F8）。
    """
    if conn.device_id:
        async with _registry_lock:
            # 只有槽位仍是本连接时才移除：踢旧场景下新连接已接管，不能误删
            if _registry.get(conn.device_id) is conn:
                del _registry[conn.device_id]
    # C5：断线必须取消 30s 计时并收敛状态，避免在已关闭连接上补发 generic
    timer = conn.identity_task
    _cancel_identity_timer(conn)
    if timer is not None and not timer.done():
        with contextlib.suppress(asyncio.CancelledError):
            await timer
    conn.identity_state = IdentityState.CLOSED
    conn.pending_ask_id = None
    for callback in conn.disconnect_callbacks:
        try:
            outcome = callback()
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            logger.warning(
                "ws_disconnect_callback_failed",
                extra=_log_extra(conn),
                exc_info=True,
            )
    logger.info("ws_disconnected", extra=_log_extra(conn))


@router.websocket("/ws/signal")
async def signal_endpoint(
    websocket: WebSocket,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """WS /ws/signal 主端点（F9：无 /api 前缀）。

    trace 在 accept 前注入（auth 失败日志也要可查），连接 finally 里精确 reset
    （contextvar 严格成对，R4：WS 长连接不串号）。
    """
    trace_token = set_trace_id(new_trace_id())
    conn = ConnectionContext(trace_id=get_trace_id() or "-", websocket=websocket)
    dead_task: asyncio.Task[None] | None = None
    writer_task: asyncio.Task[None] | None = None
    identity_token = None
    try:
        await websocket.accept()
        dead_task = asyncio.create_task(_dead_watcher(conn))

        identity = await _authenticate(conn, session)
        if identity is None:
            return  # auth_fail + 4401 已发

        # room_id 只读复用 device_service（app/audio → services 单向依赖；不 import ORM）
        candidates = await list_elder_candidates(session, identity, UUID(identity.device_id))
        conn.room_id = str(candidates.room_id)
        # C5 候选 display_name：只取**设备绑定长者**那一位，双人间另一位不进候选集
        if identity.elder_id is not None:
            for candidate in candidates.candidates:
                if str(candidate.elder_id) == identity.elder_id:
                    conn.elder_display_name = candidate.display_name
                    break
        conn.authed = True

        await _register(conn)

        # 身份注入（W2 步骤 6）：identity 只来自服务端鉴权；writer 在注入后创建，
        # task 创建时复制 contextvars，下行发送带身份上下文
        identity_token = set_identity(identity)
        writer_task = asyncio.create_task(_writer(conn))

        conn.downstream.put_nowait(
            {"type": "auth_ok", "elder_id": conn.elder_id, "room_id": conn.room_id}
        )
        logger.info("ws_authed", extra=_log_extra(conn, room_id=conn.room_id))

        await _read_loop(conn)
    except WebSocketDisconnect:
        pass  # 客户端主动断：走 finally 清理
    finally:
        for task in (dead_task, writer_task):
            if task is not None:
                task.cancel()
        if identity_token is not None:
            reset_identity(identity_token)
        await _cleanup(conn)
        reset_trace_id(trace_token)
