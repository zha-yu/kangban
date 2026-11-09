> 状态：未开始　｜　版本：v1.3　｜　日期：2026-11-09
> 负责人：dev-C（后端 · Agent 与统一出口）
> 关联：docs/plans/day06/day-06-长者侧Agent闭环.md（主计划 §3.1 节点表为唯一事实源）｜docs/team_tasks/day06/dev-a/dev-b/dev-d
> 上游依据：技术设计文档 line 822-834（§6.1 ReAct）、line 836-846（§6.2 护栏）、line 860-869（§6.4 四类失败）｜PRD line 269（FR-DLG-04）、line 270（FR-DLG-05）、line 275（FR-DLG-10）、line 516（AC-35 对话内用药确认）
> 变更记录见文末

# Task dev-C · Agent 与统一出口（Day 6）

## 1. 目标与边界

**一句话**：把"规则 / 分类器 / Agent / 工具"四条执行路径收敛到**同一个事实提交边界**，并让迟到的回调一律淘汰。

**负责节点**：N07、N11、N13、N14、N20、N24、N25、N27，合计 21.5 人时。

> v2.2 起新增 **N24/N25（天气 MCP server 与 `mcp_client`）**，见主计划 C-4 与 D-4。理由：这两块与 Agent 的工具调用同一条链路，放在同一人手里，`get_weather` 的 schema、错误映射、Agent 侧调用点三处口径一次对齐，不必跨人翻译。

> 计划 v2.0 起，原 N15（失败与降级矩阵）**并入 N14**：Day 5 已有 ASR/LLM/TTS 三档超时话术，本日只补"工具失败回传"一条分支，不单列节点。

**做**

- `ResponseStream`：`response_id` / `generation_epoch` / 分段 / ack 单调 / 打断淘汰。
- `CompanionAgent`：ReAct 循环（单轮 ≤8 次工具调用、同 Tool 熔断、高危不越级）。
- `handle_user_turn`：**唯一**事实提交边界（顺序不可乱）。
- 工具失败回传与降级分支（并入统一出口，不单列节点）。
- `mcp-servers/weather/`（`get_weather` + 缓存 + 无 key 明确失败）与 `app/mcp_client/`（发现/调用/超时/越权审计）。
- `app/router/safety.py` 最小一票否决（AC-04）：接在 `handle_user_turn` **之前**，命中即短路，不经 Agent。
- `KnowledgeService`（L 档，可裁）。
- 讲义 §12 状态实验脚本。

**不做**

- 不写 `app/router/**`、`app/system_one/**`（三级路由顺延，C-1/C-3）。
- 不改 `app/services/history_service.py`（dev-A）、`app/core/context.py`（dev-B）。
- 不碰前端（dev-D）。

**写作用域（白名单）**

```
app/services/response_service.py        新增
app/services/turn_service.py            新增
app/services/knowledge_service.py       新增（L 档）
app/agents/companion.py                 新增
app/router/safety.py                    新增（最小一票否决，不含 rules.py）
mcp-servers/weather/                    新增（server + tool schema + README）
app/mcp_client/                         新增（client + 错误映射 + 审计）
app/services/dialogue_service.py        仅改"调用目标"（L7 写锁，N14 后降为纯 LLM 调用）
app/audio/signaling.py                  仅新增 segment_ack 处理分支（与 dev-D 同锁 L6）
spikes/elder_agent_state_demo.py        新增
tests/test_response_service.py / test_turn_service.py / test_companion.py / test_mcp_weather.py / test_safety.py   新增
```

**禁触**：`alembic/versions/**`、`app/models/**`、`frontend/**`、`app/services/staff_service.py`。

## 2. 节点清单

