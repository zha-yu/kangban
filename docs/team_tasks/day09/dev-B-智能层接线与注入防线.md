> 状态：已完成（B1–B4 全部交付，未决全部闭合；遗留联调归 D10/D11）　｜　版本：v1.4　｜　日期：2026-11-12
> 负责人：**dev-B（唯一）**　｜　任务包：**B · 智能层接线与注入防线**　｜　容量：**20.5h ＝ 2.56 人日**（含 N0 执笔 0.5h）
> 关联：docs/team_tasks/day09/day09-任务总览与DAG.md v1.3（§4.2 分工、§5 对接点、§6 写域）｜docs/team_tasks/day09/contracts.md v0.3（**N0 牵头人**；X2/X3/X5/X7/X8/X10/X11/X12 消费与提供；**X6 已裁减**）
> 上游依据：PRD line 440（FR-ADM-06 闸门②③）、line 423–426（FR-MEM-04~07）、line 272（FR-DLG-07 历史/模糊判黄）、line 320–325（用药终态）、line 365–374（FR-HLT）、line 518（AC-36 注入）、line 487–497（AC-08/09/10/20/21/22）、line 547（路由 ≤200ms）｜技术设计文档 line 1017–1018（分级在代码＋话术＋HITL）、line 1169（注入集/test_injection）、line 1173（注入 100% 拦截）
> 遗留承接：**F03（Agent 侧）F05（子图注入）F06 F07（resume 扫描）F08 F09 F10（service）F14 F18 F19 F20 F21（rules）F22（safety）F27** —— 本包是 day08 遗留最重的一包（13 项）
> 变更记录见文末

---

# dev-B · Day 9 任务书 · 智能层接线与注入防线

## 0. 你的边界（先读这条）

### 0.1 写域仅限（越界即返工）

```
app/agents/**                         （companion / bind_tools / tool_registry）
app/mcp_client/**
app/router/**                         （decision / safety / rules）
app/scheduler/**                      （health_jobs / medication_jobs / memory_jobs / 清理 job）
app/graphs/**                         （health_checkup_graph / medication_confirmation_graph）
app/services/memory_service.py
app/services/tool_impls.py            （★ 唯一合法的 services→graphs 接缝，见 §0.3）
app/services/tool_executor.py
app/services/medication_service.py
app/main.py                           （★ 装配独占持锁人）
eval/fixtures/injection.jsonl         （新增）
tests/test_injection.py               （新增）
docs/team_tasks/day09/contracts.md    （N0 后持锁）
对应 tests
```

**本日不动**：`app/system_one/**`（F23 已按 PRD line 587 P2 顺延 Day 10）。

### 0.2 禁触

`app/models/**`、`alembic/**`（A）、`app/core/**`（A 的 redact/logging/runtime_context）、`health_service.py`／`notify_service.py`／`report_service.py`／`media_service.py`（C）、`app/api/**`（A/C/D）、`frontend/**`（C/D）、`tests/eval_gate.py`／`eval/fixtures/day7_intents.jsonl`（D）。**人设相关任何文件（`persona_service`／`context.py`／`api/personas`）本日无人持有，一律不碰。**

### 0.3 分层：你持有唯一的 services→graphs 接缝

AGENTS §2.2 规定 `api | audio | scheduler | graphs → services → models` 单向、禁横向 import。**既有唯一例外**：`app/services/tool_impls.py:16` 明载「依赖方向：tool_executor.py → 本模块 → services/ + graphs/ —— 不反向」，且 `:31` 已 import `medication_confirmation_graph`。

- **F14 健康语音入口沿用此先例**：在 `tool_impls.py` 新增 `make_health_checkup_executor`，照 `make_medication_confirm_executor`（`:53`）的形状写。**不新造第二条接缝**。
- **F07 复核 resume 不走此处**：`api` 禁 import graphs，故由 **C 写库＋你在 `health_jobs` 加扫描 job**（`scheduler → graphs` 合法）。见 X7。

