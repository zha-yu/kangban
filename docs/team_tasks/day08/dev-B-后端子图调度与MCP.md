> 状态：已完成　｜　负责人：dev-B（后端 · 健康子图、调度、MCP 与记忆治理）　｜　日期：2026-11-11（Day 8）　｜　版本：v1.2（v1.1＋完成证据回填）
> 依据：docs/plans/day08/day-08-健康采集与三端后台.md v1.2 ｜ 对接点与全局节奏见同目录《day08-任务总览与DAG.md》

# dev-B 个人任务书

## 任务清单

| # | 任务（对应计划） | 人时 | 前置 | 交付物 | 验收标准（量化） |
| --- | --- | --- | --- | --- | --- |
| B1 | H0：契约冻结牵头 + X8 戏曲链口径冻结 | 1h | — | 两份契约文档改「已冻结」+ 健康域端点编号（W/N/H 前缀惯例）+ **X8 四件口径**：media_url 稳定路径约定 `/static/media/opera/<media_id>.wav`、opera Tool schema（工具名+参数+返回）、规则动作→Tool/服务调用映射表、种子数据规范（20–30 条字段值域） | ① 《接口契约_照护台业务数据》v0.1→冻结、健康代填/复核/告警端点齐；② 《接口契约_家属端数据》v1.0 冻结评审；③ 变更走各文 §6 留痕；④ 09:15–09:45 组织全员评审，10:00 前发布冻结版（C/D 依赖其开工，X8 口径供 A5/C4/D4 直接落码） |
| B2 | H3：健康决策子图 | 3.5h | A2（分级引擎签名，X1） | `app/graphs/health_checkup_graph.py` + HITL 桩测试 | ① 节点：collect（5 问循环，每问 60s，无效重问一次）→validate→grade（调 A2 纯函数）→green/yellow（工单+复核任务+护工 interrupt）/red（双通知+紧急工单+医生 interrupt）→persist；② interrupt + `Command(resume=...)`、checkpointer 独立 schema 复用；③ 双入口 source=voice_scheduled/voice_active（AC-34）；④ 决策依据写 graph state、`graph_thread_id` 回指；⑤ 图内业务落库只经 services；⑥ 演示：180→「等医生确认」interrupt→resume 继续（AC-10/21）；`get_state` 三分支可演示；重启不重复建单；⑦ 桩测试绿后 14:00 向 D 交接跑评测基线（X9） |
| B3 | H8：health_jobs 调度 | 2h | B2、A3、C2 | `app/scheduler/health_jobs.py` + 集成测试 | ① 19:00 采集触发（source=voice_scheduled）、20:00 强制结束+漏采派单（FR-HLT-08）、日报生成触发、红色 2 分钟升级扫描、通知 30s 重试扫描；② 沿用 medication_jobs 驱动链：`grep "session\.add(\|from app.models" app/scheduler/` 零命中；③ 四 job 各有可重复执行集成测试；漏采工单 assignee=负责护工 |
| B4 | mock-operations 注册 + H14③ opera Tool + 素材种子 | 2.5h | B1（X8 冻结）；联调段等 A4（0018 表）+C4（服务就绪） | `app/agents/tool_registry.py` 注册 create_work_order/send_family_notification/**play_opera** + `subject_elder_id` 注入 + 越权证据 + `static/media/opera/` 程序生成 wav + media_library 种子 20–30 条（生成脚本随仓库留档） | ① mock 两工具经 ToolExecutor 调通（1072→mock-operations 7102）、越权证据（AC-39）落 audit/测试留痕；② opera Tool 经 ToolExecutor 调通（G2 权限照走）、注册单测绿；③ 素材替换演练一次成功（D-7：替换静态文件+更新元数据行，零代码改动）；④ media_history 有播放记录 |
| B5 | H15：记忆治理收尾（冲突检测+召回过滤+衰减归并） | 3.5h | B1（基线复核）；**不依赖健康主线**，冲突检测段可在等待 A2 签名的空档先行 | `app/services/memory_service.py` 增量（替换 L203/L250 deferred 占位）+ 召回过滤 + 衰减归并 job + 单测 | ① FR-MEM-03/06（P0）：新萃取候选与存量相似度 >0.9 时语义矛盾判定→旧记忆标 `superseded`、当前生效值唯一、两版本均保留；② FR-MEM-05：召回过滤排除 superseded/disputed；③ FR-MEM-07（P1，**R1① 超载先裁项**）：90 天衰减 + 每周归并 APScheduler job（scheduler 零写库）；④ 单测：「喜欢越剧→不喜欢越剧了」两版本均留、旧值 superseded、仅新值生效；删除记忆不再被召回注入；归并 job 对两条高度相似记忆输出归并记录 |
| H12 | 门禁收口（参与） | 0.5h | 各线完成 | 自检证据 | 见总览 §5 |

> 超载先裁序（R1 顺位核对）：① B5③ 衰减归并 job（裁后 B5 核心冲突检测+召回过滤仍须当日收口）→ 其余 B1–B4、B2、B3 均为健康主线/欠债核心，不容顺延。溢出池 O3（H13 声纹调优，P1）17:00 未开工即整体顺延 Day 9。

## 当日时间线

- 09:15–10:00 B1 契约冻结 + X8 四件口径（A1 并行中，互不等待）；10:00 发布冻结版给 A/C/D
- 10:00–12:00 B2 骨架（节点/边/state 定义）；**等待 A2 签名期间穿插 B5 冲突检测段**（不依赖健康线）
- 12:00 **CP1** 等 A2 签名后接 grade；12:00–14:00 B2 完成 + 桩测试
- 14:00 X9 交接：通知 D 跑评测基线
- 14:00–15:00 B4（mock 两工具 + opera Tool 注册 + 素材生成；opera 联调段等 A4/C4 就绪）
- 15:00–16:00 B3 四 job；**15:30 X5 演练**（图侧触发）
- 16:00 **CP4 戏曲端到端演练 AC-37**（Tool 侧证据：decision_logs + media_history）
- 16:00–17:30 B5 收尾（衰减归并 job；超载按 R1① 先裁）
- 16:30 X6 漏采工单→看板核对（给 C 造数据）；17:30 门禁

## 自检命令

```bash
grep -rn "session\.add(\|from app.models" app/scheduler/      # B3/B5 job 零命中
grep -rn "from app.graphs" app/api/ app/audio/                # 零横向 import
uv run pytest tests/test_health_graph*.py tests/test_health_jobs*.py -q
uv run pytest tests/test_mock_operations* -q                  # B4 mock 工具
uv run pytest tests/test_memory_service*.py -q                # B5 冲突/召回/归并
uv run pytest tests/test_tool_registry*.py -q                 # play_opera 注册
```

## 边界与注意

- 你的写域仅限：`app/graphs/health_checkup_graph.py`、`app/scheduler/health_jobs.py`、衰减归并 job 文件、`app/agents/tool_registry.py`（与 tool_executor，站会确认写锁）、`app/services/memory_service.py`（增量，v1.1 扩域）、`mcp-servers/mock-operations`（如需）、`static/media/opera/`（种子素材+生成脚本）、对应 tests 与两份契约文档。不碰 models/迁移/api/health_service/notify_service/report_service/media_service/rules.py/前端。
- 图节点内不写 SQL；分级阈值不进 prompt（照 A2 常量）；冲突判定语义矛盾用统一 LLM 适配器（既有），不得绕过。
- 迁移 0017/0018 由 dev-A 独占，你不碰。
- X8 四件口径冻结后不得私改；opera Tool 参数/返回只按冻结 schema，变更走契约 §6 留痕。

## 完成记录（2026-11-11 回填）

| # | 交付 | 证据 |
| --- | --- | --- |
| B1 | 两份契约冻结 + X8 四件口径 | 《接口契约_照护台业务数据》《接口契约_家属端数据》状态行「已冻结」，X8 四件写入，变更走 §6 留痕 |
| B2 | 健康决策子图 | `app/graphs/health_checkup_graph.py`，三分支双 HITL + 双入口；commit `7793643`（X1 接线与三分支端到端转绿）；AC-10/21（bp 180 interrupt→resume）、AC-09（bp 160 护工确认）、AC-34（source=voice_active）用例全绿 |
| B3 | health_jobs 四 job | commit `a165b7e`；AC-12 升级扫描（原事件标 escalated 不建第二事件）、AC-23 通知 30s 重试、漏采派单 assignee=负责护工；scheduler 零写库 grep 零命中 |
| B4 | 三工具注册 + 身份注入 + 素材种子 | commit `1b37981`；AC-39 双留痕（audit_logs 行 + `tool_uses_context_elder_id_ignores_model_arg` / `cross_elder_call_rejected_audited` 用例）；`static/media/opera/` 种子 + 生成脚本随仓库 |
| B5 | 记忆治理收尾 | Step 3 冲突检测（FR-MEM-03/05：`conflict_flips_preference_keeps_two_versions` / `superseded_not_recalled` / `deleted_not_recalled`）；Step 7 衰减归并 commit `6eb8a73`（FR-MEM-07：`test_consolidation_outputs_merge_record_for_similar_pair` + 90 天边界用例）；**未触发 R1① 顺延** |
| H12 | 门禁自检 | `uv run pytest -q` 1301 passed 0 skip；`ruff check app tests` + `ruff format --check app tests` 全绿；scheduler 零写库 grep 零命中；`from app.graphs` 于 app/api、app/audio 零横向 import |

**未关闭项（→ Day 9）**：O3 声纹调优（溢出池，17:00 前主线收口但互测名单未确认，按裁决②整体顺延）。