| 节点 | 任务 | 人时 | 输入 | 输出 | 依赖 | 量化验收 |
| --- | --- | --- | --- | --- | --- | --- |
| N07 | ResponseStream | 4.0 | N03 契约、N05 状态 | `start` / `ack` / `interrupt` | N03、N05 | 迟到 ack 被拒；打断后 `delivered_text` 截断；ack 单调 |
| N13 | CompanionAgent | 4.0 | N06 上下文、N12 工具 | ReAct Loop | N06、N12 | 第 9 次调用被拒并告知；同 Tool 1min 第 3 次熔断 |
| N14 | 统一出口 `handle_user_turn` + 失败分支 | 3.5 | N04/N05/N07/N13 | `app/services/turn_service.py`；失败话术常量 | N04、N05、N07、N13 | 事件流水按序完整；generic 不落库；重复 `turn_id` 不重复处理；工具失败有可读话术 |
| N11 | KnowledgeService | 2.0 | N06 | 权威业务事实查询 | N06 | 用药计划结构化查询带 `elder_id`；无结果不编造 |
| N20 | 状态实验 | 1.0 | 讲义 §12 | 脚本 + PASS 输出 | — | `uv run python spikes/elder_agent_state_demo.py` 输出 PASS |
| N24 | 天气 MCP server | 2.5 | N00 契约；PRD FR-CON-01/02 | `mcp-servers/weather/`：`get_weather(city)`、缓存、无 key 明确失败 | N00 | server 独立可调；缓存 TTL 取 `weather_cache_ttl_seconds`；无 key 返回失败不编造 |
| N25 | `app/mcp_client/` | 2.5 | N00；N24 schema | 工具发现 / 调用 / 超时 5s / 越权审计 | N00、N24 | 超时与上游失败映射到四类失败；他人 `elder_id` 调用被拒并写 `audit_logs` |
| N27 | 安全一票否决（AC-04） | 2.0 | N00；PRD FR-DLG-01/09、AC-04 | `app/router/safety.py` + 词表/阈值 + `tests/test_safety.py` | N00 | 「我胸口疼」→ 红色事件 + 工单 + 通知，`escalated_to_agent=false`；50 例普通语料误报 ≤2 |

## 3. 逐节点执行步骤

### 3.1 N07 · 响应流与送达（本日最容易做错的一个）

1. 分段：**服务端**切（Z-2），按 `。！？；` 切，单段 ≤40 字，长句再按逗号补切；每段带

```json
{"response_id":"r17","epoch":8,"segment_id":2,"index":2,"total":3,"text":"出门记得带伞。","degraded":false}
```

2. 下发：`signaling.deliver(device_id, tts_text v2)`；**老客户端忽略新字段**仍能播（向后兼容）。
3. ack 入口：C→S `segment_ack {response_id, epoch, segment_id, status: playing|played|canceled, at_ms}`。校验顺序：

```
response 存在？ → epoch 与当前 session.epoch 一致？ → response.status == playing？
   → segment_id > last_acked_segment？（单调）
任一不满足：丢弃 + 打 response_ack_rejected 日志（含 rejected_reason）
```

4. `delivered_text` = 已 `played` 的段按 `index` 拼接；`delivery_status` 取 `delivered|interrupted|unknown`，**永远不写 `understood`**（讲义 §5.1）。
5. 打断：`interrupt(response_id)` → 停 TTS 队列 → `session.epoch += 1` → 取消 LLM/TTS 任务 → 追加 `response_interrupted` 事件 → `delivery_status=interrupted`。顺序**不可交换**（讲义 §5.4）。
6. 工具副作用不随回复取消：`ToolExecutor` 返回 `succeeded` 的事实必须保留，只停止播报（讲义 §5.5）。
7. 每段下发与每次 ack 各打一条 `response_segment` 指标（复用 `record_stage`，不新建表）。

### 3.2 N13 · CompanionAgent

1. 循环：`messages = build_turn_context(...)` → LLM（复用 `LLMService`，不新建 provider）→ 若返回工具调用则 `ToolExecutor.execute` → 结果回灌 → 再思考，直到出文本或触顶。
2. 护栏全部复用 `app/agents/guardrails.py`（`MAX_TOOL_CALLS_PER_TURN=8`、同 Tool 1min 第 3 次熔断、重复调用短路）——**禁止自建第二套**（`AGENTS.md` §4-8）。
3. 高危不越级：改药单 / 关工单 / 删数据一律 `denied` + 生成护工跟进任务（FR-DLG-04）。
4. 工具失败回灌：`bad_args` 回传模型重试并计入预算；`not_found` 如实告知；`transient` 退避 ≤2；`forbidden` 拒绝 + 审计。
5. 超预算收尾话术："这个我处理不了，帮您记下来稍后再试"（TD line 834 原话口径）。

