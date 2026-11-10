"""Day 6 N07 · ResponseStream：服务端分段下发 / segment_ack 四步校验 / 打断五步。

口径唯一来源：`docs/team_tasks/day06/contracts.md` §4.1（tts_text v2）、§4.2
（segment_ack 四步校验 + v1.2 补充口径）与 dev-C 计划 C-5（§5 交付物红线）。

职责边界：

- **分段**：文本生成完成后由服务端切（`。！？；` 优先，单段 ≤40 字，超长句按逗号补切），
  逐段下发 tts_text v2（五新字段 response_id/epoch/segment_id/index/total，老字段保留，
  老客户端忽略新字段仍能整段一播）；
- **ack 四步**（任一不过丢弃并记 `response_ack_rejected`，带 rejected_reason 四枚举之一）：
  ① response_id 存在 → ② epoch 等于当前会话 epoch → ③ response.status == playing →
  ④ segment_id > last_acked_segment（单调，初始 -1）；canceled 类 ack 只走①②③跳过④；
- **打断五步（顺序不可交换）**：收到 interrupt → 停 TTS 队列 → epoch+=1 →
  取消 LLM/TTS 任务 → 追加 `response_interrupted` 事件 + delivery_status=interrupted；
- **事实口径**：delivered_text 只拼 played 段；delivery_status 只取
  delivered/interrupted/unknown，**永不写 understood**；打断不回滚已发生的事实
  （response_segment_delivered 等事件只追加，工具 succeeded 副作用不随回复取消）。

桩期边界（S3 对齐时替换，签名不变）：

- epoch 暂按 thread_id 由本服务进程内持有（`start` +1 开新代、`interrupt` +1 跨代淘汰）；
  N05 StateCenter 到位后改读 `StateSnapshot.output.generation_epoch`；
- 事件经 `EventSink` 协议追加（N04 HistoryStore 到位后由装配层适配 frozen 签名）；
- 下行投递经 `deliver` 注入（`app/audio/signaling.deliver` 由装配层绑入；
  分层红线 services 不反向 import app/audio）；「停 TTS 队列」经 `TtsQueue` 协议注入，
  默认 no-op 桩，S3 换真实下行队列清理。

指标（复用 `app/core.metrics.record_stage`，不新建第二套）：每段下发与每次被接受的 ack
各打一条 `stage=response_segment`。该 stage 不在 metrics 表七值白名单内（0006 迁移
CHECK 冻结、alembic 禁触），DB 轨由 metrics_service 白名单自滤（仅日志轨生效，与
identity 三点位同款先例）；被拒绝的 ack 走 `response_ack_rejected` 日志轨，不打该指标。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from app.core.context import get_trace_id
from app.core.metrics import Stopwatch, record_stage
from app.services.dialogue_service import THREAD_ID_PATTERN

logger = logging.getLogger("app.services.response_service")

# ------------------------------------------------------------------ 契约常量

#: 单段上限（contracts.md §4.1：单段 ≤40 字，超长句按逗号补切）
MAX_SEGMENT_CHARS: Final[int] = 40
#: 段号哨兵：服务端 last_acked_segment 初始值 = canceled 无一完整播放段时的 segment_id
SEGMENT_SENTINEL: Final[int] = -1
#: 进程内最多跟踪的 response 数（FIFO 淘汰最旧，防长连接进程无界增长）
MAX_TRACKED_RESPONSES: Final[int] = 256

_SENTENCE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[。！？；])")
_COMMA_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[，,])")

AckStatus = Literal["playing", "played", "canceled"]
DeliveryStatus = Literal["delivered", "interrupted", "unknown"]
AckRejectedReason = Literal["unknown_response", "stale_epoch", "wrong_status", "not_monotonic"]
ResponsePhase = Literal["playing", "ended"]

ACK_PLAYING: Final[AckStatus] = "playing"
ACK_PLAYED: Final[AckStatus] = "played"
ACK_CANCELED: Final[AckStatus] = "canceled"
_ACK_STATUSES: Final[frozenset[str]] = frozenset({"playing", "played", "canceled"})

PHASE_PLAYING: Final[ResponsePhase] = "playing"
PHASE_ENDED: Final[ResponsePhase] = "ended"

EVENT_RESPONSE_STARTED: Final[str] = "response_started"
EVENT_SEGMENT_DELIVERED: Final[str] = "response_segment_delivered"
EVENT_RESPONSE_INTERRUPTED: Final[str] = "response_interrupted"
EVENT_RESPONSE_COMPLETED: Final[str] = "response_completed"
#: 下发窗口期断连补偿（P2-11 本日范围）：单段重试 1 次仍失败时追加
EVENT_RESPONSE_DELIVERY_FAILED: Final[str] = "response_delivery_failed"

METRIC_STAGE_RESPONSE_SEGMENT: Final[str] = "response_segment"

#: epoch/水位回写 TTL（P2-9，handoff §2：2 小时）
_PERSIST_TTL_SECONDS: Final[int] = 7200


def split_segments(text: str) -> tuple[str, ...]:
    """按契约切分播报文本：`。！？；` 切点 → 单段 ≤40 字 → 超长句按逗号补切。

    - 切点保留标点（lookbehind 切分），`"".join(segments) == text.strip()` 恒成立；
    - 无逗号可补切的超长句按 40 字硬切（最后兜底，计划 R-1 同口径）；
    - 空白段丢弃；空文本返回空元组。
    """
    cleaned = text.strip()
    if not cleaned:
        return ()
    segments: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(cleaned):
        stripped = sentence.strip()
        if not stripped:
            continue
        if len(stripped) <= MAX_SEGMENT_CHARS:
            segments.append(stripped)
            continue
        buffer = ""
        for clause in _COMMA_SPLIT_RE.split(stripped):
            clause = clause.strip()
            if not clause:
                continue
            if len(clause) > MAX_SEGMENT_CHARS:
                # 逗号都救不了：硬切成 ≤40 字的块
                if buffer:
                    segments.append(buffer)
                    buffer = ""
                for start in range(0, len(clause), MAX_SEGMENT_CHARS):
                    segments.append(clause[start : start + MAX_SEGMENT_CHARS])
                continue
            if buffer and len(buffer) + len(clause) <= MAX_SEGMENT_CHARS:
                buffer += clause
            else:
                if buffer:
                    segments.append(buffer)
                buffer = clause
        if buffer:
            segments.append(buffer)
    return tuple(segments)


# ------------------------------------------------------------------ 消费协议（桩期注入面）


class GenerationResultLike(Protocol):
    """生成结果最小消费面（DialogueReply / N14 产物均满足）。"""

    @property
    def text(self) -> str: ...

    @property
    def degraded(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class TurnContext:
    """一轮回复的服务端上下文（N14 `handle_user_turn` 构造，客户端不可传）。"""

    thread_id: str
    turn_id: str
    device_id: str
    elder_id: str | None = None  # generic 模式为 None
    # N06 记忆召回产物：本长者 status='active' 且在有效期的偏好事实（自然语言）。
    # 召回失败 / generic 一律空元组——本轮不加载记忆，绝不臆造（PRD 口径）。
    memory_notes: tuple[str, ...] = ()
    # 本轮用户原文（B2 记忆提取需要；N14 装配时可填，缺省空串不破坏既有构造点）
    user_text: str = ""


class EventSink(Protocol):
    """事件追加消费面（N04 HistoryStore.append_event 的 frozen 签名子集）。"""

    async def append_event(
        self,
        *,
        thread_id: str,
        event_type: str,
        payload: Mapping[str, object],
        turn_id: str | None = None,
        response_id: str | None = None,
    ) -> int: ...


class TtsQueue(Protocol):
    """「停 TTS 队列」消费面：打断第②步调用，清空该 response 的待播下行。"""

    def stop(self, response_id: str) -> None: ...


class Cancellable(Protocol):
    """可取消任务持有面（asyncio.Task.cancel 返回 bool，结构兼容）。"""

    def cancel(self) -> object: ...


class NullTtsQueue:
    """桩期默认 TTS 队列：no-op 占位，S3 接线时由装配层换真实实现。"""

    def stop(self, response_id: str) -> None:
        return None


class RedisEpochPersistence:
    """`EpochPersistence` 的 Redis 实现（装配用）：直连 get/set，TTL 透传。

    与 `state_service.StateCenter` 同款纪律——连接故障不在适配层吞：调用侧
    （`_persist_*` / `_restore_epoch`）已把持久化当尽力而为（失败 warning）。
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def get(self, key: str) -> str | None:
        data = await self._client.get(key)
        if data is None:
            return None
        return data.decode("utf-8") if isinstance(data, bytes) else data

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        await self._client.set(key, value, ex=ex)


