> 状态：**已完成（A1✅ A4✅ A2✅ A3✅ A5✅ H12✅ H+1✅ 药单管理）**　｜　负责人：dev-A（后端 · 数据层、分级引擎与规则层）　｜　日期：2026-11-11（Day 8）　｜　版本：v1.3（v1.2＋H+1 药单管理追加任务完成回填）
> 依据：docs/plans/day08/day-08-健康采集与三端后台.md v1.2 ｜ 对接点与全局节奏见同目录《day08-任务总览与DAG.md》

# dev-A 个人任务书

## 任务清单

| # | 任务（对应计划） | 人时 | 前置 | 交付物 | 验收标准（量化） |
| --- | --- | --- | --- | --- | --- |
| A1 | H1：两表迁移 + ORM | 2h | 无（与 B1 并行开工；提交前对齐基线复核） | `alembic/versions/0017_day8_health_reports.py`、`app/models/health.py`（HealthRecord）、`app/models/report.py`（DailyReport） | ① 字段逐列对齐 TD §3.4/§3.5（source 四值 CHECK、severity/rule_version/version/superseded_by/graph_thread_id、medication_stats jsonb、UNIQUE(elder_id,report_date,version)）；② CHECK：血压 0–300、睡眠 0–24、mood 1–5；③ 索引 `(elder_id, recorded_at DESC)`；④ `alembic downgrade -1 && upgrade head` 可逆；⑤ ER §5 索引清单同步 |
| A4 | H14①：戏曲两表迁移 0018 | 0.5h | A1（迁移写锁顺做，避开与 0017 冲突） | `alembic/versions/0018_day8_media.py`（media_library + media_history） | ① 字段照 TD §3.4（type=opera/movie、title/genre/era/artist/media_url/duration/copyright_status；history 含播放时长/完播率）；② downgrade/upgrade 可逆；③ ER 同步；④ 与 0017 两健康表互不触碰 |
| A2 | H2：health_service（校验+分级引擎+版本链+代填） | 3.5h | A1 | `app/services/health_service.py` + `tests/test_health_service*.py` | ① 分级为无 I/O 纯函数，阈值照 PRD §4.6 写代码常量 + rule_version；② 单测覆盖六类：红/黄/绿/单分量判红/缺失判黄/无效数据（对齐 AC-09/10/11/21）；③ 补录黄升红用例过、不重复建单（AC-22）；④ 代填越权 403+审计（AC-32）；⑤ **分级函数签名写进 docstring 供 B2/C1 调用（对接点 X1）** |
| A3 | H7：通知双轨 + 2 分钟升级 + 状态机收口 | 2h | A2；X2 口径对齐（11:30，与 dev-C） | notify_service staff 侧扩展 + 升级扫描/30s 重试逻辑（供 B3 调度）+ 单测 | ① 红色事件双通知记录齐（AC-38）；② 2 分钟无人确认→escalated、无第二事件/工单、计时从 created_at（AC-12）；③ 四态 generated→pending→delivered→acknowledged 单测依次断言（AC-23）；④ dedupe_key 构造与 C2 一致（X2） |
| A5 | H14④：规则层直连真实执行（骨架先行 + 联调段） | 1.5h | 骨架：B1 冻结的 X8 映射表；联调：B4（Tool 就绪）+C4（服务就绪） | `app/router/rules.py` R-01~R-08 播放控制从 `capability_not_ready` 降级改为直连真实执行 + `tests/test_rules_opera*.py` 增量 | ① AC-37 端到端：正在播放→依次「换一段」「大声点」「小声点」「重复一遍」规则层直连执行、路由耗时 ≤200ms（decision_logs 留痕 layer=rule）；② 每动作过 G2 权限；③ 请求不存在曲目→语音+文字提示且天气功能仍可用（FR-CON-04）；④ 暂停/继续/停止直连同样生效 |
| H12 | 门禁收口（牵头） | 0.5h | 全员任务完成 | 证据回填计划 §11 | 四件套全绿（命令见下）；完成定义逐条有证据 |
| H+1 | **药单管理 REST API**（追加任务，2026-11-11） | 4.0h | 无（独立于 Day 8 主线，基于 Day 6 已合入的 MedicationPlan ORM / 迁移 0010） | 迁移 0019_day8_medplan_version.py + `app/services/medication_plan_service.py`（新建） + `app/api/medication.py`（新建） + `app/api/__init__.py` 追加 ROUTERS + 前端 `frontend/src/console/medication-api.ts` 全量替换（Mock→fetch） | 详见 `docs/plans/day08/dev-a-药单管理后端接口.md` v1.4；验收命令：① `alembic upgrade head → downgrade -1 → upgrade head` 三向可逆；② `uv run ruff check app/` 全绿；③ `uv run pytest tests/ -q` 1230 passed；④ `cd frontend && npm run build` 三入口 build 通过；⑤ ROUTERS 注册表显示 `/api/medication-plans → 5 routes` |

