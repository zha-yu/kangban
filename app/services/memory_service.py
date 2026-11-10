"""MemoryService 骨架（Day 6 N08，任务卡 §3.3 收口）。

**本版只做两步**：提取（preference 候选）+ 召回（闸门②过滤后返回 active）。
冲突检测、状态迁移（superseded/disputed）、90 天衰减、删除流水、pgvector 向量召回
全部留 Day 7 —— 见技术债清单 docstring 末尾。

任务卡 §3.3 硬性约束：
1. 只处理 category=preference 一类
2. 对话结束后**异步**跑（不阻塞主链，调用方 `asyncio.create_task()` 触发）
3. 入库前三条硬校验：无 source_turn_ids → 拒；generic 身份 → 拒；sensitivity=health/family → 拒
4. 同 (elder_id, subject, predicate) 已有 active 条目 → 值同跳过，值不同**新插一条** + 打日志
   （**不得**静默覆盖旧条目；冲突判定留给 Day 7）
5. 召回强制闸门② elder_id + status=active + 有效期过滤
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("app.services.memory_service")

# ------------------------------------------------------------------ 常量（任务卡 §3.3 锁定）

_ALLOWED_CATEGORIES: frozenset[str] = frozenset({"preference"})
_ALLOWED_SENSITIVITIES: frozenset[str] = frozenset({"normal", "private"})
_BLOCKED_SENSITIVITIES: frozenset[str] = frozenset({"health", "family"})
_ALLOWED_SOURCES: frozenset[str] = frozenset({"elder_explicit", "caregiver_report"})

# 技术债（Day 7 补）：
#   - 冲突五关系判定（supersedes / conflicts_with / disputed 等）
#   - 状态迁移 superseded / disputed（目前只写 active）
#   - 90 天衰减归并（APScheduler 任务）
#   - 删除流水（deleted 状态不是真删除）
#   - pgvector 向量召回（替换 ORDER BY importance DESC）
#   - embedding 生成（可空 vector(1536)，Day 7 接 embedding 模型）


# ------------------------------------------------------------------ 异常


class MemoryValidationError(Exception):
    """候选校验未通过（任务卡 §3.3 第 2 条三条硬校验）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"memory candidate validation failed: {reason}")
        self.reason = reason


class MemoryInsertSkipped(ValueError):
    """同 (elder_id, subject, predicate) 已有 active 条目且值相同 → 跳过（幂等）。"""

    def __init__(self, elder_id: str, subject: str, predicate: str) -> None:
        super().__init__(
            f"memory candidate skipped (duplicate): ({elder_id}, {subject}, {predicate})"
        )
        self.elder_id = elder_id
        self.subject = subject
        self.predicate = predicate


# ------------------------------------------------------------------ 候选结构（任务卡 §3.3 第 1 条 JSON 对齐）


@dataclass(slots=True)
class MemoryCandidate:
    """结构化记忆候选（从对话里抽出来、待入库）。"""

    elder_id: str
    category: Literal["preference"]
    subject: str
    predicate: str
    object_value: str
    polarity: Literal["positive", "negative", "neutral"] = "positive"
    fact: str = ""
    source_turn_ids: list[str] = field(default_factory=list)
    source_type: Literal["elder_explicit", "caregiver_report"] = "elder_explicit"
    confidence: float = 0.5
    sensitivity: Literal["normal", "private", "health", "family"] = "normal"
    valid_from: datetime | None = None


# ------------------------------------------------------------------ 入口