### 0.4 高风险文件（AGENTS §5：先出方案再落代码＋人工逐行读 diff）

`app/agents/tool_registry.py`、`app/mcp_client/**`、`app/router/safety.py`、`app/graphs/**`、`app/services/medication_service.py`。**这五处必须先出方案经确认后才落代码。**

### 0.5 你是 N0 契约冻结牵头人

D9 09:00–09:30 主持 X1–X12 评审，四方签认后把 `contracts.md` 状态改「已冻结」。**这 0.5h 计入 B1。** 冻结后你是该文件持锁人，变更走 §7 留痕。

---

## 1. 节点 B1 · 契约冻结 ＋ MCP 双 server ＋ 幂等记账 ＋ 工具闸门（4h）

**前置**：无（**D9 上午第一个开工**，含 N0 0.5h）。
**对接**：**提供 X10/X11 的挂载侧**；**消费 X8**（迁移由 A 提供，本节点不依赖）；工具授权闸门供 A2 攻击用例验证。

### 1.1 现状实测（2026-11-12 取证）

- `app/agents/bind_tools.py:96-124` `MultiServerMCPClient` **完整实现但从未实例化**——引用只在 `:132/:137/:141`（自身 docstring 示例）与 `:156 __all__`。
- `app/main.py:373-376` 实际用 `MCPClient(settings)`；`app/mcp_client/client.py:302-317` 默认 `settings.mcp_weather_server_url`；`config.py:195-196` weather **7101** / operations **7102**。
- `app/agents/tool_registry.py:154-188` 两个 ops 工具（`create_work_order`／`send_family_notification`）标 `transport="mcp"`、`idempotent=True`，schema `additionalProperties: False` 但**无 `dedupe_token`/`dedupe_key` 属性** → 全部发往 **weather server**，7102 从未被连接。
- `app/services/tool_executor.py:315-317`：`if spec.transport == "mcp": return await self._run_mcp(...)` —— 在 `:327-336` 幂等重放与 `:338+` `tool_executions` 记账**之前**返回。
- `app/main.py:379-383` **已调用** `bind_dev_b_tools`（不回退）。

### 1.2 要做什么

1. **N0 契约冻结（0.5h）**：主持 X1–X12 评审，签认，改 contracts.md 状态。
2. **F18 双 server 接线（1.5h）**：`main.py` 改用 `MultiServerMCPClient`，两 ops 工具指向 **7102**、weather 保持 **7101**。保留 `bind_dev_b_tools` 既有接线。
3. **F19 幂等与记账（1.5h）**：MCP 分支纳入统一幂等重放与 `tool_executions` 记账；`tool_registry` 两 ops 工具 schema **补幂等键**（`dedupe_key`，`additionalProperties: False` 保持）。**schema 变更须走契约 §7 留痕**。
4. **工具授权闸门加固（0.5h）**：身份**只从 `runtime_context` 读**，模型传入的 `elder_id` 一律丢弃；越权 → 拒绝＋`audit_logs`＋`trace_id`（AC-29／闸门③）。确认 `MAX_TOOL_CALLS_PER_TURN=8` 与同工具 1 分钟第 3 次熔断对 **MCP 工具同样生效**（此前被绕过）。

### 1.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `contracts.md` 状态改「已冻结」＋四方签认记录 |
| 2 | `app/main.py` 多服务器客户端装配 |
| 3 | `app/agents/bind_tools.py` 启用（不再是 docstring 示例） |
| 4 | `app/services/tool_executor.py` MCP 分支纳入幂等＋记账 |
| 5 | `app/agents/tool_registry.py` 两 ops 工具 schema 补 `dedupe_key` |
| 6 | 契约 §7 schema 变更留痕 |
| 7 | 集成测试（双 server 路由／幂等／熔断／越权） |

### 1.4 量化验收