class HistorySettlementPort(Protocol):
    """B1 回合终态结算端口（Day 7 P1-1/P1-3）：与 `history_service` 语义同构。

    - `commit_response`：只写已送达投影（`delivered_text`），不改轮次状态；
    - `finish_turn`：轮次终态唯一写口（幂等不覆盖）。
    装配层由 `HistoryStore` 适配（持 session_factory）；未注入时结算跳过并
    告警一次（显式暴露装配缺口，不静默——否则 P1-1 复发）。
    """

    async def commit_response(
        self,
        *,
        turn_id: str,
        response_id: str,
        generated_text: str,
        delivered_text: str | None,
        status: str,
    ) -> None: ...

    async def finish_turn(
        self, *, turn_id: str, status: Literal["completed", "interrupted", "failed"]
    ) -> None: ...


class EpochPersistence(Protocol):
    """epoch/水位回写端口（P2-9）：最小 KV 面，TTL 由调用方透传。

    键口径：`session:{thread_id}:epoch`（会话代）与 `response:{response_id}`
    （水位最小字段 JSON）；实现侧为 Redis 时 get/set 需容忍连接故障——
    本服务侧已把持久化视为尽力而为（失败 warning，不阻塞主链路）。
    """

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None: ...


class MemoryExtractionLike(Protocol):
    """记忆提取端口（B1↔B2 边界，裁决 O-B2）：与 `memory_service.MemoryExtractionPort`
    同构。B1 只依赖此结构面，Step 7 总收口把真实 `extract_after_turn` 换入。
    """

    async def extract_after_turn(
        self,
        *,
        elder_id: str | None,
        thread_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
    ) -> int: ...


