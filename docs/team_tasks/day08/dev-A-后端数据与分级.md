> 负责人：dev-A（后端 · 数据层与分级引擎）　｜　日期：2026-11-10（Day 8）　｜　总人时 ≈ 8h
> 依据：docs/plans/day08/day-08-健康采集与三端后台.md v1.1 ｜ 对接点与全局节奏见同目录《day08-任务总览与DAG.md》

# dev-A 个人任务书

## 任务清单

| # | 任务（对应计划） | 人时 | 前置 | 交付物 | 验收标准（量化） |
| --- | --- | --- | --- | --- | --- |
| A1 | H1：两表迁移 + ORM | 2h | 无（与 B1 并行开工；提交前对齐基线复核） | `alembic/versions/0017_day8_health_reports.py`、`app/models/health.py`（HealthRecord）、`app/models/report.py`（DailyReport） | ① 字段逐列对齐 TD §3.4/§3.5（source 四值 CHECK、severity/rule_version/version/superseded_by/graph_thread_id、medication_stats jsonb、UNIQUE(elder_id,report_date,version)）；② CHECK：血压 0–300、睡眠 0–24、mood 1–5；③ 索引 `(elder_id, recorded_at DESC)`；④ `alembic downgrade -1 && upgrade head` 可逆；⑤ ER §5 索引清单同步 |
| A2 | H2：health_service（校验+分级引擎+版本链+代填） | 3.5h | A1 | `app/services/health_service.py` + `tests/test_health_service*.py` | ① 分级为无 I/O 纯函数，阈值照 PRD §4.6 写代码常量 + rule_version；② 单测覆盖六类：红/黄/绿/单分量判红/缺失判黄/无效数据（对齐 AC-09/10/11/21）；③ 补录黄升红用例过、不重复建单（AC-22）；④ 代填越权 403+审计（AC-32）；⑤ **分级函数签名写进 docstring 供 B2/C1 调用（对接点 X1）** |
| A3 | H7：通知双轨 + 2 分钟升级 + 状态机收口 | 2h | A2；X2 口径对齐（11:30，与 dev-C） | notify_service staff 侧扩展 + 升级扫描/30s 重试逻辑（供 B3 调度）+ 单测 | ① 红色事件双通知记录齐（AC-38）；② 2 分钟无人确认→escalated、无第二事件/工单、计时从 created_at（AC-12）；③ 四态 generated→pending→delivered→acknowledged 单测依次断言（AC-23）；④ dedupe_key 构造与 C2 一致（X2） |
| H12 | 门禁收口（牵头） | 0.5h | 全员任务完成 | 证据回填计划 §11 | 四件套全绿（命令见下）；完成定义逐条有证据 |

## 当日时间线

- 09:00 A1 开工（与契约冻结并行）；11:00 前完成迁移与 ORM
- 11:00–12:00 X1 分级函数签名冻结（通知 B/C）；11:30 与 C 对齐 X2
- 12:00–15:00 A2 开发与单测（**CP1 12:00：A1 必须完成**）
- 15:00–15:30 A3 收尾并交 B3 调度调用
- 15:30 参加 X5 红色事件端到端演练（提供通知侧 SQL 证据）
- 17:30–18:00 牵头 H12 门禁四件套

## 自检命令

```bash
uv run alembic downgrade -1 && uv run alembic upgrade head   # A1 可逆
uv run pytest tests/test_health_service*.py -q               # A2/A3 全绿
grep -rn "session\.add(\|from app.models" app/scheduler/     # 配合 B3：零命中
uv run pytest -q && uv run ruff check app tests              # H12 门禁
```

## 边界与注意

- 你的写域仅限：`alembic/versions/0017_*`、`app/models/health.py`、`app/models/report.py`、`app/services/health_service.py`、`app/services/notify_service.py`（增量）、对应 tests。不碰 graphs/scheduler/api/前端。
- 通知落 `notifications` 表（0011 已建），不新建表。
- 溢出池 O4（聚合预警 FR-HLT-10，P2）仅当日产能富余才启动。
