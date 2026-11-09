> 状态：草稿　｜　版本：v1.1　｜　日期：2026-11-09
> 关联：docs/team_tasks/day06/dev-c-Agent与统一出口.md（任务卡 v1.3）｜docs/plans/day06/day-06-长者侧Agent闭环.md（主计划 v3.0，节点规格唯一事实源）｜docs/plans/day06/d-DAG.md（顺序源 v1.0）｜docs/team_tasks/day06/contracts.md（冻结契约 v1.0）｜dev-a/dev-b/dev-d 三张同日任务卡
> 上游依据：技术设计文档 line 822-834（§6.1 ReAct）、line 836-846（§6.2 护栏）、line 860-869（§6.4 四类失败）｜PRD line 266-275（FR-DLG-01~10）、line 485（AC-04）、line 516（AC-35）、FR-CON-01/02｜AGENTS.md §5（安全审查清单）、§7（硬性数值）
> 变更记录见文末

# dev-C · Agent 与统一出口（Day 6）工作推进计划

> 本计划是任务卡 dev-C v1.3（8 节点 / 21.5 人时）的**执行计划**：只回答"按什么顺序做、每步做到什么程度算完、拿什么命令自检"。节点内容规格以主计划 §3.1 与冻结契约为准，本文不另立口径；与两者冲突时**以主计划 + 契约为准**并回改本文。

---

## 0. 本轮已确认裁决（2026-11-09，主理人答复，本计划执行基线）

| # | 事项 | 裁决 |
| --- | --- | --- |
| C-0-1 | 容量基线 | **全量照任务卡**：8 节点（N07/N11/N13/N14/N20/N24/N25/N27）全部保留在计划内，不做预裁减，N11 不预登记技术债。此裁决与主计划 §8「S 档超容量」的裁减建议**不同**，按主理人本轮指令执行；若执行中途确需裁减，**回到主理人重新裁决**，不得自行砍节点 |
| C-0-2 | 计划文件名 | `docs/plans/day06/dev-c-Agent与统一出口.md`（对齐 Day5 的 `dev-c-` 全称风格），同批次登记进 `docs/plans/README.md` 索引 |
| C-0-3 | 依赖策略 | **沿用先桩后并**（Day 5 主理人已确认的纪律）：N07/N11/N13 的硬依赖（N03/N05/N06/N12）在 dev-A/B/D 手里且未开工，dev-C 按冻结契约 `contracts.md` §4/§5 的已定签名先行实现 + 本地桩自测，联调通知到达后只做对齐，人等链不空转 |
| C-0-4 | Q-1 · 50 例普通语料来源 | **dev-C 自拟**：50 句日常语句入 `tests/fixtures/`（只进测试不进业务库），在本计划 §0.2 留痕，主理人抽查；语料用途 = N27 安全一票否决的**误报率分母**（对齐 PRD FR-DLG-01 正向不漏杀 + 主计划 R13 反向不误杀） |
| C-0-5 | Q-2 · N27「通知」环节 | **方案 A：提前建表**。新增迁移 `0011` 建 `notifications` 表（TD line 659 口径：所有外发通知统一落库，无 wechat）+ `app/models/` 补 ORM；N27 命中时写一条 `status='generated'` 的护工通知记录，**落库即视为已通知**（30s 重试/未读列表等通知状态机与调度器职责仍留 Day 8，TD line 1024）；家属侧 `send_family_notification` 仍走 Day 8 `mock-operations`，本日不做 |
| C-0-6 | C-0-5 的建表人 | **授权 dev-C 建**：主理人解禁 `alembic/versions/**` 与 `app/models/**`，**仅限** `0011_day6_notifications.py` 一笔迁移 + `notifications` 一张表的 ORM；其余禁触目录与文件照旧。依据：2026-11-09 主理人答复；任务卡 v1.3 §1 禁触条款由本裁决覆盖，任务卡变更记录由本计划 §9 留痕（下次改任务卡时同步） |

### 0.1 已裁决项落地口径（C-0-4~C-0-6 的执行细则）

1. **0011 迁移规范**：照 0008~0010 同套约束——UUID 主键、CHECK 不用 PG ENUM、可 downgrade、不写死连接；字段照 TD line 659-675 的 `notifications` 定义（`notification_id / target_type / target_id / source_type / source_id / title / content / status / dedupe_key（部分唯一）/ created_at / delivered_at / acknowledged_by / acknowledged_at` 等，**以 TD §3 该表节为准逐列对齐，不得增删**）。
2. **迁移链**：0011 落在当前 head（0010）之后；若 dev-A/B 同期也在追加迁移，后写者 rebase（主计划 R2 同口径），`alembic heads` 必须始终只有一个。
3. **N27 通知写入**：`safety.py` 命中 → 事务内写 `risk_events` + `work_orders` + `notifications(target_type='staff', target_id=负责护工, status='generated')` 三行；通知行带 `dedupe_key`（如 `risk:{event_id}`）保证同一红色事件不重复通知。
4. **C-0-5 的登记义务**：`contracts.md` §1 写明「mock-operations 留 Day 8」——本裁决**不违反**该条（家属通知未提前），但 `notifications` 表落库口径属契约层新增，需在 `contracts.md` 追加版本记录（v1.1，说明仅提前建表与护工通知最小写入，mock-operations 范围不变）；该追加与代码同一次提交。

