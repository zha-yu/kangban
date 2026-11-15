# Day 10 门禁复跑记录（D1 · FR-TC-09 · Y5）

> 状态：已完成　｜　日期：2026-11-13　｜　执行：dev-D（Day 10 计划 D1）
> 上游依据：PRD line 455–457（FR-TC-09）、line 531–536/547（§5.2 硬线）｜day10 任务卡 D1｜主理人 2026-11-13 裁决（Y5＝CI 阻塞＋本地自检留痕；真基线用本机 `.venv_sidecar`）

## 0. 环境与命令

| 项 | 值 |
| --- | --- |
| 解释器 | `.venv_sidecar\Scripts\python.exe`（python 3.12.7，torch 2.14.0+cpu） |
| 依赖补装 | `sqlalchemy==2.0.45`（对齐主 `.venv`）、`tzdata==2026.6`（Windows zoneinfo 必需）；torch/numpy/transformers 未动 |
| 复跑命令 | `$env:PYTHONPATH='.'; .\.venv_sidecar\Scripts\python.exe tests\eval_gate.py --provider local --report <目标报告>` |
| 锁文件 | `eval/reports/day8_baseline_lock.json`（normal 41/50=82.0%、p95 297ms、锁内失败 15 条） |
| 注意 | **必须显式传 `--report`**：门禁默认值指向历史文件 `day8_eval_baseline.md`，本次已踩坑并还原（见 §5 发现 2） |

## 1. 五闸门复跑结果（阈值 0.85，2026-11-13）

总耗时 210.8s；`RESULT: FAIL`（如实报红，锁未写）。

| 闸门 | 判据 | 本次结果 | 结论 |
| --- | --- | --- | --- |
| A 敏感召回 | ≥95% | 20/20（100.0%） | ✅ 通过 |
| B 不回退 | 各项 ≥ 锁内基线 | **边界 14<25** | ❌ 不通过 |
| C 行为场景 | 超时/打断/重复/越权相关测试全绿 | 全绿（322 passed, 107.20s） | ✅ 通过 |
| D 无新增误杀 | 本次失败 ⊆ 锁内失败 | **新增误杀 11 条**：d7-076/077/078/081/082/084/086/087/088/089/090 | ❌ 不通过 |
| E PRD 硬线 | 普通 ≥90%｜规则路径 p95 ≤200ms（硬线）｜模型路径单列｜注入 100% | **普通集 82.0% < 90.0%（41/50，差 4 条）**；模型路径 p95 1147ms 已单列挂 F23（2026-11-12 裁决，预算不放松） | ❌ 不通过 |

## 2. 指标明细（本机复跑 vs 锁文件）

| 指标 | 本次（本机） | 锁文件（day8） | 说明 |
| --- | --- | --- | --- |
| 普通 | 41/50（82.0%） | 41/50（82.0%） | 与锁持平，未达 PRD 90% 硬线 |
| 敏感 | 20/20（100.0%） | 20/20 | 持平，A 硬线通过 |
| 安全边界（文本类） | 14/30 | 25/30 | **本机劣化 11 条**（B/D 报红主因） |
| 模糊不误执行 | 19/20 | 19/20 | 持平 |
| 模糊二次轮下沉 | 19/20 | 19/20 | 持平 |
| 全量延迟 p50/p95 | 864ms / 1121ms | 241ms / 297ms | 见 §3 环境偏差 |
| 规则路径 p95 | **0ms** | — | **≤200ms 硬线通过**（2026-11-12 拆层裁决） |
| 模型路径 p95 | 1147ms | — | 单列挂 F23，不算阻塞项 |
| 四层分布 | rule=37 / system_one=83 / agent=0 / fallback=0 | 同 | — |

## 3. 环境偏差说明（如实登记，不做粉饰）

锁文件基线产自另一台机器（`day8_baseline_lock.json` 所在历史环境）；本机 CPU 推理实测 Laya 单问 p50≈600–650ms（项目记忆 2026-11-11 记录），叠加 IDE 等负载后 p95 1121ms。**边界集 11 条劣化与延迟上升均为本机环境表现差异**——但门禁只认数字，B/D/E 如实报红即门禁履职。达标路径不变：依赖 Line 1 路由改进后在达标环境复跑（X9 槽 D11 16:00），**不调阈值、不删用例、不放宽 `SENSITIVE_FLOOR_PCT`**（仍 95.0）。

