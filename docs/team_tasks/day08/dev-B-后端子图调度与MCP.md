> 负责人：dev-B（后端 · 健康子图、调度与 MCP）　｜　日期：2026-11-10（Day 8）　｜　总人时 ≈ 8h
> 依据：docs/plans/day08/day-08-健康采集与三端后台.md v1.1 ｜ 对接点与全局节奏见同目录《day08-任务总览与DAG.md》

# dev-B 个人任务书

## 任务清单

| # | 任务（对应计划） | 人时 | 前置 | 交付物 | 验收标准（量化） |
| --- | --- | --- | --- | --- | --- |
| B1 | H0：契约冻结牵头 | 1h | — | 两份契约文档改「已冻结」+ 健康域端点编号分配（W/N/H 前缀惯例） | ① 《接口契约_照护台业务数据》v0.1→冻结、健康代填/复核/告警端点齐；② 《接口契约_家属端数据》v1.0 冻结评审；③ 变更走各文 §6 留痕；④ 09:15–09:45 组织全员评审，10:00 前发布冻结版（C/D 依赖其开工） |
| B2 | H3：健康决策子图 | 3.5h | A2（分级引擎签名，X1） | `app/graphs/health_checkup_graph.py` + HITL 桩测试 | ① 节点：collect（5 问循环，每问 60s，无效重问一次）→validate→grade（调 A2 纯函数）→green/yellow（工单+复核任务+护工 interrupt）/red（双通知+紧急工单+医生 interrupt）→persist；② interrupt + `Command(resume=...)`、checkpointer 独立 schema 复用；③ 双入口 source=voice_scheduled/voice_active（AC-34）；④ 决策依据写 graph state、`graph_thread_id` 回指；⑤ 图内业务落库只经 services；⑥ 演示：180→「等医生确认」interrupt→resume 继续（AC-10/21）；`get_state` 三分支可演示；重启不重复建单 |
| B3 | H8：health_jobs 调度 | 2h | B2、A3、C2 | `app/scheduler/health_jobs.py` + 集成测试 | ① 19:00 采集触发（source=voice_scheduled）、20:00 强制结束+漏采派单（FR-HLT-08）、日报生成触发、红色 2 分钟升级扫描、通知 30s 重试扫描；② 沿用 medication_jobs 驱动链：`grep "session\.add(\|from app.models" app/scheduler/` 零命中；③ 四 job 各有可重复执行集成测试；漏采工单 assignee=负责护工 |
| B4 | mock-operations 两工具注册 + AC-39 证据 | 1.5h | B2；写锁确认（站会确认 R6） | `app/agents/tool_registry.py` 注册 create_work_order/send_family_notification + `subject_elder_id` 注入 + 越权证据 | ① 两工具经 ToolExecutor 调通（1072→mock-operations 7102）；② 越权证据（AC-39）落 audit/测试留痕；③ 注册单测绿 |
| H12 | 门禁收口（参与） | 0.5h | 各线完成 | 自检证据 | 见总览 §5 |

## 当日时间线

- 09:15–10:00 B1 契约冻结（A1 并行中，互不等待）；10:00 发布冻结版给 C/D
- 10:00–12:00 B2 骨架（节点/边/state 定义）；**CP1 12:00 等 A2 签名后接 grade**
- 12:00–14:00 B2 完成 + 桩测试；14:00 起 B4（写锁已确认则可提前穿插）
- 15:00–16:00 B3 四 job；15:30 X5 演练提供图侧触发
- 16:30 X6 漏采工单→看板核对（给 C 造数据）
- 17:30 门禁；**溢出池 O3（H13 声纹调优，P1）：17:00 未开工即整体顺延 Day 9**

## 自检命令

```bash
grep -rn "session\.add(\|from app.models" app/scheduler/      # B3 零命中
grep -rn "from app.graphs" app/api/ app/audio/                # 零横向 import
uv run pytest tests/test_health_graph*.py tests/test_health_jobs*.py -q
uv run pytest tests/test_mock_operations* -q                  # B4
```

## 边界与注意

- 你的写域仅限：`app/graphs/health_checkup_graph.py`、`app/scheduler/health_jobs.py`、`app/agents/tool_registry.py`（与 tool_executor，站会确认写锁）、`mcp-servers/mock-operations`（如需）、对应 tests 与两份契约文档。不碰 models/services/api/前端。
- 图节点内不写 SQL；分级阈值不进 prompt（照 A2 常量）。
- 迁移 0017 由 dev-A 独占，你不碰。
