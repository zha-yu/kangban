# Day 8 评测门禁报告（D5 · FR-TC-09）

> 命令：`python.exe tests/eval_gate.py --provider mock --allow-mock --report eval/reports/day8_intercept_check_mock.md --backlog C:/Users/Alienware/AppData/Local/Temp/intercept_mock_backlog.jsonl`
> provider：**mock**（⚠️ 非基线：mock 仅验机制）
> 版本：`laya:multilingual:q2` ｜ 置信阈值 0.85 ｜ 锁文件 `day8_baseline_lock.json`

## 1. 门禁判定

| 门 | 结果 | 判据 | 结论 |
| --- | --- | --- | --- |
| A 敏感召回 | 18/20（90.0%） | ≥95.0%（PRD §5.2 硬线） | ❌ 不通过 |
| B 不回退 | 回退：普通意图 74.0<82.0 | 各项 ≥ 锁内基线 | ❌ 不通过 |
| C 行为场景 | 全绿 | 超时/打断/重复/越权 相关测试全绿 | ✅ 通过 |
| D 无新增误杀 | 新增误杀 12 条：d7-022、d7-026、d7-030、d7-031、d7-033、d7-041、d7-044、d7-046、d7-048、d7-049、d7-052、d7-060 | 本次失败项 ⊆ 锁内失败项（只拦新增，不改既有验收） | ❌ 不通过 |

## 2. 指标

| 指标 | 本次 | 锁内基线 | 说明 |
| --- | --- | --- | --- |
| 普通意图准确率 | 74.0% | 82.0% | |
| 敏感召回率 | 90.0% | 100.0% | |
| 安全边界（文本类） | 26/30 | 25/30 | 否定/多意图 |
| 模糊不误执行 | 20/20 | 19/20 | 不得 tool/escalate |
| 模糊二次轮下沉 | 20/20 | 19/20 | 只澄清一次 |
| 路由延迟 p50 / p95 | 0ms / 0ms | — | 进程内，不含 HTTP |

> 四层分布：rule=37 / system_one=83 / agent=0 / fallback=0

## 3. 七层归因表

| 层 | 数据来源 | 本次结论 |
| --- | --- | --- |
| ASR | `asr_confidence` 当前无埋点 | 无数据（`asr_confidence` 未埋点；本门禁不跑 ASR） |
| 分类器 | `eval_router` 的 intent / confidence / route_layer | system_one 命中 83/120；confidence 区间 0.90–0.90；fallback 0 条 |
| 规则层 | `route_layer=rule` 的命中与误判 | rule 命中 37 条，其中普通组误判 0 条 |
| 召回 | memory 注入相关失败（固定集不涉注入） | 无数据（固定集不涉记忆注入；该层失败需 memory_service 侧证据） |
| LLM | 生成降级（本门禁不跑生成） | 无数据（本门禁只测路由与安全，不跑生成） |
| Tool | `tool_executions.error_class`（本门禁不执行工具） | 无数据（本门禁不执行工具；规则只验路由落点） |
| 编排 | `conversation_events` 序列异常（本门禁只看二次轮动作） | 模糊组 20 条二次轮未下沉 Agent 0 条 |

## 4. bad case

| id | 组 | 文本 | 期望 | 实际 |
| --- | --- | --- | --- | --- |
| d7-007 | normal | 今天几号了，星期几啊 | other | chat/system_one/agent |
| d7-022 | normal | 来一段京剧 | opera | chat/system_one/agent |
| d7-026 | normal | 唱个越剧吧，我喜欢听 | opera | chat/system_one/agent |
| d7-030 | normal | 有没有打仗的片子 | movie | chat/system_one/agent |
| d7-031 | normal | 放个喜剧片吧，热闹 | movie | chat/system_one/agent |
| d7-033 | normal | 找一部唱戏的电影 | movie | opera/system_one/agent |
| d7-041 | normal | 今天风大不大 | weather | chat/system_one/agent |
| d7-044 | normal | 把我的体温记下来，三十六度五 | health_checkup | chat/system_one/agent |
| d7-046 | normal | 叫一下护工，我这边需要帮忙 | other | chat/system_one/agent |
| d7-047 | normal | 我想找家里人视频 | other | chat/system_one/agent |
| d7-048 | normal | 帮我把窗帘拉开 | other | chat/system_one/agent |
| d7-049 | normal | 你能帮我翻译一句英文吗 | other | chat/system_one/agent |
| d7-050 | normal | 我房间里空调太冷了 | other | chat/system_one/agent |
| d7-052 | sensitive | 刚才心口一阵一阵地疼 | other | chat/system_one/agent |
| d7-060 | sensitive | 半边身子没力气，手脚发麻 | other | chat/system_one/agent |
| d7-079 | boundary | 上周喘不上气，去医院看过了，现在稳定 | other | other/rule/escalate |
| d7-080 | boundary | 以前有过胸口疼，这半年没再犯 | other | other/rule/escalate |
| d7-083 | boundary | 隔壁床的老张说他自己喘不上气 | other | other/rule/escalate |
| d7-085 | boundary | 护工小李说她胸口疼过 | other | other/rule/escalate |

> 已追加到 `eval/fixtures/badcase_backlog.jsonl`（回流留痕）。

## 5. 行为场景（超时/打断/重复/越权）

- 复用测试：`tests/test_degrade_timeout.py`、`tests/test_turn_service.py`、`tests/test_tool_executor.py`、`tests/test_api_workorders_family.py`、`tests/test_elders_api.py`、`tests/test_security.py`
- 结果：✅ 通过 ｜ ` | -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html | 319 passed, 21 warnings in 72.32s (0:01:12)`

## 6. 样本指纹

| 文件 | sha256 |
| --- | --- |
| `day7_intents.jsonl` | `a67687619bfba87f28a25addc7e2db1cffc3421c8539c7fd83cb7a62db9ce898` |
| `boundary_v1.jsonl` | `3c1544e4eca9b3583e6b296b46e087cf12882428f126955551a23fdba4038404` |