### 0.2 50 例普通语料留痕（C-0-4，S1 执行时回填）

- 落点：`tests/fixtures/safety_normal_corpus.py`（`NORMAL_CORPUS: list[str]`，50 句）。
- 拟题原则：覆盖闲聊/点播/用药查询/天气/告别等高频场景，**刻意混入易误杀表达**（「心里难受」「腰酸背疼」「想起去世的老伴」等情绪表达不触发急症口径的边界样本），与 FR-DLG-07 敏感边界集互斥。
- 用途：`tests/test_safety.py` 断言 `sum(safety_check(s).is_sensitive for s in NORMAL_CORPUS) <= 2`。
- 语料定稿后在此回填：拟题时间、条数、易误杀样本数、实测误报数。

---

## 1. 项目认知摘要（dev-C 视角）

- **产品边界**（PRD）：3D 数字人陪伴系统不做医疗诊断、不替代 SOS；dev-C 本日的两道安全底线是 AC-04（急症一票否决，命中即红色事件，**不进大模型**）与 FR-DLG-04（Agent 不得自动执行改药单/关工单/删数据）。
- **本日主题**（主理人 2026-11-09 裁决，主计划 C-1）：Day 6 走「长者侧 Agent 完整闭环」，**三级路由后置**——`app/router/rules.py`、`app/system_one/`、`decision_logs` 本日不建；dev-C 只建 `app/router/safety.py` 的**最小一票否决**（不做固定意图映射，契约冻结件 §1 禁区 3）。
- **dev-C 的职责一句话**（主计划 §7）：把「规则 / 分类器 / Agent / 工具」四条执行路径收敛到**同一个事实提交边界**（`handle_user_turn`，七步顺序即契约），并保证敏感输入在进 Agent 之前就被短路。
- **三道隔离闸门**（PRD FR-ADM-06）：本日做实现侧——闸门① `thread_id` 服务端生成（Day 4 已交付）、闸门③ 工具只读 `runtime_context` 身份（`app/core/runtime_context.py` 已就绪，构造时自证合法，读不到身份即拒绝）；取证矩阵属 Day 9。
- **硬性数值**（AGENTS §7，常量写死不得调）：`MAX_TOOL_CALLS_PER_TURN=8`、同 Tool 1min 第 3 次熔断、四类失败（bad_args/not_found/transient/forbidden）、简单指令响应 ≤1.5s、路由 ≤200ms（本日不接线但常量不动）。

---

## 2. 现状核查（代码级证据，2026-11-09 实测）

| # | 核查项 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1 | `app/agents/` | 仅 `guardrails.py`，**签名已冻结**（`check(tool_name, args) -> GuardrailDecision`），预算 8 次 + 同 Tool 60s 第 3 次熔断 + 同参短路已实现并有 `tests/test_guardrails.py`；Day 6 **零改动接入，禁止自建第二套** | app/agents/guardrails.py L10/L94-146 |
| 2 | `app/services/dialogue_service.py` | `DialogueOrchestrator.handle_asr_final` 是 ASR FINAL 唯一入口，含进程内 `_seen_turns` 去重（512 容量）；`DialogueService.respond` 持有「Redis 最近 5 轮 + LLM + 写回」全链路——N14 要把它**降为纯 LLM 调用**（L7 锁），`RedisTurnStore` 只留缓存职责 | dialogue_service.py L187-277/L372-483 |
| 3 | `app/audio/signaling.py` | `TtsTextMsg` TypedDict 现仅 `type/text/degraded` 三字段，**无** response_id/epoch/segment_id/index/total；C→S 无 `segment_ack` 分支——N07 需新增（L6 锁：dev-D 起草契约、dev-C 复核后由 dev-C 一次性改服务端分支） | signaling.py L204-213 |
| 4 | `app/mcp_client/`、`app/router/`、`app/graphs/` | 均只有空 `__init__.py`；`mcp-servers/` 目录**不存在**——N24/N25/N27 全部从零新建 | 目录实测 |
| 5 | 天气相关配置 | `weather_api_key=""`、`weather_cache_ttl_seconds=600`、`mcp_call_timeout_seconds=5` 已在 Settings，**不新增配置项**（自裁 Z-6 同口径） | app/core/config.py L139-154 |
| 6 | `app/core/context.py` | `build_dialogue_messages()` 单轮拼接（签名冻结，dev-B N06 只新增函数不改旧签名）；`DIALOGUE_MAX_ROUNDS=5` | context.py L61/L135 |
| 7 | `app/core/runtime_context.py` | `Identity` 构造时自证合法、contextvar 只由 auth 写入、`IdentityNotSetError` 语义明确——闸门③ 直接复用，工具层「读不到身份即拒绝并审计」有现成异常可用 | runtime_context.py L1-58 |
| 8 | 数据底座 | 0008/0009/0010 三迁移已 apply（事件三表 + `memory_chunks` + 用药/风险/工单六表），表数 24 业务表；`conversation_threads.last_event_seq` 在；G1 迁移部分已通过 | 主计划 §11.1、contracts.md §7 |
| 9 | 契约 | `contracts.md` v1.0 已冻结（G0 通过）：`tts_text` v2 五新字段、`segment_ack` 服务端校验四步、`HistoryStore`/`StateSnapshot`/`ToolResult`/`MCPResult` 四组签名、12 种事件枚举——**桩期全部照抄，不自行改字段** | contracts.md 全文 |
| 10 | Day 5 回归面 | `tests/test_guardrails.py`、`tests/test_dialogue_integration.py`、`tests/test_dialogue_service.py` 是本日三个必保绿的回归文件（任务卡 §5 自检明示） | 任务卡 v1.3 §5 |
| 11 | 延迟基线 | e2e P50 7872ms / P95 13537ms（a1 已入档）；本日新增分段与 ack 打点**复用 `record_stage`**，不新建表 | day06/a1-延迟基线表.md §4.2 |