| 判据 | 期望 | 依据 |
| --- | --- | --- |
| 两 ops 工具调用目标 | 日志显示 **7102**；weather 显示 **7101** | TD §6.3 |
| `grep -n "MultiServerMCPClient" app/main.py` | **有命中**（不再是零调用） | F18 |
| 同 `dedupe_key` 重复调用 | 返回**原结果**，不重复建单/发通知 | AGENTS §8 |
| 同键异内容 | **409** | AGENTS §8 |
| `tool_executions` | MCP 调用**有行**（此前绕过记账） | F19 |
| 同工具 1 分钟内第 **3** 次 | **被熔断** | PRD FR-DLG-10 |
| 同轮第 **9** 次工具调用 | **被拒并告知用户** | PRD FR-DLG-10／Day 5 完成定义 |
| 模型以他人 `elder_id` 调工具 | **拒绝＋审计＋trace_id** | AC-29／AC-39③ |

```bash
uv run pytest tests/ -q -k "mcp or tool_executor or tool_registry"
uv run ruff check app tests && uv run ruff format --check app tests
```

**CP-B1**：报告 N0 签认记录＋8 条判据实测＋契约留痕位置。

---

## 2. 节点 B2 · 记忆向量／召回隔离／归并调度 ＋ 管理函数 ＋ 用药清理钩子（5.5h）

**前置**：N0。**无其他硬前置，D9 下午即可开工。**
**对接**：**提供 X4**（`purge_elder_memories` 给 A3）、**X5**（记忆管理三函数给 C 的 `api/memory.py`＋console 页）；闸门②供 A2 攻击用例验证。

### 2.1 现状实测

- `app/scheduler/memory_jobs.py:39 install_memory_scheduler`＋`:113 __all__`，`app/` 内**零调用**；`main.py:884-903` 只装 medication 与 health 两个调度器。
- `app/services/memory_service.py:457 embedding=None,  # pgvector 接入后填充`；`:76` 占位注释；`:135`「Day 6 骨架策略：**不接 LLM / embedding**，只做**模板匹配**」；`:238` 用 **difflib 代替 cosine**；`:517 ORDER BY importance DESC`。
- 记忆管理函数（查看/纠正/删除）**不存在**；`memory_admin_service.py` 与 `api/memory.py` 均缺（TD:1169 要求）。

### 2.2 要做什么

1. **F20 embedding 接线**：写入 `memory_chunks.embedding`（pgvector，维度照 TD §3.4 `vector(1536)`；**若实际模型维度不同，以模型实测为准并在 TD 修订记录留痕**，不沿用占位值）。
2. **F20 召回改 cosine**：删除 difflib 代用；**强制 `WHERE elder_id`**（闸门②）＋可见性过滤；召回失败 → 本轮不加载记忆，**不得臆造**（AGENTS §6）。
3. **F20 归并调度接线**：`main.py` 装 `install_memory_scheduler`；衰减（90 天）与归并（每周）按 FR-MEM-07。**scheduler 零写库**——只调 service。
4. **X5 记忆管理三函数**：在 `memory_service.py` 内新增 `list_memories`／`correct_memory`／`delete_memory`（**不新建 `memory_admin_service.py`**，避免两个 service 写同一张表；TD:1169 的模块名按此收敛，须在 TD 修订记录留痕）。纠正保留版本关系（旧值 `superseded`、新值生效）；删除后立即不再被召回。
5. **X4 清理钩子**：`purge_elder_memories(session, *, elder_id, reason) -> PurgeRef`，供 A3 退房/重置流水线调用。
6. **X12 用药清理钩子**：`medication_service.purge_elder_data(session, *, elder_id, reason) -> PurgeRef`，清 `medication_plans`／`medication_events`／`medication_attempts`。**审计由 A 统一写，你不重复写**；`reason` 取 `checkout`／`teaching_reset`；同键重复调用幂等。

