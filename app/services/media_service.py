"""媒体域服务（Day 8 · C4）。

**media_library / media_history 两表唯一写库入口**（0018 迁移表注释锚定：
种子数据经 :func:`seed_media_library`，点播历史经 :func:`start_playback` /
:func:`finish_playback`；B4 play_opera Tool、A5 规则直连、D4 pad 播放一律
只经本文件取数/写 history，不直连 ORM——TD §3.4 + FR-CON-06 P1）。

X8 冻结口径（《接口契约_照护台业务数据》§6.1，2026-11-11 冻结；同日 v1.3
扩展名放开，依据 docs/plans/adhoc-20261111-媒体素材格式扩展名.md）：
- media_url **只产**稳定路径 ``/static/media/opera/<media_id>.<ext>``，
  ext ∈ wav|mp3 由种子行落库值决定（默认 wav），nginx 静态托管、
  浏览器（pad）直连、不经后端转发；
- 「稳定」语义：路径随 media_id 一次落库，行不变 URL 不变。本服务不提供
  media_url 改写口，热替换只更新 duration 等元数据；素材换格式（wav→mp3）
  时重灌该行 media_url（一条 UPDATE），仍零代码改动。

能力面（任务卡 C4）：列表 / 按剧种（genre）剧目（title）搜索 /
取播放 URL / media_history 写入（播放时长→完播率）。
**不碰** Tool 层（play_opera 归 dev-B B4）、规则层（归 dev-A A5）、
pad 播放（归 dev-D D4）。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.media import MediaHistory, MediaLibrary

logger = logging.getLogger(__name__)

#: X8 口径① 冻结稳定路径（契约 §6.1 v1.3；opera 种子 type=opera，movie 归 P2）
MEDIA_URL_TEMPLATE: Final = "/static/media/opera/{media_id}.{ext}"

#: 稳定路径扩展名值域（契约 §6.1 v1.3：由落库值决定，防自由放任，第三种格式再扩）
MEDIA_EXTS: Final = ("wav", "mp3")

#: media_library.type 两值（0018 CHECK ck_media_library_type 同源）
MEDIA_TYPES: Final = ("opera", "movie")

#: copyright_status 两值（0018 CHECK ck_media_library_copyright_status 同源；
#: 注意契约 §6.2/§6.4 示例写 `teacher_licensed`，DB 枚举实为 `teacher_authorized`，
#: 落库以 TD/迁移为准，偏差已登记待 dev-B 契约留痕）
COPYRIGHT_STATUSES: Final = ("teacher_authorized", "public_domain")


def stable_media_url(media_id: uuid.UUID, ext: str = "wav") -> str:
    """按 X8 口径① 拼稳定播放路径（唯一拼 URL 处，测试逐字断言）。

    ext 值域 wav|mp3（契约 §6.1 v1.3），越界即拒绝——参数错不静默纠偏。
    """
    if ext not in MEDIA_EXTS:
        raise ValueError(f"ext 必须为 {MEDIA_EXTS}，收到 {ext!r}")
    return MEDIA_URL_TEMPLATE.format(media_id=media_id, ext=ext)


# ---------- 种子数据（media_library 唯一写库入口） ----------


def _validate_seed_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """种子行强校验（参数错即拒绝，不静默纠偏；X8 口径④ 值域）。"""
    media_type = item.get("type")
    if media_type not in MEDIA_TYPES:
        raise ValueError(f"type 必须为 {MEDIA_TYPES}，收到 {media_type!r}")
    title = item.get("title")
    if not title:
        raise ValueError("title 必填")
    copyright_status = item.get("copyright_status")
    if copyright_status not in COPYRIGHT_STATUSES:
        raise ValueError(f"copyright_status 必须为 {COPYRIGHT_STATUSES}，收到 {copyright_status!r}")
    ext = item.get("ext", "wav")
    if ext not in MEDIA_EXTS:
        raise ValueError(f"ext 必须为 {MEDIA_EXTS}，收到 {ext!r}")
    return {
        "type": media_type,
        "title": title,
        "genre": item.get("genre"),
        "era": item.get("era"),
        "artist": item.get("artist"),
        "duration": item.get("duration"),
        "copyright_status": copyright_status,
        "ext": ext,
    }


async def seed_media_library(
    session: AsyncSession, items: Sequence[Mapping[str, Any]]
) -> list[MediaLibrary]:
    """批量写入种子媒体行（media_id 服务端生成，media_url 强制稳定路径）。

    种子项可带 ``ext``（默认 ``"wav"``，值域 wav|mp3）决定素材扩展名。

    Returns:
        落库后的 ORM 行列表（含生成的 media_id，调用方据此按落库扩展名生成素材文件）。

    Raises:
        ValueError: type/title/copyright_status/ext 不在值域。
    """
    rows: list[MediaLibrary] = []
    for item in items:
        fields = _validate_seed_item(item)
        media_id = uuid.uuid4()  # 客户端生成：media_url 稳定路径在 INSERT 前即可拼好
        ext = fields.pop("ext")
        row = MediaLibrary(
            media_id=media_id, media_url=stable_media_url(media_id, ext=ext), **fields
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    logger.info("media_library 种子写入 %d 行", len(rows), extra={"trace_id": "-"})
    return rows


# ---------- 取数口（B4 Tool / A5 规则 / D4 pad 只经这里读） ----------


async def list_media(
    session: AsyncSession,
    *,
    media_type: str | None = None,
    genre: str | None = None,
    title: str | None = None,
    limit: int = 50,
) -> list[MediaLibrary]:
    """媒体列表：按类型/剧种（genre）/剧目（title，模糊）过滤，id 升序稳定分页。

    「换一段」（X8 口径③ next_track）取同剧种下一段即：
    ``list_media(genre=当前剧种, title 不限)`` 后由调用方排除当前 media_id。
    """
    stmt = select(MediaLibrary)
    if media_type is not None:
        stmt = stmt.where(MediaLibrary.type == media_type)
    if genre is not None:
        stmt = stmt.where(MediaLibrary.genre == genre)
    if title is not None:
        stmt = stmt.where(MediaLibrary.title.ilike(f"%{title}%"))
    stmt = stmt.order_by(MediaLibrary.media_id.asc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def search_media(
    session: AsyncSession,
    query: str,
    *,
    media_type: str | None = None,
    limit: int = 10,
) -> list[MediaLibrary]:
    """检索词搜索（X8 口径② play_opera 入参口径）：匹配 title/genre/era/artist。

    Returns:
        命中行（无命中返回空列表，上层按 FR-CON-04「提示 + 推荐」，不编造）。
    """
    pattern = f"%{query}%"
    stmt = select(MediaLibrary).where(
        or_(
            MediaLibrary.title.ilike(pattern),
            MediaLibrary.genre.ilike(pattern),
            MediaLibrary.era.ilike(pattern),
            MediaLibrary.artist.ilike(pattern),
        )
    )
    if media_type is not None:
        stmt = stmt.where(MediaLibrary.type == media_type)
    stmt = stmt.order_by(MediaLibrary.media_id.asc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_media(session: AsyncSession, *, media_id: uuid.UUID) -> MediaLibrary | None:
    """按 media_id 取单行（不存在返回 None，调用方如实告知不编造）。"""
    return await session.get(MediaLibrary, media_id)


async def get_playback_url(session: AsyncSession, *, media_id: uuid.UUID) -> str | None:
    """取播放 URL（X8 口径①：只回稳定路径；媒体不存在返回 None）。"""
    row = await get_media(session, media_id=media_id)
    if row is None:
        return None
    return row.media_url


# ---------- 点播历史（media_history 唯一写库入口，FR-CON-06 P1） ----------


async def _require_elder(session: AsyncSession, elder_id: uuid.UUID) -> None:
    from app.models.organization import Elder  # 局部导入避免潜在循环依赖

    exists = (
        await session.execute(select(Elder.elder_id).where(Elder.elder_id == elder_id))
    ).scalar_one_or_none()
    if exists is None:
        raise ValueError(f"elder {elder_id} not found")


async def start_playback(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    media_id: uuid.UUID,
    started_at: datetime.datetime | None = None,
) -> MediaHistory:
    """点播开始：落一条 history 行（finished_at/completion_rate 为 NULL 待回填）。

    Raises:
        ValueError: 长者或媒体不存在（FK 前置友好报错，失败分类「目标不存在」）。
    """
    await _require_elder(session, elder_id)
    if await get_media(session, media_id=media_id) is None:
        raise ValueError(f"media {media_id} not found")
    history = MediaHistory(
        elder_id=elder_id,
        media_id=media_id,
        started_at=started_at or datetime.datetime.now(datetime.UTC),
        finished_at=None,
        completion_rate=None,
    )
    session.add(history)
    await session.flush()
    return history


async def finish_playback(
    session: AsyncSession,
    *,
    history_id: uuid.UUID,
    played_seconds: int,
    finished_at: datetime.datetime | None = None,
) -> MediaHistory:
    """点播结束：回填 finished_at + 完播率 = played_seconds / media.duration。

    - 播放时长经 played_seconds 显式传入（pad 上报口径，比 finished_at-started_at
      更贴近真实播放量——暂停不计时）；
    - 完播率截断到 [0, 1]（0018 CHECK 范围；超长播放按完播计）；
    - 媒体无 duration（元数据缺失）→ completion_rate 置 None，不猜。

    Raises:
        ValueError: history 不存在 / played_seconds 为负 / finished_at 早于 started_at。
    """
    if played_seconds < 0:
        raise ValueError("played_seconds 不能为负")
    history = await session.get(MediaHistory, history_id)
    if history is None:
        raise ValueError(f"media_history {history_id} not found")
    end = finished_at or datetime.datetime.now(datetime.UTC)
    if end < history.started_at:
        raise ValueError("finished_at 不能早于 started_at")
    media = await get_media(session, media_id=history.media_id)
    if media is not None and media.duration:
        history.completion_rate = min(played_seconds / media.duration, 1.0)
    else:
        history.completion_rate = None
    history.finished_at = end
    await session.flush()
    return history


async def list_media_history(
    session: AsyncSession, *, elder_id: uuid.UUID, limit: int = 20
) -> list[MediaHistory]:
    """某长者点播历史（started_at 倒序，走 0018 索引 ix_media_history_elder_id_started_at）。"""
    result = await session.execute(
        select(MediaHistory)
        .where(MediaHistory.elder_id == elder_id)
        .order_by(MediaHistory.started_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


__all__ = [
    "COPYRIGHT_STATUSES",
    "MEDIA_EXTS",
    "MEDIA_TYPES",
    "MEDIA_URL_TEMPLATE",
    "finish_playback",
    "get_media",
    "get_playback_url",
    "list_media",
    "list_media_history",
    "search_media",
    "seed_media_library",
    "stable_media_url",
    "start_playback",
]
