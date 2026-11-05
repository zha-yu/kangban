# Day 3 进度看板

> ⚠️ **本文件由 `scripts/update_progress.py` 自动生成，不要手改**——改任务卡首部的 `> 状态：` 行，再跑一次脚本（或直接提交，pre-commit 钩子会自动刷新）。
> 刻意不写生成时间与提交哈希：那两样每次提交都会变，会让看板变成噪音。提交历史看 `git log`。

## 总览：**1 / 5** 张任务卡已收口

| # | 任务卡 | 状态 | 文件 |
| --- | --- | --- | --- |
| 1 | A1 · ASR/TTS 适配层 | ✅ 已完成（N8 真链路未达成，见文末收口记录） | `docs/team_tasks_day03/a1-asr-tts.md` |
| 2 | A2 · 信令 WS 与鉴权 | 🚧 进行中 | `docs/team_tasks_day03/a2-signaling-ws.md` |
| 3 | A3 · WebRTC 媒体层 | 🚧 进行中 | `docs/team_tasks_day03/a3-webrtc-media.md` |
| 4 | A4 · pad 前端 | 🚧 进行中 | `docs/team_tasks_day03/a4-pad-frontend.md` |
| 5 | A5 · 机动：VAD 兜底 + 降级链路 + 测试收口 | 🚧 进行中 | `docs/team_tasks_day03/a5-flex-vad-fallback-tests.md` |

## 计划件状态

| 计划 | 状态 |
| --- | --- |
| `docs/plans/task-a1-ASR与TTS适配层.md` | ✅ 已完成（2026-11-05 N9 收口；N1–N7、N9 交付，**N8 真链路未达成**——funasr 镜像未拉全，见 §10 v1.2） |
| `docs/plans/task-a1-DAG.md` | ✅ 已完成（2026-11-05；N1–N7、N9 完成，N8 未达成——镜像未拉全） |

## 契约冻结：**C1 已冻结并交付；C2 / C3 待各自冻结人确认**

## 怎么更新

1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。
2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。
3. 想手动看差异：`uv run python scripts/update_progress.py`；只查是否过期：加 `--check`。
