# Day 10 进度看板

> ⚠️ **本文件由 `scripts/update_progress.py` 自动生成，不要手改**——改任务卡首部的 `> 状态：` 行，再跑一次脚本（或直接提交，pre-commit 钩子会自动刷新）。
> 刻意不写生成时间与提交哈希：那两样每次提交都会变，会让看板变成噪音。提交历史看 `git log`。

## 总览：**0 / 4** 张任务卡已收口

| # | 任务卡 | 状态 | 文件 |
| --- | --- | --- | --- |
| 1 | dev-A · Day 10 任务书 · 容器化与部署链 | ❔ 可执行（待 M6.0 启动核对后开工） | `docs/team_tasks/day10/dev-A-容器化与部署链.md` |
| 2 | dev-B · Day 10 任务书 · 观测与成本线 | ❔ 可执行（待 M6.0 启动核对后开工） | `docs/team_tasks/day10/dev-B-观测与成本线.md` |
| 3 | dev-C · Day 10 任务书 · 前端构建与文档齐套线 | 🚧 进行中（C1–C5 已交付推送；README §12/§14/§15 已按 A2a/B2/B1 素材填实，**仅 §13 待 dev-A A4 素材**） | `docs/team_tasks/day10/dev-C-前端构建与文档齐套.md` |
| 4 | dev-D · Day 10 任务书 · 评测回归与演示冻结线 | ❔ 可执行（待 M6.0 启动核对后开工） | `docs/team_tasks/day10/dev-D-评测回归与演示冻结.md` |

## 计划件状态

| 计划 | 状态 |
| --- | --- |
| `docs/plans/day10/d-评测回归与演示冻结.md` | 🚧 进行中（S0/D1/D1-R2 已完成，下一步 D2 P0 全量回归） |
| `docs/plans/day10/day-10-交付与观测-工作推进计划.md` | 🚧 进行中（v1.4 · C 包 S0–S3 已收口；Q1/Q3/Q5/Q10 已裁决，Q2/Q4/Q6–Q9 待 M6.0 收敛） |
| `docs/plans/day10/dev-a-容器化与部署链.md` | 🚧 进行中（S1–S7 已收口 · A 组遗留复核修正完成 · M6.1 A 侧就位待与 dev-C 合验；真门禁豁免已由 dev-D D1 复跑＋阻塞门禁接线闭环；下一步 S8） |
| `docs/plans/day10/dev-b-day10-工作推进计划.md` | ❔ **已完成（2026-11-13 主理人确认）· Step 0–6 全部收口（M6.2 达成；Step 5/6 经主理人 20:22/20:45 确认）· 越域修复 redact.py 误伤 bug（授权已交 dev-A）· 唯一挂起＝D3 演练抽验（支援项，待 D13 时点，素材已备：docs/guides/langfuse-操作指引.md）** |
| `docs/plans/day10/dev-c-day10-工作推进计划.md` | 🚧 进行中（S0–S3 收口；J1/J2 Y3 闭环、J4 B2/B1 闭环、J5 门禁口径闭环；**仅剩 J3·A4 素材回填 README §13**） |

## 契约冻结：（本日无 contracts.md）

## 怎么更新

1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。
2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。
3. 想手动看差异：`uv run python scripts/update_progress.py --day 10`；只查是否过期：加 `--check`。