# ------------------------------------------------------------------ 句柄与内部状态


@dataclass(frozen=True, slots=True)
class ResponseHandle:
    """一次回复的只读快照（`response_stream.start` 产物，N14 幂等键持有它）。

    字段口径对齐契约：response_id/epoch/segment_id/index/total 为 tts_text v2 五字段
    的服务端同构视图；at_ms 为最近一次被接受 ack 的客户端单调时钟（无 ack 时 None）。
    """

    response_id: str
    epoch: int
    thread_id: str
    turn_id: str
    total: int
    segment_id: int  # 服务端单调基线 last_acked_segment（只由 played 推进，初始 -1，
    #                  与 canceled 上报的「最后完整播放段」同构，v1.2 口径）
    index: int  # 与 segment_id 同值（契约 §4.1 保留两字段便于未来乱序）
    at_ms: int | None
    delivered_text: str  # 只拼 played 段
    last_played_segment: int  # 最后一个完整播放段（response_interrupted payload 同值）
    delivery_status: DeliveryStatus


@dataclass(slots=True)
class _ResponseState:
    """服务端单 response 可变状态（`_responses` 注册表的值）。"""

    response_id: str
    epoch: int
    device_id: str
    thread_id: str
    turn_id: str
    elder_id: str | None
    user_text: str
    degraded: bool
    segments: tuple[str, ...]
    total: int
    status: ResponsePhase = PHASE_PLAYING
    last_acked_segment: int = SEGMENT_SENTINEL
    last_played_segment: int = SEGMENT_SENTINEL
    at_ms: int | None = None
    delivered_text: str = ""
    delivery_status: DeliveryStatus = "unknown"
    delivered_at: dict[int, float] = field(default_factory=dict)  # segment_id → 下发时刻
    cancellables: list[Cancellable] = field(default_factory=list)


# ------------------------------------------------------------------ ResponseService


