"""media_service（C4 · 媒体唯一写库层）单元测试。

覆盖口径（任务卡 C4 + 计划 S5）：
- 列表 / 按剧种（genre）剧目（title）搜索（X8 口径② 检索词匹配
  title/genre/era/artist）；
- media_url 只产 X8 冻结稳定路径 `/static/media/opera/<media_id>.wav`
  （口径①：种子行强制回填、get_playback_url 逐字等于模板）；
- history 写入含播放时长→完播率（FR-CON-06 P1：截断 [0,1]、无 duration 不猜）；
- 目标不存在：长者/媒体/历史行缺 → ValueError（失败分类「如实告知不编造」）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.media import MediaLibrary
from app.models.organization import Elder
from app.services import media_service

_OPERA_SEEDS: list[dict] = [
    {
        "type": "opera",
        "title": "梁山伯与祝英台",
        "genre": "越剧",
        "era": "1950s",
        "artist": "范瑞娟",
        "duration": 60,
        "copyright_status": "teacher_authorized",
    },
    {
        "type": "opera",
        "title": "红楼梦",
        "genre": "越剧",
        "era": "1960s",
        "artist": "徐玉兰",
        "duration": 45,
        "copyright_status": "teacher_authorized",
    },
    {
        "type": "opera",
        "title": "霸王别姬",
        "genre": "京剧",
        "era": "1950s",
        "artist": "梅兰芳",
        "duration": 30,
        "copyright_status": "public_domain",
    },
    {
        "type": "opera",
        "title": "贵妃醉酒",
        "genre": "京剧",
        "era": None,
        "artist": None,
        "duration": None,
        "copyright_status": "public_domain",
    },
    {
        "type": "opera",
        "title": "天仙配",
        "genre": "黄梅戏",
        "era": "1950s",
        "artist": "严凤英",
        "duration": 20,
        "copyright_status": "teacher_authorized",
    },
]


@dataclass(frozen=True)
class MediaWorld:
    elder_id: uuid.UUID
    by_title: dict[str, MediaLibrary]  # title → 落库行（含生成的 media_id）


@pytest_asyncio.fixture(loop_scope="module")
async def world(db_conn) -> AsyncIterator[MediaWorld]:
    session = AsyncSession(db_conn)
    elder = Elder(name="长者-媒体S5", status="active")
    session.add(elder)
    await session.flush()
    rows = await media_service.seed_media_library(session, _OPERA_SEEDS)
    try:
        yield MediaWorld(elder_id=elder.elder_id, by_title={r.title: r for r in rows})
    finally:
        await session.close()


def _utc(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime.datetime:
    return datetime.datetime(y, mo, d, h, mi, tzinfo=datetime.UTC)


async def test_seed_forces_stable_media_url(world: MediaWorld, db_conn) -> None:
    """X8 口径①：种子行 media_url 逐条等于稳定路径模板，不允许其他形态。"""
    session = AsyncSession(db_conn)
    rows = await media_service.list_media(session, media_type="opera", limit=100)
    assert len(rows) == len(_OPERA_SEEDS)
    for row in rows:
        assert row.media_url == f"/static/media/opera/{row.media_id}.wav"
    await session.rollback()


async def test_seed_rejects_invalid_enum(world: MediaWorld, db_conn) -> None:
    """种子校验：type/copyright_status 越出 0018 CHECK 值域即拒绝。"""
    session = AsyncSession(db_conn)
    with pytest.raises(ValueError, match="type"):
        await media_service.seed_media_library(
            session, [{"type": "song", "title": "x", "copyright_status": "public_domain"}]
        )
    with pytest.raises(ValueError, match="copyright_status"):
        await media_service.seed_media_library(
            session, [{"type": "opera", "title": "x", "copyright_status": "teacher_licensed"}]
        )
    await session.rollback()


async def test_list_media_filters_genre_and_title(world: MediaWorld, db_conn) -> None:
    """列表：按剧种过滤 / 按剧目模糊过滤（X8 口径③ next_track 取同剧种即用 genre）。"""
    session = AsyncSession(db_conn)
    yueju = await media_service.list_media(session, genre="越剧")
    assert sorted(r.title for r in yueju) == sorted(["红楼梦", "梁山伯与祝英台"])
    jingju = await media_service.list_media(session, genre="京剧")
    assert sorted(r.title for r in jingju) == sorted(["霸王别姬", "贵妃醉酒"])
    hlm = await media_service.list_media(session, title="红楼")
    assert [r.title for r in hlm] == ["红楼梦"]
    await session.rollback()


async def test_search_media_matches_all_four_columns(world: MediaWorld, db_conn) -> None:
    """X8 口径②：检索词匹配 title/genre/era/artist 任一列。"""
    session = AsyncSession(db_conn)
    by_title = await media_service.search_media(session, "梁山伯")
    assert [r.title for r in by_title] == ["梁山伯与祝英台"]
    by_genre = {r.title for r in await media_service.search_media(session, "京剧")}
    assert by_genre == {"霸王别姬", "贵妃醉酒"}
    by_era = {r.title for r in await media_service.search_media(session, "1960s")}
    assert by_era == {"红楼梦"}
    by_artist = {r.title for r in await media_service.search_media(session, "严凤英")}
    assert by_artist == {"天仙配"}
    assert await media_service.search_media(session, "不存在曲目xyz") == []
    await session.rollback()


async def test_get_playback_url_exact_template_or_none(world: MediaWorld, db_conn) -> None:
    """取播放 URL：存在 → 逐字等于稳定路径；不存在 → None（不编造）。"""
    session = AsyncSession(db_conn)
    row = world.by_title["天仙配"]
    url = await media_service.get_playback_url(session, media_id=row.media_id)
    assert url == f"/static/media/opera/{row.media_id}.wav"
    assert await media_service.get_playback_url(session, media_id=uuid.uuid4()) is None
    await session.rollback()


async def test_playback_history_completion_rate(world: MediaWorld, db_conn) -> None:
    """FR-CON-06：start→finish 回填 finished_at + 完播率（30/60=0.5）。"""
    session = AsyncSession(db_conn)
    row = world.by_title["梁山伯与祝英台"]  # duration=60
    started = _utc(2026, 11, 11, 10, 0)
    history = await media_service.start_playback(
        session, elder_id=world.elder_id, media_id=row.media_id, started_at=started
    )
    assert history.finished_at is None and history.completion_rate is None
    finished = await media_service.finish_playback(
        session,
        history_id=history.history_id,
        played_seconds=30,
        finished_at=_utc(2026, 11, 11, 10, 1),
    )
    assert finished.finished_at == _utc(2026, 11, 11, 10, 1)
    assert float(finished.completion_rate) == pytest.approx(0.5)

    hist = await media_service.list_media_history(session, elder_id=world.elder_id)
    assert [h.history_id for h in hist] == [history.history_id]
    await session.rollback()


async def test_completion_rate_clamped_and_missing_duration(world: MediaWorld, db_conn) -> None:
    """完播率截断 [0,1]（超长按完播）；媒体无 duration → None 不猜。"""
    session = AsyncSession(db_conn)
    long_row = world.by_title["红楼梦"]  # duration=45
    h1 = await media_service.start_playback(
        session, elder_id=world.elder_id, media_id=long_row.media_id
    )
    done = await media_service.finish_playback(
        session, history_id=h1.history_id, played_seconds=120
    )
    assert float(done.completion_rate) == 1.0

    no_dur = world.by_title["贵妃醉酒"]  # duration=None
    h2 = await media_service.start_playback(
        session, elder_id=world.elder_id, media_id=no_dur.media_id
    )
    done2 = await media_service.finish_playback(
        session, history_id=h2.history_id, played_seconds=10
    )
    assert done2.completion_rate is None
    await session.rollback()


async def test_finish_playback_validation_errors(world: MediaWorld, db_conn) -> None:
    """结束回填校验：history 不存在 / 负时长 / finished_at 早于 started_at。"""
    session = AsyncSession(db_conn)
    row = world.by_title["天仙配"]
    h = await media_service.start_playback(
        session, elder_id=world.elder_id, media_id=row.media_id, started_at=_utc(2026, 11, 11, 9)
    )
    with pytest.raises(ValueError, match="not found"):
        await media_service.finish_playback(session, history_id=uuid.uuid4(), played_seconds=5)
    with pytest.raises(ValueError, match="为负"):
        await media_service.finish_playback(session, history_id=h.history_id, played_seconds=-1)
    with pytest.raises(ValueError, match="早于"):
        await media_service.finish_playback(
            session, history_id=h.history_id, played_seconds=5, finished_at=_utc(2026, 11, 11, 8)
        )
    await session.rollback()


async def test_start_playback_missing_targets(world: MediaWorld, db_conn) -> None:
    """目标不存在：长者/媒体缺 → ValueError，不写半截 history。"""
    session = AsyncSession(db_conn)
    row = world.by_title["霸王别姬"]
    with pytest.raises(ValueError, match="elder"):
        await media_service.start_playback(session, elder_id=uuid.uuid4(), media_id=row.media_id)
    with pytest.raises(ValueError, match="media"):
        await media_service.start_playback(session, elder_id=world.elder_id, media_id=uuid.uuid4())
    assert await media_service.list_media_history(session, elder_id=world.elder_id) == []
    await session.rollback()


async def test_seed_rejects_missing_title(world: MediaWorld, db_conn) -> None:
    session = AsyncSession(db_conn)
    with pytest.raises(ValueError, match="title"):
        await media_service.seed_media_library(
            session, [{"type": "opera", "title": "", "copyright_status": "public_domain"}]
        )
    await session.rollback()
