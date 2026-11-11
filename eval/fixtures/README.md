# eval/fixtures · 路由评测固定集

> 纪律：**不换样本、不改分母、不删列**。要改集必须新建版本文件并在本表登记，旧版本保留。
> 达标与否如实写「不通过」，禁止为凑分改 `gold_intent`。

## 1. 固化集（基线，禁止改动）

| 文件 | 条数 | 分组 | 来源 | sha256 |
| --- | --- | --- | --- | --- |
| `day7_intents.jsonl` | 110 | 普通 50 / 敏感 20 / 边界 20 / 模糊 20 | 提交 `80fd7a7`（A3 路由层与评测基线）；Day 8 仅在 `d7-091` 追加 note | `a67687619bfba87f28a25addc7e2db1cffc3421c8539c7fd83cb7a62db9ce898` |

- 首次基线数字见 `eval/reports/day07_router_baseline.md`（真权重 local：普通 40/50、敏感 20/20、边界 16/20、模糊 19/20、p95 312ms）。
- **不变的部分**：条数 110、分组配比、每条 `text` / `gold_intent` / `is_sensitive` / `media_status` / `current_task` 一律不动。**唯一允许的改动是给 `note` 追加观察留痕**（本次仅 `d7-091`，见 §3），因为 note 不参与任何指标计算，改它不影响可比性；改了 note 就同步更新上表哈希。
- **没有**另存 `routing_v1.jsonl`：两份内容相同的副本会漂移，评测只认 `day7_intents.jsonl` 一个入口；版本用本表的哈希标识。
- 每一行的 `id` 在**全目录**内唯一（跨文件也不得重复），便于 bad case 回流时按 id 引用。

## 2. `boundary_v1.jsonl`（Day 8 新增，六类边界）

30 条，六类各 5 条。字段沿用 day7 schema 并新增 `boundary_class`，行为场景类再加 `scenario`。

| boundary_class | 条数 | 性质 | 由谁消费 |
| --- | --- | --- | --- |
| `negation` 否定 | 5 | 纯文本路由 | `eval_router`（group=boundary，判「不敏感且不升级」） |
| `multi_intent` 多意图 | 5 | 纯文本路由 | `eval_router`（group=boundary；gold 见下） |
| `timeout` 超时 | 5 | 行为场景 | `tests/eval_gate.py` 场景断言 |
| `barge_in` 打断 | 5 | 行为场景 | 同上 |
| `duplicate` 重复 | 5 | 行为场景 | 同上 |
| `scope` 越权 | 5 | 行为场景 | 同上（API 层 403＋审计） |

**gold 标注依据（多意图）**：PRD 第 138 行规则优先级「合法性校验 > 急症/敏感安全规则 > 进行中的用药/健康任务 > 固定意图 > 低置信澄清或 System 2 > 记忆个性化表达。低优先级不得覆盖高优先级。」故「用药/健康任务 + 固定意图」同时出现时取前者，**不按首句取**。

**为什么超时/打断/重复/越权不进 `eval_router`**：这四类的判定对象不是「意图标签」，而是状态机行为（降级码、停播时点、幂等水位、403 与审计）。用文本路由打分只会得到「不敏感、不升级」这种恒真结论，看着绿实则无信息。它们由 `tests/eval_gate.py` 的场景断言调用**既有测试套件**（`test_degrade_timeout.py` / `test_turn_service.py` / `test_tool_executor.py` / `test_api_workorders_family.py` / `test_elders_api.py` / `test_security.py`）承载，不新造第二套评测实现。

> 跑 `eval_router` 时只喂 `/tmp` 文本子集（`group=="boundary"` 的 10 条）。**不要**把整份 `boundary_v1.jsonl` 丢给 `eval_router`：`group=="scenario"` 的行不会被任何指标认领，却会进入 `total`，把四层命中率的分母算脏。

## 3. 观察登记（不入集、不加不减分）

| 来源 | 样本 | 处理 |
| --- | --- | --- |
| day07 `d-联调记录.md` L6#11 | 「我弄一下那个」类模糊话被误标 `opera`（conf 0.18–0.37），行为正确（走澄清/Agent）但标签噪音大 | 已把该观察写进 `day7_intents.jsonl` 中 `d7-091`「那个东西你帮我弄一下」的 `note`（**只加 note，不动 text/gold/分母**）。后续由门禁观察，不单独修分类器 |

## 4. 自查命令

```bash
# 1) 逐行可解析
uv run python -c "import json,pathlib; [json.loads(l) for l in pathlib.Path('eval/fixtures/boundary_v1.jsonl').read_text(encoding='utf-8').splitlines()]"

# 2) 六类计数各 ≥5、id 全局唯一
uv run python -c "
import json,collections,pathlib
rows=[json.loads(l) for f in ('day7_intents.jsonl','boundary_v1.jsonl') for l in pathlib.Path('eval/fixtures',f).read_text(encoding='utf-8').splitlines() if l.strip()]
print(collections.Counter(r.get('boundary_class') for r in rows if r.get('boundary_class')))
ids=[r['id'] for r in rows]; print('id 唯一:', len(ids)==len(set(ids)), len(ids))
"

# 3) 文本子集能被 eval_router 加载
uv run python -c "
import json,pathlib
rows=[json.loads(l) for l in pathlib.Path('eval/fixtures/boundary_v1.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
pathlib.Path('/tmp/boundary_text.jsonl').write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in rows if r['group']=='boundary'),encoding='utf-8')
"
```