---

## 3. 任务拆解总表（对应任务卡 N07/N11/N13/N14/N20/N24/N25/N27）

| # | 节点 | 任务 | 前置依赖（真实状态） | 执行落点 | 量化验收标准（照抄任务卡 §2，补实现口径） |
| --- | --- | --- | --- | --- | --- |
| C-1 | N20 | 讲义 §12 状态实验 | **无依赖，立即可做**；讲义已存档 | `spikes/elder_agent_state_demo.py` | ① 只依赖标准库，不改生产代码 ② `uv run python spikes/elder_agent_state_demo.py` 输出 PASS ③ 完成讲义 §12 末「当场改动题」（迟到回调 epoch 改为 `session.epoch`，观察仍因 `status != playing` 被拒），结论写进脚本注释 ④ PASS 输出贴进主计划 §5 汇报清单 |
| C-2 | N24 | 天气 MCP server | N00 已冻结（`get_weather` schema 在契约）；`weather_api_key` 已有配置位 | `mcp-servers/weather/`（server + tool schema + README） | ① server 独立可调（本地进程，端口 7101，Z-5）② 缓存 TTL 读 `weather_cache_ttl_seconds` ③ 无 key / 上游失败明确返回失败**不编造** ④ 入参强校验、错误不回显内部细节 ⑤ `tests/test_mcp_weather.py` 覆盖三场景（正常/无 key/超时） |
| C-3 | N25 | `app/mcp_client/` | C-2 的 tool schema | `app/mcp_client/`（client + 错误映射 + 审计） | ① 工具发现 / 调用 / 超时 5s（读 `mcp_call_timeout_seconds`）② `MCPResult` 四类错误（timeout/upstream_error/bad_response/unauthorized）映射到工具层四类失败 ③ 他人 `elder_id` 调用被拒并写 `audit_logs` ④ 每次调用写审计（工具名、耗时、结果分类） |
| C-4 | N27 | 安全一票否决（AC-04） | N00 已冻结（词表口径）；接在 `handle_user_turn` **之前**；0011 迁移（C-0-5/C-0-6 授权） | `app/router/safety.py` + 词表/阈值常量 + `tests/test_safety.py` + `0011_day6_notifications.py` + notifications ORM | ① 「我胸口疼」→ `is_sensitive=true` → 红色事件（`risk_events`）+ 工单（`work_orders`）+ 护工通知（`notifications`，status=`generated`，dedupe_key 防重）三行同事务落库，`escalated_to_agent=false`、**不调 LLM** ② 词表/阈值写在代码不写 prompt（AGENTS §5）③ 按「部位 + 症状」双命中判定，不靠单字（风险 R13）④ 50 例普通语料（dev-C 自拟，C-0-4）误报 ≤2 ⑤ 急症判定先于一切，`handle_user_turn` 入口第一步调用 |
| C-5 | N07 | ResponseStream（本日最容易做错的一个） | 契约已冻结可**桩先行**；N03（dev-D 消息契约）、N05（dev-B 状态中心）未开工 | `app/services/response_service.py` + `app/audio/signaling.py` segment_ack 分支 | ① 服务端分段：`。！？；` 切、单段 ≤40 字、长句按逗号补切（Z-2）② `tts_text` v2 五新字段、老客户端忽略仍能播 ③ ack 校验四步：response 存在 → epoch 一致 → `status==playing` → `segment_id > last_acked_segment`（单调），任一不过丢弃 + `response_ack_rejected` 日志（含 rejected_reason）④ `delivered_text` 只拼 `played` 段，`delivery_status` 只取 `delivered/interrupted/unknown`，**永不写 understood** ⑤ 打断五步顺序不可交换：`interrupt` → 停 TTS 队列 → `epoch+=1` → 取消 LLM/TTS 任务 → 追加事件 + `delivery_status=interrupted` ⑥ 工具副作用不随回复取消（`succeeded` 事实保留）⑦ 每段下发与每次 ack 各打一条 `response_segment` 指标（复用 `record_stage`） |
| C-6 | N11 | KnowledgeService（L 档，全量保留——C-0-1） | N06（dev-B ContextBuilder）未开工，可**桩先行**（软依赖，边 N06→N11） | `app/services/knowledge_service.py` | ① 只做权威业务事实：用药计划按 `elder_id` 结构化查询，**不做向量检索**（讲义 §9.2）② 每次返回带 `source/queried_at/filters` 元数据 ③ 无结果返回空，Agent 说「暂时查不到」不编造（FR-CON-02 同口径）④ 机构文档 RAG 本日不做，**登记 Day 7 技术债**（这不是裁减 N11，是节点内边界） |
| C-7 | N13 | CompanionAgent ReAct | 契约已冻结可**桩先行**；N06（上下文）、N12（工具执行器）未开工；N26（dev-A 接 MCP）软依赖 | `app/agents/companion.py` | ① ReAct 循环：`build_turn_context` → LLM（复用 `LLMService`，不新建 provider）→ 工具调用则 `ToolExecutor.execute` → 结果回灌 → 再思考，直到出文本或触顶 ② 护栏全部复用 `guardrails.py`，禁止第二套 ③ 第 9 次调用被拒并告知用户 ④ 同 Tool 1min 第 3 次熔断 ⑤ 高危不越级：改药单/关工单/删数据一律 `denied` + 生成护工跟进任务（FR-DLG-04）⑥ 工具失败四分类回灌：`bad_args` 回传重试计预算、`not_found` 如实告知、`transient` 退避 ≤2、`forbidden` 拒绝+审计 ⑦ 超预算收尾话术「这个我处理不了，帮您记下来稍后再试」（TD line 834 原话） |
| C-8 | N14 | 统一出口 `handle_user_turn` + 失败分支（原 N15 并入） | **合流瓶颈，最后做**：N04/N05（历史/状态，dev-A/B）、C-5、C-7、C-4 全绿 | `app/services/turn_service.py`；`DialogueOrchestrator` 改调它；`dialogue_service.py` 降级（L7 锁） | ① 七步顺序不可乱：`begin_turn` → `snapshot` → `apply(TurnStarted, AsrFinalCommitted)` → `build` → `agent.run` → `commit_response`（先记生成）→ `apply(state_events)`（再改状态）→ `response_stream.start`（最后下发）② `generic` 模式只写事件，`commit_response` 跳过 `conversation_turns`，`read_turns` 返回空 ③ 同 `(device_id, turn_id)` 幂等：第二次直接返回首次 `ResponseHandle`，不重复调 LLM（`_seen_turns` 保留作快路径）④ `DialogueService` 降为纯 LLM 调用，`respond()` 签名不变，`test_dialogue_service.py` 必须绿 ⑤ 接线点：`handle_asr_final` 内部改调 `handle_user_turn`，**对外签名不变**（Z-1），`app/audio` 不 import `app/agents` ⑥ 失败分支：LLM 超时沿用 Day 5 `llm_slow/llm_timeout` 话术**不覆盖**；工具 transient →「这个我暂时查不到，帮您记下来」+ `tool_failed(error_class=transient)`；Redis 不可用 → 闲聊单进程短会话、用药任务提示人工 + `state_degraded`；ASR 空文本不进本入口，补断言日志 |

