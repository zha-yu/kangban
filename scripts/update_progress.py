#!/usr/bin/env python
"""生成 `docs/team_tasks/day03/PROGRESS.md`（Day 3 进度看板）。

单一事实源是各任务卡首部的 `> 状态：…` 行与 task-a*.md 计划件的同名行；
本脚本只做搬运与渲染，**不要手改进度文件**（下次生成会被覆盖）。

用法：
    python scripts/update_progress.py           # 重新生成
    python scripts/update_progress.py --check   # 只检查是否过期（CI / pre-commit 用）
    python scripts/update_progress.py --quiet   # 安静模式（hook 用）

退出码：0 正常；1 --check 时已过期；2 读不到任务卡。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAY_DIR = ROOT / "docs" / "team_tasks" / "day03"
OUT = DAY_DIR / "PROGRESS.md"

# 状态 → 图标与排序权重（越靠前越"完成"）
STATUS_STYLE: list[tuple[str, tuple[str, int]]] = [
    ("已完成", ("✅", 0)),
    ("已收口", ("✅", 0)),
    ("已取消", ("🔕", 2)),
    ("进行中", ("🚧", 1)),
    ("待认领", ("⬜", 3)),
    ("未开始", ("⬜", 3)),
    ("待契约会确认", ("🟡", 1)),
]

STATUS_RE = re.compile(r"^>\s*状态：\s*(.+?)\s*(?:[｜|]|$)", re.MULTILINE)
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


PLAN_GLOB = "docs/plans/day03/task-a*.md"


def style(status: str) -> tuple[str, int]:
    for key, value in STATUS_STYLE:
        if status.startswith(key):
            return value
    return ("❔", 9)


def parse_md(path: Path) -> tuple[str, str]:
    """返回 (标题, 状态)。缺状态行算「未标注」。"""
    text = path.read_text(encoding="utf-8")
    title_m = TITLE_RE.search(text)
    title = title_m.group(1) if title_m else path.stem
    status_m = STATUS_RE.search(text)
    return title, (status_m.group(1).strip() if status_m else "未标注")


def collect_cards() -> list[dict[str, str]]:
    cards = sorted(p for p in DAY_DIR.glob("a*.md") if p.name[0] == "a" and p.stem[1].isdigit())
    if not cards:
        print(f"[error] {DAY_DIR} 下没有找到 a*.md 任务卡", file=sys.stderr)
        raise SystemExit(2)
    rows = []
    for path in cards:
        title, status = parse_md(path)
        icon, weight = style(status)
        rows.append(
            {
                "name": path.stem,
                "title": title,
                "status": status,
                "icon": icon,
                "weight": str(weight),
                "file": path.relative_to(ROOT).as_posix(),
            }
        )
    return rows


def collect_plans() -> list[dict[str, str]]:
    rows = []
    for path in sorted(ROOT.glob(PLAN_GLOB)):
        title, status = parse_md(path)
        icon, _ = style(status)
        rows.append(
            {
                "file": path.relative_to(ROOT).as_posix(),
                "title": title,
                "status": status,
                "icon": icon,
            }
        )
    return rows


def collect_contract() -> str:
    """契约文件的首行状态（C1/C2/C3 冻结与否由它承载）。"""
    path = DAY_DIR / "contracts.md"
    if not path.exists():
        return "未找到 contracts.md"
    _, status = parse_md(path)
    return status


def build() -> str:
    cards = collect_cards()
    plans = collect_plans()
    done = sum(1 for c in cards if c["icon"] == "✅")
    total = len(cards)

    lines: list[str] = [
        "# Day 3 进度看板",
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
        "| 计划 | 状态 |",
        "| --- | --- |",
    ]
    if plans:
        for plan in plans:
            lines.append(f"| `{plan['file']}` | {plan['icon']} {plan['status']} |")
    else:
        lines.append("| （暂无） | — |")

    lines += ["", f"## 契约冻结：**{collect_contract()}**", "", "## 怎么更新", ""]
    lines += [
        "1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。",
        "2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。",
        "3. 想手动看差异：`uv run python scripts/update_progress.py`；只查是否过期：加 `--check`。",
        "",
    ]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 Day 3 进度看板")
    parser.add_argument("--check", action="store_true", help="只检查是否过期，不写文件")
    parser.add_argument("--quiet", action="store_true", help="安静模式")
    args = parser.parse_args(argv)

    content = build()
    old = OUT.read_text(encoding="utf-8") if OUT.exists() else ""

    if args.check:
        if content != old:
            if not args.quiet:
                print(
                    f"[stale] {OUT.relative_to(ROOT)} 已过期，请跑一次 scripts/update_progress.py"
                )
            return 1
        if not args.quiet:
            print(f"[ok] {OUT.relative_to(ROOT)} 是最新的")
        return 0

    if content == old:
        if not args.quiet:
            print(f"[skip] {OUT.relative_to(ROOT)} 无变化")
        return 0

    OUT.write_text(content, encoding="utf-8", newline="\n")
    if not args.quiet:
        print(f"[write] {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
