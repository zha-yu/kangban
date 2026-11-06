# Day 04 进度看板

> ⚠️ **本文件由 `scripts/update_progress.py` 自动生成，不要手改**——改任务卡首部的 `> 状态：` 行，再跑一次脚本（或直接提交，pre-commit 钩子会自动刷新）。
> 刻意不写生成时间与提交哈希：那两样每次提交都会变，会让看板变成噪音。提交历史看 `git log`。

## 总览：**0 / 1** 张任务卡已收口

| # | 任务卡 | 状态 | 文件 |
| --- | --- | --- | --- |
| 1 | Day 4 · a0 联调遗留问题修复 | 🚧 进行中 | `docs/team_tasks/day04/a0-联调遗留问题修复.md` |

## 计划件状态

| 计划 | 状态 |
| --- | --- |
| `docs/plans/day02/adhoc-20261104-鉴权内核审查底稿.md` | 🚧 进行中（待主理人逐段签署） |
| `docs/plans/day02/task-03-库与模型基座.md` | ❔ 已冻结 |
| `docs/plans/day02/task-04-鉴权内核.md` | ❔ 未标注 |
| `docs/plans/day02/task-05-登录与凭证接口.md` | ❔ 草稿 |
| `docs/plans/day02/task-06-长者CRUD与设备绑定.md` | ❔ 已冻结 |
| `docs/plans/day03/adhoc-20261105-A3媒体层执行计划.md` | 🚧 进行中 |
| `docs/plans/day03/adhoc-20261105-音频传输与设备链路技术方案.md` | ❔ 已冻结 |
| `docs/plans/day03/day-03-a5-VAD兜底与降级测试.md` | ❔ 草稿 |
| `docs/plans/day03/day-03-WebRTC与语音闭环.md` | ❔ 草稿 |
| `docs/plans/day03/task-a1-ASR与TTS适配层.md` | ✅ 已完成（2026-11-05；N0–N9 全部达成，**N8 真链路已出字**，见 §10 v1.4） |
| `docs/plans/day03/task-a1-DAG.md` | ✅ 已完成（2026-11-05；N0–N9 全部达成，N8 已拿到真实出字） |
| `docs/plans/day03/task-a2-信令WS与鉴权.md` | ❔ 草稿 |
| `docs/plans/day03/task-a4-pad前端执行计划.md` | 🚧 进行中 |
| `docs/plans/day04/a4-VAD主链与遗留修复.md` | ❔ 已冻结 |

## 契约冻结：**C4 / C5 / C6 / C7 均已定稿（主理人裁决提前冻结，晨会签字确认）**

## 怎么更新

1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。
2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。
3. 想手动看差异：`uv run python scripts/update_progress.py`；只查是否过期：加 `--check`。