---

## 4. 分阶段推进计划（每阶段完成后停下汇报，经确认再进入下一阶段）

> 执行主线：**S0 核查 → S1 无依赖五步（N20→N24→N25→0011→N27）→ S2 桩期（N07∥N11∥N13）→ S3 对齐联演 → S4 N14 合流 → S5 收口**。
> S1 与 S2 可交叉穿插（S2 的 N07 桩在等 N03/N05 时先做）；汇报格式固定：diff 摘要 + 自检命令输出 + 遗留问题。

### S0 · 开工核查与报备（约 0.5h，无外部依赖）

**工作内容与步骤**：
1. ~~向主理人报备 Q-1/Q-2~~ → **已完成**（2026-11-09 主理人答复：Q-1 dev-C 自拟、Q-2 方案 A 提前建表、0011 授权 dev-C 建，见 C-0-4~C-0-6）；S0 剩余动作照旧执行。
2. 核对三把写锁的当前状态：L6（`protocol.ts` + `signaling.py` 消息段，dev-D 起草 → dev-C 复核后由 **dev-C 一次性改服务端分支**）、L7（`dialogue_service.py`，N14 接线后释放）、L9（`mcp-servers/weather/**` + `app/mcp_client/**`，dev-C 持有，N26 时 dev-A 只读接入）；并向 dev-A/B 通报 0011 由 dev-C 建，避免迁移并行写。
3. 建立回归基线：`uv run pytest -q` 当前全绿确认（Day 5 收口基线），记录测试数。
4. 确认 Docker 环境（postgres/redis）就绪，`uv run alembic current` 停在 head。

