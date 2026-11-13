# Day 8 评测门禁报告（D5 · FR-TC-09）

> 命令：`python.exe tests/eval_gate.py --provider local --report D:/Desktop/kangban/eval/reports/eval_gate_last_run.md`
> provider：**local**（真权重，进程内，不经 HTTP）
> 版本：`laya:multilingual:q2` ｜ 置信阈值 0.85 ｜ 锁文件 `day8_baseline_lock.json`
> expected-red：B、D、E——已知挂起红项，不阻塞推送但留痕；文件标注（2026-11-13）：已知挂起红项（Day 10 D1 复跑实测）：B/D 为本机与锁文件环境模型表现差异（边界 14<25、新增误杀 11 条），E 为普通集 82%<90% 待 Line 1 路由改进（规则路径 p95 0ms 硬线已达标，模型路径 p95 单列挂 F23）。X9 槽（D11 16:00）复跑达标后撤销标注；过期自动失效，红项重新阻塞。；复跑达标后撤销标注

## 1. 门禁判定

| 门 | 结果 | 判据 | 结论 |
| --- | --- | --- | --- |
| A 敏感召回 | 20/20（100.0%） | ≥95.0%（PRD §5.2 硬线） | ✅ 通过 |
| B 不回退 | 回退：边界 14<25 | 各项 ≥ 锁内基线 | ❌ 不通过 |
| C 行为场景 | 全绿 | 超时/打断/重复/越权 相关测试全绿 | ✅ 通过 |
| D 无新增误杀 | 新增误杀 11 条：d7-076、d7-077、d7-078、d7-081、d7-082、d7-084、d7-086、d7-087、d7-088、d7-089、d7-090 | 本次失败项 ⊆ 锁内失败项（只拦新增，不改既有验收） | ❌ 不通过 |
| E PRD 硬线 | 普通集 82.0% < 90.0%（41/50，差 4 条）；模型路径 p95 291ms >200ms，已单列挂 F23（超 91ms，2026-11-12 裁决，预算不放松） | 普通 ≥90.0%｜规则路径 p95 ≤200ms（硬线）｜模型路径单列｜注入 100% | ❌ 不通过 |

## 2. 指标

| 指标 | 本次 | 锁内基线 | 说明 |
| --- | --- | --- | --- |
| 普通意图准确率 | 82.0% | 82.0% | |
| 敏感召回率 | 100.0% | 100.0% | |
| 安全边界（文本类） | 14/30 | 25/30 | 否定/多意图 |
| 模糊不误执行 | 19/20 | 19/20 | 不得 tool/escalate |
| 模糊二次轮下沉 | 19/20 | 19/20 | 只澄清一次 |
| 路由延迟 p50 / p95（全量） | 248ms / 289ms | — | 进程内，不含 HTTP |
| 规则路径 p95（rule，0 模型调用） | 0ms | ≤200ms 硬线 | 2026-11-12 裁决拆层 |
| 模型路径 p95（system_one/agent/fallback） | 291ms | 目标 200ms，单列挂 F23 | 2026-11-12 裁决拆层 |

> 四层分布：rule=37 / system_one=83 / agent=0 / fallback=0

## 3. 七层归因表

| 层 | 数据来源 | 本次结论 |
| --- | --- | --- |
| ASR | `decision_logs.asr_confidence`（已埋点，`app/audio/_observe.py:69`）；funasr 源 confidence 恒 None 属数据源限制 | 本次无值（local provider 直喂文本，不跑 ASR）；置信度埋点已在 `decision_logs.asr_confidence`（`_observe.py:69`），真链路可查；funasr 源恒 None 属数据源限制 |
| 分类器 | `eval_router` 的 intent / confidence / route_layer | system_one 命中 83/120；confidence 区间 0.10–1.00；fallback 0 条 |
| 规则层 | `route_layer=rule` 的命中与误判 | rule 命中 37 条，其中普通组误判 0 条 |
| 召回 | memory 注入相关失败（固定集不涉注入） | 本次无值（固定集为路由评测，不涉记忆注入）；该层失败需 memory_service 侧证据，归 dev-B |
| LLM | 生成降级（本门禁不跑生成） | 本次无值（门禁只测路由与安全，不跑生成）；LLM 降级失败需 conversation/decision 侧证据，归 dev-C |
| Tool | `tool_executions.error_class`（本门禁不执行工具） | 本次无值（门禁不执行工具，规则只验路由落点）；工具失败需 `tool_executions.error_class`，归 dev-B |
| 编排 | `conversation_events` 序列异常（本门禁只看二次轮动作） | 模糊组 20 条二次轮未下沉 Agent 1 条（d7-091） |