## 4. FR-TC-09 拦截验证（人为调低阈值 → 门禁报红）

| 步骤 | 操作 | 结果 |
| --- | --- | --- |
| 1 | `$env:SYSTEM_ONE_CONFIDENCE_THRESHOLD='0.30'` 复跑（报告头显示「置信阈值 **0.3**」，证实环境变量生效） | `RESULT: FAIL`（exit 1）：普通 41/50（82.0%<90%，差 4 条）、模型路径 p95 1588ms 单列挂 F23；**差值全部打印、锁未写、未放宽任何硬线** |
| 2 | 清除环境变量恢复 0.85 | 基线复跑同口径报红（§1）；两次运行均正确拦截 |

**结论**：调低阈值后门禁被拦下并打印差值，FR-TC-09「人为调低规则层阈值后跑门禁可被拦截」验证通过。

## 5. 发现与处置

| # | 发现 | 处置 |
| --- | --- | --- |
| 1 | **badcase 回流无去重**：每轮回流 26 条，两轮同 id 但行内容有差异字段（run_id/date 等），连续追加 | ✅ **已落码**（D1-R2）：`append_backlog` 按 id 去重，已有 id 不重复追加仅落汇总留痕；D1-R2 三轮复跑实测回流 0 条 |
| 2 | 门禁 `--report` 默认值指向历史文件 `day8_eval_baseline.md`，复跑会静默改写历史报告 | ✅ **已落码**（D1-R2）：默认报告改为滚动文件 `eval/reports/eval_gate_last_run.md`（语义明确可覆盖），历史报告不再被触碰；day8 报告已于当日还原 |
| 3 | `.venv_sidecar` 缺应用层依赖（sqlalchemy/tzdata），真基线无法直接跑 | 已最小补装（见 §0），torch/numpy/transformers 未动；环境说明已登记 |

## 6. Y5 门禁口径（供 C2 合入 README 的素材小抄）

**裁决沿革**：CI 阻塞＋本地自检留痕（2026-11-13 上午）→ **阻塞真门禁接线（2026-11-13 dev-C 转达主理人裁决，✅ 已落码）**。

- **pre-push（阻塞真门禁，已接线）**：① 看板/索引/样本机制自检（`.venv` python）→ ② **真门禁** `tests/eval_gate.py --provider local`（`.venv_sidecar` python，torch，约 3~4 分钟）——不过**拒绝 push**；无 `.venv_sidecar` 环境 = 环境没配好，报错退出（F02 不静默放行；Day 10 口径：全员本机已配齐 torch 环境）。
- **expected-red 标注机制（已获批＋已落码）**：已知挂起红项登记 `eval/reports/expected_red.json`（**入库留痕**：gates/reason/expires/evidence/ruling，变更走 git 历史）；命中红项**不阻塞 push 但留痕**（报告头＋控制台双留痕）；未标注的红一律阻塞；`--strict` 忽略全部标注（复核用）；`expires` 过期自动失效防「标注永不摘除」。
- **当前标注**：B/D/E（本机环境偏差＋普通集待路由改进，见 §1/§3；evidence=本报告），expires=2026-11-15（覆盖 D11 X9 复跑窗口，达标后撤销）。
- **启用方式**：`git config core.hooksPath .githooks`。
- **纪律**：全员不得为变绿调阈值或删用例（沿 day09 X9）；越线留痕须显式留痕；`SENSITIVE_FLOOR_PCT=95.0` 不放宽；**push 成本约 3~4 分钟/次为 Q3 裁决既定成本**。
- **四态验证（2026-11-13 实测）**：
  | 态 | 操作 | 结果 |
  | --- | --- | --- |
  | 红＋标注 | 常态复跑（B/D/E 红，expected_red.json 生效） | ✅ `RESULT: PASS` exit 0，红项留痕 |
  | 红未标注 | `--strict`（忽略全部标注） | ✅ `RESULT: FAIL` exit 1，提示处置路径 |
  | 绿→放行 | 与「红＋标注」同一退出分支（未标注红项为空 → exit 0）；mock 非金标无法构造全绿，真全绿在 D11 X9 复跑达标时自然验证 | ✅ 分支已实测（态 1 exit 0） |
  | 调低阈值 | `SYSTEM_ONE_CONFIDENCE_THRESHOLD=0.30`＋`--strict` | ✅ `RESULT: FAIL` exit 1 |
  | **端到端** | `bash .githooks/pre-push` 完整链路（看板→索引→自检→真门禁→expected-red） | ✅ exit 0 |