**交付物**：写锁与 0011 通报结论；回归基线数字。

**自检校验**：
- [x] Q-1/Q-2/建表人三项已有裁决（C-0-4~C-0-6）
- [ ] L6/L7/L9 持有与释放条件与 dev-A/B/D 无异议；dev-A/B 知悉 0011 归 dev-C
- [ ] `uv run pytest -q` 基线数字已记录
- [ ] `uv run alembic current` == head（0010）

### S1 · 无依赖五步（约 9.5h，全部只依赖已冻结契约与已裁决项）

**顺序**：N20（1.0h）→ N24（2.5h）→ N25（2.5h）→ 0011 迁移+ORM（~1.5h，C-0-5 新增）→ N27（2.0h）。N20 最先：它是理解 epoch/status 双校验语义的**教学输入**，直接影响 N07 的实现正确性。

**工作内容与步骤**：
1. **N20**：照讲义 §12 写 `spikes/elder_agent_state_demo.py`（标准库），跑出 PASS；做当场改动题并把结论写进注释（为什么生产要**同时**校验 epoch 与 status）。
2. **N24**：建 `mcp-servers/weather/`——MCP server + `get_weather(city)` tool schema（照契约冻结件与 N00 口径）+ 进程内缓存（TTL 读 `weather_cache_ttl_seconds`）+ 无 key 明确失败路径 + README；本地进程起在 7101（Z-5，compose 收口留 Day 10）。
3. **N25**：建 `app/mcp_client/`——发现/调用/超时（5s）/`MCPResult` 错误映射/审计写入；越权用例：`args` 携带他人 `elder_id` → 拒绝 + `audit_logs`。
4. **0011 迁移 + ORM（C-0-5/C-0-6 授权，先于 N27）**：`alembic/versions/0011_day6_notifications.py`（`upgrade head` + `downgrade -1` + 再 `upgrade` 三次验证）+ `app/models/` 补 `notifications` ORM（逐列对齐 TD §3 该表节，不得增删列）；`contracts.md` 追加 v1.1 版本记录（§0.1 第 4 条义务）。
5. **N27**：建 `app/router/safety.py`——急症/敏感词表（「部位 + 症状」双命中）、阈值常量、`safety_check(text) -> SafetyDecision`；命中 → 同事务写 `risk_events` + `work_orders` + `notifications`（护工，`status='generated'`，`dedupe_key='risk:{event_id}'`）；自拟 50 例普通语料入 `tests/fixtures/`（C-0-4，§0.2 留痕）；`tests/test_safety.py` 覆盖：命中急症三表齐、否定不触发（FR-DLG-07 边界四类各抽 1 条做冒烟，全量属 Day 9）、50 例普通语料误报 ≤2、同事件重复触发通知不重复（dedupe 生效）。

**交付物**：`spikes/elder_agent_state_demo.py`、`mcp-servers/weather/`、`app/mcp_client/`、`alembic/versions/0011_day6_notifications.py`、`app/models/notifications.py`（挂进 `app/models/__init__.py`，L5 锁按序追加）、`contracts.md` v1.1 追加记录、`tests/fixtures/safety_normal_corpus.py`（50 例）、`app/router/safety.py`、`tests/test_mcp_weather.py`、`tests/test_safety.py`。

**自检校验**：
- [ ] `uv run python spikes/elder_agent_state_demo.py` 输出 PASS，注释含当场改动结论
- [ ] `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` 三次成功；`alembic heads` 单 head；`psql \d notifications` 可见 `dedupe_key` 部分唯一索引
- [ ] `uv run pytest tests/test_mcp_weather.py tests/test_safety.py -q` 全绿
- [ ] 说「我胸口疼」→ `risk_events`/`work_orders`/`notifications` 三表各一行；重复触发同一事件通知不重复（dedupe 生效）
- [ ] weather server 无 key 时返回明确失败（手测一次），不产出编造天气
- [ ] 他人 `elder_id` 调用被拒且 `audit_logs` 可查到一行
- [ ] 50 例语料清单已按 §0.2 回填本计划留痕
- [ ] 新增代码 `ruff check app tests mcp-servers` 通过、全量 type hint、I/O async

### S2 · 桩期（先桩后并，C-0-3；约 6h 净开发）

> 依据：N03/N05/N06/N12 的**签名已在 `contracts.md` §4/§5 冻结**，桩期照抄签名 + 假实现 + 单测，联调通知到达后只做对齐。

