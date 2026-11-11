"""MemoryService 骨架（Day 6 N08，任务卡 §3.3 收口）。

**本版只做两步**：提取（preference 候选）+ 召回（闸门②过滤后返回 active）。
冲突检测、状态迁移（superseded/disputed）、90 天衰减、删除流水、pgvector 向量召回
全部留 Day 7 —— 见技术债清单 docstring 末尾。

任务卡 §3.3 硬性约束：
1. 只处理 category=preference 一类
2. 对话结束后**异步**跑（不阻塞主链，调用方 `asyncio.create_task()` 触发）
3. 入库前三条硬校验：无 source_turn_ids → 拒；generic 身份 → 拒；sensitivity=health/family → 拒
4. 同 (elder_id, subject, predicate) 已有 active 条目 → 值同跳过，值不同**新插一条** + 打日志
   （**不得**静默覆盖旧条目；Day 8 B5 已补冲突检测）
5. 召回强制闸门② elder_id + status=active + 有效期过滤
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("app.services.memory_service")

# ------------------------------------------------------------------ 常量（任务卡 §3.3 锁定）

_ALLOWED_CATEGORIES: frozenset[str] = frozenset({"preference"})
_ALLOWED_SENSITIVITIES: frozenset[str] = frozenset({"normal", "private"})
_BLOCKED_SENSITIVITIES: frozenset[str] = frozenset({"health", "family"})
_ALLOWED_SOURCES: frozenset[str] = frozenset({"elder_explicit", "caregiver_report"})

# --- Day 8 B5 冲突检测常量（计划 Step 3 / TD §3.4 FR-MEM-03）---

#: 相似度阈值：新候选与存量同键条目相似度 > 0.9 才进入语义矛盾判定（LLM 调用），
#: ≤ 0.9 视为可共存偏好（如既喜欢越剧又喜欢京剧），照 Day 6 语义双 active 并存。
_CONFLICT_SIMILARITY_THRESHOLD = 0.9

#: 语义矛盾判定允许的关系取值（TD 口径；none = 不矛盾可共存）
_CONFLICT_RELATIONS: frozenset[str] = frozenset(
    {"supersedes", "disputed", "conflicts_with", "none"}
)

_JUDGE_SYSTEM_PROMPT = (
    "你是长者长期记忆治理助手。输入同一长者同一偏好维度的旧记忆与新陈述，"
    "判断二者的关系，只输出 JSON，不要输出其他内容："
    '{"relation": "supersedes|disputed|conflicts_with|none"}。'
    "supersedes=新陈述明确推翻旧偏好（如「不喜欢X了」），旧记忆应被替代；"
    "disputed=两条矛盾且无法判断谁对；conflicts_with=矛盾但需人工复核；"
    "none=不矛盾，可以共存（如对同类不同项的偏好）。"
)

# --- Day 8 Step 7 B5：90 天衰减 + 每周归并（FR-MEM-07，裁决④ 双接口）---

#: 衰减判定：最后召回时间（无则取创建时间）早于 90 天前即衰减（满 90 天整即触发，`<=` 边界）
_DECAY_INACTIVE_DAYS = 90

#: 单次衰减系数：importance × 0.5，周维护 job 每次执行渐进减半
_DECAY_FACTOR = 0.5

#: importance 衰减下限（列类型 numeric(3,2)）：到达后不再变化，重复执行幂等收敛
_DECAY_FLOOR = Decimal("0.10")

#: 归并判定：同长者同谓词下内容相似度 > 0.9 视为高度相似重复记忆（FR-MEM-07「高度相似」）
_CONSOLIDATION_SIMILARITY_THRESHOLD = 0.9

# 技术债（后续迭代补）：
#   - 删除流水（deleted 状态不是真删除）
#   - pgvector 向量召回（替换 ORDER BY importance DESC）
#   - embedding 生成（可空 vector(1536)，接入后 _text_similarity 占位实现
#     替换为向量余弦相似度，阈值口径不变）


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


# ------------------------------------------------------------------ 冲突检测（Day 8 B5，计划 Step 3）


def _text_similarity(a: str, b: str) -> float:
    """文本相似度占位实现（difflib 序列匹配 + 否定词归一）。

    pgvector 向量召回落地后替换为 embedding 余弦相似度，阈值口径不变。
    归一化剥掉否定词（不/没/别）再比对：「长者喜欢越剧」vs「长者不喜欢越剧了」
    是同一对象的正反陈述，应得高相似度从而触发语义矛盾判定；「越剧」vs「京剧」
    是不同对象，相似度低于阈值走可共存路径。
    """
    from difflib import SequenceMatcher

    negations = ("不", "没", "别")
    a_norm = "".join(ch for ch in " ".join(a.split()) if ch not in negations)
    b_norm = "".join(ch for ch in " ".join(b.split()) if ch not in negations)
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()


_LLM_INSTANCE: Any = None


def _get_llm() -> Any:
    """统一 LLM 适配器懒加载单例（无 key 时 LLMService 自身 degraded=True → R7 降级）。"""
    global _LLM_INSTANCE
    if _LLM_INSTANCE is None:
        from app.core.config import get_settings
        from app.services.llm_service import LLMService

        _LLM_INSTANCE = LLMService(get_settings())
    return _LLM_INSTANCE


async def _judge_conflict_relation(old_content: str, new_content: str) -> str | None:
    """经统一 LLM 适配器判定新旧记忆关系（计划 R7 语义矛盾判定）。

    返回 supersedes / disputed / conflicts_with / none；LLM 异常、降级
    （degraded）或输出解析失败一律返回 None——调用方走 R7 降级：
    保留双版本人工复核，不阻塞写入。
    """
    llm = _get_llm()
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"旧记忆：{old_content}\n新陈述：{new_content}",
                },
            ]
        )
    except Exception:  # 适配器任何异常都归入降级路径
        logger.exception(
            "memory_conflict_judge_failed",
            extra={"old": old_content[:50], "new": new_content[:50]},
        )
        return None
    if getattr(reply, "degraded", True):
        return None
    try:
        relation = str(json.loads(reply.text).get("relation") or "").strip()
    except (ValueError, AttributeError, TypeError):
        return None
    return relation if relation in _CONFLICT_RELATIONS else None


def _normalize_sensitivity(sensitivity: str) -> str:
    """落库边界归一：ORM CHECK 枚举只有 private/health/family（TD §3.4）。

    服务层默认 "normal" 是 Day 6 骨架历史口径（Day 6/7 单测全程 monkeypatch
    未触真库，契约错位从未暴露）；落库前归一为 private。服务层校验保持放行
    "normal"，不动既有调用方。
    """
    return "private" if sensitivity == "normal" else sensitivity


async def insert_candidate(
    candidate: MemoryCandidate, session: AsyncSession | None = None
) -> dict[str, str]:
    """单条候选入库（先校验、再查重、再做冲突检测、再 insert）。

    任务卡 §3.3 第 2 条三条硬校验 + 第 3 条同键 diff 处理。
    Day 8 B5 冲突检测：值不同时先比相似度——≤0.9 可共存双 active；>0.9 交
    LLM 语义判定（计划 R7）：supersedes→旧值让位（superseded）+ 新值生效；
    disputed/conflicts_with→旧值 disputed + 新值 pending_review 人工复核；
    LLM 失败/降级→保留双版本、新值 pending_review，**不阻塞写入**。
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

        # --- 冲突检测（Day 8 B5）：新内容与旧条目同维度但值不同 ---
        new_content = candidate.fact or f"{candidate.predicate}={candidate.object_value}"
        new_status = "active"
        supersedes_id: uuid.UUID | None = None
        conflicts_with_id: uuid.UUID | None = None
        link_back_conflict = False

        if existing is not None:
            if str(existing.object_value) == candidate.object_value:
                # 值完全相同 → 幂等跳过
                raise MemoryInsertSkipped(
                    candidate.elder_id, candidate.subject, candidate.predicate
                )
            similarity = _text_similarity(new_content, str(existing.content))
            if similarity <= _CONFLICT_SIMILARITY_THRESHOLD:
                # 可共存偏好（如既喜欢越剧又喜欢京剧）：照 Day 6 语义双 active 并存
                logger.info(
                    "memory_candidate_coexist_low_similarity",
                    extra={
                        "elder_id": candidate.elder_id,
                        "predicate": candidate.predicate,
                        "old_value": str(existing.object_value),
                        "new_value": candidate.object_value,
                        "similarity": round(similarity, 3),
                    },
                )
            else:
                relation = await _judge_conflict_relation(str(existing.content), new_content)
                now = datetime.now(UTC)
                if relation == "supersedes":
                    # 旧值让位：superseded + valid_to 收口；新值 active + supersedes_id
                    # （当前生效值唯一、两版本均保留——FR-MEM-03）
                    existing.status = "superseded"
                    existing.valid_to = now
                    existing.updated_at = now
                    supersedes_id = existing.chunk_id
                    logger.info(
                        "memory_superseded",
                        extra={
                            "elder_id": candidate.elder_id,
                            "old_chunk_id": str(existing.chunk_id),
                            "old_value": str(existing.object_value),
                            "new_value": candidate.object_value,
                        },
                    )
                elif relation in ("disputed", "conflicts_with"):
                    # 语义矛盾但无法定谁对：旧值 disputed（退出召回），
                    # 新值 pending_review 等人工复核，两版本均保留
                    existing.status = "disputed"
                    existing.updated_at = now
                    conflicts_with_id = existing.chunk_id
                    new_status = "pending_review"
                    link_back_conflict = True
                    logger.info(
                        "memory_disputed_pending_review",
                        extra={
                            "elder_id": candidate.elder_id,
                            "old_chunk_id": str(existing.chunk_id),
                            "relation": relation,
                            "new_value": candidate.object_value,
                        },
                    )
                elif relation == "none":
                    # LLM 判不矛盾：可共存，新插 active，旧条目不动
                    logger.info(
                        "memory_candidate_coexist_judged",
                        extra={
                            "elder_id": candidate.elder_id,
                            "old_value": str(existing.object_value),
                            "new_value": candidate.object_value,
                        },
                    )
                else:
                    # R7 降级：LLM 失败/降级/解析失败（relation is None）
                    # 保留双版本人工复核，不阻塞写入；旧条目保持 active
                    conflicts_with_id = existing.chunk_id
                    new_status = "pending_review"
                    logger.warning(
                        "memory_conflict_review_deferred",
                        extra={
                            "elder_id": candidate.elder_id,
                            "old_chunk_id": str(existing.chunk_id),
                            "old_value": str(existing.object_value),
                            "new_value": candidate.object_value,
                        },
                    )

        chunk = MemoryChunk(
            elder_id=candidate.elder_id,
            subject=candidate.subject,
            predicate=candidate.predicate,
            object_value=candidate.object_value,
            polarity=candidate.polarity,
            content=new_content,
            confidence=candidate.confidence,
            sensitivity=_normalize_sensitivity(candidate.sensitivity),
            # category 是可见性字段（TD §3.4：默认 private，本版无 shared），
            # 不是 Day 6 服务层的域类别（preference）——域维度由 predicate 表达，
            # 透传 candidate.category 会撞 ck_memory_chunks_category CHECK
            category="private",
            source_type=candidate.source_type,
            source_turn_ids=candidate.source_turn_ids,
            status=new_status,
            supersedes_id=supersedes_id,
            conflicts_with_id=conflicts_with_id,
            valid_from=candidate.valid_from or datetime.now(UTC),
            embedding=None,  # pgvector 接入后填充
        )
        session.add(chunk)
        await session.flush()
        if link_back_conflict:
            # 双向冲突指针：新条目落库后回填旧条目的 conflicts_with_id
            existing.conflicts_with_id = chunk.chunk_id  # type: ignore[union-attr]
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
    status='active' 闸门天然排除 superseded / disputed / deleted / pending_review，
    旧值退出、人工复核未决、软删条目均不进召回（Day 8 B5 回归测试固化）。
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