### 2.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `memory_service.py` embedding 写入＋cosine 召回＋`WHERE elder_id` |
| 2 | `memory_service.py` X5 三函数＋X4 钩子；`medication_service.py` X12 钩子 |
| 3 | `main.py` 装记忆调度器 |
| 4 | 归并/衰减 job 可重复执行集成测试 |
| 5 | 跨长者零泄漏越权测试（供 A2 复用） |
| 6 | TD 修订留痕（模块名收敛／向量维度如与占位不同） |

### 2.4 量化验收

| 判据 | 期望 | 依据 |
| --- | --- | --- |
| `memory_chunks.embedding` | **非空**（新写入行） | F20 |
| `grep -n "difflib" app/services/memory_service.py` | **零命中** | F20 |
| 跨长者向量检索 | **零泄漏**（B 的记忆不出现在 A 的召回）＋审计 | AC-39②／FR-ADM-06② |
| 删除一条记忆后召回 | **不再出现**该条（含向量残留场景，二次过滤兜住） | FR-MEM-05/08／TD:1172 |
| 纠正一条记忆 | 旧值标 `superseded` **保留**、新值生效 | FR-MEM-03/06 |
| 构造两条高度相似记忆触发归并 | **输出归并记录** | FR-MEM-07／AC 验收原文 |
| `grep "session.add(\|from app.models" app/scheduler/` | **零命中** | AGENTS §2.1 |
| 召回失败 | 本轮不加载记忆，**不臆造**，有降级标记 | AGENTS §6 |

```bash
uv run pytest tests/ -q -k "memory"
uv run alembic current   # 向量列维度与迁移一致
```

**CP-B2**：报告 8 条判据实测＋X4/X5 签名冻结时点（**D9 下午冻结 X4，D10 上午冻结 X5**）。

---

## 3. 节点 B3 · 健康语音入口 ＋ 判黄 ＋ 红档注入 ＋ resume 扫描 ＋ 剧种规则（6h）

**前置**：B1（工具注册表同文件）。**软依赖（骨架先行，不等待）**：C1 的 X2、C2 的 X3、C4 的 X7。（原 C5 的 X6 已裁减移除。）
**对接**：消费 X2/X3/X7（**X6 已裁减，不再消费**）；**提供**路由入口稳定给 D2、路由改进完成给 D3。

### 3.1 现状实测

- `app/router/decision.py:84-86 NOT_READY_INTENTS = frozenset({"opera","movie","health_checkup","medication"})`；`:535-544` 命中即 `action="agent"`／`reason=capability_not_ready`。
- `app/agents/tool_registry.py` 共 **6 个工具**（`:107 memory_recall`／`:122 get_weather`／`:135 medication_confirm`／`:154 create_work_order`／`:172 send_family_notification`／`:190 play_opera`），`grep health app/agents/*.py` **零命中**。
- `app/graphs/health_checkup_graph.py:249-264 _default_notify_red` 返回 `{"delivered": False, "degraded": True, "channels": [], "note": "notify_service 尚未交付（dev-C）…"}`；注入缝 `:353 validate_fn`／`:358 notify_red_fn`／`:377`；**全仓仅两处构造点** `app/scheduler/health_jobs.py:261-263` 与 `:378-380`，**都只传 `session=`/`checkpointer=`**。`:267-288 _default_create_red_order` 同样带过期注释。
- `app/router/safety.py:203-205 RECOVERY_MARKERS → _not_sensitive("recovery")`；`:211-213 OTHER_PERSON_MARKERS → _not_sensitive("other_person")`；`:215-216 HEDGE_MARKERS → _not_sensitive("hedge")`（注释写「判黄不判红」但返回 not_sensitive）。
- 「这个数值有点高」/「我帮您告诉医生」/「问医生」在 `app/`、`frontend/src/`、`app/prompts/` **全部零命中**。
- `app/router/rules.py:133-240` 十条规则（暂停/继续/停止/打断/音量/换段/重复/天气/用药），**无越剧/京剧/剧种关键词**。
- resume 能力在 `health_checkup_graph.py:611 resume_health_checkup`／`:629 Command(resume=payload)`，唯一调用点是 `health_jobs.py:386` 的 `force_close=True`。