**工作内容与步骤**：
1. **N07 主体**（4.0h）：`app/services/response_service.py`——`ResponseHandle` / `start(turn, result)` 分段下发 / `ack(...)` 四步校验 / `interrupt(response_id)` 五步顺序；对 `StateSnapshot`（§5.2）与 `signaling.deliver` 用桩；`tests/test_response_service.py` 断言：迟到 ack 被拒（epoch 过期 + status≠playing 两条路径）、ack 单调、打断后 `delivered_text` 截断、**打断顺序用调用顺序断言**（不看结果看顺序，任务卡 §7 风险条目）。
2. **`signaling.py` segment_ack 分支**（与 dev-D 同锁 L6）：dev-D 的 `protocol.ts` 契约起草到达后，dev-C 复核字段零差异，然后由 dev-C 一次性加 C→S `segment_ack` 处理分支；未到达前只写 `response_service.py`，不碰 signaling。
3. **N11**（2.0h）：`app/services/knowledge_service.py`——用药计划按 `elder_id` 结构化查询（走 services 层既有查询路径，不直连 ORM 写操作）；`source/queried_at/filters` 元数据；无结果返回空。对 `ContextBuilder` 的接入点用桩（软依赖）。
4. **N13 主体**（4.0h）：`app/agents/companion.py`——ReAct 循环骨架 + `Guardrails` 接入（构造时注入，零改动）+ 对 `ToolExecutor`（§5.3 签名）与 LLM 用桩；`tests/test_companion.py` 断言：第 9 次调用被拒、同 Tool 熔断、高危 `denied` + 跟进任务、四类失败各一条回灌路径、超预算收尾话术。

**交付物**：`response_service.py`、`knowledge_service.py`、`companion.py`、三份测试文件；`signaling.py` 改动**仅在本阶段末 dev-D 契约到达后**进行。

**自检校验**：
- [ ] `uv run pytest tests/test_response_service.py tests/test_companion.py -q` 全绿（桩期即绿，不依赖他人代码）
- [ ] `uv run pytest tests/test_guardrails.py -q` 未回归（guardrails 零改动）
- [ ] `app/agents/companion.py` 中 grep 不到自建的熔断/预算常量（只有 `MAX_TOOL_CALLS_PER_TURN` 引用自 guardrails）
- [ ] 打断顺序断言存在且验证的是调用顺序

### S3 · 对齐联演（外部到达后触发，非固定时长）

**触发条件与动作**：
| 外部事件 | 对齐动作 | 完成判据 |
| --- | --- | --- |
| N03 到（dev-D 契约实现） | L6 复核 `protocol.ts` ↔ `signaling.py` 字段零差异；dev-C 加 `segment_ack` 服务端分支 | J3：TypedDict 与 interface 逐字段对照通过 |
| N05 到（dev-B 状态中心） | N07 桩换真 `snapshot/apply`；epoch 从 `StateSnapshot.output.generation_epoch` 读 | `test_response_service.py` 对真实现全绿 |
| N06 到（dev-B 上下文） | N11 接入点、N13 的 `build_turn_context` 换真 | 上下文含 `waiting_for`、只取 `delivered_text` |
| N12 到（dev-A 工具执行器） | N13 的桩换真 `ToolExecutor`；J4 四类失败各跑一条 | `execute()` 与 `ToolResult` 契约逐字段通过 |
| N26 到（dev-A 接 MCP） | 验证「今天天气」全链路（Agent → ToolExecutor → mcp_client → weather server） | 缓存命中带数据时间；无 key 说「查不到」 |

**自检校验**：
- [ ] J3 字段对照表留痕（对照结论写进本计划 §9 变更记录或单独 adhoc）
- [ ] J4 四类失败各有一条运行证据（不是代码审查）
- [ ] 「今天天气」→ 真实天气播报一次成功

### S4 · N14 统一出口合流（3.5h，W5 唯一串行点）

**进入条件**：N04/N05/C-5(N07)/C-7(N13)/C-4(N27) 全部就绪（桩亦可，但七步必须真跑通）。

**工作内容与步骤**：
1. 写 `app/services/turn_service.py`：照任务卡 §3.3 七步伪代码逐行落，**顺序即契约**；每步之间的事件写入点对齐 12 种枚举。
2. `DialogueOrchestrator.handle_asr_final` 内部改调 `handle_user_turn`，对外签名不变；`app/audio` 不出现 `app.agents` import（分层红线，自查 grep）。
3. `dialogue_service.py` 改造（L7）：`respond()` 保留签名，内部数据源切到 `handle_user_turn` 的产物；`RedisTurnStore` 降为最近 5 轮缓存。
4. 失败分支按 C-8 ⑥ 落四条；generic 双重判定（`HistoryStore.begin_turn` 与 `turn_service` 各一道，风险 R6）。
5. `tests/test_turn_service.py`：事件流水按序完整、generic 不落库、重复 `turn_id` 幂等返回原 `ResponseHandle`、工具失败有可读话术、safety 命中时 `escalated_to_agent=false` 且不进 Agent。

**自检校验**：
- [ ] `uv run pytest tests/test_turn_service.py -q` 全绿
- [ ] `uv run pytest tests/test_dialogue_service.py tests/test_dialogue_integration.py -q` 未回归
- [ ] `grep -r "app.agents" app/audio/` 零命中
- [ ] `generic:` 前缀 thread 跑一轮 → `conversation_turns` 零新行、`conversation_events` 有事件

### S5 · 收口（约 1h）

