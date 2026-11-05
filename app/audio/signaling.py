"""C2 · 信令 WS 消息表（冻结人 A2，消费方 A3/A4）。

本文件当前只落 **消息类型**，不实现 `/ws/signal`、鉴权、心跳或断线清理；
这些行为归 A2，A3 只消费消息语义（offer/answer/ice、asr_start/asr_end、关闭）。

传输：`WS /ws/signal`，文本帧 JSON，每消息一个对象。
方向：C→S 客户端发，S→C 服务端发。`token` 只进首帧 `auth`，不进 URL。
"""

from __future__ import annotations

from typing import Literal, TypedDict

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


class AuthMsg(TypedDict):
    """C→S，连接后 300ms 内必须首发；token 不进 URL。"""

    type: Literal["auth"]
    token: str


class AuthOkMsg(TypedDict):
    """S→C；`elder_id` 仅展示用，业务归属一律服务端裁决。"""

    type: Literal["auth_ok"]
    elder_id: str | None
    room_id: str


class AuthFailMsg(TypedDict):
    """S→C，随后以 4401 关闭。"""

    type: Literal["auth_fail"]
    code: Literal["timeout", "invalid", "expired"]


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


class ErrorMsg(TypedDict):
    """S→C，服务端降级/异常通知；message 为已脱敏的长者可读话术。"""

    type: Literal["error"]
    code: Literal["asr_unavailable", "llm_timeout", "tool_breaker", "internal"]
    message: str