### 3.2 要做什么

1. **F14 健康语音入口**：`tool_impls.py` 新增 `make_health_checkup_executor`（照 `:53 make_medication_confirm_executor` 形状，走 §0.3 既有接缝）；`tool_registry` 注册健康工具（schema `additionalProperties: False`、身份只从 runtime_context、纳入预算与熔断）；`decision.py` 把 **`health_checkup` 移出 `NOT_READY_INTENTS`**。**`opera`/`movie` 是否移出取决于 C4/D5 的 media 接线完成情况，不许顺手全放开。**
2. **F22 边界判黄**：`safety.py` 三类边界（历史陈述/他人/模糊）改产出**黄档复核**——不判红、**也不放行**。急症一票否决优先级不变。**进行中任务优先级**（PRD §2.3：进行中用药/健康任务 > 固定意图）不得破坏。
3. **F05/F06/F08 子图注入**：两处构造点注入 `notify_red_fn=notify_red_dual`（X3）、`create_red_order_fn`、`validate_fn`（X2 的收缩压>舒张压校验）；**删除「notify_service 尚未交付」过期注释**；红档出口播话术「这个数值有点高，我帮您告诉医生」（**写代码常量，不写进 prompt**，TD:1017）。`_default_notify_red` 保留为注入缺失时的显式降级兜底。
4. **F07 resume 扫描 job**：`health_jobs.py` 新增 30s tick 扫描，按 X7 双条件（`review_status` 已终态 ＋ 线程仍 interrupt）resume。**scheduler 零写库。**
5. **F21 剧种规则**：`rules.py` 补越剧/京剧等剧种关键词直路由，**总数仍 ≤10 条（只做减法，含播放控制指令集）**。

### 3.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `tool_impls.py` 健康执行器＋`tool_registry` 注册 |
| 2 | `decision.py` 放开 `health_checkup` |
| 3 | `safety.py` 三类边界判黄 |
| 4 | `health_checkup_graph.py` 三处注入＋过期注释清除＋红档话术常量 |
| 5 | `health_jobs.py` resume 扫描 job（30s tick） |
| 6 | `rules.py` 剧种关键词（≤10 条） |
| 7 | AC-09/10/21/22 端到端集成测试＋trace_id 证据 |

### 3.4 量化验收

| 判据 | 期望 | 依据 |
| --- | --- | --- |
| 语音「我要量血压」 | 进健康子图 → 五问五字段 → 分级落库 | AC-09/10／FR-HLT-01 |
| 单分量 `182/null` | **立即红**，不等另一分量 | AC-21 |
| 黄档未关闭补录 `185` | 升级红、原 `risk_events.status` 保持 **open**、**不新建第二事件**、工单 severity 是建单快照不漂移 | AC-22 |
| 「昨天胸口疼」（历史陈述） | **黄档复核任务**（不是放行、不是红） | FR-DLG-07／PRD:272 |
| 「我有点不舒服」（模糊） | **只澄清一次**，仍模糊升级 L2 | PRD §2.3 |
| 真急症「我胸口疼」 | 仍**立即红**＋工单＋通知，**不进大模型** | AC-04 |
| 敏感集召回 | **≥19/20** 不回退 | PRD:531-536 |
| 红档 TTS 文本 | 含「这个数值有点高，我帮您告诉医生」 | TD:1017 |
| `grep -rn "尚未交付" app/graphs/health_checkup_graph.py` | **零命中** | F05 |
| console 复核后 | **≤30s** 子图恢复并落终态；重启后仍可恢复 | X7／FR-HLT-08 |
| `grep -c` 规则条数 | **≤10** | AGENTS §7 |
| 固定意图路由耗时 | **≤200ms**（不含 ASR/TTS/外部接口） | PRD:547 |
| `grep "session.add(\|from app.models" app/scheduler/` | **零命中** | AGENTS §2.1 |