**工作内容与步骤**：
1. 任务卡 §5 四条自检命令全跑，输出留痕。
2. J5：与 dev-A/dev-B/dev-D 取真实一轮的 `trace_id`，验证同一 trace 串起 asr→turn→response→ack。
3. PASS 输出与联调证据贴主计划 §5 / 汇报清单。
4. 更新任务卡 `> 状态：` 行与 `docs/plans/README.md` 索引状态列；提交（pre-commit 自动刷新 PROGRESS.md）。

**自检校验**：
- [ ] `uv run pytest -q` 全绿且核心包 cov ≥70%（`uv run pytest --cov=app --cov-report=term-missing`）
- [ ] V6/V10/V11/V15/V16 判据逐条过（主计划 §6）
- [ ] `uv run python scripts/update_progress.py --check` 退出码 0
- [ ] 任务卡状态行已更新、本计划变更记录已补

---

## 5. 交付物规格

| 交付物 | 格式 | 内容范围 | 质量红线（违反即返工） |
| --- | --- | --- | --- |
| `app/services/response_service.py` | Python | 分段、response_id/epoch、ack 单调、打断 | epoch + status 双校验；打断五步顺序固定；`delivery_status` 永不写 `understood` |
| `app/services/turn_service.py` | Python | `handle_user_turn` 唯一事实提交边界 | 七步顺序固定；generic 不落库；`(device_id, turn_id)` 幂等 |
| `app/agents/companion.py` | Python | ReAct Loop | 复用 guardrails 不二建；预算 ≤8；高危拒绝 + 跟进任务 |
| `app/services/knowledge_service.py` | Python | 权威业务事实结构化查询 | 强制 `elder_id`；带来源元数据；无结果不编造 |
| `alembic/versions/0011_day6_notifications.py` + `app/models/notifications.py` | Python | `notifications` 表（C-0-5 提前建表授权） | 逐列对齐 TD §3 该表节不得增删；可 downgrade；CHECK 不用 PG ENUM；`dedupe_key` 部分唯一；`alembic heads` 单 head |
| `tests/fixtures/safety_normal_corpus.py` | Python | 50 例普通语料（C-0-4 dev-C 自拟） | 只进测试不进业务库；§0.2 留痕；误报 ≤2 可复算 |
| `app/router/safety.py` | Python | 最小一票否决（AC-04） | 词表/阈值写代码不写 prompt；命中即短路不经 Agent；不做意图映射；三表同事务写入 |
| `mcp-servers/weather/` | Python + Markdown | `get_weather` + 缓存 + 无 key 失败 + README | 入参强校验；错误不回显内部细节；无自由外网访问；无 key 不编造 |
| `app/mcp_client/` | Python | 发现/调用/超时/越权审计 | 超时与上游失败映射四类失败；越权写 `audit_logs`；不新增配置项 |
| `spikes/elder_agent_state_demo.py` | Python | 讲义 §12 状态实验 | 标准库；PASS 输出；注释含当场改动结论 |
| `tests/test_response_service.py` `test_turn_service.py` `test_companion.py` `test_mcp_weather.py` `test_safety.py` | Python | 各节点量化验收的可复跑证据 | 打断顺序断言看调用顺序；安全 50 例误报 ≤2 可复算 |
| `app/services/dialogue_service.py`（改造） | Python | 降为纯 LLM 调用（L7） | `respond()` 签名不变；`test_dialogue_service.py` 绿 |
| `app/audio/signaling.py`（扩展） | Python | 仅新增 segment_ack 分支（L6） | 不动既有消息字段；与 `protocol.ts` 字段零差异 |

**禁触**（任务卡 §1，C-0-6 例外已标注）：`alembic/versions/**` 与 `app/models/**` **仅解禁** `0011_day6_notifications.py` + `notifications` ORM（C-0-6 授权，其余迁移与模型仍禁触）；`frontend/**`、`app/services/staff_service.py`、`app/services/history_service.py`（dev-A）、`app/core/context.py`（dev-B）照旧禁触。

---

## 6. 全量自检校验清单（S5 逐条跑）

```bash
uv run python spikes/elder_agent_state_demo.py                       # PASS 输出
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head   # 0011 可逆 + 单 head
uv run pytest tests/test_response_service.py tests/test_turn_service.py tests/test_companion.py -q
uv run pytest tests/test_mcp_weather.py tests/test_safety.py -q
uv run pytest tests/test_guardrails.py tests/test_dialogue_integration.py tests/test_dialogue_service.py -q   # 不得回归
uv run pytest -q                                                     # 全绿
uv run pytest --cov=app --cov-report=term-missing                    # 核心包 ≥70%
ruff check app tests mcp-servers && ruff format --check app tests
grep -r "app.agents" app/audio/                                      # 零命中
uv run python scripts/update_progress.py --check                     # 退出码 0
```

判据归属：V6（送达与打断）、V10（预算熔断）、V11（全量门禁）、V15（天气 MCP）、V16（安全一票否决）见主计划 §6；V13（真实链路）与 V14（文档一致性）由全队合流出。

---

## 7. 对接与联调（任务卡 §6 + 主计划 J3/J4/J5/J7）