async def extract_preference_candidates(
    *,
    elder_id: str | None,
    is_generic: bool,
    turns: list[dict[str, str]],
) -> list[MemoryCandidate]:
    """**偏好记忆**候选提取（对话结束后异步跑，调用方 `asyncio.create_task()`）。

    Day 6 骨架策略：**不接 LLM / embedding**，只做**模板匹配**——从 user_text 里抓
    "喜欢/爱/偏好/讨厌/不喜欢/爱听/爱看" + 类别词（越剧/京剧/电影/音乐/颜色...）。
    任务卡明确 Day 6 先接"一类低风险偏好记忆"——简单够用，Day 7 接真正的 extraction pipeline。

    Args:
        elder_id: 已确认的长者 ID；generic 模式传 None
        is_generic: 身份未确认（闸门②）
        turns: 近 N 轮对话，每轮 `{"user": "...", "assistant": "..."}`

    Returns:
        MemoryCandidate 列表（可能为空）。generic 模式**不返回任何候选**（闸门②）。
    """
    if is_generic or elder_id is None:
        # 闸门②：generic 身份不写个人记忆
        logger.info(
            "memory_extract_skipped_generic",
            extra={"is_generic": is_generic, "has_elder_id": elder_id is not None},
        )
        return []

    # --- Day 6 骨架：模板匹配 ---
    # 偏好动词 + 类别词简表（讲义 §13.3 "先接一类低风险偏好记忆"）
    _VERBS = ("喜欢", "爱", "偏好", "讨厌", "不喜欢", "爱听", "爱看", "爱吃", "爱穿")
    _CATEGORY_MAP = {
        ("越剧", "京戏", "京剧", "黄梅戏"): ("preferred_opera_genre", "戏曲"),
        ("电影", "影片", "片"): ("preferred_movie_genre", "电影"),
        ("音乐", "歌曲"): ("preferred_music_genre", "音乐"),
        ("红", "红色"): ("preferred_color", "颜色"),
        ("蓝", "蓝色"): ("preferred_color", "颜色"),
        ("粥", "小米粥"): ("preferred_food", "食物"),
        ("软", "硬"): ("preferred_drink_texture", "饮品口感"),
        ("甜", "咸", "辣"): ("preferred_food_taste", "食物口味"),
        ("早起", "早睡"): ("preferred_sleep_habit", "作息"),
        ("散步", "太极拳", "跳舞"): ("preferred_exercise", "运动"),
        ("猫", "狗", "鸟", "鱼"): ("preferred_animal", "动物"),
        ("花", "书", "棋", "画"): ("preferred_hobby", "爱好"),
    }

    candidates: list[MemoryCandidate] = []

    for idx, turn in enumerate(turns):
        user_text = (turn.get("user") or "").strip()
        if not user_text:
            continue
        turn_id = turn.get("turn_id") or f"auto_{idx}"

        for verbs, (predicate, category_label) in _CATEGORY_MAP.items():
            for verb in _VERBS:
                if verb in user_text and any(k in user_text for k in verbs):
                    # 粗抽 value（第一个命中的词）
                    value = next((k for k in verbs if k in user_text), category_label)
                    polarity: Literal["positive", "negative", "neutral"] = (
                        "negative" if ("讨厌" in verb or "不喜欢" in verb) else "positive"
                    )

                    candidate = MemoryCandidate(
                        elder_id=elder_id,
                        category="preference",
                        subject=elder_id,
                        predicate=predicate,
                        object_value=value,
                        polarity=polarity,
                        fact=f"长者{verb}{value}",
                        source_turn_ids=[turn_id],
                        source_type="elder_explicit",
                        confidence=0.8,
                        sensitivity="normal",
                        valid_from=datetime.now(UTC),
                    )
                    candidates.append(candidate)
                    break
            if any(c.predicate == predicate and c.object_value == value for c in candidates):
                break

    # 去重（同 predicate + object_value + polarity + subject 合并 source_turn_ids）
    deduped: list[MemoryCandidate] = []
    seen: dict[tuple[str, str, str, str], MemoryCandidate] = {}
    for c in candidates:
        key = (c.elder_id, c.predicate, c.object_value, c.polarity)
        if key in seen:
            # 合并 source_turn_ids 去重
            existing = seen[key]
            merged_turns = list(dict.fromkeys([*existing.source_turn_ids, *c.source_turn_ids]))
            existing.source_turn_ids = merged_turns
        else:
            seen[key] = c
            deduped.append(c)

    if deduped:
        logger.info(
            "memory_extract_complete",
            extra={"elder_id": elder_id, "candidate_count": len(deduped)},
        )

    return deduped