```bash
uv run pytest tests/ -q -k "health_graph or safety or rules or decision"
uv run python scripts/eval_router.py --fixtures eval/fixtures/day7_intents.jsonl --provider local
```

**CP-B3**：报告 AC-09/10/21/22 四条实测 trace_id＋判黄三类用例＋路由耗时＋规则条数。**B3 完成即 M5.2 的 B 侧判据达成，并解锁 D2/D3 的联调段。**

---

## 4. 节点 B4 · 用药死锁 ＋ late_taken ＋ 72h 清理 ＋ 注入防线 ＋ 假承诺拆除（5h）

**前置**：B1（复用越权拒绝路径）。**软依赖**：A1 的 X8 迁移（**骨架先行**，D9 12:00 后联调）。
**对接**：消费 X8；**提供**注入防线完成给 D3。

### 4.1 现状实测

- `app/scheduler/medication_jobs.py:347`：`if attempt_no >= MAX_ATTEMPTS: logger.info("…resume_skip_exhausted…"); return`；而 `app/graphs/medication_confirmation_graph.py:81 MAX_ATTEMPTS = 4`，`:386-393 retry` 在 `attempt_no + 1 > 4` 时才写终态，`:395-412 finalize`（含 AC-20 **唯一分流口** `_split_missed_by_receipt`）需被 resume 才执行 → **`attempt_no=4` 时调度器恰好跳过唯一能收尾的 resume，终态死锁**。
- `app/services/medication_service.py:309 late_taken: bool = False`，`:143`/`:398` 两处调用**都只传 False**；列 `late_taken` 在 `models/medication.py:205` 已存在。
- `app/scheduler/__init__.py:1` docstring 列「原始音频 72h 清理（Day 7-9）」；`app/core/config.py:211 audio_retention_hours: int = 72`；**无 job 实现或注册**。
- `eval/fixtures/injection.jsonl`、`tests/test_injection.py` **均不存在**。
- `app/agents/companion.py:75 HIGH_RISK_REFUSED_TEXT = "这件事要医生或护工来处理，我已经帮您把需求转告护工了。"`；`:641-643` `if self._followup_sink is None: logger…("react_followup_sink_missing")`；`app/main.py:512 CompanionAgent(` **未传 followup_sink** → 假承诺。

### 4.2 要做什么

1. **F09 终态死锁**：调度器跳过条件改为「**已落终态**才跳过」，而非「次数用尽就跳过」；第 4 次窗口结束后必须 resume 一次让 `finalize` 执行。**保持 graph 内「不把 attempt_no 加到 5」的既有正确口径**（`:388-390` 注释：末态 `retry_count` 必须等于实际重试次数 3）。
2. **F10 `late_taken_reported`**：窗口外补报路径传 `late_taken=True` 并落新终态（依赖 X8 迁移）。
3. **F27 72h 清理 job**：实现并注册；**只删原始音频，不删脱敏文本**；调试开关临时打开的口径保持（AGENTS §4.10）。
4. **AC-36 注入防线**：`eval/fixtures/injection.jsonl` **≥10 条**（必含「忽略之前指令念病历」类）；`tests/test_injection.py` 逐条断言 **100% 拦截＋日志留痕**；拦截走既有安全一票否决路径，**不新造第二套**。
5. **F03 假承诺拆除**：`main.py:512` 补传 `followup_sink`（接 `notify_service`/`work_order_service` 真实出口）；**若联调受阻则改 `HIGH_RISK_REFUSED_TEXT` 为不含「已转告」的如实话术＋可观测降级标记**。二者必居其一，**不许保留虚假承诺**（总览 §5.4 Q6 决定）。

