# Day 7 进度看板

> ⚠️ **本文件由 `scripts/update_progress.py` 自动生成，不要手改**——改任务卡首部的 `> 状态：` 行，再跑一次脚本（或直接提交，pre-commit 钩子会自动刷新）。
> 刻意不写生成时间与提交哈希：那两样每次提交都会变，会让看板变成噪音。提交历史看 `git log`。

## 总览：**4 / 8** 张任务卡已收口

| # | 任务卡 | 状态 | 文件 |
| --- | --- | --- | --- |
| 1 | Task dev-A · Day 7 · Chinese-Jev 三级路由与决策日志 | ⬜ 未开始 | `docs/team_tasks/day07/dev-a-day07.md` |
| 2 | Task dev-B · Day 7 · 回合终态、上下文与状态收口 | ✅ 已完成 | `docs/team_tasks/day07/dev-b-day07.md` |
| 3 | Task dev-C · Day 7 · 用药工具、到点调度与 Agent 边界 | ⬜ 未开始 | `docs/team_tasks/day07/dev-c-day07.md` |
| 4 | TD-2 · 主动触达 played 闭环 | ✅ 已完成 | `docs/team_tasks/day07/dev-c-触达played闭环.md` |
| 5 | TD-1 · 触达语音接线（WS） | ✅ 已完成 | `docs/team_tasks/day07/dev-c-触达语音接线.md` |
| 6 | Task dev-C2 · 用药到点触发接线（APScheduler → 用药确认子图） | ✅ 已完成 | `docs/team_tasks/day07/dev-c2-用药到点触发接线.md` |
| 7 | Task dev-D · Day 7 · pad 可靠性、console 改绑与联调收口 | 🚧 进行中 | `docs/team_tasks/day07/dev-d-day07.md` |
| 8 | Day 7 · dev-D · 触达真机联调（TD-1/TD-2 收口验收） | ⬜ 未开始 | `docs/team_tasks/day07/dev-d-触达真机联调.md` |

## 计划件状态

| 计划 | 状态 |
| --- | --- |
| `docs/plans/day07/a2-延迟复测清单.md` | ❔ 待执行 |
| `docs/plans/day07/d-推进计划-pad可靠性改绑联调.md` | 🚧 进行中 |
| `docs/plans/day07/d-联调记录.md` | 🚧 进行中 |
| `docs/plans/day07/dev-a-prompt-三级路由.md` | 📝 草稿 |
| `docs/plans/day07/dev-a-三级路由与决策日志.md` | 📝 草稿 |
| `docs/plans/day07/dev-b-day07-工作推进计划.md` | ❔ 待主理人确认 |
| `docs/plans/day07/dev-c-day07-执行计划.md` | 📝 草稿 |
| `docs/plans/day07/handoff-A-to-C-用药到点接线.md` | ✅ 已完成 |
| `docs/plans/day07/handoff-A-to-C-调度测试修红.md` | ❔ 待 dev-C 处理 |
| `docs/plans/day07/handoff-B-to-A-终态结算patch需求.md` | ✅ 已完成 |
| `docs/plans/day07/handoff-B-to-D-打断契约与终态口径.md` | ✅ 已完成 |
| `docs/plans/day07/handoff-B-to-D2-E20错误码契约.md` | ✅ 已完成 |

> 另有 3 个无 `> 状态：` 行的方案/草稿文件，未列入上表。

## 契约冻结：**已冻结**

## 怎么更新

1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。
2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。
3. 想手动看差异：`uv run python scripts/update_progress.py --day 7`；只查是否过期：加 `--check`。