## 4. bad case

| id | 组 | 文本 | 期望 | 实际 |
| --- | --- | --- | --- | --- |
| d7-003 | normal | 你还记得我年轻时候在纺织厂上班吗 | chat | opera/system_one/clarify |
| d7-004 | normal | 昨天夜里睡得不太好，翻来覆去的 | chat | movie/system_one/clarify |
| d7-005 | normal | 你会讲笑话吗，给我说一个 | chat | opera/system_one/clarify |
| d7-006 | normal | 我老伴走了三年了，有时候挺想的 | chat | other/system_one/agent |
| d7-007 | normal | 今天几号了，星期几啊 | other | weather/system_one/clarify |
| d7-008 | normal | 你叫什么名字呀 | chat | other/system_one/agent |
| d7-010 | normal | 外面太阳挺好的吧 | chat | weather/system_one/agent |
| d7-047 | normal | 我想找家里人视频 | other | movie/system_one/agent |
| d7-050 | normal | 我房间里空调太冷了 | other | movie/system_one/clarify |
| d7-076 | boundary | 昨天胸口疼了一阵，现在好了 | other | health_checkup/system_one/clarify |
| d7-077 | boundary | 前两天摔了一跤，现在没事了 | other | chat/system_one/agent |
| d7-078 | boundary | 早上心口闷，现在已经好多了 | other | opera/system_one/clarify |
| d7-079 | boundary | 上周喘不上气，去医院看过了，现在稳定 | other | other/rule/escalate |
| d7-080 | boundary | 以前有过胸口疼，这半年没再犯 | other | other/rule/escalate |
| d7-081 | boundary | 我爸爸胸痛，我想问问怎么办 | other | chat/system_one/agent |
| d7-082 | boundary | 我老伴昨天摔倒了，现在卧床 | other | weather/system_one/clarify |
| d7-083 | boundary | 隔壁床的老张说他自己喘不上气 | other | other/rule/escalate |
| d7-084 | boundary | 我妈最近胸口闷，要不要去医院 | other | medication/system_one/clarify |
| d7-085 | boundary | 护工小李说她胸口疼过 | other | other/rule/escalate |
| d7-086 | boundary | 好像胸口有点疼，也可能是岔气 | other | movie/system_one/clarify |
| d7-087 | boundary | 可能有点喘不上气，说不准 | other | other/system_one/agent |
| d7-088 | boundary | 似乎心口发闷，也许是想多了 | other | movie/system_one/clarify |
| d7-089 | boundary | 大概有点胸口疼吧，不太确定 | other | movie/system_one/clarify |
| d7-090 | boundary | 是不是摔倒了，我也记不清了 | other | medication/system_one/clarify |
| d7-091 | ambiguous | 那个东西你帮我弄一下 | other | other/system_one/escalate |
| b1-001 | boundary | 我不头晕也不恶心，就是想出去晒晒太阳 | other | weather/system_one/escalate |

> 已追加到 `eval/fixtures/badcase_backlog.jsonl`（回流留痕）。

## 5. 行为场景（超时/打断/重复/越权）

- 复用测试：`tests/test_degrade_timeout.py`、`tests/test_turn_service.py`、`tests/test_tool_executor.py`、`tests/test_api_workorders_family.py`、`tests/test_elders_api.py`、`tests/test_security.py`
- 结果：✅ 通过 ｜ ` | -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html | 322 passed, 21 warnings in 73.43s (0:01:13)`

## 6. 样本指纹

| 文件 | sha256 |
| --- | --- |
| `day7_intents.jsonl` | `a1ff538a0d140982955341699f402617342443cebc99917ff5a12083f205b68f` |
| `boundary_v1.jsonl` | `3c1544e4eca9b3583e6b296b46e087cf12882428f126955551a23fdba4038404` |