### 3.3 N14 · 统一出口（顺序即契约）

```python
async def handle_user_turn(runtime, asr_final, *, call_id, turn_id) -> ResponseHandle:
    turn = await history.begin_turn(...)          # 1 事实先落
    snapshot = await state_center.snapshot(runtime.thread_id)   # 2 读一致状态
    await state_center.apply(thread_id, snapshot.version, [TurnStarted, AsrFinalCommitted])
    context = await context_builder.build(runtime, snapshot, turn)   # 3 组装
    result = await agent.run(context)             # 4 执行
    await history.commit_response(turn_id, result.response_id,
                                  result.generated_text, "", "playing")  # 5 先记生成
    await state_center.apply(..., result.state_events)                    # 6 再改状态
    return await response_stream.start(turn, result)                      # 7 最后下发
```

1. **顺序不可乱**：先提交用户定稿，再读状态，最后统一出口保存（讲义 §2.2）。
2. `generic` 模式：`begin_turn` 只写事件，`commit_response` 跳过 `conversation_turns`；`read_turns` 返回空。
3. 幂等：同 `(device_id, turn_id)` 第二次调用直接返回首次 `ResponseHandle`，不重复调 LLM（现 `DialogueOrchestrator._seen_turns` 的进程内去重保留，仅作快路径）。
4. `DialogueService` 降级为"纯 LLM 调用"，不再持有历史与写回；`RedisTurnStore` 仅作最近 5 轮缓存（主计划 R3）。
5. 接线点：`DialogueOrchestrator.handle_asr_final` 内部改调 `handle_user_turn`，**对外签名不变**（Z-1），`app/audio` 不 import `app/agents`。

### 3.4 N14 内嵌 · 失败与降级分支

> 原 N15 并入本节。**不新建话术体系**：ASR/LLM/TTS 三档沿用 Day 5 的 `degrade.ts` 与 `ErrorMsg` 枚举，本日只补工具层这一条。

| 场景 | 表现 | 落库 |
| --- | --- | --- |
| LLM 超时/限流（Day 5 已有三档） | 沿用 `llm_slow` / `llm_timeout` 话术，**不覆盖 Day 5 实现** | `turn_completed` + `status=failed` |
| 工具 transient 连续失败 | "这个我暂时查不到，帮您记下来" | `tool_failed` + `error_class=transient` |
| Redis 不可用 | 闲聊退化为单进程短会话；用药任务提示人工 | `state_degraded` |
| ASR 空文本 | 不进入本入口（`Orchestrator` 已拦），补一条断言日志 | 无 |

| 场景 | 表现 | 落库 |
| --- | --- | --- |
| LLM 超时/限流（Day 5 已有三档） | 沿用 `llm_slow` / `llm_timeout` 话术，不覆盖 Day 5 实现 | `turn_completed` + `status=failed` |
| 工具 transient 连续失败 | "这个我暂时查不到，帮您记下来" | `tool_failed` + `error_class=transient` |
| Redis 不可用 | 闲聊退化为单进程短会话；用药任务提示人工 | `state_degraded` |
| ASR 空文本 | 不进入本入口（`Orchestrator` 已拦），补一条断言日志 | 无 |

### 3.5 N20 · 讲义 §12 实验

1. 文件 `spikes/elder_agent_state_demo.py`，只依赖标准库，**不改生产代码**。
2. 跑通后把输出（`PASS: partial 未污染历史，旧回复未覆盖新状态，上下文只含已送达文本`）贴进主计划 §5 D16 或汇报清单。
3. **当场改动题**（讲义 §12 末）：把迟到回调的 epoch 改为 `session.epoch`，观察仍因 `status != playing` 被拒——把结论写进该脚本注释（为什么生产要**同时**校验 epoch 与 status）。

