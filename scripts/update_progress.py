#!/usr/bin/env python
"""按天生成进度看板 `docs/team_tasks/day<N>/PROGRESS.md`。

单一事实源是各任务卡首部的 `> 状态：…` 行；本脚本只做搬运与渲染，
**不要手改进度文件**（下次生成会被覆盖）。

扫描规则（自动发现，不需要改配置）：
- 日目录：`docs/team_tasks/day*/`（day02、day03、day04……按名字排序）
- 任务卡：`a*.md`（a1/a2/a3…，也含 a0）与 `task-*.md`（task-1/task-01…）
  排除 `README.md`、`contracts.md`、`PROGRESS.md`
- 计划件：`docs/plans/day<N>/**/*.md`（递归）与 `docs/plans/day-<N>-*.md`（早期平铺）
  只有带 `> 状态：` 行的才进表，其余只统计条数

用法：
    python scripts/update_progress.py           # 重新生成全部日看板
    python scripts/update_progress.py --check   # 只检查是否过期（CI / pre-commit 用）
    python scripts/update_progress.py --quiet   # 安静模式（hook 用）
    python scripts/update_progress.py --day 4   # 只处理某一天

退出码：0 正常；1 --check 时存在过期；2 找不到任何日目录。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAY_ROOT = ROOT / "docs" / "team_tasks"
PLAN_ROOT = ROOT / "docs" / "plans"
OUT_NAME = "PROGRESS.md"

# 状态 → 图标与排序权重（越靠前越"完成"）
STATUS_STYLE: list[tuple[str, tuple[str, int]]] = [
    ("已完成", ("✅", 0)),
    ("已收口", ("✅", 0)),
    ("已合入", ("✅", 0)),
    ("已取消", ("🔕", 2)),
    ("进行中", ("🚧", 1)),
    ("待认领", ("⬜", 3)),
    ("未开始", ("⬜", 3)),
    ("待契约会确认", ("🟡", 1)),
]

STATUS_RE = re.compile(r"^>\s*状态：\s*(.+?)\s*(?:[｜|]|$)", re.MULTILINE)
# Day 2 的任务卡用表格行写状态：| 状态 | ✅ 已完成（2026-11-04） |
TABLE_STATUS_RE = re.compile(r"^\|\s*状态\s*\|\s*(.+?)\s*\|", re.MULTILINE)
EMOJI_PREFIX_RE = re.compile(r"^[✅🚧⬜🟡🔕❔]\ufe0f?\s*")
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
DAY_DIR_RE = re.compile(r"^day(\d{1,2})$")

SKIP_NAMES = {"readme.md", "contracts.md", "progress.md"}


def style(status: str) -> tuple[str, int]:
    for key, value in STATUS_STYLE:
        if status.startswith(key):
            return value
    return ("❔", 9)


def parse_md(path: Path) -> tuple[str, str]:
    """返回 (标题, 状态)。兼容引言行与表格行两种写法；缺状态行算「未标注」。"""
    text = path.read_text(encoding="utf-8")
    title_m = TITLE_RE.search(text)
    title = title_m.group(1) if title_m else path.stem
    status_m = STATUS_RE.search(text) or TABLE_STATUS_RE.search(text)
    if not status_m:
        return title, "未标注"
    return title, EMOJI_PREFIX_RE.sub("", status_m.group(1).strip())


def is_card(path: Path) -> bool:
    name = path.name
    if name.lower() in SKIP_NAMES:
        return False
    if path.stem.startswith("a"):
        return len(path.stem) > 1 and path.stem[1].isdigit()
    return path.stem.startswith("task-")


def discover_days() -> list[tuple[Path, int]]:
    days: list[tuple[Path, int]] = []
    for path in sorted(DAY_ROOT.iterdir() if DAY_ROOT.exists() else []):
        if not path.is_dir():
            continue
        m = DAY_DIR_RE.match(path.name)
        if m:
            days.append((path, int(m.group(1))))
    return days


def collect_cards(day_dir: Path) -> list[dict[str, str]]:
    cards = sorted(p for p in day_dir.glob("*.md") if is_card(p))
    if not cards:
        return []
    rows = []
    for path in cards:
        title, status = parse_md(path)
        icon, _ = style(status)
        rows.append(
            {
                "name": path.stem,
                "title": title,
                "status": status,
                "icon": icon,
                "file": path.relative_to(ROOT).as_posix(),
            }
        )
    return rows


def collect_plans(day: int) -> tuple[list[dict[str, str]], int]:
    """返回 (有状态行的计划件, 无状态行的条数)。"""
    seen: dict[str, Path] = {}
    sub = PLAN_ROOT / f"day{day:02d}"
    if sub.exists():
        for path in sorted(sub.rglob("*.md")):
            if path.name.lower() in SKIP_NAMES:
                continue
            seen[path.relative_to(ROOT).as_posix()] = path
    for path in sorted(PLAN_ROOT.glob(f"day-{day:02d}-*.md")):
        seen[path.relative_to(ROOT).as_posix()] = path

    rows: list[dict[str, str]] = []
    unstaged = 0
    for rel, path in sorted(seen.items()):
        title, status = parse_md(path)
        if status == "未标注":
            unstaged += 1
            continue
        icon, _ = style(status)
        rows.append({"file": rel, "title": title, "status": status, "icon": icon})
    return rows, unstaged


def collect_contract(day_dir: Path) -> str | None:
    path = day_dir / "contracts.md"
    if not path.exists():
        return None
    _, status = parse_md(path)
    return status


def build(day_dir: Path, day: int) -> str:
    cards = collect_cards(day_dir)
    if not cards:
        return ""
    plans, unstaged = collect_plans(day)
    done = sum(1 for c in cards if c["icon"] == "✅")
    total = len(cards)

    lines: list[str] = [
        f"# Day {day} 进度看板",
        "",
        "> ⚠️ **本文件由 `scripts/update_progress.py` 自动生成，不要手改**——改任务卡首部的 `> 状态：` 行，"
        "再跑一次脚本（或直接提交，pre-commit 钩子会自动刷新）。",
        "> 刻意不写生成时间与提交哈希：那两样每次提交都会变，会让看板变成噪音。提交历史看 `git log`。",
        "",
        f"## 总览：**{done} / {total}** 张任务卡已收口",
        "",
        "| # | 任务卡 | 状态 | 文件 |",
        "| --- | --- | --- | --- |",
    ]
    for idx, card in enumerate(cards, start=1):
        lines.append(
            f"| {idx} | {card['title']} | {card['icon']} {card['status']} | `{card['file']}` |"
        )

    lines += [
        "",
        "## 计划件状态",
        "",
    ]
    if plans:
        lines += ["| 计划 | 状态 |", "| --- | --- |"]
        for plan in plans:
            lines.append(f"| `{plan['file']}` | {plan['icon']} {plan['status']} |")
    else:
        lines += ["| （暂无带状态标记的计划件） | — |"]
    if unstaged:
        lines += ["", f"> 另有 {unstaged} 个无 `> 状态：` 行的方案/草稿文件，未列入上表。"]

    contract = collect_contract(day_dir)
    lines.append("")
    lines.append(f"## 契约冻结：**{contract}**" if contract else "## 契约冻结：（本日无 contracts.md）")

    lines += [
        "",
        "## 怎么更新",
        "",
        "1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。",
        "2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。",
        "3. 想手动看差异：`uv run python scripts/update_progress.py --day "
        f"{day}`；只查是否过期：加 `--check`。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按天生成任务进度看板")
    parser.add_argument("--check", action="store_true", help="只检查是否过期，不写文件")
    parser.add_argument("--quiet", action="store_true", help="安静模式")
    parser.add_argument("--day", type=int, default=None, help="只处理某一天，如 4")
    args = parser.parse_args(argv)

    days = discover_days()
    if args.day is not None:
        days = [(d, n) for d, n in days if n == args.day]
    if not days:
        print(f"[error] {DAY_ROOT} 下没有发现 day*/ 目录", file=sys.stderr)
        return 2

    stale: list[Path] = []
    wrote = 0
    for day_dir, day in days:
        content = build(day_dir, day)
        if not content:
            if not args.quiet:
                print(f"[skip] {day_dir.relative_to(ROOT)} 没有任务卡")
            continue
        out = day_dir / OUT_NAME
        old = out.read_text(encoding="utf-8") if out.exists() else ""
        if content == old:
            continue
        stale.append(out)
        if not args.check:
            out.write_text(content, encoding="utf-8", newline="\n")
            wrote += 1

    if args.check:
        if stale:
            if not args.quiet:
                for path in stale:
                    print(f"[stale] {path.relative_to(ROOT)} 已过期，请跑一次 scripts/update_progress.py")
            return 1
        if not args.quiet:
            print("[ok] 全部日看板都是最新的")
        return 0

    if wrote == 0:
        if not args.quiet:
            print("[skip] 所有日看板无变化")
        return 0

    if not args.quiet:
        for path in stale:
            print(f"[write] {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