| 时点 | 对象 | 我给出 | 我要到 | 完成判据 |
| --- | --- | --- | --- | --- |
| J3 · W3 末 | dev-D | `tts_text` v2 字段表、`segment_ack` 服务端校验四步 | 前端 interface 定义 | TypedDict ↔ interface 逐字段对照通过，L6 释放 |
| J4 · W4 末 | dev-A | Agent 侧工具调用点、四类失败预期行为 | `execute()` 与 `ToolResult` 实现 | 四类失败各跑一条留证据 |
| J7 · W4 末 | dev-A | `get_weather` 入参/返回 schema、`MCPResult` 错误映射（我持有 L9） | N26 的 mcp 分支接入 | 「今天天气」经 MCP 返回；越权被拒留审计 |
| J5 · W5 末 | dev-A/B/D | 真实一轮的 `trace_id` 与事件流水 | 事件落库、ack 落库 | 同一 `trace_id` 串起 asr→turn→response→ack |

**联调纪律**（主计划 §7）：契约字段只以 `contracts.md` 为准，口头约定无效；要加字段先改契约再改代码；跨端问题带 `trace_id` 与请求/响应原文。

---

## 8. 风险与回退

| # | 风险 | 触发信号 | 处置 |
| --- | --- | --- | --- |
| R-1 | 分段导致首包变慢（基线 P95 已 13.5s） | TTS 首包上升 >100ms | 分段在**文本生成后**做，不预切 TTS；再退化为「整段下发 + 服务端仍记 segment_id」（任务卡 §7） |
| R-2 | 与 dev-D 抢 `signaling.py` | 两人同时改消息段 | L6 锁：dev-D 起草 → 我复核 → **我一次性改服务端分支**；未到 S3 不碰该文件 |
| R-3 | 打断五步顺序写反 | 单测不断言顺序 | `test_response_service.py` 断言**调用顺序**，不看结果看顺序 |
| R-4 | `DialogueService` 改造打破 Day 3–5 | 回归测试变红 | 保留 `respond()` 签名只换内部数据源；`test_dialogue_service.py` 必须绿才进 S5 |
| R-5 | 安全词表误杀（「心里难受」「疼得受不了」） | 普通语料误报 >2 | 「部位 + 症状」双命中；漏判用例进 `test_safety.py` 回归；宁可错杀不可漏杀（PRD 敏感召回口径，风险 R13） |
| R-6 | generic 模式误落库 | `generic:` 前缀 thread 出现历史行 | `begin_turn` 与 `turn_service` 双重判定（R6 同名，主计划 §10） |
| R-7 | 天气 key 缺失 / 上游超时 | 手测「今天天气」得到编造内容 | 无 key 或失败一律「这个我暂时查不到」+ `tool_failed(transient)`；有缓存播缓存并标注数据时间；**禁止**模型常识生成天气（R12 同口径） |
| R-8 | 依赖节点（N03/N05/N06/N12）当天迟迟不到 | S2 结束仍无对齐通知 | 桩期产物已自测绿，S3 转次日继续；**不**为实现进度自行放宽七步顺序或双校验 |
| R-9 | 容量不足需裁 N11 | S3 结束 N11 未开工 | **回主理人重新裁决**（C-0-1 约束），不自行砍；裁决前 N11 保持计划内 |
| R-10 | ~~Q-1/Q-2 未及时裁决~~ → 已消除 | — | C-0-4~C-0-6 已裁决（2026-11-09），风险关闭 |
| R-11 | 迁移并行写冲突（0011 与 dev-A/B 同期新增迁移） | `alembic heads` 出现双 head | 0011 落当前 head（0010）之后；S0 已向 dev-A/B 通报归属；发现双 head 由后写者 rebase；提交前 `alembic heads` 必须单 head |
| R-12 | 0011 与 TD §3 表定义漂移 | 自检发现 ORM 列与 TD 不一致 | 逐列对齐 TD §3 `notifications` 节，不得增删列；TD 无明确列时回主理人裁决，不自裁字段语义 |

---

## 9. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 首版：通读 PRD/TD/主计划/DAG/冻结契约/任务卡与 dev-C 相关代码后制定；三项已确认裁决（全量照任务卡、文件名、先桩后并）+ 两项待报备裁决（Q-1 安全语料来源、Q-2 N27 通知形态）；8 节点拆为 C-1~C-8，S0–S5 六阶段逐步确认制；交付物规格 11 项、自检 9 命令、风险 10 条 |
| v1.1 | 2026-11-09 | 主理人裁决落档：Q-1 → dev-C 自拟 50 例语料（C-0-4）；Q-2 → 方案 A 提前建 `notifications` 表，N27 三表同事务落库、落库即视为已通知（C-0-5）；0011 迁移 + ORM 授权 dev-C 建、禁触目录仅限该笔解禁（C-0-6）。S1 由四节点变五步（+0011，约 9.5h），N27 验收补通知三表齐与 dedupe 断言，交付物表 +2 项、自检 +alembic 三次验证、风险 R-10 关闭并新增 R-11/R-12 |