### 3.6 N11 · KnowledgeService（本版保留）

1. 只做"权威业务事实"：用药计划按 `elder_id` 结构化查询（**不做向量检索**，讲义 §9.2 明文）。
2. 每次返回带 `source` / `queried_at` / `filters` 元数据，供 ContextBuilder 打边界标签。
3. 无结果返回空并让 Agent 说"暂时查不到"，不补造（FR-CON-02 同口径）。
4. 机构文档 RAG 本日不做，登记为 Day 7 技术债。

## 4. 交付物规格

| 交付物 | 格式 | 质量红线 |
| --- | --- | --- |
| `response_service.py` | Python | epoch + status 双校验；ack 单调；打断顺序固定 |
| `companion.py` | Python | 复用 guardrails；预算 ≤8；高危拒绝 |
| `turn_service.py` | Python | 七步顺序固定；generic 不落库；`turn_id` 幂等 |
| `knowledge_service.py` | Python | 结构化查询带 `elder_id`；带来源元数据 |
| `elder_agent_state_demo.py` | Python | 标准库；PASS 输出；注释含当场改动结论 |

## 5. 自检

```bash
uv run python spikes/elder_agent_state_demo.py
uv run pytest tests/test_response_service.py tests/test_turn_service.py tests/test_companion.py -q
uv run pytest tests/test_guardrails.py tests/test_dialogue_integration.py -q    # 不得回归
uv run pytest -q
```

判据见主计划 §6 的 V6/V10/V11。

## 6. 对接与联调

| 时点 | 对象 | 我给出 | 我要到 | 判据 |
| --- | --- | --- | --- | --- |
| J3 W3 末 | dev-D | `tts_text` v2 字段表、`segment_ack` 校验规则 | 前端字段定义 | TypedDict 与 interface 逐字段对照通过 |
| J4 W4 末 | dev-A | Agent 侧工具调用点 | `execute()` 与 `ToolResult` | 四类失败各跑一条 |
| J5 W5 末 | dev-A、dev-B、dev-D | 真实一轮的 `trace_id` 与事件流水 | 事件落库、ack 落库 | 同一 `trace_id` 串起 asr→turn→response→ack |

## 7. 风险与回退

| 风险 | 处置 |
| --- | --- |
| 分段导致首包变慢 | 分段在**文本生成后**做，不预切 TTS；若首包 +>100ms 则退化为"整段下发 + 前端按标点分段"，服务端仍记 `segment_id` |
| 与 dev-D 抢 `signaling.py` | L6 锁：dev-D 起草契约，我复核后由**我**一次性改服务端分支 |
| 打断顺序写反（先取消再停声） | 单测断言调用顺序，不看结果看顺序 |
| `DialogueService` 改造打破 Day 3–5 | 保留 `respond()` 方法签名，只把内部数据源换掉；`test_dialogue_service.py` 必须绿 |

## 8. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 首版：按主计划 v1.0 拆 6 个节点；含 ack 校验四步、打断五步顺序、统一出口七步、降级矩阵、讲义实验与当场改动结论要求 |
| v1.1 | 2026-11-09 | 随主计划 v2.0 收紧：**N15 并入 N14**（N14 人时 3.0→3.5），节点 6→5，本卡合计 15.5→14.5；降级分支明确"不新建话术体系"，沿用 Day 5 三档 |
| v1.2 | 2026-11-09 | 随主计划 v2.2：**新增 N24/N25**（天气 MCP server + `mcp_client`，共 5.0h），节点 5→7、人时 14.5→19.5；写作用域加 `mcp-servers/weather/`、`app/mcp_client/`、`tests/test_mcp_weather.py` |
| v1.3 | 2026-11-09 | 随主计划 v2.3：**新增 N27**（`app/router/safety.py` 最小一票否决，AC-04 走 B，2.0h），节点 7→8、人时 19.5→21.5；**只做 safety，不做 rules.py/Laya/decision_logs** |
