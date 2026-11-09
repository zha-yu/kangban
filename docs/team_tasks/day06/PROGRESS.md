# Day 6 进度看板

> ⚠️ **本文件由 `scripts/update_progress.py` 自动生成，不要手改**——改任务卡首部的 `> 状态：` 行，再跑一次脚本（或直接提交，pre-commit 钩子会自动刷新）。
> 刻意不写生成时间与提交哈希：那两样每次提交都会变，会让看板变成噪音。提交历史看 `git log`。

## 总览：**1 / 6** 张任务卡已收口

| # | 任务卡 | 状态 | 文件 |
| --- | --- | --- | --- |
| 1 | Task a0 · 员工-长者改绑接口与 console UI（建号后调整负责关系） | ⬜ 未开始 | `docs/team_tasks/day06/a0-员工长者改绑接口.md` |
| 2 | Day 5 延迟基线表（P50/P95 · SQL 复算） | ✅ 已完成 | `docs/team_tasks/day06/a1-延迟基线表.md` |
| 3 | Task dev-A · 历史事件与工具事实（Day 6） | ⬜ 未开始 | `docs/team_tasks/day06/dev-a-历史事件与工具事实.md` |
| 4 | Task dev-B · 状态中心与上下文记忆（Day 6） | ⬜ 未开始 | `docs/team_tasks/day06/dev-b-状态中心与上下文记忆.md` |
| 5 | Task dev-C · Agent 与统一出口（Day 6） | ⬜ 未开始 | `docs/team_tasks/day06/dev-c-Agent与统一出口.md` |
| 6 | Task dev-D · pad 送达确认与 console 改绑（Day 6） | ⬜ 未开始 | `docs/team_tasks/day06/dev-d-pad送达确认与改绑UI.md` |

## 计划件状态

| 计划 | 状态 |
| --- | --- |
| `docs/plans/day06/adhoc-20261109-Day06讲义原文存档.md` | ✅ 已完成 |
| `docs/plans/day06/d-DAG.md` | 🚧 进行中（N00 与 S1 数据底座已执行） |
| `docs/plans/day06/day-06-长者侧Agent闭环.md` | 🚧 进行中（N00 与 S1 数据底座已执行，其余节点待开工） |

## 契约冻结：**已冻结**

## 怎么更新

1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。
2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。
3. 想手动看差异：`uv run python scripts/update_progress.py --day 6`；只查是否过期：加 `--check`。
