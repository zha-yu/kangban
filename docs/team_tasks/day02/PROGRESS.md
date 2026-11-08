# Day 2 进度看板

> ⚠️ **本文件由 `scripts/update_progress.py` 自动生成，不要手改**——改任务卡首部的 `> 状态：` 行，再跑一次脚本（或直接提交，pre-commit 钩子会自动刷新）。
> 刻意不写生成时间与提交哈希：那两样每次提交都会变，会让看板变成噪音。提交历史看 `git log`。

## 总览：**5 / 7** 张任务卡已收口

| # | 任务卡 | 状态 | 文件 |
| --- | --- | --- | --- |
| 1 | task-1 · Step0 身份与权限契约冻结 | ✅ 已完成（2026-11-04） | `docs/team_tasks/day02/task-1-契约冻结.md` |
| 2 | task-2 · 提示词模板库 v1（6 个模板） | 🔕 已取消（2026-11-05 主理人裁决，见文末取消记录） | `docs/team_tasks/day02/task-2-提示词模板库.md` |
| 3 | task-3 · Step1 库与模型基座（5 张表 + `0002` 迁移 + conftest） | ✅ 已完成（2026-11-04，验证与交付记录见 `docs/plans/task-03-库与模型基座.md`） | `docs/team_tasks/day02/task-3-库与模型基座.md` |
| 4 | task-4 · Step2 鉴权内核（security / 依赖注入 / runtime_context） | 🚧 进行中（判定层 `16513da`、T4b 接线 `2107292`、文档 `9ee9f0a`、T5 测试收口 `92ea5ae`；剩 T6 人工逐行审查与交付报告） | `docs/team_tasks/day02/task-4-鉴权内核.md` |
| 5 | task-5 · Step3 登录与凭证接口 | ✅ 已合入 | `docs/team_tasks/day02/task-5-登录与凭证接口.md` |
| 6 | task-6 · Step4 长者档案 CRUD + 设备绑定第一层 + 范围过滤 | ✅ 已完成（2026-11-04；router 注册与当日总验收归 `task-7`） | `docs/team_tasks/day02/task-6-长者CRUD与设备绑定.md` |
| 7 | task-7 · Step5 验收收口（攻击用例 + router 注册 + Iter 1 演示 + DoD） | ✅ 已完成（2026-11-05；全量 `270 passed`、ruff 全绿，复跑配方见文末） | `docs/team_tasks/day02/task-7-验收收口.md` |

## 计划件状态

| 计划 | 状态 |
| --- | --- |
| `docs/plans/day02/adhoc-20261104-鉴权内核审查底稿.md` | 🚧 进行中（待主理人逐段签署） |
| `docs/plans/day02/task-03-库与模型基座.md` | 🔒 已冻结 |
| `docs/plans/day02/task-04-鉴权内核.md` | 🚧 进行中（T1、T2、T3、T4a、T4b-1、T4b-2、T5 已完成；剩 T6 人工审查交付） |
| `docs/plans/day02/task-05-登录与凭证接口.md` | 📝 草稿 |
| `docs/plans/day02/task-06-长者CRUD与设备绑定.md` | 🔒 已冻结 |

## 契约冻结：（本日无 contracts.md）

## 怎么更新

1. 把对应任务卡首部的 `> 状态：<词>` 改成新状态（取值见 `AGENTS.md` §10.5）。
2. 直接提交即可——`pre-commit` 钩子会重算本文件并纳入同一次提交。
3. 想手动看差异：`uv run python scripts/update_progress.py --day 2`；只查是否过期：加 `--check`。