class ResponseService:
    """分段下发 / segment_ack 四步校验 / 打断五步的唯一实现。

    并发口径：同一 thread 的 start/interrupt 由上游 N14 串行化；ack 只依赖
    进程内 dict 读写（事件循环内调用），不做分布式锁。
    """

    def __init__(
        self,
        *,
        deliver: Callable[[str, Mapping[str, object]], bool],
        events: EventSink,
        tts_queue: TtsQueue | None = None,
        history_store: HistorySettlementPort | None = None,
        persistence: EpochPersistence | None = None,
        memory_port: MemoryExtractionLike | None = None,
    ) -> None:
        self._deliver = deliver
        self._events = events
        self._tts_queue: TtsQueue = tts_queue if tts_queue is not None else NullTtsQueue()
        self._history = history_store
        self._persistence = persistence
        self._memory = memory_port
        self._settlement_warned = False
        self._memory_tasks: set[asyncio.Task[None]] = set()  # create_task 持引用防 GC
        self._responses: dict[str, _ResponseState] = {}
        self._epochs: dict[str, int] = {}  # thread_id → 当前会话 epoch（桩期进程内持有）

    # -------------------------------------------------- 查询面

    def session_epoch(self, thread_id: str) -> int:
        """当前会话 epoch（桩期；S3 换 StateSnapshot.output.generation_epoch）。"""
        return self._epochs.get(thread_id, 0)

    def handle(self, response_id: str) -> ResponseHandle | None:
        """按 response_id 取只读快照；不存在返回 None。"""
        state = self._responses.get(response_id)
        return None if state is None else self._snapshot(state)

    # -------------------------------------------------- start：分段逐段下发

    async def start(
        self,
        turn: TurnContext,
        result: GenerationResultLike,
        *,
        response_id: str | None = None,
    ) -> ResponseHandle:
        """服务端分段并把每段以 tts_text v2 下发（N14 七步的最后一步）。

        会话 epoch +1 开新代：同一 thread 上未打断的旧 response 的迟到 ack 将因
        ② stale_epoch 被淘汰（interrupt 的 +1 同理，见 contracts.md §4.2）。

        `response_id` 可选注入：N14 统一出口在第 5 步先记生成时已铸造同一枚
        response_id，第 7 步下发沿用，保证事实与下发可对账；缺省时自行铸造。
        """
        if not turn.thread_id or not THREAD_ID_PATTERN.match(turn.thread_id):
            raise ValueError("thread_id must be a server-generated safe token")
        if not turn.device_id or not turn.turn_id:
            raise ValueError("device_id and turn_id must be non-empty server values")
        text = result.text.strip() if isinstance(result.text, str) else ""
        if not text:
            raise ValueError("generation text must be non-empty before response stream")

        stopwatch = Stopwatch()
        response_id = response_id if response_id is not None else uuid.uuid4().hex
        # P2-9：进程重启后从 Redis 恢复会话 epoch（单进程范围；多 worker 登记 Day 9），
        # 否则重启后 epoch 归 0，携带旧代 epoch 的 ack 会被 stale_epoch 全拒。
        await self._restore_epoch(turn.thread_id)
        self._epochs[turn.thread_id] = self.session_epoch(turn.thread_id) + 1
        epoch = self._epochs[turn.thread_id]
        await self._persist_epoch(turn.thread_id)
        segments = split_segments(text)
        state = _ResponseState(
            response_id=response_id,
            epoch=epoch,
            device_id=turn.device_id,
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            elder_id=turn.elder_id,
            user_text=turn.user_text,
            degraded=bool(result.degraded),
            segments=segments,
            total=len(segments),
        )
        self._responses[response_id] = state
        self._evict_overflow()

        logger.info(
            "response_started",
            extra=self._log_extra(
                state,
                response_id=response_id,
                epoch=epoch,
                total_segments=len(segments),
                degraded=state.degraded,
            ),
        )
        await self._safe_append_event(
            state,
            EVENT_RESPONSE_STARTED,
            {"response_id": response_id, "degraded": state.degraded},
        )
        await self._persist_response(state)

        total = len(segments)
        for index, segment in enumerate(segments):
            message: dict[str, object] = {
                "type": "tts_text",
                "text": segment,
                "degraded": state.degraded,
                "response_id": response_id,
                "epoch": epoch,
                "segment_id": index,
                "index": index,
                "total": total,
            }
            delivered = self._deliver(turn.device_id, message)
            if not delivered:
                # P2-11（本日范围）：单段下发失败重试 1 次；仍失败写补偿事件。
                # 完整重放（断连后逐段补发）登记 Day 9。
                delivered = self._deliver(turn.device_id, message)
            state.delivered_at[index] = time.monotonic()
            if not delivered:
                logger.error(
                    "response_delivery_failed",
                    extra=self._log_extra(state, segment_id=index, response_id=response_id),
                )
                await self._safe_append_event(
                    state,
                    EVENT_RESPONSE_DELIVERY_FAILED,
                    {"segment_id": index, "response_id": response_id},
                )
            # 每段下发一条 response_segment 指标（复用 record_stage，elapsed=start 起累计）
            record_stage(
                METRIC_STAGE_RESPONSE_SEGMENT,
                stopwatch.elapsed_ms(),
                elder_id=state.elder_id,
                source="server",
            )
        return self._snapshot(state)

    # -------------------------------------------------- ack：契约四步校验

    async def handle_segment_ack(self, *, device_id: str, message: Mapping[str, Any]) -> bool:
        """C→S segment_ack 的服务端入口（signaling 层已完成字段形状校验）。"""
        response_id = message.get("response_id")
        if not isinstance(response_id, str):
            logger.warning(
                "response_ack_rejected",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "device_id": device_id,
                    "response_id": "-",
                    "rejected_reason": "unknown_response",
                },
            )
            return False
        at_ms = message.get("at_ms")
        epoch = message.get("epoch")
        segment_id = message.get("segment_id")
        status = message.get("status")
        # P3-30：公开入口不做下标取键——形状不合法显式拒绝，KeyError 不得穿透
        if (
            not isinstance(epoch, int)
            or not isinstance(segment_id, int)
            or not isinstance(status, str)
        ):
            logger.warning(
                "response_ack_malformed",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "device_id": device_id,
                    "response_id": response_id,
                },
            )
            return False
        return await self.ack(
            device_id=device_id,
            response_id=response_id,
            epoch=epoch,
            segment_id=segment_id,
            status=status,
            at_ms=int(at_ms) if isinstance(at_ms, (int, float)) else None,
        )

    async def ack(
        self,
        *,
        device_id: str,
        response_id: str,
        epoch: int,
        segment_id: int,
        status: str,
        at_ms: int | None = None,
    ) -> bool:
        """segment_ack 契约四步校验；被接受的 ack 才推进服务端状态。

        canceled 只走①②③（陈述性上报，不推进基线、不改状态）；playing/played 走
        完整四步，④ 校验后仅 played 推进 last_acked_segment 并累计 delivered_text
        与 last_played_segment（v1.2「与最后完整播放段同值」口径）。
        """
        state = self._responses.get(response_id)

        def reject(reason: AckRejectedReason) -> bool:
            # device_id 以入参为准：unknown_response 时 state 可能属于另一台设备
            logger.warning(
                "response_ack_rejected",
                extra=self._log_extra(
                    state,
                    device_id=device_id,
                    response_id=response_id,
                    ack_epoch=epoch,
                    ack_segment_id=segment_id,
                    ack_status=status,
                    rejected_reason=reason,
                ),
            )
            return False

        # ① response_id 存在（且来自接收该回复的同一设备连接）
        if state is None or state.device_id != device_id:
            return reject("unknown_response")
        # ② epoch 等于当前会话 epoch（跨代淘汰）
        if epoch != self.session_epoch(state.thread_id):
            return reject("stale_epoch")
        # ③ response 仍在播放（同代内打断后的追认拦截）
        if state.status != PHASE_PLAYING:
            return reject("wrong_status")

        if status == ACK_CANCELED:
            # P2-7（Day 7 改写）：canceled 是终态上报——置 interrupted、按水位截断、
            # 写事件并结算，不再只是陈述性上报。跳过规则④（不受单调性约束）。
            water_mark = state.last_played_segment
            if segment_id != water_mark:
                # P2-8：客户端段号与水位不一致 → 告警并以水位为准（不按客户端截断）
                logger.warning(
                    "response_ack_canceled_watermark_mismatch",
                    extra=self._log_extra(
                        state,
                        client_segment_id=segment_id,
                        watermark=water_mark,
                    ),
                )
            state.status = PHASE_ENDED
            state.delivery_status = "interrupted"
            state.delivered_text = (
                "".join(state.segments[: water_mark + 1]) if water_mark >= 0 else ""
            )
            logger.info(
                "response_ack_canceled",
                extra=self._log_extra(state, response_id=response_id, ack_segment_id=segment_id),
            )
            if water_mark >= 0:
                # P2-8：打点按水位段计时；-1 哨兵（无一完整播放段）无耗时可测，不打点
                record_stage(
                    METRIC_STAGE_RESPONSE_SEGMENT,
                    self._ack_elapsed_ms(state, water_mark),
                    elder_id=state.elder_id,
                    source="server",
                )
            await self._safe_append_event(
                state,
                EVENT_RESPONSE_INTERRUPTED,
                {
                    "response_id": state.response_id,
                    "last_played_segment": water_mark,
                    "reason": "canceled",
                },
            )
            await self._persist_response(state)
            await self._settle_turn(
                state, status="interrupted", delivered_text=state.delivered_text
            )
            return True

        # ④ 单调（playing/played 同规则）：segment_id 须落在合法域 [0, total-1]
        #    且严格大于 last_acked_segment（contracts.md §4.2④ + v1.2 合法域口径）。
        #    基线推进只由 played 完成（v1.2「last_played_segment/delivered_text 沿用
        #    同值」：last_acked_segment 与 canceled 上报的「最后完整播放段」同构），
        #    故 playing(i) → played(i) 的自然序列逐条满足 ④，playing ack 不推进基线。
        if not 0 <= segment_id < state.total or segment_id <= state.last_acked_segment:
            return reject("not_monotonic")

        state.at_ms = at_ms
        record_stage(
            METRIC_STAGE_RESPONSE_SEGMENT,
            self._ack_elapsed_ms(state, segment_id),
            elder_id=state.elder_id,
            source="server",
        )
        if status == ACK_PLAYED:
            state.last_acked_segment = segment_id
            state.last_played_segment = segment_id
            # delivered_text 只收录客户端确认完整播放的连续前缀；`playing` 是进度信号，
            # 不能让上下文把尚未播完的片段当成用户已听到的事实。
            state.delivered_text = "".join(state.segments[: segment_id + 1])
            await self._persist_response(state)
            await self._safe_append_event(
                state,
                EVENT_SEGMENT_DELIVERED,
                {
                    "segment_id": segment_id,
                    "index": segment_id,
                    "text": state.segments[segment_id],
                },
            )
            if segment_id == state.total - 1:
                state.status = PHASE_ENDED
                state.delivery_status = "delivered"
                # P2-10（B1）：response_completed 的 payload 在写入时就写全——本服务是
                # 该事件的唯一写口，事件只追加不回填，事后 commit_response 只补投影
                # 补不了 payload（见 handoff-B-to-A §2）。generated_text 供审计，
                # delivered_text=已播前缀，response_status 为终态语义。
                await self._safe_append_event(
                    state,
                    EVENT_RESPONSE_COMPLETED,
                    {
                        "total_segments": state.total,
                        "generated_text": "".join(state.segments),
                        "delivered_text": state.delivered_text,
                        "response_status": "completed",
                    },
                )
                logger.info(
                    "response_completed",
                    extra=self._log_extra(
                        state, response_id=response_id, total_segments=state.total
                    ),
                )
                # P1-1：回合终态结算——投影（delivered_text）+ finish_turn("completed")。
                # 响应层是 response_completed 的唯一写口；turn_service 统一出口的第二
                # 写口由 A3 侧删除（P2-10，本侧 commit_response 防御只补投影）。
                await self._settle_turn(
                    state, status="completed", delivered_text=state.delivered_text
                )
        return True

    # -------------------------------------------------- interrupt：五步顺序（不可交换）

    async def interrupt(
        self, response_id: str, *, device_id: str | None = None
    ) -> ResponseHandle | None:
        """打断一次进行中的回复；五步顺序固定：①收到 → ②停队列 → ③epoch+1 →
        ④取消任务 → ⑤追加事件 + delivery_status=interrupted。

        归属校验（§5.4）：传入 `device_id` 时 response 必须属于该设备连接，
        否则按 unknown_response 处理（no-op、不落库、不 bump epoch）；N14 内部
        调用可不传（None = 不做归属校验）。

        幂等口径：response 不存在或已结束（delivered/interrupted）为 no-op，
        不重复 bump epoch、不重复写事件。工具副作用不随回复取消：
        已写事件与 played 事实一律保留（只追加，不回滚）。
        """
        state = self._responses.get(response_id)
        if state is None or (device_id is not None and state.device_id != device_id):
            logger.warning(
                "response_interrupt_ignored",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "device_id": device_id or "-",
                    "response_id": response_id,
                    "reason": "unknown_response",
                },
            )
            return None
        if state.status != PHASE_PLAYING:
            logger.warning(
                "response_interrupt_ignored",
                extra=self._log_extra(state, response_id=response_id, reason="not_playing"),
            )
            return self._snapshot(state)

        logger.info(  # ① 收到 interrupt
            "response_interrupt_received",
            extra=self._log_extra(state, response_id=response_id),
        )
        self._tts_queue.stop(response_id)  # ② 停 TTS 队列
        self._epochs[state.thread_id] = self.session_epoch(state.thread_id) + 1  # ③ epoch += 1
        for cancellable in state.cancellables:  # ④ 取消 LLM/TTS 任务
            cancellable.cancel()
        await self._safe_append_event(  # ⑤ 追加事件 + delivery_status=interrupted
            state,
            EVENT_RESPONSE_INTERRUPTED,
            {"response_id": response_id, "last_played_segment": state.last_played_segment},
        )
        state.status = PHASE_ENDED
        state.delivery_status = "interrupted"
        await self._persist_epoch(state.thread_id)  # ③ 的 epoch 变化回写
        await self._persist_response(state)
        # P1-3：打断结算——投影按水位截断 + finish_turn("interrupted") + 记忆提取调度
        await self._settle_turn(state, status="interrupted", delivered_text=state.delivered_text)
        logger.info(
            "response_interrupted",
            extra=self._log_extra(
                state,
                response_id=response_id,
                last_played_segment=state.last_played_segment,
                epoch=self.session_epoch(state.thread_id),
            ),
        )
        return self._snapshot(state)

    def register_cancellable(self, response_id: str, cancellable: Cancellable) -> bool:
        """登记该 response 的在途 LLM/TTS 任务（打断第④步逐个 cancel）。"""
        state = self._responses.get(response_id)
        if state is None or state.status != PHASE_PLAYING:
            logger.warning(
                "response_cancellable_ignored",
                extra={
                    "trace_id": get_trace_id() or "-",
                    "device_id": state.device_id if state else "-",
                    "response_id": response_id,
                },
            )
            return False
        state.cancellables.append(cancellable)
        return True

    # -------------------------------------------------- 终态结算与持久化（Day 7 B1）

    async def _settle_turn(
        self,
        state: _ResponseState,
        *,
        status: Literal["completed", "interrupted"],
        delivered_text: str,
    ) -> None:
        """回合终态结算（P1-1/P1-3）：commit_response 投影 → finish_turn 终态 → 记忆提取调度。

        - 结算异常只记日志不抛：终态落库失败不应炸穿 ack/interrupt 主链路
          （补偿重试登记 Day 9）；CancelledError 不在此捕获（传播语义见 P3-27）。
        - `history_store` 未注入（装配缺失）时告警一次并跳过——显式暴露装配缺口，
          不静默吞掉，否则 P1-1（assistant_delivered_text 恒 NULL）悄悄复发。
        """
        if self._history is None:
            if not self._settlement_warned:
                self._settlement_warned = True
                logger.error(
                    "response_settlement_port_missing",
                    extra=self._log_extra(state, hint="wire history_store adapter in assembly"),
                )
            return
        generated_text = "".join(state.segments)
        try:
            await self._history.commit_response(
                turn_id=state.turn_id,
                response_id=state.response_id,
                generated_text=generated_text,
                delivered_text=delivered_text,
                status=status,
            )
            await self._history.finish_turn(turn_id=state.turn_id, status=status)
        except Exception as exc:  # 结算容错：见 docstring
            logger.error(
                "response_settlement_failed",
                extra=self._log_extra(state, status=status, error_type=type(exc).__name__),
            )
            return
        if self._memory is not None and state.elder_id is not None:
            self._schedule_memory_extraction(state, delivered_text=delivered_text)

    def _schedule_memory_extraction(self, state: _ResponseState, *, delivered_text: str) -> None:
        """后台调度记忆提取（B2 端口）：终态结算后执行，不阻塞 ack/interrupt 返回。"""
        memory = self._memory
        assert memory is not None  # 仅在非 None 分支调用

        async def _run() -> None:
            try:
                await memory.extract_after_turn(
                    elder_id=state.elder_id,
                    thread_id=state.thread_id,
                    turn_id=state.turn_id,
                    user_text=state.user_text,
                    assistant_text=delivered_text,
                )
            except Exception:  # 后台容错：提取失败不回滚终态
                logger.exception("response_memory_extract_failed", extra=self._log_extra(state))

        task = asyncio.create_task(_run())
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_tasks.discard)

    async def _restore_epoch(self, thread_id: str) -> None:
        """进程重启后从 Redis 恢复会话 epoch（P2-9，单进程范围）。"""
        if self._persistence is None or thread_id in self._epochs:
            return
        try:
            raw = await self._persistence.get(f"session:{thread_id}:epoch")
        except Exception as exc:  # 持久化尽力而为
            logger.warning(
                "response_epoch_restore_failed", extra={"error_type": type(exc).__name__}
            )
            return
        if raw is None:
            return
        try:
            restored = int(raw)
        except ValueError:
            logger.warning("response_epoch_restore_malformed", extra={"raw": raw[:32]})
            return
        self._epochs[thread_id] = restored
        logger.info(
            "response_epoch_restored",
            extra={"trace_id": get_trace_id() or "-", "thread_id": thread_id, "epoch": restored},
        )

    async def _persist_epoch(self, thread_id: str) -> None:
        """会话 epoch 回写 Redis（P2-9），键 `session:{thread_id}:epoch`，TTL 2h。"""
        if self._persistence is None:
            return
        try:
            await self._persistence.set(
                f"session:{thread_id}:epoch",
                str(self.session_epoch(thread_id)),
                ex=_PERSIST_TTL_SECONDS,
            )
        except Exception as exc:  # 持久化尽力而为
            logger.warning(
                "response_epoch_persist_failed", extra={"error_type": type(exc).__name__}
            )

    async def _persist_response(self, state: _ResponseState) -> None:
        """response 水位最小字段回写（P2-9），键 `response:{response_id}`，TTL 2h。"""
        if self._persistence is None:
            return
        record = json.dumps(
            {
                "thread_id": state.thread_id,
                "epoch": state.epoch,
                "last_acked_segment": state.last_acked_segment,
                "delivery_status": state.delivery_status,
            },
            ensure_ascii=False,
        )
        try:
            await self._persistence.set(
                f"response:{state.response_id}", record, ex=_PERSIST_TTL_SECONDS
            )
        except Exception as exc:  # 持久化尽力而为
            logger.warning(
                "response_state_persist_failed", extra={"error_type": type(exc).__name__}
            )

    # -------------------------------------------------- 内部工具

    def _snapshot(self, state: _ResponseState) -> ResponseHandle:
        return ResponseHandle(
            response_id=state.response_id,
            epoch=state.epoch,
            thread_id=state.thread_id,
            turn_id=state.turn_id,
            total=len(state.segments),
            segment_id=state.last_acked_segment,
            index=state.last_acked_segment,
            at_ms=state.at_ms,
            delivered_text=state.delivered_text,
            last_played_segment=state.last_played_segment,
            delivery_status=state.delivery_status,
        )

    def _evict_overflow(self) -> None:
        while len(self._responses) > MAX_TRACKED_RESPONSES:
            oldest = next(iter(self._responses))
            self._responses.pop(oldest)

    async def _safe_append_event(
        self, state: _ResponseState, event_type: str, payload: Mapping[str, object]
    ) -> None:
        """事件追加失败不拖垮主链路（与 dialogue_service 写回降级同口径）。

        `CancelledError` 必须重抛（P3-27）：吞掉违反 asyncio 取消传播——取消即
        回合任务被取消（如 ws_superseded），主链路本就该终止，吞掉反而丢失取消
        语义。其余异常仍按尽力而为的观测面处理，不炸穿 ack/interrupt 主链路。
        """
        try:
            await self._events.append_event(
                thread_id=state.thread_id,
                event_type=event_type,
                payload=dict(payload),
                turn_id=state.turn_id,
                response_id=state.response_id,
            )
        except asyncio.CancelledError:
            logger.warning(
                "response_event_append_cancelled",
                extra=self._log_extra(state, event_type=event_type),
            )
            raise
        except Exception as exc:
            logger.error(
                "response_event_append_failed",
                extra=self._log_extra(state, event_type=event_type, error_type=type(exc).__name__),
            )

    @staticmethod
    def _ack_elapsed_ms(state: _ResponseState, segment_id: int) -> float:
        """ack 指标耗时：该段下发 → ack 到达的服务端墙钟差（at_ms 仅客户端口径）。"""
        delivered_at = state.delivered_at.get(segment_id)
        if delivered_at is None:
            return 0.0
        return max((time.monotonic() - delivered_at) * 1000.0, 0.0)

    @staticmethod
    def _log_extra(state: _ResponseState | None, **fields: object) -> dict[str, object]:
        extra: dict[str, object] = {
            "trace_id": get_trace_id() or "-",
            "device_id": state.device_id if state is not None else "-",
        }
        if state is not None:
            extra["thread_id"] = state.thread_id
        extra.update(fields)
        return extra


__all__ = [
    "ACK_CANCELED",
    "ACK_PLAYED",
    "ACK_PLAYING",
    "MAX_SEGMENT_CHARS",
    "MAX_TRACKED_RESPONSES",
    "SEGMENT_SENTINEL",
    "AckRejectedReason",
    "AckStatus",
    "Cancellable",
    "DeliveryStatus",
    "EpochPersistence",
    "EventSink",
    "GenerationResultLike",
    "HistorySettlementPort",
    "MemoryExtractionLike",
    "NullTtsQueue",
    "RedisEpochPersistence",
    "ResponseHandle",
    "ResponsePhase",
    "ResponseService",
    "TtsQueue",
    "TurnContext",
    "split_segments",
]
