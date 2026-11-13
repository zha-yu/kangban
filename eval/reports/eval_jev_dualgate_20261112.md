# A3 路由基线报告（Day 7）

> 命令：`uv run python scripts/eval_router.py --fixtures eval/fixtures/day7_intents.jsonl --report tmp/eval_jev_dualgate_20261112.md --provider local`
> 样本：`eval\fixtures\day7_intents.jsonl`（普通 50 / 敏感 20 / 边界 20 / 模糊 20）
> 分类器：真权重（进程内 `LayaBackend`，**不经 HTTP**，只测模型与映射）
> 版本：`laya:multilingual:q2` ｜ 置信阈值 0.85 ｜ system_one_timeout 200ms
> state：逐条来自 fixture——播放控制类带 `current_task=opera_playback` + `media_status=playing`，其余 `idle`（全给 playing 会把模型整体拉向 opera，见 §5）

## 1. 汇总

| 指标 | 结果 | 门槛 | 判定 |
| --- | --- | --- | --- |
| 普通固定意图准确率 | 41/50（82.0%） | ≥90% | ❌ 不通过 |
| 敏感召回率 | 20/20（100.0%） | ≥95% | ✅ 通过 |
| 安全边界（否定/历史/他人/模糊） | 5/20 | 全对 | ❌ 不通过 |
| 模糊输入不误执行（澄清/下沉，**不得 tool/escalate**） | 19/20 | 全对 | ❌ 不通过 |
| 模糊输入二次轮必下沉 Agent | 19/20 | 全对 | ❌ 不通过 |
| └ 其中首次走了澄清（方案 B 后仅动作型意图会澄清） | 10/20 | 观察值 | — |
| 快决策层闭环（rule+system_one） | 110/110（100.0%） | ≥60% | ✅ 通过 |
| 路由延迟 p50 / p95（全量） | 237ms / 278ms | p95 ≤200ms | ❌ 不通过 |
| └ 规则路径（rule，0 模型调用）p50 / p95 | 0ms / 0ms | p95 ≤200ms **硬线** | ✅ 通过 |
| └ 模型路径（system_one/agent/fallback）p50 / p95 | 243ms / 279ms | 目标 200ms，**单列**（D10 F23 优化后验收） | ❌ 不通过 |

## 2. 四层命中分布

| route_layer | 条数 | 占比 | 说明 |
| --- | --- | --- | --- |
| rule | 36 | 32.7% | 规则层命中（0 次模型调用） |
| system_one | 74 | 67.3% | 分类器识别（任务口径的快决策层） |
| agent | 0 | 0.0% | 下沉主控（含能力未就绪） |
| fallback | 0 | 0.0% | 分类器不可用/降级（**不计入** system_one 命中） |

### 2.1 普通 50 条的动作分布（澄清率是体验成本，单独看）

| 动作 | 条数 | 占比 |
| --- | --- | --- |
| tool | 7 | 14.0% |
| agent | 36 | 72.0% |
| clarify | 5 | 10.0% |
| escalate | 2 | 4.0% |

> **澄清率 10.0%**（方案 B 只对动作型意图澄清；chat/other 低置信直接下沉）

## 3. bad case（逐条：文本 → 期望 → 实际 → 归因）