async def insert_candidate(
    candidate: MemoryCandidate, session: AsyncSession | None = None
) -> dict[str, str]:
    """单条候选入库（先校验、再查重、再 insert）。

    任务卡 §3.3 第 2 条三条硬校验 + 第 3 条同键 diff 处理。
    **不做**冲突判定（superseded/disputed）—— Day 7 补。
    """
    from app.models.memory import MemoryChunk

    # --- 硬校验 ---
    _validate_candidate(candidate)

    # --- 自开会话（若调用方未注入） ---
    own_session = False
    if session is None:
        from app.db import session_factory

        session = session_factory()
        own_session = True

    try:
        # --- 查重：同 (elder_id, subject, predicate) 已有 active 条目？ ---
        existing = (
            (
                await session.execute(
                    select(MemoryChunk).where(
                        MemoryChunk.elder_id == candidate.elder_id,
                        MemoryChunk.subject == candidate.subject,
                        MemoryChunk.predicate == candidate.predicate,
                        MemoryChunk.status == "active",
                    )
                )
            )
            .scalars()
            .first()
        )

        if existing is not None:
            if str(existing.object_value) == candidate.object_value:
                # 值完全相同 → 幂等跳过
                raise MemoryInsertSkipped(
                    candidate.elder_id, candidate.subject, candidate.predicate
                )
            # 值不同 → 新插一条 + 打日志（不覆盖旧条目！）
            logger.info(
                "memory_candidate_pending",
                extra={
                    "elder_id": candidate.elder_id,
                    "subject": candidate.subject,
                    "predicate": candidate.predicate,
                    "old_value": str(existing.object_value),
                    "new_value": candidate.object_value,
                    "note": "conflict judgment deferred to Day 7",
                },
            )

        chunk = MemoryChunk(
            elder_id=candidate.elder_id,
            subject=candidate.subject,
            predicate=candidate.predicate,
            object_value=candidate.object_value,
            category=candidate.category,
            polarity=candidate.polarity,
            content=candidate.fact or f"{candidate.predicate}={candidate.object_value}",
            confidence=candidate.confidence,
            sensitivity=candidate.sensitivity,
            source_type=candidate.source_type,
            source_turn_ids=candidate.source_turn_ids,
            status="active",
            valid_from=candidate.valid_from or datetime.now(UTC),
            embedding=None,  # Day 7 接 pgvector 后填充
        )
        session.add(chunk)
        await session.flush()
        logger.info(
            "memory_chunk_inserted",
            extra={"chunk_id": str(chunk.chunk_id), "elder_id": candidate.elder_id},
        )
    finally:
        if own_session:
            await session.close()

    return {
        "chunk_id": str(chunk.chunk_id),
        "elder_id": candidate.elder_id,
        "subject": candidate.subject,
        "predicate": candidate.predicate,
        "object_value": candidate.object_value,
    }


async def recall_active_chunks(
    *,
    elder_id: str | None,
    is_generic: bool,
    limit: int = 5,
    min_importance: float | None = None,
    session: AsyncSession | None = None,
) -> list[Any]:
    """召回：闸门② + 有效期过滤 → 返回 status='active' 的条目。

    任务卡 §3.3 第 4 条 + PRD FR-MEM-05 闸门②：
        WHERE elder_id=:runtime.elder_id
          AND status='active'
          AND (valid_to IS NULL OR valid_to > now())
    Day 6 骨架：ORDER BY importance DESC；Day 8 接 pgvector 后替换为向量相似度排序，
    届时把 `list[Any]` 收紧为 ORM 行类型。
    """
    from app.models.memory import MemoryChunk

    if is_generic or elder_id is None:
        # 闸门②：generic 模式不返回个人记忆
        return []

    stmt = select(MemoryChunk).where(
        MemoryChunk.elder_id == elder_id,
        MemoryChunk.status == "active",
    )
    # 有效期过滤
    now = datetime.now(UTC)
    stmt = stmt.where((MemoryChunk.valid_to.is_(None)) | (MemoryChunk.valid_to > now))
    if min_importance is not None:
        stmt = stmt.where(MemoryChunk.importance >= min_importance)

    stmt = stmt.order_by(
        MemoryChunk.importance.desc(), MemoryChunk.last_accessed.desc().nullslast()
    )
    stmt = stmt.limit(limit)

    if session is None:
        from app.db import session_factory

        async with session_factory() as s:
            rows = (await s.execute(stmt)).scalars().all()
    else:
        rows = (await session.execute(stmt)).scalars().all()

    return list(rows)


# ------------------------------------------------------------------ 内部


def _validate_candidate(candidate: MemoryCandidate) -> None:
    """任务卡 §3.3 第 2 条三条硬校验。"""
    # 1) 无 source_turn_ids 不入库
    if not candidate.source_turn_ids:
        raise MemoryValidationError("source_turn_ids must be non-empty")

    # 2) category 必须是 preference（Day 6 只做一类）
    if candidate.category not in _ALLOWED_CATEGORIES:
        raise MemoryValidationError(
            f"category '{candidate.category}' not allowed (Day 6 only supports preference)"
        )

    # 3) sensitivity=health/family 不写
    if candidate.sensitivity in _BLOCKED_SENSITIVITIES:
        raise MemoryValidationError(
            f"sensitivity '{candidate.sensitivity}' blocked (health/family not written in Day 6)"
        )

    # 4) polarity / source_type / confidence 范围
    if candidate.polarity not in {"positive", "negative", "neutral"}:
        raise MemoryValidationError(f"polarity '{candidate.polarity}' invalid")
    if candidate.source_type not in _ALLOWED_SOURCES:
        raise MemoryValidationError(f"source_type '{candidate.source_type}' invalid")
    if not (0.0 <= candidate.confidence <= 1.0):
        raise MemoryValidationError(f"confidence {candidate.confidence} out of range [0.0, 1.0]")
    if not candidate.elder_id.strip():
        raise MemoryValidationError("elder_id must be non-empty")
    if not candidate.subject.strip():
        raise MemoryValidationError("subject must be non-empty")
    if not candidate.predicate.strip():
        raise MemoryValidationError("predicate must be non-empty")
    if not candidate.object_value.strip():
        raise MemoryValidationError("object_value must be non-empty")


