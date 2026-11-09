# Day 5 进度看板

> ⚠️ **本文件由 `scripts/update_progress.py` 自动生成，不要手改**——改任务卡首部的 `> 状态：` 行，再跑一次脚本（或直接提交，pre-commit 钩子会自动刷新）。
> 刻意不写生成时间与提交哈希：那两样每次提交都会变，会让看板变成噪音。提交历史看 `git log`。

## 总览：**2 / 6** 张任务卡已收口

| # | 任务卡 | 状态 | 文件 |
| --- | --- | --- | --- |
| 1 | Task a0 · 设备密钥轮换溯源（联调遗留跟进） | ⬜ 未开始 | `docs/team_tasks/day05/a0-设备密钥轮换溯源.md` |
| 2 | Task a1 · 本地 VRM 与形象选择页（Day05 A1） | ✅ 已完成 | `docs/team_tasks/day05/a1-本地VRM与形象选择页.md` |
| 3 | Task dev-A · 后端 · 观测与数据链路（metrics 全链路） | 🚧 进行中（A-1/A-2/A-4 已交付，A-3 待 N1） | `docs/team_tasks/day05/dev-a-后端观测与数据.md` |
| 4 | Task dev-B · 后端 · 护栏与超时降级（guardrails + 三档超时） | ✅ 已完成 | `docs/team_tasks/day05/dev-b-后端护栏与降级.md` |
| 5 | Task dev-C · 前端 · pad 端（超时表现、延迟测量、声纹联调） | 🚧 进行中 | `docs/team_tasks/day05/dev-c-前端pad语音与3D.md` |
| 6 | Task dev-D · 前端 · console/family 双端（admin 页、契约对齐、切换层） | 🚧 进行中 | `docs/team_tasks/day05/dev-d-前端console与family.md` |

## 计划件状态

| 计划 | 状态 |
| --- | --- |
| `docs/plans/day05/a-后端观测与数据.md` | 🚧 进行中 |
| `docs/plans/day05/a1-本地VRM与形象选择页.md` | 🔒 已冻结 |
| `docs/plans/day05/d-DAG.md` | 🚧 进行中（G0–G-F 已执行，G-G 待 X4） |
| `docs/plans/day05/d-admin页走查记录.md` | ✅ 已完成（真实端点走查） |
| `docs/plans/day05/d-prompt-双端前端.md` | 📝 草稿 |
| `docs/plans/day05/d-双端前端推进计划.md` | 🚧 进行中（W0–W4 已执行，W5 待 X4） |
| `docs/plans/day05/d-启动门槛核对.md` | 🚧 进行中 |
| `docs/plans/day05/d-字段差异清单.md` | ✅ 已完成（N10 文档级双向对齐；N11 零代码改动） |
| `docs/plans/day05/d-家属端切换层说明.md` | 🚧 进行中（本地逐端点自检完成；B-3 v1.1 已到，X3 冻结未排期） |
| `docs/plans/day05/d-家属端契约差异表.md` | 🚧 进行中（本地逐端点自检完成；B-3 v1.1 已到，X3 冻结未排期） |
| `docs/plans/day05/d-汇报清单.md` | 🚧 进行中（D-1/D-2/D-3 前端交付完成；契约回执与 M5.4 演示待外部） |
| `docs/plans/day05/day-05-延迟基线与护栏.md` | 🚧 进行中 |
| `docs/plans/day05/dev-b-护栏与降级.md` | 🔒 已冻结 |
| `docs/plans/day05/dev-c-pad语音与3D.md` | 🚧 进行中 |

## 契约冻结：**待冻结（家属端 X3 未排期；照护台 A-4 v0.1 待冻结）**

## 怎么更新

1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。
2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。
3. 想手动看差异：`uv run python scripts/update_progress.py --day 5`；只查是否过期：加 `--check`。
