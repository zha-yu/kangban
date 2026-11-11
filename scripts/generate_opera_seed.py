"""戏曲素材与 media_library 种子生成（Day 8 Step 5 / B4，X8 口径①④）。

产出：
1. ``static/media/opera/<media_id>.wav`` —— 程序生成的短 wav 素材
   （正弦波 + 包络，单声道 8kHz 16bit，约 3s，几十 KB/个；mock 音频，
   X8 ④：duration 元数据按 15–60s 值域标注「应当」的选段时长）；
2. ``media_library`` 种子 20–30 条（越剧/京剧/黄梅戏/豫剧/评剧 ≥4 剧种，
   年代 1950s–1990s，真实著名演员与经典剧目名，值域照 X8 规范④）。

用法：
    uv run python scripts/generate_opera_seed.py            # wav + 落库
    uv run python scripts/generate_opera_seed.py --wav-only # 只生成 wav，不动库

幂等：media_id 由标题 uuid5 确定性派生（同名跳过，重复运行零副作用）；
落库走 ORM（maintenance 脚本直连，与 scripts/seed_test_data.py 同先例）——
C4 media_service.seed_media_library 交付后，本脚本落库段切换为调用该入口。
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import struct
import sys
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

MEDIA_DIR = REPO_ROOT / "static" / "media" / "opera"
NAMESPACE_OPERA = uuid.uuid5(uuid.NAMESPACE_URL, "https://kangban.local/static/media/opera")

#: X8 规范④的种子数据：title / genre / era / artist / duration(15–60s) / copyright
SEEDS: list[dict[str, object]] = [
    {
        "title": "梁山伯与祝英台·楼台会",
        "genre": "越剧",
        "era": "1950s",
        "artist": "袁雪芬",
        "duration": 42,
        "copyright_status": "public_domain",
    },
    {
        "title": "梁山伯与祝英台·十八相送",
        "genre": "越剧",
        "era": "1950s",
        "artist": "范瑞娟",
        "duration": 38,
        "copyright_status": "public_domain",
    },
    {
        "title": "红楼梦·宝玉哭灵",
        "genre": "越剧",
        "era": "1960s",
        "artist": "徐玉兰",
        "duration": 55,
        "copyright_status": "teacher_authorized",
    },
    {
        "title": "红楼梦·黛玉焚稿",
        "genre": "越剧",
        "era": "1960s",
        "artist": "王文娟",
        "duration": 47,
        "copyright_status": "teacher_authorized",
    },
    {
        "title": "追鱼·拔鳞",
        "genre": "越剧",
        "era": "1950s",
        "artist": "袁雪芬",
        "duration": 33,
        "copyright_status": "public_domain",
    },
    {
        "title": "碧玉簪·送凤冠",
        "genre": "越剧",
        "era": "1950s",
        "artist": "金采风",
        "duration": 29,
        "copyright_status": "public_domain",
    },
    {
        "title": "西厢记·琴心",
        "genre": "越剧",
        "era": "1960s",
        "artist": "吕瑞英",
        "duration": 36,
        "copyright_status": "teacher_authorized",
    },
    {
        "title": "穆桂英挂帅·出征",
        "genre": "京剧",
        "era": "1950s",
        "artist": "梅兰芳",
        "duration": 58,
        "copyright_status": "public_domain",
    },
    {
        "title": "贵妃醉酒·海岛冰轮",
        "genre": "京剧",
        "era": "1950s",
        "artist": "梅兰芳",
        "duration": 44,
        "copyright_status": "public_domain",
    },
    {
        "title": "霸王别姬·夜深沉",
        "genre": "京剧",
        "era": "1950s",
        "artist": "程砚秋",
        "duration": 51,
        "copyright_status": "public_domain",
    },
    {
        "title": "锁麟囊·春秋亭",
        "genre": "京剧",
        "era": "1950s",
        "artist": "程砚秋",
        "duration": 39,
        "copyright_status": "public_domain",
    },
    {
        "title": "四郎探母·坐宫",
        "genre": "京剧",
        "era": "1960s",
        "artist": "马连良",
        "duration": 46,
        "copyright_status": "public_domain",
    },
    {
        "title": "空城计·我正在城楼观山景",
        "genre": "京剧",
        "era": "1960s",
        "artist": "谭富英",
        "duration": 40,
        "copyright_status": "public_domain",
    },
    {
        "title": "沙家浜·智斗",
        "genre": "京剧",
        "era": "1970s",
        "artist": "赵燕侠",
        "duration": 35,
        "copyright_status": "teacher_authorized",
    },
    {
        "title": "天仙配·满工对唱",
        "genre": "黄梅戏",
        "era": "1950s",
        "artist": "严凤英",
        "duration": 31,
        "copyright_status": "public_domain",
    },
    {
        "title": "天仙配·夫妻双双把家还",
        "genre": "黄梅戏",
        "era": "1950s",
        "artist": "严凤英",
        "duration": 27,
        "copyright_status": "public_domain",
    },
    {
        "title": "女驸马·谁料皇榜中状元",
        "genre": "黄梅戏",
        "era": "1950s",
        "artist": "严凤英",
        "duration": 24,
        "copyright_status": "public_domain",
    },
    {
        "title": "牛郎织女·到底人间欢乐多",
        "genre": "黄梅戏",
        "era": "1960s",
        "artist": "严凤英",
        "duration": 33,
        "copyright_status": "teacher_authorized",
    },
    {
        "title": "花木兰·谁说女子不如男",
        "genre": "豫剧",
        "era": "1950s",
        "artist": "常香玉",
        "duration": 37,
        "copyright_status": "public_domain",
    },
    {
        "title": "朝阳沟·亲家母你坐下",
        "genre": "豫剧",
        "era": "1960s",
        "artist": "常香玉",
        "duration": 43,
        "copyright_status": "teacher_authorized",
    },
    {
        "title": "穆桂英挂帅·辕门外三声炮",
        "genre": "豫剧",
        "era": "1960s",
        "artist": "常香玉",
        "duration": 49,
        "copyright_status": "public_domain",
    },
    {
        "title": "刘巧儿·小桥送线",
        "genre": "评剧",
        "era": "1950s",
        "artist": "新凤霞",
        "duration": 30,
        "copyright_status": "public_domain",
    },
    {
        "title": "花为媒·报花名",
        "genre": "评剧",
        "era": "1950s",
        "artist": "新凤霞",
        "duration": 28,
        "copyright_status": "public_domain",
    },
    {
        "title": "杨三姐告状·哭灵",
        "genre": "评剧",
        "era": "1960s",
        "artist": "新凤霞",
        "duration": 45,
        "copyright_status": "teacher_authorized",
    },
]

assert 20 <= len(SEEDS) <= 30, f"种子条数应在 20–30（X8 规范④），当前 {len(SEEDS)}"


def media_id_for(title: str) -> str:
    return str(uuid.uuid5(NAMESPACE_OPERA, title))


def render_wav(path: Path, title: str) -> int:
    """生成确定性短 wav（同标题同波形）：正弦扫频 + 音量包络，约 3 秒。"""
    sample_rate = 8000
    seconds = 3
    rng = random.Random(title)  # noqa: S311 - 确定性波形合成，非加密用途
    base_freq = rng.uniform(220.0, 440.0)  # A3–A4 附近，戏曲唱腔近似音区
    sweep = rng.uniform(0.02, 0.08)
    frames = bytearray()
    total = sample_rate * seconds
    for i in range(total):
        t = i / sample_rate
        freq = base_freq * (1.0 + sweep * math.sin(2 * math.pi * 0.25 * t))
        envelope = min(1.0, t / 0.3, (seconds - t) / 0.5)  # 淡入淡出
        value = 0.35 * envelope * math.sin(2 * math.pi * freq * t)
        frames += struct.pack("<h", int(value * 32767))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return len(frames)


def seed_media_library() -> int:
    """落库 media_library 种子（幂等：同名跳过）。返回新插入条数。"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings
    from app.models.media import MediaLibrary

    async def _run() -> int:
        engine = create_async_engine(get_settings().database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        inserted = 0
        try:
            async with factory() as session:
                for seed in SEEDS:
                    title = str(seed["title"])
                    existing = (
                        await session.execute(
                            select(MediaLibrary).where(MediaLibrary.title == title)
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        continue
                    mid = uuid.UUID(media_id_for(title))
                    session.add(
                        MediaLibrary(
                            media_id=mid,
                            type="opera",  # X8 ④：种子全部为戏曲（movie 为 P2）
                            title=title,
                            genre=str(seed["genre"]),
                            era=str(seed["era"]),
                            artist=str(seed["artist"]),
                            media_url=f"/static/media/opera/{mid}.wav",
                            duration=int(seed["duration"]),
                            copyright_status=str(seed["copyright_status"]),
                        )
                    )
                    inserted += 1
                await session.commit()
        finally:
            await engine.dispose()
        return inserted

    return asyncio.run(_run())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav-only", action="store_true", help="只生成 wav，不落库")
    args = parser.parse_args()

    written = 0
    for seed in SEEDS:
        mid = media_id_for(str(seed["title"]))
        written += 1
        print(f"  wav: static/media/opera/{mid}.wav  <- {seed['title']}")
        render_wav(MEDIA_DIR / f"{mid}.wav", str(seed["title"]))
    print(f"wav 生成完成：{written} 个 -> {MEDIA_DIR}")

    if args.wav_only:
        return 0

    inserted = seed_media_library()
    print(
        f"media_library 种子完成：新增 {inserted} 条（总计 {len(SEEDS)} 条口径，"
        f"已有 {len(SEEDS) - inserted} 条跳过）@ {datetime.now(UTC).isoformat()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
