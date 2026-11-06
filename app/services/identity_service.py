"""Day 4 T8 · 身份级联编排 — identity_service.py（闸门①落点）。

职责（沿 task-01 T8 验收矩阵）：
    1. 候选集恒为设备绑定长者 1v1（裁决 v2.3）
    2. 每会话首轮强制校验：有音频 → voiceprint_service.cosine_compare → voice 命中 / 主动确认
    3. 容器不可用 → 主动确认（reason=vp_unavailable）
    4. 无音频首轮 → 直接主动确认（reason=no_audio）
    5. conversation_threads 闸门①：身份确定后事务内 INSERT，thread_id 服务端生成
    6. 返回结构化 IdentityDecision，**不写 WS 消息发送**（A2 signaling.py 写作域）

分层硬约束（AGENTS §2）：
    - 本模块只 import voiceprint_service（比对）+ ORM（唯一写库层）
    - 不 import FastAPI（REST 层）、不 import signaling.py（A2 写作域）、不直接 httpx
    - 所有 service 异常内部消化 → 只产出结构化决策（reason 枚举 / identity_source）
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.conversation import ConversationThread
from app.models.organization import Device, Elder
from app.services import voiceprint_service as vs

logger = logging.getLogger("identity_service")

IdentitySource = Literal["device", "voice", "confirm", "generic"]
ConfirmReason = Literal["low_confidence", "vp_unavailable", "no_audio", "elder_unbound"]


# -----------------------------------------------------------
# 输出：身份决策（消费方：A2 对话主链 / REST 层）
# -----------------------------------------------------------


@dataclass
class IdentityDecision:
    """一次身份校验的结构化结果。

    **三类终态**：
      - voice/device/confirm → elder_id 已确定（会话可继续走记忆/健康/用药）
      - generic → elder_id=None，下游必须跳过所有 elder 绑定功能
    """

    source: IdentitySource
    elder_id: uuid.UUID | None
    thread_id: str | None  # 终态时必产生（闸门① INSERT 后）；需 confirm 时为 None
    reason: ConfirmReason | None = None  # source=confirm/generic 时填；终态不填
    confidence: float | None = None  # source=voice 时 cosine 值；其他终态 None
    started_at: datetime | None = None  # thread.started_at（供下游日志/trace 用）
    session_seq: int | None = None  # 终态时：personal 用 Elders.session_seq 新值；generic=0

    # 便捷属性
    @property
    def confirmed(self) -> bool:
        """闸门①已落库且 elder_id 非空（voice/device/confirm 终态）。

        用 thread_id 是否存在而非 source 判断：主动确认阶段（thread_id=None）
        虽然 source=confirm 且 elder_id 已拿到候选，但 thread 没落库前下游不得用
        elder_id 做任何写操作（AGENTS §4.9 身份只从服务端注入 + 闸门①约束）。
        """
        return self.thread_id is not None and self.elder_id is not None

    @property
    def needs_confirm(self) -> bool:
        """是否需要向前端发 identity_ask 等待 identity_confirm。

        双条件：source=confirm（处于主动确认分支）AND thread_id=None（闸门①
        未落地，还在等用户点「是我」）。open_thread_from_confirm 返回的终态 confirm
        source 也是 confirm，但 thread_id 已存在 → needs_confirm=False（不再需要 ask）。
        """
        return self.source == "confirm" and self.thread_id is None


# -----------------------------------------------------------
# 输入：候选集 + 首轮校验
# -----------------------------------------------------------


async def resolve_device_elder(session: AsyncSession, device_id: uuid.UUID) -> uuid.UUID | None:
    """候选集获取：设备 → 绑定长者（1v1，裁决 v2.3）。None 表示未绑定。"""
    row = (
        await session.execute(select(Device.elder_id).where(Device.device_id == device_id))
    ).scalar_one_or_none()
    if row is None:
        # row 可能是 None（elder_id 为 NULL）或 Device 行不存在
        # scalar_one_or_none() 返回 None 在行不存在 + 列为 NULL 两种情况 —— 但 DB 不会让
        # device_id 找不到就跑过来（voiceprint 注册路径已在 REST 层拦 device_unbound），
        # 这里兜底防行被删除的 race。
        return None
    return row  # scalar_one_or_none() 已经是 Device.elder_id 列值（UUID | None）


# -----------------------------------------------------------
# thread_id 生成 + 闸门① INSERT
# -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _OpenedThread:
    """一次 INSERT 的结果：thread 行 + 本次会话序号（generic 为 0）。"""

    row: ConversationThread
    session_seq: int


def _new_generic_thread_id() -> str:
    """generic 无 elder，无法用 (elder_id, session_seq) 拼；用服务端 UUID hex 保唯一。"""
    return uuid.uuid4().hex


async def _open_thread(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    elder_id: uuid.UUID | None,
    identity_source: IdentitySource,
    confidence: float | None,
) -> _OpenedThread:
    """事务内 INSERT conversation_threads（闸门①唯一落点）。

    personal thread：同一事务内 `Elders.session_seq + 1` 并 `RETURNING`，thread_id 用
    `(elder_id, session_seq)` 生成；generic 无 elder，session_seq=0、thread_id 用 UUID hex。
    序号增量与 INSERT 在同一个 commit 里完成，避免并发下两个 thread 拿到同一 seq。
    """
    if elder_id is not None:
        result = await session.execute(
            update(Elder)
            .where(Elder.elder_id == elder_id)
            .values(session_seq=Elder.session_seq + 1)
            .returning(Elder.session_seq)
        )
        session_seq = int(result.scalar_one())
        thread_id = f"{elder_id}:{session_seq}"
    else:
        session_seq = 0
        thread_id = _new_generic_thread_id()

    row = ConversationThread(
        thread_id=thread_id,
        elder_id=elder_id,
        device_id=device_id,
        identity_source=identity_source,
        identity_confidence=(Decimal(str(confidence)) if confidence is not None else None),
    )
    session.add(row)
    await session.commit()
    logger.info(
        "identity thread opened",
        extra={
            "thread_id": thread_id,
            "device_id": str(device_id),
            "elder_id": str(elder_id) if elder_id else None,
            "source": identity_source,
            "confidence": confidence,
            "session_seq": session_seq,
        },
    )
    return _OpenedThread(row=row, session_seq=session_seq)


async def _close_active_thread(session: AsyncSession, *, device_id: uuid.UUID) -> None:
    """同一设备当前未结束 thread 回填 ended_at（换人/退出发起新 thread 前调）。"""

    await session.execute(
        update(ConversationThread)
        .where(
            ConversationThread.device_id == device_id,
            ConversationThread.ended_at.is_(None),
        )
        .values(ended_at=datetime.now(UTC))
    )
    await session.commit()


# -----------------------------------------------------------
# 公开入口：首轮身份校验
# -----------------------------------------------------------


async def verify_session_identity(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    audio_bytes: bytes | None = None,
) -> IdentityDecision:
    """会话首轮：强制校验身份（裁决 v2.3）。

    **前置条件**：设备已从 JWT 解出 device_id；调用方（A2 对话主链）拿到 decision 后
    决定是否发 identity_ask、如何组装 initial greeting、以及 thread 闸门已否已就位。

    返回三类结果：
      - **终态**（source=voice/device）：elder_id 已定，thread_id 已生成
      - **终态**（source=generic）：elder_id=None，thread_id 已生成（下游跳过 elder 绑定功能）
      - **待确认**（source=confirm + reason）：需要发 identity_ask 等用户 identity_confirm；
        此时 thread 不 INSERT（等 confirm 到达再调 open_thread_from_confirm）
    """
    # 1. 候选集：恒 1 位（裁决 v2.3）
    elder_id = await resolve_device_elder(session, device_id)

    # 新会话首轮：先结束该设备所有未结束 thread，避免重连/重开后同设备出现多个
    # 同时 active 的 conversation_threads（换人/退房同理，由 recheck 处理）。
    await _close_active_thread(session, device_id=device_id)

    if elder_id is None:
        # 设备未绑长者 → generic（不挑时间点，直接降级）
        opened = await _open_thread(
            session,
            device_id=device_id,
            elder_id=None,
            identity_source="generic",
            confidence=None,
        )
        return IdentityDecision(
            source="generic",
            elder_id=None,
            thread_id=opened.row.thread_id,
            reason="elder_unbound",
            started_at=opened.row.started_at,
            session_seq=opened.session_seq,
        )

    # 2. 有音频 → 走声纹比对
    if audio_bytes is not None and len(audio_bytes) > 0:
        try:
            match = await vs.cosine_compare_against_elder(session, elder_id, audio_bytes)
        except vs.VoiceprintExtractUnavailable:
            # 推理容器不可用 → 主动确认（C5 reason=vp_unavailable）
            logger.warning("vp 容器不可用，降级主动确认（device=%s）", device_id)
            return IdentityDecision(
                source="confirm",
                elder_id=elder_id,
                thread_id=None,
                reason="vp_unavailable",
            )

        if match.matched:
            # 命中 → 终态 voice
            opened = await _open_thread(
                session,
                device_id=device_id,
                elder_id=elder_id,
                identity_source="voice",
                confidence=match.score,
            )
            return IdentityDecision(
                source="voice",
                elder_id=elder_id,
                thread_id=opened.row.thread_id,
                confidence=match.score,
                started_at=opened.row.started_at,
                session_seq=opened.session_seq,
            )
        else:
            # 低置信 → 主动确认（C5 reason=low_confidence）
            logger.info(
                "vp 低置信，请求确认（score=%.4f < %.2f, elder=%s）",
                match.score,
                get_settings().voiceprint_similarity_threshold,
                elder_id,
            )
            return IdentityDecision(
                source="confirm",
                elder_id=elder_id,
                thread_id=None,
                reason="low_confidence",
            )

    # 3. 无音频首轮（pad 刚上线、用户还没开口）→ 主动确认（reason=no_audio）
    logger.info("无音频首轮，请求确认（device=%s, elder=%s）", device_id, elder_id)
    return IdentityDecision(
        source="confirm",
        elder_id=elder_id,
        thread_id=None,
        reason="no_audio",
    )


# -----------------------------------------------------------
# 公开入口：主动确认到达后终态落 thread
# -----------------------------------------------------------


async def open_thread_from_confirm(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    confirmed_elder_id: uuid.UUID,
    candidate_elder_id: uuid.UUID,  # 候选集恒 1 位（identity_ask.candidates[0].elder_id）
) -> IdentityDecision:
    """用户点了「是我」大按钮（C5 identity_confirm 到达）→ 闸门① INSERT。

    关键判定（C5 开放点裁决 3）：**confirm 的 elder_id 必须等于候选集那 1 位**，否则
    一律按 generic 处理（不入库 elder_id，也不报错）。这是前端伪造 elder_id 的防线。
    """
    if str(confirmed_elder_id) != str(candidate_elder_id):
        logger.warning(
            "identity_confirm elder_id 不匹配候选集，降级 generic（confirm=%s, expected=%s）",
            confirmed_elder_id,
            candidate_elder_id,
        )
        await _close_active_thread(session, device_id=device_id)
        opened = await _open_thread(
            session,
            device_id=device_id,
            elder_id=None,
            identity_source="generic",
            confidence=None,
        )
        return IdentityDecision(
            source="generic",
            elder_id=None,
            thread_id=opened.row.thread_id,
            reason="elder_unbound",
            started_at=opened.row.started_at,
            session_seq=opened.session_seq,
        )

    await _close_active_thread(session, device_id=device_id)
    opened = await _open_thread(
        session,
        device_id=device_id,
        elder_id=confirmed_elder_id,
        identity_source="confirm",
        confidence=None,
    )
    return IdentityDecision(
        source="confirm",
        elder_id=confirmed_elder_id,
        thread_id=opened.row.thread_id,
        started_at=opened.row.started_at,
        session_seq=opened.session_seq,
    )


async def open_thread_as_generic_on_timeout(
    session: AsyncSession, *, device_id: uuid.UUID
) -> IdentityDecision:
    """主动确认 30s 超时 → generic（C5 开放点裁决 2）。"""
    await _close_active_thread(session, device_id=device_id)
    opened = await _open_thread(
        session,
        device_id=device_id,
        elder_id=None,
        identity_source="generic",
        confidence=None,
    )
    return IdentityDecision(
        source="generic",
        elder_id=None,
        thread_id=opened.row.thread_id,
        reason="elder_unbound",
        started_at=opened.row.started_at,
        session_seq=opened.session_seq,
    )


# -----------------------------------------------------------
# 公开入口：静默复检（会话内后续 utterance）
# -----------------------------------------------------------


async def recheck_session_identity(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    current_thread_id: str,
    audio_bytes: bytes,
) -> IdentityDecision | None:
    """每 utterance 后复检一次（裁决 v2.3 静默复检）。

    1v1 候选集下「换人」只有两种可判定结果：
      - generic 会话命中绑定长者 → 升级 voice，结束旧 generic thread、开新 thread；
      - 已识别会话低于阈值 → 无法确认是绑定长者，结束旧 thread 并返回
        `confirm/low_confidence`，由 A2 发 identity_ask（FR-VPR-02/03）。
    命中同一人 / generic 未命中 / vp 容器不可用 → 返回 None，不打断对话。
    """
    # 1. 找到当前 thread
    current_thread = (
        await session.execute(
            select(ConversationThread).where(
                ConversationThread.thread_id == current_thread_id,
                ConversationThread.device_id == device_id,
                ConversationThread.ended_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if current_thread is None:
        logger.warning(
            "recheck：当前 thread %s 不存在或已结束（device=%s）",
            current_thread_id,
            device_id,
        )
        return None

    # 2. 比对新音频
    elder_id = await resolve_device_elder(session, device_id)
    if elder_id is None:
        return None  # 未绑定设备：无可比对的候选

    try:
        match = await vs.cosine_compare_against_elder(session, elder_id, audio_bytes)
    except vs.VoiceprintExtractUnavailable:
        # 容器不可用不改变当前 thread；保留现有身份继续对话
        logger.warning("recheck：vp 容器不可用，跳过静默复检")
        return None

    if match.matched:
        # 命中同一人 → 不打断
        if current_thread.elder_id is not None and str(match.elder_id) == str(
            current_thread.elder_id
        ):
            return None

        # generic → voice（或当前 thread 与候选不一致）：开新 voice thread
        await _close_active_thread(session, device_id=device_id)
        logger.info(
            "recheck：命中绑定长者，切 voice thread（old_elder=%s → new_elder=%s）",
            current_thread.elder_id,
            match.elder_id,
        )
        new_opened = await _open_thread(
            session,
            device_id=device_id,
            elder_id=match.elder_id,
            identity_source="voice",
            confidence=match.score,
        )
        return IdentityDecision(
            source="voice",
            elder_id=match.elder_id,
            thread_id=new_opened.row.thread_id,
            confidence=match.score,
            started_at=new_opened.row.started_at,
            session_seq=new_opened.session_seq,
        )

    # 未命中且当前是 generic：继续通用模式，不主动打断
    if current_thread.elder_id is None:
        return None

    # 已识别长者说话人低于阈值：结束旧 thread，转主动确认（不再静默继续个人上下文）
    await _close_active_thread(session, device_id=device_id)
    logger.info(
        "recheck：低置信，结束旧 thread 并转主动确认（score=%.4f < %.2f, elder=%s）",
        match.score,
        get_settings().voiceprint_similarity_threshold,
        elder_id,
    )
    return IdentityDecision(
        source="confirm",
        elder_id=elder_id,
        thread_id=None,
        reason="low_confidence",
    )


# -----------------------------------------------------------
# 公开入口：thread 合法性校验（REST 层拦截伪造的守门函数）
# -----------------------------------------------------------


async def validate_thread_belongs_to_device(
    session: AsyncSession,
    *,
    thread_id: str,
    device_id: uuid.UUID,
) -> bool:
    """判断某 thread 是否属于某 device（rest 层鉴权用）。

    **thread_id 伪造**时返回 False（调用方映射为 403）。identity_service 不抛
    403，因为它不是 HTTP 层——REST 层统一处理（task-01 T8-6 验收矩阵）。
    """
    row = (
        await session.execute(
            select(ConversationThread).where(
                ConversationThread.thread_id == thread_id,
                ConversationThread.device_id == device_id,
            )
        )
    ).scalar_one_or_none()
    return row is not None