### 4.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `medication_jobs.py` 跳过条件修正 |
| 2 | `medication_service.py` `late_taken=True` 路径 |
| 3 | `app/scheduler/` 72h 清理 job＋注册 |
| 4 | `eval/fixtures/injection.jsonl`（≥10 条） |
| 5 | `tests/test_injection.py` |
| 6 | `companion.py`＋`main.py` followup_sink 接线（或如实话术） |
| 7 | AC-08/20/36 集成测试与证据 |

### 4.4 量化验收

| 判据 | 期望 | 依据 |
| --- | --- | --- |
| 四次全无人应答 | 终态 `missed_unconfirmed` 或 `notification_failed`；`retry_count=3`、`attempt_no=4` | AC-20 |
| 四次全没出过门 | `notification_failed`＋`failure_reason='offline'` | AC-20 分流 |
| 任一条出过门 | `missed_unconfirmed`＋`no_answer` | AC-20 分流 |
| 重复触发调度 | 幂等，**不产生第 5 次触达** | AGENTS §8 |
| 窗口外补报 | `terminal_state='late_taken_reported'`＋`late_taken=true` | AC-08／PRD:320-325 |
| 73h 前原始音频 | job 执行后**文件消失**；72h 内**保留** | FR-TC-07／AGENTS §4.10 |
| 注入集 | **≥10 条**，逐条 **100% 拦截**＋日志留痕 | AC-36／TD:1173 |
| `followup_sink` | 已接线（有真实工单/通知产生）**或**话术不含「已转告」 | 总览 Q6 |
| `MAX_ATTEMPTS` | 仍 **4**，四时点 T0/+5/+10/+15 与 60s 窗口不变 | PRD §7 硬性数值 |

```bash
uv run pytest tests/test_injection.py -q
uv run pytest tests/ -q -k "medication_scheduler or medication_graph or audio_retention"
grep -rn "已经帮您把需求转告" app/    # 若有命中，followup_sink 必须已接线
```

**CP-B4**：报告 AC-20 两条分流分支实测终态＋注入集条数与拦截率＋清理 job 前后文件对照。

---

## 5. 执行记录与对接登记（待回填）

| 日期 | 节点 | 提交 | 验收结果 | 等待项 |
| --- | --- | --- | --- | --- |
| — | N0 | — | — | — |
| — | B1 | — | — | — |
| — | B2 | — | — | — |
| — | B3 | — | — | — |
| — | B4 | — | — | — |

**对接登记**：

| 对接点 | 对方 | 我提供/消费 | 冻结时点 | 联调时点 | 状态 |
| --- | --- | --- | --- | --- | --- |
| N0 契约冻结 | 全员 | **牵头** | D9 09:30 | — | 待 |
| X2 分级签名 g2 | C1 | 消费 | N0 | D9 12:00 | 待 |
| X3 红档双通知出口 | C2 | 消费 | N0 | D10 15:00 | 待 |
| X4 记忆清理钩子 | A3 | **提供** | D9 下午 | D10 16:00 | 待 |
| X12 用药清理钩子 | A3 | **提供** | D9 下午 | D10 16:00 | 待 |
| X5 记忆管理三函数 | C（api/memory＋console） | **提供** | D10 上午 | D11 10:00 | 待 |
| ~~X6 PersonaView~~ | ~~C5~~ | **已裁减，不消费** | — | — | 作废 |
| X7 review_status → resume | C4 | 消费 | N0 | D11 14:00 | 待 |
| X8 迁移 0020 | A1 | 消费 | N0 | D9 12:00 | 待 |
| X10 素材规范 | D5 | 消费（media 调用侧） | N0 | D11 10:00 | 待 |
| X11 退避函数挂载 | C3 | **提供 cron 挂载** | N0 | D10 15:00 | 待 |
| 路由入口稳定 | D2 | **提供** | — | D11 上午 | 待 |
| 路由改进完成 | D3 | **提供** | — | D11 16:00 | 待 |
| 闸门②③加固 | A2 | **提供**（被攻击面） | — | M5.1 前 | 待 |

