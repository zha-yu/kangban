# Day 10 进度看板

> ⚠️ **本文件由 `scripts/update_progress.py` 自动生成，不要手改**——改任务卡首部的 `> 状态：` 行，再跑一次脚本（或直接提交，pre-commit 钩子会自动刷新）。
> 刻意不写生成时间与提交哈希：那两样每次提交都会变，会让看板变成噪音。提交历史看 `git log`。

## 总览：**0 / 4** 张任务卡已收口

| # | 任务卡 | 状态 | 文件 |
| --- | --- | --- | --- |
| 1 | dev-A · Day 10 任务书 · 容器化与部署链 | ❔ 可执行（待 M6.0 启动核对后开工） | `docs/team_tasks/day10/dev-A-容器化与部署链.md` |
| 2 | dev-B · Day 10 任务书 · 观测与成本线 | ❔ 可执行（待 M6.0 启动核对后开工） | `docs/team_tasks/day10/dev-B-观测与成本线.md` |
| 3 | dev-C · Day 10 任务书 · 前端构建与文档齐套线 | 🚧 进行中（C1/C3/C4/C5 已交付，C2 骨架完成待素材回填） | `docs/team_tasks/day10/dev-C-前端构建与文档齐套.md` |
| 4 | dev-D · Day 10 任务书 · 评测回归与演示冻结线 | ❔ 可执行（待 M6.0 启动核对后开工） | `docs/team_tasks/day10/dev-D-评测回归与演示冻结.md` |

## 计划件状态

| 计划 | 状态 |
| --- | --- |
| `docs/plans/day10/d-评测回归与演示冻结.md` | 📝 草稿（待主理人确认后按步执行） |
| `docs/plans/day10/day-10-交付与观测-工作推进计划.md` | 🚧 进行中（v1.3 · C 包 S0–S3 已收口；Q1/Q3/Q5/Q10 已裁决，Q2/Q4/Q6–Q9 待 M6.0 收敛） |
| `docs/plans/day10/dev-a-容器化与部署链.md` | 🚧 进行中（S0 已收口，Q4/Q7/Q-A1/Q-A2 已裁决；下一步 S1） |
| `docs/plans/day10/dev-c-day10-工作推进计划.md` | 🚧 进行中（S0–S3 已收口，剩余素材回填待 A4/B2/B1/D1） |

## 契约冻结：（本日无 contracts.md）

## 怎么更新

1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。
2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。
3. 想手动看差异：`uv run python scripts/update_progress.py --day 10`；只查是否过期：加 `--check`。
