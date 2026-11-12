# Day 9 进度看板

> ⚠️ **本文件由 `scripts/update_progress.py` 自动生成，不要手改**——改任务卡首部的 `> 状态：` 行，再跑一次脚本（或直接提交，pre-commit 钩子会自动刷新）。
> 刻意不写生成时间与提交哈希：那两样每次提交都会变，会让看板变成噪音。提交历史看 `git log`。

## 总览：**0 / 4** 张任务卡已收口

| # | 任务卡 | 状态 | 文件 |
| --- | --- | --- | --- |
| 1 | dev-A · Day 9 任务书 · 安全闸门与数据生命周期 | ⬜ 未开始 | `docs/team_tasks/day09/dev-A-安全闸门与生命周期.md` |
| 2 | dev-B · Day 9 任务书 · 智能层接线与注入防线 | ⬜ 未开始 | `docs/team_tasks/day09/dev-B-智能层接线与注入防线.md` |
| 3 | dev-C · Day 9 任务书 · 健康通知闭环收口 ＋ console 管理台 | ⬜ 未开始 | `docs/team_tasks/day09/dev-C-健康通知闭环与管理台.md` |
| 4 | dev-D · Day 9 任务书 · 长者端语音补全 ＋ 评测门禁 ＋ 家属端 | ⬜ 未开始 | `docs/team_tasks/day09/dev-D-长者端语音与评测门禁.md` |

## 计划件状态

| （暂无带状态标记的计划件） | — |

## 契约冻结：**待冻结（N0 · D9 09:30 冻结评审）**

## 怎么更新

1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。
2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。
3. 想手动看差异：`uv run python scripts/update_progress.py --day 9`；只查是否过期：加 `--check`。
