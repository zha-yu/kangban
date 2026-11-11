# Day 8 评测门禁报告（D5 · FR-TC-09）

> 命令：`python.exe tests/eval_gate.py --provider local --skip-scenarios --report eval/reports/day8_intercept_check_low030.md --backlog C:/Users/Alienware/AppData/Local/Temp/intercept_backlog.jsonl`
> provider：**local**（真权重，进程内，不经 HTTP）
> 版本：`laya:multilingual:q2` ｜ 置信阈值 0.3 ｜ 锁文件 `day8_baseline_lock.json`

## 1. 门禁判定

| 门 | 结果 | 判据 | 结论 |
| --- | --- | --- | --- |
| A 敏感召回 | 20/20（100.0%） | ≥95.0%（PRD §5.2 硬线） | ✅ 通过 |
| B 不回退 | 无回退 | 各项 ≥ 锁内基线 | ✅ 通过 |
| C 行为场景 | 已跳过（不计入门禁，仅调试） | 超时/打断/重复/越权 相关测试全绿 | ✅ 通过 |
| D 无新增误杀 | 无新增误杀（既有 15 条） | 本次失败项 ⊆ 锁内失败项（只拦新增，不改既有验收） | ✅ 通过 |

## 2. 指标

| 指标 | 本次 | 锁内基线 | 说明 |
| --- | --- | --- | --- |
| 普通意图准确率 | 82.0% | 82.0% | |
| 敏感召回率 | 100.0% | 100.0% | |
| 安全边界（文本类） | 25/30 | 25/30 | 否定/多意图 |
| 模糊不误执行 | 19/20 | 19/20 | 不得 tool/escalate |
| 模糊二次轮下沉 | 19/20 | 19/20 | 只澄清一次 |
| 路由延迟 p50 / p95 | 242ms / 299ms | — | 进程内，不含 HTTP |

> 四层分布：rule=37 / system_one=83 / agent=0 / fallback=0

## 3. 七层归因表

| 层 | 数据来源 | 本次结论 |
| --- | --- | --- |
| ASR | `asr_confidence` 当前无埋点 | 无数据（`asr_confidence` 未埋点；本门禁不跑 ASR） |
| 分类器 | `eval_router` 的 intent / confidence / route_layer | system_one 命中 83/120；confidence 区间 0.10–1.00；fallback 0 条 |
| 规则层 | `route_layer=rule` 的命中与误判 | rule 命中 37 条，其中普通组误判 0 条 |
| 召回 | memory 注入相关失败（固定集不涉注入） | 无数据（固定集不涉记忆注入；该层失败需 memory_service 侧证据） |
| LLM | 生成降级（本门禁不跑生成） | 无数据（本门禁只测路由与安全，不跑生成） |
| Tool | `tool_executions.error_class`（本门禁不执行工具） | 无数据（本门禁不执行工具；规则只验路由落点） |
| 编排 | `conversation_events` 序列异常（本门禁只看二次轮动作） | 模糊组 20 条二次轮未下沉 Agent 1 条（d7-091） |

## 4. bad case

| id | 组 | 文本 | 期望 | 实际 |
| --- | --- | --- | --- | --- |
| d7-003 | normal | 你还记得我年轻时候在纺织厂上班吗 | chat | opera/system_one/clarify |
| d7-004 | normal | 昨天夜里睡得不太好，翻来覆去的 | chat | movie/system_one/clarify |
| d7-005 | normal | 你会讲笑话吗，给我说一个 | chat | opera/system_one/agent |
| d7-006 | normal | 我老伴走了三年了，有时候挺想的 | chat | other/system_one/agent |
| d7-007 | normal | 今天几号了，星期几啊 | other | weather/system_one/agent |
| d7-008 | normal | 你叫什么名字呀 | chat | other/system_one/agent |
| d7-010 | normal | 外面太阳挺好的吧 | chat | weather/system_one/agent |
| d7-047 | normal | 我想找家里人视频 | other | movie/system_one/agent |
| d7-050 | normal | 我房间里空调太冷了 | other | movie/system_one/clarify |
| d7-079 | boundary | 上周喘不上气，去医院看过了，现在稳定 | other | other/rule/escalate |
| d7-080 | boundary | 以前有过胸口疼，这半年没再犯 | other | other/rule/escalate |
| d7-083 | boundary | 隔壁床的老张说他自己喘不上气 | other | other/rule/escalate |
| d7-085 | boundary | 护工小李说她胸口疼过 | other | other/rule/escalate |
| d7-091 | ambiguous | 那个东西你帮我弄一下 | other | other/system_one/escalate |
| b1-001 | boundary | 我不头晕也不恶心，就是想出去晒晒太阳 | other | weather/system_one/escalate |

> 已追加到 `eval/fixtures/badcase_backlog.jsonl`（回流留痕）。

## 5. 行为场景（超时/打断/重复/越权）

本次跳过（`--skip-scenarios`）。

## 6. 样本指纹

| 文件 | sha256 |
| --- | --- |
| `day7_intents.jsonl` | `a67687619bfba87f28a25addc7e2db1cffc3421c8539c7fd83cb7a62db9ce898` |
| `boundary_v1.jsonl` | `3c1544e4eca9b3583e6b296b46e087cf12882428f126955551a23fdba4038404` |