> 超载先裁序（R1 顺位核对）：A5 的联调段若 16:00 CP4 未就绪→顺延 Day 9（AC-37 为欠债核心，**骨架必须当日完成**）；A1–A4、A2、A3 均为健康主线/欠债核心，不容顺延。溢出池 O4（聚合预警，P2）仅产能富余才启动。

## 当日时间线

- 09:00 A1 开工（与契约冻结并行）；11:00 前完成迁移与 ORM
- 11:00–11:30 A4 戏曲迁移（迁移写锁顺做）；11:30 与 C 对齐 X2
- 11:00–12:00 X1 分级函数签名冻结（通知 B/C）；12:00 **CP1**
- 12:00–14:30 A2 开发与单测
- 14:30–16:00 A3 收口并交 B3 调度调用；**15:30 参加 X5 红色事件端到端演练**（通知侧 SQL 证据）
- 14:30–15:00 A5 骨架（规则→Tool/服务映射照 X8 冻结口径，先改降级逻辑不联调）
- 16:00 **CP4 戏曲端到端演练 AC-37**（A5 联调段，与 B/C/D 四方）
- 17:30–18:00 牵头 H12 门禁四件套

## 自检命令

```bash
uv run alembic downgrade -1 && uv run alembic upgrade head   # A1 可逆
uv run alembic downgrade -1 && uv run alembic upgrade head   # A4 可逆（依次回退验证两迁移）
uv run pytest tests/test_health_service*.py -q               # A2/A3 全绿
uv run pytest tests/test_rules_opera*.py -q                  # A5 规则直连
grep -n "layer.*rule\|layer=\"rule\"" docs/..../decision_logs # AC-37 留痕核对（或 SQL 查 decision_logs）
grep -rn "session\.add(\|from app.models" app/scheduler/     # 配合 B3：零命中
uv run pytest -q && uv run ruff check app tests              # H12 门禁
```

## 边界与注意

- 你的写域仅限：`alembic/versions/0017_*`、`alembic/versions/0018_*`、`app/models/health.py`、`app/models/report.py`、`app/services/health_service.py`、`app/services/notify_service.py`（增量）、`app/router/rules.py`（增量，仅播放控制 8 条）、对应 tests。不碰 graphs/scheduler/api/tool_registry/memory_service/media_service/前端。
- 通知落 `notifications` 表（0011 已建），不新建表；戏曲表字段照 TD §3.4 与 X8 冻结口径，media_url 只产稳定路径约定 `/static/media/opera/<media_id>.wav`。
- rules.py 只做「降级→直连」升级，不动 ASR/分类阈值（D5 评测门禁依赖阈值稳定，X9）。
- 规则直连不绕 ToolExecutor：每动作经 Tool 调用（G2 权限照走），不得在 rules.py 内直接写库。
