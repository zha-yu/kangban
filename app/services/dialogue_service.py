"""Day04 A2 · 对话回合服务（C6 契约 + Redis 最近 5 轮 + FINAL 编排入口）。

分三层，全部只依赖注入的组件：

1. `RedisTurnStore`：`<redis_key_prefix>:dialogue:<thread_id>:turns` 的轮次存取；
   `RPUSH + LTRIM` 在同一事务管线内原子完成，不增加 TTL；
2. `DialogueService`：加载最近 5 轮 → `app.core.context.build_dialogue_messages` →
   `LLMService.chat` → 写回本轮；generic 不读、不写任何历史；
3. `DialogueOrchestrator`：ASR FINAL 的唯一 awaitable 入口，顺序固定为
   A1 `identity_service`（身份 + thread）→ 最近 5 轮 → LLM → 写回 → 文本/degraded。

边界（`docs/plans/day04/a2-impl-信令与对话.md` N04/N06）：

- thread_id / Redis key / 身份只来自服务端；客户端与模型传入一律丢弃；
- **`conversation_threads` 由 A1 `identity_service` 独占创建/结束**，A2 只消费
  `IdentityThreadResult`，不建 thread、不自生成 generic thread_id、不双写；
- 生产 Redis 客户端由上层生命周期创建并关闭，本模块只在构造参数接收，不新建客户端；
- 不存原始音频、embedding、token；不新增 TTL；Fake Redis 只用于测试；
- 不写健康/用药，不读长期记忆，不操作 WebRTC 音轨，不发 WebSocket。

**B1 已裁决（2026-11-06）**：`ThreadInfo.elder_id` 改为 `str | None`，generic 为 None，
与 `conversation_threads` 的 CHECK 对齐；本模块不把 `None` 强转成字符串或伪造默认长者。
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

from redis.exceptions import RedisError

from app.core.context import (
    DIALOGUE_MAX_ROUNDS,
    DialogueIdentity,
    DialogueTurn,
    PersonaContext,
    build_dialogue_messages,
    get_trace_id,
    reset_trace_id,
    set_trace_id,
)
from app.core.metrics import Stopwatch, record_stage
from app.services.llm_service import LLMService

if TYPE_CHECKING:
    # A1 的 IdentityDecision 只用于类型标注；运行时不 import，避免 services 硬耦合。
    from app.services.identity_service import IdentityDecision

logger = logging.getLogger("app.services.dialogue_service")

# ------------------------------------------------------------------ C6 契约（contracts.md v1.2 修订）
# 方向标记：C→S 客户端发，S→C 服务端发。


class TtsTextMsgV2(TypedDict):  # S→C：C2 tts_text 的向后兼容扩展
    type: Literal["tts_text"]
    text: str
    degraded: bool  # 新增可选字段，缺省 False：True=LLM 降级模板话术


class ThreadInfo(TypedDict):  # 服务端内部契约（闸门①），前端无感知；A1 identity_service 创建
    thread_id: str  # 服务端生成；客户端/模型传入一律丢弃
    elder_id: (
        str | None
    )  # B1 裁决 2026-11-06：generic 时为 None，与 conversation_threads CHECK 对齐
    session_seq: int  # 会话内递增；换人 → 旧 thread ended_at 落库 + 新 thread


# ------------------------------------------------------------------ Redis 轮次存储

# A1 的 thread_id 可能是 `(elder_id, session_seq)` 或 `generic:<device_id>:<seq>` 形式；
# 允许冒号，仍拒绝空白、斜杠与控制字符。Redis key 不回解析，冒号不会造成键碰撞。
THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DIALOGUE_KEY_SEGMENT = "dialogue"
_TURNS_KEY_SEGMENT = "turns"

# 只依赖这几个 Redis 方法，便于测试替身与生产客户端并存
RedisClient = Any  # redis.asyncio.Redis 的鸭子类型；不在此重新实现连接层


class RedisPipeline(Protocol):
    def rpush(self, name: str, *values: str) -> Any: ...

    def ltrim(self, name: str, start: int, end: int) -> Any: ...

    async def execute(self) -> list[Any]: ...


def _validate_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str) or not THREAD_ID_PATTERN.match(thread_id):
        raise ValueError("thread_id must be a server-generated safe token")
    return thread_id


@dataclass(frozen=True, slots=True)
class RedisTurnStore:
    """Redis 最近 5 轮存取；客户端生命周期由上层持有。"""

    client: RedisClient
    key_prefix: str
    max_turns: int = DIALOGUE_MAX_ROUNDS

    def __post_init__(self) -> None:
        if not self.key_prefix.strip():
            raise ValueError("redis key_prefix must not be empty")
        if self.max_turns <= 0:
            raise ValueError("max_turns must be positive")

    def key_for(self, thread_id: str) -> str:
        """只接受服务端 thread_id；key 形状固定，客户端不能传入 key。"""
        safe_thread_id = _validate_thread_id(thread_id)
        return f"{self.key_prefix}:{_DIALOGUE_KEY_SEGMENT}:{safe_thread_id}:{_TURNS_KEY_SEGMENT}"

    async def load_recent_turns(self, thread_id: str) -> list[DialogueTurn]:
        """读取列表末尾 N 轮；任一条结构非法 → 安全空历史 + 脱敏 warn。"""
        key = self.key_for(thread_id)
        raw_items = await self.client.lrange(key, -self.max_turns, -1)
        turns: list[DialogueTurn] = []
        for raw in raw_items:
            if isinstance(raw, bytes):
                raw_text = raw.decode("utf-8", errors="replace")
            elif isinstance(raw, str):
                raw_text = raw
            else:
                raw_text = ""
            parsed = _parse_turn(raw_text)
            if parsed is None:
                logger.warning(
                    "dialogue_history_invalid",
                    extra={
                        "trace_id": get_trace_id() or "-",
                        "thread_id": thread_id,
                        "invalid_count": len(raw_items),
                    },
                )
                return []
            turns.append(parsed)
        return turns

    async def append_turn(self, thread_id: str, turn: DialogueTurn) -> None:
        """`RPUSH + LTRIM` 必须同一条事务管线，避免追加与裁剪分离导致超 5 轮。"""
        key = self.key_for(thread_id)
        payload = json.dumps(
            {"user": turn.user, "assistant": turn.assistant},
            ensure_ascii=False,
        )
        pipe = self.client.pipeline(transaction=True)
        pipe.rpush(key, payload)
        pipe.ltrim(key, -self.max_turns, -1)
        await pipe.execute()


def _parse_turn(raw_text: str) -> DialogueTurn | None:
    try:
        payload: Any = json.loads(raw_text) if raw_text else None
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    user = payload.get("user")
    assistant = payload.get("assistant")
    if not isinstance(user, str) or not isinstance(assistant, str):
        return None
    try:
        return DialogueTurn(user=user, assistant=assistant)
    except ValueError:
        return None


# ------------------------------------------------------------------ 对话回合服务


@dataclass(frozen=True, slots=True)
class DialogueReply:
    """对话服务返回给编排/发送层的最小结果。"""

    text: str
    degraded: bool


class DialogueService:
    """固定前缀 + Redis 最近 5 轮 + LLM 的单轮对话。

    并发口径：本类不做跨进程锁；同一 thread 的并发限制由 N07 装配层确认。
    """

    def __init__(
        self,
        *,
        llm: LLMService,
        store: RedisTurnStore,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._llm = llm
        self._store = store
        self._now_provider = now_provider or (lambda: datetime.now(UTC))

    async def respond(
        self,
        *,
        thread_id: str,
        identity: DialogueIdentity,
        user_text: str,
        persona: PersonaContext | None = None,
    ) -> DialogueReply:
        """一轮对话：身份 → 历史 → LLM → 写回。generic 全程不碰 Redis。"""
        text = user_text.strip()
        if not text:
            raise ValueError("user_text must be non-empty")

        history: list[DialogueTurn] = []
        if not identity.is_generic:
            try:
                history = await self._store.load_recent_turns(thread_id)
            except (RedisError, OSError, ValueError) as exc:
                logger.warning(
                    "dialogue_history_unavailable",
                    extra={
                        "trace_id": get_trace_id() or "-",
                        "thread_id": thread_id,
                        "error_type": type(exc).__name__,
                    },
                )
                history = []

        messages = build_dialogue_messages(
            identity=identity,
            user_text=text,
            history=history,
            persona=None if identity.is_generic else persona,
            now=self._now_provider(),
        )
        llm_sw = Stopwatch()
        reply = await self._llm.chat(messages)
        # Day 5 llm_first_token 打点（自裁 Z1：LLM 非流式，首 token ≈ 整段返回时刻，
        # source 标注口径；Day 6+ 流式化后口径自动变准）
        record_stage(
            "llm_first_token",
            llm_sw.elapsed_ms(),
            elder_id=identity.elder_id,
            source="non_stream",
        )

        if not identity.is_generic:
            try:
                await self._store.append_turn(
                    thread_id,
                    DialogueTurn(user=text, assistant=reply.text),
                )
            except (RedisError, OSError, ValueError) as exc:
                logger.warning(
                    "dialogue_writeback_failed",
                    extra={
                        "trace_id": get_trace_id() or "-",
                        "thread_id": thread_id,
                        "error_type": type(exc).__name__,
                    },
                )

        logger.info(
            "dialogue_turn",
            extra={
                "trace_id": get_trace_id() or "-",
                "thread_id": thread_id,
                "identity_source": identity.source,
                "history_rounds": len(history),
                "degraded": reply.degraded,
                "text_len": len(reply.text),
            },
        )
        return DialogueReply(text=reply.text, degraded=reply.degraded)


# ------------------------------------------------------------------ FINAL 编排入口


@dataclass(frozen=True, slots=True)
class IdentityThreadResult:
    """A1 `IdentityDecision` 的 A2 消费投影（仅终态）。

    A1 当前 `IdentityDecision` 只回传 `thread_id` / `confidence` / `reason`，不回传
    `ThreadInfo` 或 `session_seq`；A2 **不伪造** `session_seq`，故此处允许为 `None`。
    `conversation_threads` 创建/结束与 thread_id 仍由 A1 独占完成。
    """

    identity: DialogueIdentity
    thread_id: str
    identity_confidence: float | None = None
    session_seq: int | None = None  # A1 未回传时为 None；不得填占位值


class IdentityConfirmationRequired(Exception):
    """A1 处于主动确认分支（thread 尚未创建）：调用方应发 C5 `identity_ask`。"""

    def __init__(self, *, reason: str, candidate_elder_id: str | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.candidate_elder_id = candidate_elder_id


def identity_decision_to_result(decision: IdentityDecision) -> IdentityThreadResult:
    """A1 `IdentityDecision` → A2 最小消费结果；不建 thread、不补伪造 session_seq。

    `needs_confirm` 分支抛 `IdentityConfirmationRequired`，由 A3 接线层发
    `identity_ask`；A2 编排入口不自行发 C5 消息。
    """
    if decision.thread_id is None:
        if decision.source == "confirm":
            raise IdentityConfirmationRequired(
                reason=decision.reason or "low_confidence",
                candidate_elder_id=(
                    str(decision.elder_id) if decision.elder_id is not None else None
                ),
            )
        raise ValueError("identity decision has no thread_id")

    elder_id = str(decision.elder_id) if decision.elder_id is not None else None
    identity = DialogueIdentity(source=decision.source, elder_id=elder_id)
    return IdentityThreadResult(
        identity=identity,
        thread_id=decision.thread_id,
        identity_confidence=decision.confidence,
        session_seq=decision.session_seq,
    )


class IdentityThreadResolver(Protocol):
    """A1 identity_service 的消费面：一次返回身份与 thread_id，A2 不单独建 thread。

    接线层用 `identity_decision_to_result(IdentityDecision)` 把 A1 终态转成本类型；
    `needs_confirm`（thread_id=None）应抛 `IdentityConfirmationRequired` 并发 C5 ask。
    """

    async def __call__(
        self,
        *,
        device_id: str,
        turn_id: str,
        text: str,
        trace_id: str,
        audio_bytes: bytes | None,
    ) -> IdentityThreadResult: ...


class PersonaProvider(Protocol):
    """只读 persona 上下文；generic 不得调用。"""

    async def __call__(self, *, elder_id: str) -> PersonaContext | None: ...


_ORCHESTRATION_SEEN_CAPACITY = 512


def _validate_identity_result(result: IdentityThreadResult) -> None:
    """A1 终态结果必须有服务端安全 thread_id；session_seq 可空但不得为负。"""
    if not isinstance(result.thread_id, str) or not THREAD_ID_PATTERN.match(result.thread_id):
        raise ValueError("thread_id must be a server-generated safe token")
    if result.session_seq is not None and (
        not isinstance(result.session_seq, int)
        or isinstance(result.session_seq, bool)
        or result.session_seq < 0
    ):
        raise ValueError("session_seq must be a non-negative int or None")


class DialogueOrchestrator:
    """ASR FINAL 的唯一 awaitable 入口。

    只接收服务端 device/trace 上下文、turn_id 和 FINAL 文本；不接收客户端
    elder_id/thread_id。PARTIAL/ERROR 根本不会进入本入口（A3 只对 FINAL 调用）。

    A1 `identity_service` 独占创建/结束 `conversation_threads`；本入口只消费
    `IdentityThreadResult`，不建 thread、不自生成 thread_id、不双写。A1 返回
    `needs_confirm` 时由接线层通过 `identity_decision_to_result` 转成
    `IdentityConfirmationRequired`，再发 C5 `identity_ask`。
    """

    def __init__(
        self,
        *,
        service: DialogueService,
        identity_thread_resolver: IdentityThreadResolver,
        persona_provider: PersonaProvider | None = None,
    ) -> None:
        self._service = service
        self._identity_thread_resolver = identity_thread_resolver
        self._persona_provider = persona_provider
        # 同 (device_id, turn_id) 只编排一次；进程内去重，不做分布式锁
        self._seen_turns: OrderedDict[tuple[str, str], None] = OrderedDict()

    async def handle_asr_final(
        self,
        *,
        device_id: str,
        trace_id: str,
        turn_id: str,
        text: str,
        audio_bytes: bytes | None = None,
    ) -> DialogueReply | None:
        """FINAL 文本 → 文本/degraded；空文本与重复 turn 返回 None，零 LLM 调用。"""
        final_text = text.strip()
        if not device_id or not trace_id or not turn_id or not final_text:
            return None

        dedupe_key = (device_id, turn_id)
        if dedupe_key in self._seen_turns:
            logger.warning(
                "dialogue_turn_duplicate",
                extra={
                    "trace_id": trace_id,
                    "device_id": device_id,
                    "turn_id": turn_id,
                },
            )
            return None
        self._seen_turns[dedupe_key] = None
        while len(self._seen_turns) > _ORCHESTRATION_SEEN_CAPACITY:
            self._seen_turns.popitem(last=False)

        # FINAL 入口显式绑定本次 trace，后续 Redis/LLM 日志不依赖 task 上下文继承
        try:
            trace_token = set_trace_id(trace_id)
            try:
                result = await self._identity_thread_resolver(
                    device_id=device_id,
                    turn_id=turn_id,
                    text=final_text,
                    trace_id=trace_id,
                    audio_bytes=audio_bytes,
                )
                identity = result.identity
                try:
                    _validate_identity_result(result)
                except ValueError as exc:
                    logger.warning(
                        "dialogue_identity_thread_mismatch",
                        extra={
                            "trace_id": trace_id,
                            "device_id": device_id,
                            "identity_source": identity.source,
                            "reason": str(exc),
                        },
                    )
                    raise

                thread_id = result.thread_id
                logger.info(
                    "dialogue_orchestrated",
                    extra={
                        "trace_id": trace_id,
                        "device_id": device_id,
                        "thread_id": thread_id,
                        "session_seq": result.session_seq
                        if result.session_seq is not None
                        else "-",
                        "identity_source": identity.source,
                    },
                )

                persona: PersonaContext | None = None
                if not identity.is_generic and self._persona_provider is not None:
                    elder_id = identity.elder_id
                    if elder_id is not None:
                        persona = await self._persona_provider(elder_id=elder_id)

                return await self._service.respond(
                    thread_id=thread_id,
                    identity=identity,
                    user_text=final_text,
                    persona=persona,
                )
            finally:
                reset_trace_id(trace_token)
        except BaseException:
            # A1 identity/thread 或下游异常（含取消）时允许同一 FINAL 重试，不永久吞 turn
            self._seen_turns.pop(dedupe_key, None)
            raise


__all__ = [
    "THREAD_ID_PATTERN",
    "DialogueOrchestrator",
    "DialogueReply",
    "DialogueService",
    "IdentityConfirmationRequired",
    "IdentityThreadResolver",
    "IdentityThreadResult",
    "PersonaProvider",
    "RedisTurnStore",
    "ThreadInfo",
    "TtsTextMsgV2",
    "identity_decision_to_result",
]