# ------------------------------------------------------------------ 衰减与归并（Day 8 Step 7 B5，FR-MEM-07，裁决④ 方案 A）


def _apply_decay(rows: list[Any], *, now: datetime) -> list[dict[str, Any]]:
    """对满 90 天未召回的条目执行一次 importance 减半（原地改行，不提交）。

    衰减语义：**不改状态、不删内容**——只降 importance，衰减条目在召回排序
    （importance DESC）中自然沉底（TD §3.4「冷热分层依据」）；召回闸门
    （status=active + 有效期）语义不变。importance 到达下限 0.10 后不再变化，
    重复执行幂等收敛。
    """
    decayed: list[dict[str, Any]] = []
    for row in rows:
        current = Decimal(str(row.importance))
        if current <= _DECAY_FLOOR:
            continue  # 已到下限：收敛，重复执行不再产生变更
        target = max(
            _DECAY_FLOOR,
            (current * Decimal(str(_DECAY_FACTOR))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
        )
        row.importance = target
        row.updated_at = now
        record: dict[str, Any] = {
            "chunk_id": str(row.chunk_id),
            "elder_id": str(row.elder_id),
            "importance_before": float(current),
            "importance_after": float(target),
        }
        decayed.append(record)
        logger.info("memory_decay_applied", extra=record)
    return decayed


async def decay_stale(
    *, now: datetime | None = None, session: AsyncSession | None = None
) -> list[dict[str, Any]]:
    """**90 天衰减**（FR-MEM-07，裁决④ 接口①）：周维护 job 每次执行时对
    满足「最后召回（`last_accessed`，无则取 `created_at`）早于 90 天前」的
    active 条目 importance 减半（下限 0.10）。

    事务边界：调用方注入 session 则由调用方控制提交（与 `insert_candidate`
    同形）；未注入则自开会话并自行提交（memory_jobs 走此路径，scheduler 零写库）。

    Returns:
        衰减条目记录列表（chunk_id / elder_id / importance 前后值），
        供 job 层日志汇总与测试断言。
    """
    from app.models.memory import MemoryChunk

    now_dt = now or datetime.now(UTC)
    cutoff = now_dt - timedelta(days=_DECAY_INACTIVE_DAYS)
    stmt = (
        select(MemoryChunk)
        .where(
            MemoryChunk.status == "active",
            func.coalesce(MemoryChunk.last_accessed, MemoryChunk.created_at) <= cutoff,
        )
        .order_by(MemoryChunk.elder_id, MemoryChunk.chunk_id)
    )

    if session is None:
        from app.db import session_factory

        async with session_factory() as own:
            rows = (await own.execute(stmt)).scalars().all()
            decayed = _apply_decay(list(rows), now=now_dt)
            await own.commit()
    else:
        rows = (await session.execute(stmt)).scalars().all()
        decayed = _apply_decay(list(rows), now=now_dt)
    return decayed


def _pick_keeper(left: Any, right: Any) -> tuple[Any, Any]:
    """归并保留侧选择：importance 高者保留；并列取创建时间新者；再并列取左（查询序稳定）。"""
    left_importance = Decimal(str(left.importance))
    right_importance = Decimal(str(right.importance))
    if left_importance != right_importance:
        return (left, right) if left_importance > right_importance else (right, left)
    epoch = datetime.min.replace(tzinfo=UTC)
    left_created = left.created_at or epoch
    right_created = right.created_at or epoch
    if left_created != right_created:
        return (left, right) if left_created > right_created else (right, left)
    return left, right


def _consolidate_rows(rows: list[Any], *, now: datetime) -> list[dict[str, Any]]:
    """对同 (elder_id, predicate) 分组内两两高度相似（>0.9）的 active 条目执行归并。

    归并语义（FR-MEM-07，TD §3.6 迁移批次明确「记忆衰减归并不新增表」）：
    保留 importance 高（并列取新）的一条生效；另一条标 `superseded` +
    `valid_to` 收口退出召回，两版本均保留；保留条 `supersedes_id` 指向被
    归并条（既有指针不覆盖，保持原版本链）。**归并记录** = 结构化日志
    `memory_consolidated` + `supersedes_id` 链 + 本函数返回值（job 层汇总日志）。
    """
    groups: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        groups.setdefault((str(row.elder_id), str(row.predicate)), []).append(row)

    records: list[dict[str, Any]] = []
    for elder_key, predicate_key in sorted(groups):
        members = groups[(elder_key, predicate_key)]
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                left, right = members[i], members[j]
                if left.status != "active" or right.status != "active":
                    continue  # 本轮已被归并的条目不再参与后续配对
                similarity = _text_similarity(str(left.content), str(right.content))
                if similarity <= _CONSOLIDATION_SIMILARITY_THRESHOLD:
                    continue
                keeper, merged = _pick_keeper(left, right)
                merged.status = "superseded"
                merged.valid_to = now
                merged.updated_at = now
                if keeper.supersedes_id is None:
                    keeper.supersedes_id = merged.chunk_id
                keeper.updated_at = now
                record: dict[str, Any] = {
                    "kept_chunk_id": str(keeper.chunk_id),
                    "merged_chunk_id": str(merged.chunk_id),
                    "elder_id": elder_key,
                    "predicate": predicate_key,
                    "similarity": round(similarity, 3),
                }
                records.append(record)
                logger.info("memory_consolidated", extra=record)
    return records


async def consolidate_similar(
    *, now: datetime | None = None, session: AsyncSession | None = None
) -> list[dict[str, Any]]:
    """**每周归并**（FR-MEM-07，裁决④ 接口②）：对同长者同谓词下内容高度
    相似（相似度 >0.9，复用 `_text_similarity`）的重复记忆执行归并，输出
    归并记录（保留条生效、重复条 superseded、两版本均保留）。

    事务边界与 `decay_stale` 一致：注入 session 由调用方提交，否则自开自提
    （memory_jobs 走后者）。状态条件扫描天然幂等：归并后旧条目退出 active，
    重复执行不产生重复变更。

    Returns:
        归并记录列表（kept/merged chunk_id、similarity 等）。
    """
    from app.models.memory import MemoryChunk

    now_dt = now or datetime.now(UTC)
    stmt = (
        select(MemoryChunk)
        .where(MemoryChunk.status == "active")
        .order_by(MemoryChunk.elder_id, MemoryChunk.predicate, MemoryChunk.chunk_id)
    )

    if session is None:
        from app.db import session_factory

        async with session_factory() as own:
            rows = (await own.execute(stmt)).scalars().all()
            records = _consolidate_rows(list(rows), now=now_dt)
            await own.commit()
    else:
        rows = (await session.execute(stmt)).scalars().all()
        records = _consolidate_rows(list(rows), now=now_dt)
    return records


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
    "consolidate_similar",
    "decay_stale",
    "extract_after_turn",
    "extract_preference_candidates",
    "insert_candidate",
    "recall_active_chunks",
    "recall_for_turn",
    "schedule_memory_extraction",
]