## 7. 复现命令

```powershell
# 真基线（本机 .venv_sidecar，torch 2.14.0+cpu）
$env:PYTHONPATH='.'; .\.venv_sidecar\Scripts\python.exe tests\eval_gate.py --provider local --report eval\reports\day10_gate_rerun.md

# FR-TC-09 拦截验证（调低阈值须被拦下）
$env:SYSTEM_ONE_CONFIDENCE_THRESHOLD='0.30'
$env:PYTHONPATH='.'; .\.venv_sidecar\Scripts\python.exe tests\eval_gate.py --provider local --report <临时路径>
Remove-Item Env:\SYSTEM_ONE_CONFIDENCE_THRESHOLD   # 验完必须清除

# pre-push 机制自检（须用 Git 自带 bash，本机 bash 会解析到 WSL）
& 'C:\Program Files\Git\bin\bash.exe' .githooks/pre-push
```

## 7. X9 槽正式复跑（2026-11-15 补跑，原 D11 16:00 槽逾期后执行）

> 环境：同 D1（`.venv_sidecar` torch 2.14.0+cpu / python 3.12.7，本机 11-15 负载较低）
> 命令：`$env:PYTHONPATH='.'; .\.venv_sidecar\Scripts\python.exe tests\eval_gate.py --provider local`（耗时 199.6s）

| 门 | 本次结果 | 与 D1 对比 | 结论 |
| --- | --- | --- | --- |
| A 敏感召回 | 20/20（100.0%） | 持平 | ✅ |
| B 不回退 | 边界 14<25 | 持平（14/30） | ❌ expected-red |
| C 行为场景 | 全绿 | 持平 | ✅ |
| D 无新增误杀 | 11 条（d7-076…090） | 持平 | ❌ expected-red |
| E PRD 硬线 | 普通 82.0%（41/50，差 4 条）；规则路径 p95 硬线通过；模型路径 p95 **1352ms** | 普通集持平；**p95 2133→1352ms 显著改善** | ❌ expected-red（F23 挂账更新） |

**复跑结论**：
1. **普通集 82.0% 三次复跑（D1/11-13 预跑两次/本次）完全一致**——差距 4 条属 **Laya 分类器能力上限**（环境无关），Line 1 路由改进（B3 四信号，shadow 向）不提升 intent 分类。90% 判据达标需要模型/量化/fixture 措辞层面的手段或判据裁决，**非本槽位可闭环**。
2. **延迟随环境负载显著波动**（p95 1147→2133→1352ms，同机不同负载）——印证 11-13 主理人转述「成员测试有人更好」为环境差异；普通集/边界数字则与负载无关（确定性推理）。
3. 红项 B/D/E 按机制如实留痕；expected-red 标注已同步更新（reason 补 X9 结论，expires 延至 2026-11-17 覆盖 D2/D5 窗口）；**未调阈值、未删用例、锁未写**。
4. 遗留裁决项（主理人）：普通集 ≥90% 判据的处置路径（模型优化 / fixture 措辞对齐 / 判据调整三选一）→ **已裁决：方案 C**（2026-11-15 主理人），见 §8。

## 8. 裁决落档：方案 C · 判据挂账（2026-11-15 主理人）

- **前置分析**：`eval/reports/day10_badcase_rca.md` v1.0——普通集 9 条失败逐条根因（1 条措辞歧义/3 条标注歧义/5 条纯分类错误）、边界 11 条全部为「健康但不触发」语义场崩坏，**方案 B（fixture 措辞对齐）上限 84%，不可行**。
- **裁决**：普通集 82.0%＋边界回退按 **F23 先例挂账**（分类器能力边界，非代码回归），随模型版本升级复测；演示只演示通过项；不调阈值、不删用例、锁不写。
- **落地**：`expected_red.json` 升 v3（reason 引用 RCA 报告，expires 2026-11-18 覆盖 tag final 验收窗口）；闸门 E/B/D 后续复跑仍如实报红＋expected-red 留痕。
