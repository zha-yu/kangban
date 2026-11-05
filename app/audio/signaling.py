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

"""C2 · 信令 WS 消息表（冻结人 A2，消费方 A3/A4）。

本文件当前只落 **消息类型**，不实现 `/ws/signal`、鉴权、心跳或断线清理；
这些行为归 A2，A3 只消费消息语义（offer/answer/ice、asr_start/asr_end、关闭）。

传输：`WS /ws/signal`，文本帧 JSON，每消息一个对象。
方向：C→S 客户端发，S→C 服务端发。`token` 只进首帧 `auth`，不进 URL。
"""

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict
from uuid import UUID

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    KIND_DEVICE,
    AuthError,
    CredentialExpiredError,
    resolve_principal,
)
from app.core.context import get_trace_id, new_trace_id, reset_trace_id, set_trace_id
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

# 自定义关闭码（4000–4999 为应用私有区间）
WS_CLOSE_AUTH_FAILED = 4401  # 鉴权失败（契约定值）
WS_CLOSE_SUPERSEDED = 4002  # 同设备踢旧留新（契约会裁决③）

router = APIRouter()

# ------------------------------------------------------------------ C2 契约（contracts.md 原样拷贝，冻结 v1.0）
# 方向标记：C→S 客户端发，S→C 服务端发。


class AuthMsg(TypedDict):  # C→S，连接后 300ms 内必须首发
    type: Literal["auth"]
    token: str  # 设备会话 access_token（E4 签发）；token 不进 URL


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
    "ErrorMsg",
    "IceMsg",
    "OfferMsg",
    "PingMsg",
    "PongMsg",
    "TtsTextMsg",
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
    """S→C，大字幕渲染数字人回复（打断时前端立即停播）。"""

    type: Literal["tts_text"]
    text: str


class ErrorMsg(TypedDict):  # S→C，服务端降级/异常通知
    type: Literal["error"]
    code: Literal["asr_unavailable", "llm_timeout", "tool_breaker", "internal"]
    message: str  # 已脱敏的长者可读话术


# ------------------------------------------------------------------ 连接上下文与注册表


@dataclass
class ConnectionContext:
    """一条 WS 连接的全部服务端状态。

    `websocket` 是执行计划 §3 W1 字段清单之外的实现必需项：踢旧留新（裁决③）要拿到
    旧连接的 WebSocket 才能发 close 帧。对 A3 仍不暴露（只读上层字段即可）。
    """

    trace_id: str
    websocket: WebSocket
    device_id: str | None = None
    elder_id: str | None = None
    room_id: str | None = None
    authed: bool = False
    last_frame_at: float = field(default_factory=time.monotonic)
    downstream: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    disconnect_callbacks: list[Callable[[], object]] = field(default_factory=list)


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


def deliver_asr_event(device_id: str, event: Any) -> bool:
    """C1 `ASREvent` → C2 下行消息映射（A1/A3 消费）。

    `event` 是 `app.audio.asr.ASREvent`（C1 冻结件）；C1 模块当前尚未落码（A1 域），
    这里按契约结构鸭子读取（kind/result/error），C1 落码后由 A1 侧切正式 import。
    `ERROR` 只映射固定脱敏话术，不透出 `ASRError.message` 内部细节。
    """
    kind = getattr(event, "kind", None)
    kind_value = str(getattr(kind, "value", kind))
    result = getattr(event, "result", None)
    text = getattr(result, "text", None)
    if kind_value == "partial":
        return deliver(device_id, {"type": "asr_partial", "text": text or ""})
    if kind_value == "final":
        return deliver(device_id, {"type": "asr_final", "text": text or ""})
    if kind_value == "error":
        return deliver(
            device_id,
            {
                "type": "error",
                "code": "asr_unavailable",
                "message": _ERROR_TALKBACK["asr_unavailable"],
            },
        )
    return False


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
    conn.elder_id = identity.elder_id
    conn.last_frame_at = time.monotonic()
    return identity


async def _register(conn: ConnectionContext) -> None:
    """接管注册表槽位；同设备旧连接踢下线（裁决③：踢旧留新，注册表操作加锁）。"""
    assert conn.device_id is not None  # _authenticate 通过后必非空
    async with _registry_lock:
        old = _registry.get(conn.device_id)
        _registry[conn.device_id] = conn
    if old is not None:
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
            message = await conn.downstream.get()
            await conn.websocket.send_text(json.dumps(message, ensure_ascii=False))
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