# ------------------------------------------------------------------ Day 7 B2-3：生产接线


class MemoryExtractionPort(Protocol):
    """B1 回合终态结算持有的记忆提取端口（总收口时由 stub 切换到真实现）。

    B1 侧依赖此 Protocol 解耦：终态结算只调 `extract_after_turn`，
    不感知模板匹配/入库细节；本日按裁决 B1 用 stub，Step 7 统一接真端口。
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


# 后台任务引用集：create_task 的返回值必须持引用，否则可能被 GC（asyncio 文档明确警告）
_BACKGROUND_TASKS: set[asyncio.Task[int]] = set()


async def extract_after_turn(
    *,
    elder_id: str | None,
    thread_id: str,
    turn_id: str,
    user_text: str,
    assistant_text: str,
) -> int:
    """**单轮记忆提取生产入口**（B2-3）：回合结算后调用，提取 preference 候选并入库。

    - generic / 无 elder_id → 直接返回 0（闸门②，不触发任何查询）；
    - 返回**成功入库**的候选数（幂等跳过不计入）；
    - 后台任务纪律：提取/入库失败只记日志（`logger.exception`），**不抛**——
      记忆提取是有意为之的尽力而为，绝不让回合终态因它失败。
    """
    if not elder_id or not elder_id.strip():
        return 0
    try:
        candidates = await extract_preference_candidates(
            elder_id=elder_id,
            is_generic=False,
            turns=[
                {
                    "user": user_text or "",
                    "assistant": assistant_text or "",
                    "turn_id": turn_id,
                }
            ],
        )
    except Exception:  # 后台容错：有意吞掉一切提取异常
        logger.exception(
            "memory_extract_after_turn_failed",
            extra={"thread_id": thread_id, "turn_id": turn_id},
        )
        return 0

    inserted = 0
    for candidate in candidates:
        try:
            await insert_candidate(candidate)
            inserted += 1
        except MemoryInsertSkipped:
            # 同键值同 → 幂等跳过（不算失败，也不计入插入数）
            pass
        except Exception:  # 后台容错：单条失败不影响其余候选
            logger.exception(
                "memory_insert_after_turn_failed",
                extra={
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "predicate": candidate.predicate,
                },
            )
    return inserted


def schedule_memory_extraction(
    *,
    elder_id: str | None,
    thread_id: str,
    turn_id: str,
    user_text: str,
    assistant_text: str,
) -> None:
    """**后台触发**记忆提取（B1 终态结算的调用入口）。

    `asyncio.create_task` 立即返回，不阻塞首包（验收口径：<50ms）；
    任务引用存入模块级集合防 GC，done 回调自动清理。
    """
    task = asyncio.get_running_loop().create_task(
        extract_after_turn(
            elder_id=elder_id,
            thread_id=thread_id,
            turn_id=turn_id,
            user_text=user_text,
            assistant_text=assistant_text,
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def recall_for_turn(
    *,
    elder_id: str | None,
    query: str,
    limit: int = 5,
) -> list[str]:
    """**C1 交接契约**：`memory_recall` 工具的单轮召回入口。

    签名冻结：`recall_for_turn(*, elder_id, query, limit=5) -> list[str]`；
    返回 chunk 文本列表（C1 包成 `ToolResult(data={"notes": [...]})`）。

    - 闸门②：generic / 无 elder_id → 空列表，不触发任何查询；
    - 有效性：强制 `status='active'` + 有效期过滤（复用 `recall_active_chunks`）；
    - `query` 本日不参与排序（pgvector 向量召回留后续迭代），仅作入参占位
      保证 C1 联调签名稳定；
    - 存储故障 → 空列表 + 日志（记忆召回是尽力而为，绝不让回合失败）。
    """
    if not elder_id or not elder_id.strip():
        return []
    try:
        rows = await recall_active_chunks(elder_id=elder_id, is_generic=False, limit=limit)
    except Exception:  # 召回容错：与 extract 后台纪律一致
        logger.exception(
            "memory_recall_for_turn_failed",
            extra={"elder_id": elder_id, "query_len": len(query or "")},
        )
        return []
    return [str(row.content) for row in rows if getattr(row, "content", None)]


__all__ = [
    "MemoryCandidate",
    "MemoryExtractionPort",
    "MemoryInsertSkipped",
    "MemoryValidationError",
    "extract_after_turn",
    "extract_preference_candidates",
    "insert_candidate",
    "recall_active_chunks",
    "recall_for_turn",
    "schedule_memory_extraction",
]