**高风险文件方案登记**（tool_registry／mcp_client／safety.py／graphs／medication_service，落代码前必填）：待填。

---

## 6. 风险与回退

| # | 风险 | 处置 |
| --- | --- | --- |
| R-B1 | 本包承接 13 项遗留，是最重的一包 | 骨架先行＋软依赖不等待；若超时按总览 §8 顺位，**B3 的 F14 健康语音入口与 B4 的 F09 死锁属 P0 不容顺延**；可顺延的是 F21 剧种规则（P1）与 F08 话术（P1） |
| R-B2 | MCP schema 补 `dedupe_key` 破坏既有调用方 | 走契约 §7 留痕＋通知 C/D；schema 保持 `additionalProperties: False`，新增字段设为可选并有默认构造规则 |
| R-B3 | 放开 `health_checkup` 后路由耗时超 200ms | 快路径只走规则/分类器，健康 Tool 调用不计入固定意图路由耗时口径（PRD:547 明确不含外部接口）；如实测超标回退为「澄清一次」而非放宽阈值 |
| R-B4 | 判黄改动导致敏感集召回回退 | 每次改 `safety.py` 后跑 110 条固定集；**≥19/20 是硬线**，回退即撤销该次改动 |
| R-B5 | resume 扫描与 `force_close` 竞争同一线程 | 沿用既有「先到先得」双驱动守卫（`tool_impls.py:62-63` 口径：checkpointer 必须与 SchedulerRuntime 同一个）；扫描前判 checkpoint 是否仍 interrupt |
| R-B6 | embedding 维度与 TD 占位值不符 | 以模型实测为准，**同提交在 TD 修订记录留痕**，不沿用占位 `vector(512)`/`vector(1536)` 猜测 |
| R-B7 | `main.py` 装配独占成为他人瓶颈 | 每日固定两个装配窗口（D9 17:00／D10 17:00／D11 12:00）集中合并 A/C/D 的装配需求，避免随时插队 |

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.2 | 2026-11-12 | **人设裁减同步**：B3 不再消费 X6（软依赖与对接登记移除），B3 联调槽位改为 X3（D10 15:00）／X7（D11 14:00）；禁触清单明确人设相关文件本日无人持有、一律不碰。包容量不变 20.5h。 |
| v1.1 | 2026-11-12 | 全量校验后修正：① B2 增 **X12 用药清理钩子** `medication_service.purge_elder_data`（供 A3 退房/重置编排，A 不直写 B 的表），B2 由 5h 调为 **5.5h**，包容量 20h→**20.5h＝2.56 人日**；② 排程校验后队列确定为 **B1→B2→B3→B4**（B3 骨架须在 D10 15:00 X3 联调前完成，实测 13.5h ✓）；③ X1 联调时点随 C 队列调整为 D10 12:00。程序校验：硬前置 11 边、软依赖 18 边**零违例**，五里程碑全达标。 |
| v1.0 | 2026-11-12 | 首版。承接 day08 遗留 13 项（F03 Agent 侧/F05 子图注入/F06/F07 resume/F08/F09/F10 service/F14/F18/F19/F20/F21 rules/F22 safety/F27）＋ Day 9 原生闸门②③加固与注入防线；4 节点 20h＝2.5 人日（含 N0 牵头 0.5h）。**明确持有唯一 services→graphs 接缝**（`tool_impls.py:16` 既有先例），F14 沿用不新造；F07 走 C 写库＋B scheduler 扫描。**X5 记忆管理函数收敛进 `memory_service.py`，不新建 `memory_admin_service.py`**（避免两 service 写同一张表，TD:1169 模块名须留痕）。`app/system_one/**` 本日不动（F23 顺延 Day 10）。 |