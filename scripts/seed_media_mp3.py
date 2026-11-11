"""真素材 mp3 种子 + 归位脚本（D-7 热替换正式版，契约 §6.1 v1.3）。

用途：把下载好的 mp3 素材按 X8 稳定路径归位——先对账 media_library，
再把文件复制为 ``static/media/opera/<media_id>.mp3``。

用法::

    uv run python scripts/seed_media_mp3.py <mp3目录> [--manifest 清单.json] [--dry-run]
    uv run python scripts/seed_media_mp3.py --backfill 清单.json [--dry-run]   # 只回填元数据，无需 mp3

回填模式（``--backfill``）::

    按 manifest 的 title 对账已有行（精确优先、唯一子串次之），只更新条目里
    **显式提供**的字段（genre/era/artist/duration/copyright_status）——
    未提供的列保持原值，media_url/文件一律不动。用于给「没带 manifest
    就跑过种子」的库补 genre/era（剧种点播靠 genre 命中）。

流程（每个 mp3 文件独立处理）：

1. **文件名解析**：``+``/``...``（下载转码残留）先归一为空格，再按第一个
   连字符（``-``/``－``/``–``/``—``）拆出 ``(artist, title)``——如
   ``二手玫瑰乐队-我要开花.mp3`` → artist=二手玫瑰乐队、title=我要开花；
   无连字符则 artist=None、title=全名；
2. 对账 media_library（按 title）：
   - 精确匹配（忽略首尾空格）→ 复用该行；
   - 唯一子串匹配（双向 contains）→ 复用并打印映射；
   - 多个候选 → 报错列出，人工改文件名后重跑（不猜）；
   - 无匹配 → 经 :func:`media_service.seed_media_library` 种子新行（ext=mp3）；
3. **旧行修复**：复用已有行时，若该行 artist 为空且 title 还是老版脚本
   落的「歌手-歌名」整串，自动拆开修正（仅当行标题本身能拆出 artist 才动）；
4. 已有行 media_url 以 ``.wav`` 结尾 → UPDATE 为 ``.mp3``
   （契约 §6.1 v1.3 重灌口径：素材换格式重灌该行，零代码改动）；
5. 复制文件到 ``static/media/opera/<media_id>.mp3``（copy 不 move，保留原素材）；
6. 打印「标题 → media_url → 落地文件」映射表。

**元数据来源**（genre/era/duration 为什么有/没有值）：

- ``duration``：纯标准库解析 mp3 帧头自动读出（Xing/VBRI 帧数优先，
  CBR 估算兜底；解析失败置 None，完播率按 FR-CON-06 口径不猜）；
- ``artist``：从文件名拆出（manifest 可覆写）；
- ``genre`` / ``era``：**人工知识，脚本不从文件名猜**，只来自 --manifest，
  不提供即为 NULL——这是设计行为，不是 bug；
- ``copyright_status``：默认 teacher_authorized，manifest 可改 public_domain。

清单 JSON 格式（数组；``title`` = **解析后的歌名**，精确对账，其余选填，
选填项优先级高于自动解析）::

    [
      {"title": "我要开花", "genre": "摇滚", "era": "2010s",
       "artist": "二手玫瑰乐队", "duration": 219, "copyright_status": "teacher_authorized"}
    ]

所有行 type=opera（0018 CHECK 仅 opera|movie 两值，movie 归 P2 不做）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_factory
from app.models.media import MediaLibrary
from app.services import media_service

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed_media_mp3")

REPO_ROOT = Path(__file__).resolve().parents[1]
MEDIA_DIR = REPO_ROOT / "static" / "media" / "opera"

#: 下载转码残留：``+``（空格转义）与 ``...``（多字符截断）统一归一为空格
_MANGLED_CHARS = re.compile(r"\+|\.{3}")
_MULTISPACE = re.compile(r"\s+")
#: 歌名分隔连字符（半角/全角/en/em dash），只按第一个拆
_FILENAME_DASH = re.compile(r"[-－–—]")


@dataclass(frozen=True)
class ManifestEntry:
    genre: str | None
    era: str | None
    artist: str | None
    duration: int | None
    copyright_status: str | None  # None = 清单未写；种子路径回退 teacher_authorized，回填路径跳过


def _norm(title: str) -> str:
    return title.strip()


def _parse_stem(stem: str) -> tuple[str | None, str]:
    """文件名 → (artist, title)。``二手玫瑰乐队-我要开花`` → (二手玫瑰乐队, 我要开花)。"""
    text = _MANGLED_CHARS.sub(" ", stem)
    text = _MULTISPACE.sub(" ", text).strip()
    parts = _FILENAME_DASH.split(text, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return None, text  # 无连字符（或拆空）→ artist 置空，不猜


# ---------- mp3 时长解析（纯标准库，跨平台，不做新依赖） ----------

_BITRATES_V1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_BITRATES_V2_L3 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_duration_seconds(path: Path) -> int | None:
    """读 mp3 时长（秒）：Xing/VBRI 帧数优先，CBR 估算兜底，失败 None 不猜。"""
    try:
        data = path.read_bytes()
        off = 0
        if data[:3] == b"ID3" and len(data) >= 10:  # 跳过 ID3v2 标签（syncsafe 编码长度）
            off = 10 + (
                (data[6] & 0x7F) << 21
                | (data[7] & 0x7F) << 14
                | (data[8] & 0x7F) << 7
                | (data[9] & 0x7F)
            )
        n = len(data)
        while off < n - 4:  # 找帧同步字 0xFFEx
            if data[off] == 0xFF and (data[off + 1] & 0xE0) == 0xE0:
                break
            off += 1
        else:
            return None
        b1, b2 = data[off + 1], data[off + 2]
        version_bits, layer_bits = (b1 >> 3) & 3, (b1 >> 1) & 3
        if version_bits not in _SAMPLE_RATES or layer_bits != 1:  # 仅 Layer III
            return None
        bitrate = (_BITRATES_V1_L3 if version_bits == 3 else _BITRATES_V2_L3)[b2 >> 4]
        sample_rate = _SAMPLE_RATES[version_bits][(b2 >> 2) & 3]
        samples_per_frame = 1152 if version_bits == 3 else 576
        if bitrate == 0 or sample_rate == 0:
            return None
        window = data[off + 4 : off + 200]
        xing = window.find(b"Xing") if b"Xing" in window else window.find(b"Info")
        if xing >= 0:  # VBR：帧数在 tag(4)+flags(4) 之后
            flags = int.from_bytes(window[xing + 4 : xing + 8], "big")
            if flags & 1:
                frames = int.from_bytes(window[xing + 8 : xing + 12], "big")
                if frames:
                    return round(frames * samples_per_frame / sample_rate)
        vbri = window.find(b"VBRI")
        if vbri >= 0:  # Fraunhofer VBRI：帧数在 tag(4)+version(2)+delay(2)+quality(2) 之后
            frames = int.from_bytes(window[vbri + 14 : vbri + 18], "big")
            if frames:
                return round(frames * samples_per_frame / sample_rate)
        return round((n - off) * 8 / (bitrate * 1000))  # CBR 估算
    except Exception:  # 任何畸形文件一律 None，不阻塞种子
        return None


def _load_manifest(path: Path | None) -> dict[str, ManifestEntry]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries: dict[str, ManifestEntry] = {}
    for item in raw:
        title = _norm(item["title"])
        if title in entries:
            raise SystemExit(f"manifest 内 title 重复：{title!r}")
        entries[title] = ManifestEntry(
            genre=item.get("genre"),
            era=item.get("era"),
            artist=item.get("artist"),
            duration=item.get("duration"),
            copyright_status=item.get("copyright_status"),  # 未写即 None，各路径自行回退/跳过
        )
    return entries


async def _match_row(
    session: AsyncSession, title: str
) -> tuple[MediaLibrary | None, list[MediaLibrary]]:
    """对账一行媒体：精确匹配优先，其次唯一子串匹配（双向）。"""
    rows = list(
        (await session.execute(select(MediaLibrary).order_by(MediaLibrary.title))).scalars()
    )
    exact = [r for r in rows if _norm(r.title) == title]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact  # 标题重复即数据问题，报错人工处理
    fuzzy = [r for r in rows if title in _norm(r.title) or _norm(r.title) in title]
    if len(fuzzy) == 1:
        return fuzzy[0], []
    return None, fuzzy  # 0 个 = 可种子；多个 = 报错


def _copy_file(src: Path, media_id: uuid.UUID) -> Path:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = MEDIA_DIR / f"{media_id}.mp3"
    shutil.copy2(src, dest)
    return dest


async def run(
    files: list[Path],
    durations: dict[Path, int | None],
    manifest: dict[str, ManifestEntry],
    dry_run: bool,
) -> None:
    log.info("扫描到 %d 个 mp3；manifest %d 条\n", len(files), len(manifest))

    results: list[tuple[str, str, Path | None]] = []  # (title, media_url, dest)
    pending_writes = False

    async with session_factory() as session:
        for src in files:
            artist, title = _parse_stem(src.stem)
            row, fuzzy = await _match_row(session, title)
            if row is None and len(fuzzy) > 1:
                log.error(
                    "✗ %s：对账命中 %d 行，拒绝猜测 → %s",
                    title,
                    len(fuzzy),
                    [r.title for r in fuzzy],
                )
                continue
            entry = manifest.get(title)
            if row is None:
                if not dry_run:
                    rows = await media_service.seed_media_library(
                        session,
                        [
                            {
                                "type": "opera",
                                "title": title,
                                "genre": entry.genre if entry else None,
                                "era": entry.era if entry else None,
                                "artist": (entry.artist if entry and entry.artist else artist),
                                "duration": (
                                    entry.duration
                                    if entry and entry.duration is not None
                                    else durations.get(src)
                                ),
                                "copyright_status": (
                                    entry.copyright_status or "teacher_authorized"
                                    if entry
                                    else "teacher_authorized"
                                ),
                                "ext": "mp3",
                            }
                        ],
                    )
                    row = rows[0]
                else:
                    log.info(
                        "… %s（artist=%s, duration=%ss）：将种子新行（dry-run 不落库）",
                        title,
                        artist or "NULL",
                        durations.get(src) or "?",
                    )
                    continue
            pending_writes = True
            if row.artist is None and _FILENAME_DASH.search(row.title or ""):
                # 旧行修复：老版脚本把「歌手-歌名」整串当 title 落库（artist=NULL），
                # 复用这种行时拆开修正——仅当行标题本身可拆出 artist 才动，避免误伤
                legacy_artist, legacy_title = _parse_stem(row.title)
                if legacy_artist is not None and legacy_title:
                    log.info(
                        "↻ %s：旧行标题修复 → title=%s, artist=%s",
                        row.title,
                        legacy_title,
                        legacy_artist,
                    )
                    if not dry_run:
                        row.title, row.artist = legacy_title, legacy_artist
            if row.media_url.endswith(".wav"):
                # 契约 §6.1 v1.3 重灌口径：素材换格式重灌该行 media_url（一条 UPDATE）
                log.info("↻ %s：media_url 重灌 wav → mp3", row.title)
                if not dry_run:
                    row.media_url = media_service.stable_media_url(row.media_id, ext="mp3")
            if row.duration is None:  # 元数据回填：manifest 优先，其次自动解析值
                fill = (
                    entry.duration if entry and entry.duration is not None else durations.get(src)
                )
                if fill is not None:
                    log.info("↻ %s：回填 duration=%ss（原缺失）", row.title, fill)
                    if not dry_run:
                        row.duration = fill
            dest = None if dry_run else _copy_file(src, row.media_id)
            planned_url = (
                media_service.stable_media_url(row.media_id, ext="mp3")
                if row.media_url.endswith(".wav")
                else row.media_url
            )
            log.info(
                "✓ %s（artist=%s）→ %s%s",
                title,
                row.artist or "NULL",
                planned_url,
                "（dry-run 未落库）" if dry_run else "",
            )
            results.append((row.title, planned_url, dest))

        if dry_run:
            log.info("\n[dry-run] 未写库未复制；去掉 --dry-run 正式执行。")
            return
        if pending_writes:
            await session.commit()
            log.info("\n已提交 media_library 变更。")

    log.info("\n========== 归位映射表 ==========")
    for title, url, dest in results:
        log.info("%s\n    %s\n    → %s", title, url, dest or "(未复制)")
    wav_rows = sum(1 for _, u, _ in results if u.endswith(".wav"))
    if wav_rows:
        log.warning(
            "\n注意：%d 行 media_url 仍为 .wav（多为对账失败跳过），对应文件未归位。", wav_rows
        )


async def run_backfill(manifest: dict[str, ManifestEntry], dry_run: bool) -> None:
    """回填模式：按 manifest 对账已有行，只更新显式提供的元数据字段。"""
    log.info("回填模式：manifest %d 条\n", len(manifest))
    updated = missing = ambiguous = 0

    async with session_factory() as session:
        for title, entry in sorted(manifest.items()):
            row, fuzzy = await _match_row(session, title)
            if row is None and len(fuzzy) > 1:
                log.error(
                    "✗ %s：命中 %d 行，拒绝猜测 → %s", title, len(fuzzy), [r.title for r in fuzzy]
                )
                ambiguous += 1
                continue
            if row is None:
                log.warning("✗ %s：库里没有这行（先跑种子模式归位），跳过", title)
                missing += 1
                continue
            changes: list[str] = []
            for field in ("genre", "era", "artist", "duration", "copyright_status"):
                value = getattr(entry, field)
                if value is None or value == getattr(row, field):
                    continue
                changes.append(f"{field}: {getattr(row, field)!r} → {value!r}")
                if not dry_run:
                    setattr(row, field, value)
            if changes:
                updated += 1
                log.info("↻ %s：%s", row.title, "；".join(changes))
            else:
                log.info("＝ %s：无变化", row.title)

        if dry_run:
            log.info("\n[dry-run] 未写库；去掉 --dry-run 正式回填。")
            return
        if updated:
            await session.commit()
            log.info("\n已提交：更新 %d 行（缺失 %d，歧义 %d）。", updated, missing, ambiguous)
        else:
            log.info("\n没有可更新的行（缺失 %d，歧义 %d）。", missing, ambiguous)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "source_dir",
        type=Path,
        nargs="?",
        default=None,
        help="mp3 素材目录（--backfill 模式不需要）",
    )
    parser.add_argument("--manifest", type=Path, default=None, help="元数据清单 JSON（可选）")
    parser.add_argument(
        "--backfill", action="store_true", help="回填模式：只按 manifest 更新已有行元数据，不动文件"
    )
    parser.add_argument("--dry-run", action="store_true", help="只对账打印，不写库不复制")
    args = parser.parse_args()
    manifest = _load_manifest(args.manifest)
    if args.backfill:
        if not manifest:
            raise SystemExit("--backfill 需要 --manifest 清单.json")
        asyncio.run(run_backfill(manifest, args.dry_run))
        return
    if args.source_dir is None or not args.source_dir.is_dir():
        raise SystemExit(f"目录不存在：{args.source_dir}")
    files = sorted(args.source_dir.glob("*.mp3"))  # 阻塞扫描放 async 外（ASYNC240）
    if not files:
        raise SystemExit(f"{args.source_dir} 下没有 mp3 文件")
    durations = {p: _mp3_duration_seconds(p) for p in files}  # 同上，阻塞 I/O 放 async 外
    try:
        asyncio.run(run(files, durations, manifest, args.dry_run))
    except ValueError as exc:
        log.error("种子校验失败：%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
