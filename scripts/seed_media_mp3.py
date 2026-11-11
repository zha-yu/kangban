"""真素材 mp3 种子 + 归位脚本（D-7 热替换正式版，契约 §6.1 v1.3）。

用途：把下载好的 mp3 素材按 X8 稳定路径归位——先对账 media_library，
再把文件复制为 ``static/media/opera/<media_id>.mp3``。

用法::

    uv run python scripts/seed_media_mp3.py <mp3目录> [--manifest 清单.json] [--dry-run]

流程（每个 mp3 文件独立处理）：

1. 标题 = 文件名去扩展名（去首尾空格）；
2. 对账 media_library：
   - 精确匹配（忽略首尾空格）→ 复用该行；
   - 唯一子串匹配（文件名与行标题双向 contains）→ 复用并打印映射；
   - 多个候选 → 报错列出，人工改文件名后重跑（不猜）；
   - 无匹配 → 按清单（--manifest，可选）或默认值经
     :func:`media_service.seed_media_library` 种子新行（ext=mp3）；
3. 已有行 media_url 以 ``.wav`` 结尾 → UPDATE 为 ``.mp3``
   （契约 §6.1 v1.3 重灌口径：素材换格式重灌该行，零代码改动）；
4. 复制文件到 ``static/media/opera/<media_id>.mp3``（copy 不 move，保留原素材）；
5. 打印「标题 → media_url → 落地文件」映射表。

清单 JSON 格式（数组，title 精确对账，其余字段选填）::

    [
      {"title": "梁祝·十八相送", "genre": "越剧", "era": "1950s",
       "artist": "范瑞娟", "duration": 420, "copyright_status": "public_domain"}
    ]

无清单命中时默认：type=opera、genre/era/artist/duration=None、
copyright_status=teacher_authorized（教师素材）。duration 缺失时完播率
按口径置 None 不猜（FR-CON-06），可后续补清单重跑回填。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
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


@dataclass(frozen=True)
class ManifestEntry:
    genre: str | None
    era: str | None
    artist: str | None
    duration: int | None
    copyright_status: str


def _norm(title: str) -> str:
    return title.strip()


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
            copyright_status=item.get("copyright_status", "teacher_authorized"),
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
    files: list[Path], manifest: dict[str, ManifestEntry], dry_run: bool
) -> None:
    log.info("扫描到 %d 个 mp3；manifest %d 条\n", len(files), len(manifest))

    results: list[tuple[str, str, Path | None]] = []  # (title, media_url, dest)
    pending_writes = False

    async with session_factory() as session:
        for src in files:
            title = _norm(src.stem)
            row, fuzzy = await _match_row(session, title)
            if row is None and len(fuzzy) > 1:
                log.error("✗ %s：对账命中 %d 行，拒绝猜测 → %s", title, len(fuzzy), [r.title for r in fuzzy])
                continue
            if row is None:
                entry = manifest.get(title)
                if not dry_run:
                    rows = await media_service.seed_media_library(
                        session,
                        [
                            {
                                "type": "opera",
                                "title": title,
                                "genre": entry.genre if entry else None,
                                "era": entry.era if entry else None,
                                "artist": entry.artist if entry else None,
                                "duration": entry.duration if entry else None,
                                "copyright_status": (
                                    entry.copyright_status if entry else "teacher_authorized"
                                ),
                                "ext": "mp3",
                            }
                        ],
                    )
                    row = rows[0]
                else:
                    log.info("… %s：将种子新行（dry-run 不落库）", title)
                    continue
            pending_writes = True
            if row.media_url.endswith(".wav"):
                # 契约 §6.1 v1.3 重灌口径：素材换格式重灌该行 media_url（一条 UPDATE）
                log.info("↻ %s：media_url 重灌 wav → mp3", row.title)
                if not dry_run:
                    row.media_url = media_service.stable_media_url(row.media_id, ext="mp3")
            if entry and entry.duration is not None and row.duration is None:
                log.info("↻ %s：回填 duration=%ss（原缺失）", row.title, entry.duration)
                if not dry_run:
                    row.duration = entry.duration
            dest = None if dry_run else _copy_file(src, row.media_id)
            planned_url = (
                media_service.stable_media_url(row.media_id, ext="mp3")
                if row.media_url.endswith(".wav")
                else row.media_url
            )
            log.info("✓ %s → %s%s", title, planned_url, "（dry-run 未落库）" if dry_run else "")
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
        log.warning("\n注意：%d 行 media_url 仍为 .wav（多为对账失败跳过），对应文件未归位。", wav_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source_dir", type=Path, help="mp3 素材目录")
    parser.add_argument("--manifest", type=Path, default=None, help="元数据清单 JSON（可选）")
    parser.add_argument("--dry-run", action="store_true", help="只对账打印，不写库不复制")
    args = parser.parse_args()
    if not args.source_dir.is_dir():
        raise SystemExit(f"目录不存在：{args.source_dir}")
    files = sorted(args.source_dir.glob("*.mp3"))  # 阻塞扫描放 async 外（ASYNC240）
    if not files:
        raise SystemExit(f"{args.source_dir} 下没有 mp3 文件")
    manifest = _load_manifest(args.manifest)
    try:
        asyncio.run(run(files, manifest, args.dry_run))
    except ValueError as exc:
        log.error("种子校验失败：%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
