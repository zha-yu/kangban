# Day 8 进度看板

> ⚠️ **本文件由 `scripts/update_progress.py` 自动生成，不要手改**——改任务卡首部的 `> 状态：` 行，再跑一次脚本（或直接提交，pre-commit 钩子会自动刷新）。
> 刻意不写生成时间与提交哈希：那两样每次提交都会变，会让看板变成噪音。提交历史看 `git log`。

## 总览：**0 / 4** 张任务卡已收口

| # | 任务卡 | 状态 | 文件 |
| --- | --- | --- | --- |
| 1 | dev-A 个人任务书 | 🚧 进行中（A1✅ A4✅ A2✅ A3✅ A5✅ H12⏳全员收口） | `docs/team_tasks/day08/dev-A-后端数据与分级.md` |
| 2 | dev-B 个人任务书 | ⬜ 未开始 | `docs/team_tasks/day08/dev-B-后端子图调度与MCP.md` |
| 3 | dev-C 个人任务书 | ⬜ 未开始 | `docs/team_tasks/day08/dev-C-健康域API与console.md` |
| 4 | dev-D 个人任务书 | ⬜ 未开始 | `docs/team_tasks/day08/dev-D-日报通知API与family-pad.md` |

## 计划件状态

| 计划 | 状态 |
| --- | --- |
| `docs/plans/day08/day-08-健康采集与三端后台.md` | 📝 草稿（待主理人确认后执行） |
| `docs/plans/day08/dev-a-数据分级规则层.md` | 📝 草稿 |
| `docs/plans/day08/dev-a-药单管理后端接口.md` | 📝 草稿 |
| `docs/plans/day08/dev-b-day08-工作推进计划.md` | 🚧 进行中（Step 0 已裁决，2026-11-11） |
| `docs/plans/day08/dev-c-day08-工作推进计划.md` | ❔ 执行中 |

> 另有 1 个无 `> 状态：` 行的方案/草稿文件，未列入上表。

## 契约冻结：（本日无 contracts.md）

## 怎么更新

1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。
2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。
3. 想手动看差异：`uv run python scripts/update_progress.py --day 8`；只查是否过期：加 `--check`。
