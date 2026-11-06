# Day 4 进度看板

> ⚠️ **本文件由 `scripts/update_progress.py` 自动生成，不要手改**——改任务卡首部的 `> 状态：` 行，再跑一次脚本（或直接提交，pre-commit 钩子会自动刷新）。
> 刻意不写生成时间与提交哈希：那两样每次提交都会变，会让看板变成噪音。提交历史看 `git log`。

## 总览：**7 / 12** 张任务卡已收口

| # | 任务卡 | 状态 | 文件 |
| --- | --- | --- | --- |
| 1 | Day 4 · a0 联调遗留问题修复 | 🚧 进行中 | `docs/team_tasks/day04/a0-联调遗留问题修复.md` |
| 2 | Task 01 · 声纹身份闭环（A1，W2） | ⬜ 未开始 | `docs/team_tasks/day04/task-01-声纹身份闭环.md` |
| 3 | Task 02 · 信令追加与对话闭环（A2，W3） | ⬜ 未开始 | `docs/team_tasks/day04/task-02-信令与对话闭环.md` |
| 4 | Task 03 · 3D 展示后端（A3，W1） | ✅ 已完成 | `docs/team_tasks/day04/task-03-3D展示后端.md` |
| 5 | Task 04 · VAD 主链接入与遗留修复（A4，W4/W5 + a0） | 🚧 进行中 | `docs/team_tasks/day04/task-04-VAD主链与遗留修复.md` |
| 6 | Task 05 · Day 4 前端全量（pad 联调主线 + 双端登录） | ⬜ 未开始 | `docs/team_tasks/day04/task-05-Day4前端联调.md` |
| 7 | Task 06 · 护工工单看板（console 第 3 批首项，Mock 先行） | ✅ 已完成 | `docs/team_tasks/day04/task-06-护工工单看板.md` |
| 8 | Task 07 · 负责长者列表与详情（console，真实对接） | ✅ 已完成 | `docs/team_tasks/day04/task-07-负责长者列表.md` |
| 9 | Task 08 · 告警通知中心（console 护工，Mock 先行） | ✅ 已完成 | `docs/team_tasks/day04/task-08-告警通知中心.md` |
| 10 | Task 09 · 健康采集代填表（console 护工，提交 Mock） | ✅ 已完成 | `docs/team_tasks/day04/task-09-健康代填.md` |
| 11 | Task 10 · 药单只读（console 护工，Mock 先行） | ✅ 已完成 | `docs/team_tasks/day04/task-10-药单只读.md` |
| 12 | Task 11 · 健康记录复核队列（console 医生，Mock 先行） | ✅ 已完成 | `docs/team_tasks/day04/task-11-复核队列.md` |

## 计划件状态

| 计划 | 状态 |
| --- | --- |
| `docs/plans/day04/a3-3D展示后端.md` | ✅ 已完成 |
| `docs/plans/day04/a3-DAG.md` | ✅ 已完成 |
| `docs/plans/day04/a4-VAD主链与遗留修复.md` | ❔ 已冻结 |
| `docs/plans/day04/day-04-3D与身份闭环.md` | ❔ 草稿 |
| `docs/plans/day04/f1-Day4前端联调.md` | 🚧 进行中 |
| `docs/plans/day04/f1-双端登录页.md` | ✅ 已完成 |
| `docs/plans/day04/f2-护工工单看板.md` | ✅ 已完成 |
| `docs/plans/day04/f3-负责长者列表.md` | ✅ 已完成 |
| `docs/plans/day04/f4-告警通知中心.md` | ✅ 已完成 |
| `docs/plans/day04/f5-健康代填.md` | ✅ 已完成 |
| `docs/plans/day04/f6-药单只读.md` | ✅ 已完成 |
| `docs/plans/day04/f7-复核队列.md` | ✅ 已完成 |
| `docs/plans/day04/task-01-声纹身份.md` | ❔ 草稿 |

## 契约冻结：**C4 / C5 / C6 / C7 均已定稿（主理人裁决提前冻结，晨会签字确认）**

## 怎么更新

1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。
2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。
3. 想手动看差异：`uv run python scripts/update_progress.py --day 4`；只查是否过期：加 `--check`。