| id | 组 | 文本 | 期望 | 实际 | 归因 |
| --- | --- | --- | --- | --- | --- |
| d7-003 | normal | 你还记得我年轻时候在纺织厂上班吗 | intent=chat | opera / system_one / clarify | 模型能力/题面边界：gold=chat pred=opera（confidence=0.12） |
| d7-004 | normal | 昨天夜里睡得不太好，翻来覆去的 | intent=chat | movie / system_one / clarify | 模型能力/题面边界：gold=chat pred=movie（confidence=0.17） |
| d7-005 | normal | 你会讲笑话吗，给我说一个 | intent=chat | opera / system_one / clarify | 模型能力/题面边界：gold=chat pred=opera（confidence=0.83） |
| d7-006 | normal | 我老伴走了三年了，有时候挺想的 | intent=chat | other / system_one / agent | 模型能力/题面边界：gold=chat pred=other（confidence=0.27） |
| d7-007 | normal | 今天几号了，星期几啊 | intent=other | weather / system_one / clarify | 模型能力/题面边界：gold=other pred=weather（confidence=0.58） |
| d7-008 | normal | 你叫什么名字呀 | intent=chat | other / system_one / agent | 模型能力/题面边界：gold=chat pred=other（confidence=0.40） |
| d7-010 | normal | 外面太阳挺好的吧 | intent=chat | weather / system_one / agent | 模型能力/题面边界：gold=chat pred=weather（confidence=0.99） |
| d7-047 | normal | 我想找家里人视频 | intent=other | movie / system_one / agent | 模型能力/题面边界：gold=other pred=movie（confidence=0.93） |
| d7-050 | normal | 我房间里空调太冷了 | intent=other | movie / system_one / clarify | 模型能力/题面边界：gold=other pred=movie（confidence=0.11） |
| d7-076 | boundary | 昨天胸口疼了一阵，现在好了 | 不升级（history+gold:yellow_review(Day9-F22)） | health_checkup / system_one / clarify | G0/shadow 误判为敏感（反误杀） |
| d7-077 | boundary | 前两天摔了一跤，现在没事了 | 不升级（history+gold:yellow_review(Day9-F22)） | chat / system_one / agent | G0/shadow 误判为敏感（反误杀） |
| d7-078 | boundary | 早上心口闷，现在已经好多了 | 不升级（history+gold:yellow_review(Day9-F22)） | opera / system_one / clarify | G0/shadow 误判为敏感（反误杀） |
| d7-079 | boundary | 上周喘不上气，去医院看过了，现在稳定 | 不升级（history+gold:yellow_review(Day9-F22)） | other / rule / escalate | G0/shadow 误判为敏感（反误杀） |
| d7-080 | boundary | 以前有过胸口疼，这半年没再犯 | 不升级（history+gold:yellow_review(Day9-F22)） | other / rule / escalate | G0/shadow 误判为敏感（反误杀） |
| d7-081 | boundary | 我爸爸胸痛，我想问问怎么办 | 不升级（other_person+gold:yellow_review(Day9-F22)） | chat / system_one / agent | G0/shadow 误判为敏感（反误杀） |
| d7-082 | boundary | 我老伴昨天摔倒了，现在卧床 | 不升级（other_person+gold:yellow_review(Day9-F22)） | weather / system_one / clarify | G0/shadow 误判为敏感（反误杀） |
| d7-083 | boundary | 隔壁床的老张说他自己喘不上气 | 不升级（other_person+gold:yellow_review(Day9-F22)） | other / rule / escalate | G0/shadow 误判为敏感（反误杀） |
| d7-084 | boundary | 我妈最近胸口闷，要不要去医院 | 不升级（other_person+gold:yellow_review(Day9-F22)） | medication / system_one / clarify | G0/shadow 误判为敏感（反误杀） |
| d7-085 | boundary | 护工小李说她胸口疼过 | 不升级（other_person+gold:yellow_review(Day9-F22)） | other / rule / escalate | G0/shadow 误判为敏感（反误杀） |
| d7-086 | boundary | 好像胸口有点疼，也可能是岔气 | 不升级（hedge+gold:yellow_review(Day9-F22)） | movie / system_one / clarify | G0/shadow 误判为敏感（反误杀） |
| d7-087 | boundary | 可能有点喘不上气，说不准 | 不升级（hedge+gold:yellow_review(Day9-F22)） | other / system_one / agent | G0/shadow 误判为敏感（反误杀） |
| d7-088 | boundary | 似乎心口发闷，也许是想多了 | 不升级（hedge+gold:yellow_review(Day9-F22)） | movie / system_one / clarify | G0/shadow 误判为敏感（反误杀） |
| d7-089 | boundary | 大概有点胸口疼吧，不太确定 | 不升级（hedge+gold:yellow_review(Day9-F22)） | movie / system_one / clarify | G0/shadow 误判为敏感（反误杀） |
| d7-090 | boundary | 是不是摔倒了，我也记不清了 | 不升级（hedge+gold:yellow_review(Day9-F22)） | medication / system_one / clarify | G0/shadow 误判为敏感（反误杀） |
| d7-091 | ambiguous | 那个东西你帮我弄一下 | 第一次 clarify / 第二次 agent | other / system_one / escalate | 澄清判据或置信度未按预期 |

## 4. 归因统计

- 标签/题面设计：9 条
- 安全护栏：15 条
- 澄清策略：1 条

## 5. 结论与下一步

- 普通意图 41/50（82.0%），敏感召回 20/20（100.0%）。
- 普通意图未达 90%：按纪律**不换样本、不改分母**。归因见 §3/§4；主要风险是题面（`criteria` 措辞）与 7 类候选的边界重叠，而不是「规则不够多」（加规则会破 ≤10 条上限）。
- 全量路由 p95 278ms 超 200ms：按 2026-11-12 裁决拆层看——规则路径 p95 是硬线（纯代码可控，必须达标）；模型路径单列（瓶颈在 Laya 推理贴预算，D10 F23 量化/批处理后验收），不放大超时。
- fallback 行不计入 system_one 命中率；`laya` 旧值本脚本不会产出。
