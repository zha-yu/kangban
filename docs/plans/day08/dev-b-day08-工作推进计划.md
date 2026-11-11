# dev-B Day 8 工作推进计划 · 健康子图、调度、MCP 与记忆治理收尾

> 状态：已收口（Step 1–10 完成，O3 当日补做收口）　｜　版本：v1.4　｜　日期：2026-11-11
> 负责人：dev-B　｜　性质：**执行计划文档（本文件不含任何业务代码，本轮未修改任何代码文件）**
> 任务来源：`docs/team_tasks/day08/dev-B-后端子图调度与MCP.md` v1.1（B1–B5 + H12 参与，O3 溢出池）｜`docs/team_tasks/day08/day08-任务总览与DAG.md` v1.1（对接点 X1/X5/X6/X8/X9、当日节奏）
> 上游依据（事实只引这几份）：`docs/康伴3D数字人_PRD.md` v3.0（AC-09/10/11/21/22/23/32/34/38/12/39、FR-MEM-03/05/06/07、FR-CON-03/04、FR-VPR-04）｜`docs/康伴3D数字人_技术设计文档.md` v2.1（§7.2 健康子图、§3.4/§3.5 表定义、§3.6 迁移批次）｜`docs/plans/day08/day-08-健康采集与三端后台.md` v1.2（H0–H16 拆解、§6 命令级自检、§7 R1 顺延顺位）
> 流程来源：`docs/康伴3D数字人_大纲.md` §十 Day 8（仅取流程与产出物顺序，不取任何字段/表名/接口/阈值）
> 执行纪律：**逐步执行，每一步完成后自检、向主理人汇报，经确认后再进入下一步。**

---

## 0. 项目认知基线与分工摘要

1. **本日定位**：实际 Day 8 = 原大纲 Day 8 ＋ 原大纲 Day 7 剩余项（娱乐点播、记忆治理收尾、评测基线 v1，主理人裁决 D-5 全列 P0）。Day 8 交付第三张也是最后一张业务子图（健康决策图）＋三端后台真实数据闭环＋Day 7 欠债三项收口。
2. **dev-B 分工（v1.1 已裁决，不限工时全量分配）**：
   - B1 契约冻结牵头 + X8 戏曲链四件口径冻结（1h，晨间最优先，A/C/D 三线等此开工）；
   - B2 健康决策子图 `health_checkup_graph.py`（3.5h，健康主线关键路径）；
   - B3 调度 `health_jobs.py` 四类 job（2h）；
   - B4 mock-operations 两工具注册 + opera Tool 注册 + 素材种子（2.5h）；
   - B5 记忆治理收尾 H15（3.5h，冲突检测段可在等待 A2 签名空档穿插）；
   - H12 门禁收口参与（0.5h）；O3 声纹调优（溢出池 P1，17:00 未开工即整体顺延 Day 9）。
3. **dev-B 写域（独占，v1.1 扩域声明）**：`app/graphs/health_checkup_graph.py`、`app/scheduler/health_jobs.py`、衰减归并 job 文件、`app/agents/tool_registry.py`（与 tool_executor，站会确认写锁）、`app/services/memory_service.py`（增量）、`mcp-servers/mock-operations`（如需）、`static/media/opera/`（种子素材+生成脚本）、对应 tests、两份契约文档（B1 牵头冻结）。
4. **禁触**：`app/models/**` 与迁移 0017/0018（dev-A 独占）、`app/api/**`、`health_service/notify_service/report_service/media_service`（C/D 域）、`rules.py`（A5 域）、前端三端全部。图节点内不写 SQL（业务落库只经 services）；分级阈值不进 prompt（照 A2 常量）；冲突判定语义矛盾走统一 LLM 适配器，不得绕过直连 SDK。
5. **前置状态核查结果（2026-11-11 实测，与本计划起点一致）**：
   - `app/graphs/` 仅 `medication_confirmation_graph.py` + `checkpointer.py`（独立 schema `langgraph`，可直接复用）；
   - `app/scheduler/` 仅 `medication_jobs.py`（驱动链模式：scheduler 不写库、只调子图/service——B3/B5 job 照此模式）；
   - `app/agents/tool_registry.py` 仅注册 `get_weather`；mock-operations server + 单测已提前交付（operations_core 覆盖率 99%），**两工具未注册、`subject_elder_id` 注入与 AC-39 证据留本日 B4**；
   - `memory_service.py` L203/L250 明确「conflict judgment deferred to Day 7」，无 superseded/disputed 状态迁移、无衰减归并 job——B5 全新补齐；
   - `static/` 仅有 TTS 缓存 wav，无戏曲素材——B4 程序生成 mock 素材（D-7 裁决）；
   - day07 dev-C C3 已收口（tool_executor 写锁已释放，2026-11-11 核验），B4 可承接。
6. **当日关键检查点（总览 §5）**：09:15–10:00 B1；12:00 CP1（A1+A4 完成、X1/X2 对齐）；14:00 X9 交接（B2 桩测试绿 → D 启动评测门禁）；15:30 CP3 红色事件端到端演练；16:00 CP4 戏曲端到端演练 AC-37；17:30–18:00 H12 门禁。

---

## 1. 执行顺序与依赖图

```text
Step 0  启动确认（站会：tool_registry 写锁归属确认 + 本计划裁决，主理人点头）
  └─ Step 1  B1 契约冻结 + X8 四件口径（09:15–10:00，A/C/D 三线的前置，最优先）
       └─ Step 2  B2 骨架（10:00–12:00：节点/边/state/桩测试红）
            ├─ (穿插) Step 3  B5-冲突检测段（等待 A2 X1 签名的空档，不依赖健康主线）
            └─ Step 4  CP1 后 B2 完成（12:00–14:00：接 X1 分级引擎、双入口、interrupt/resume、桩测试绿）
                 └─ 14:00 X9 交接（→ dev-D 跑评测基线）
                      └─ Step 5  B4（14:00–15:00：mock 两工具 + subject_elder_id + AC-39；opera Tool 注册 + 素材种子；联调段等 A4/C4）
                           └─ Step 6  B3（15:00–16:00：四 job + 集成测试；15:30 X5 演练图侧触发）
                                └─ (16:00 CP4：B4 提供 Tool 侧证据)
                                     └─ Step 7  B5 收尾（16:00–17:30：衰减归并 job，R1① 超载先裁项）
                                          └─ Step 8  H12 门禁收口参与 + 任务卡回写（17:30–18:00）
                                               └─ Step 9  O3 声纹调优（溢出池，仅当 17:00 前主线已收口且主理人放行）
```

- **关键路径**：B1 → B2 →（X9）→ B3。B2 是健康主线全局瓶颈之一，晨会确认后立即进 B1→B2，不做任何等待前置。
- **唯一外部硬前置**：B2 的 grade 节点等 A2 的 X1 分级引擎签名（12:00 CP1）；B4 的 opera 联调段等 A4（0018 表）+ C4（media_service 就绪）。
- **可穿插**：B5 冲突检测段（不依赖健康主线）；B5 fixtures 无——fixtures 归 dev-D。

---

## 2. 分步计划（每步：操作 → 交付物与质量要求 → 自检标准 → 确认点）

### Step 0 · 启动确认（0.25h，站会）

**操作**：向主理人提交本计划；站会确认三件事：
1. `tool_registry.py`/`tool_executor.py` 写锁归属 dev-B（B4 唯一写点，总览 §2 已声明，站会最终确认）；
2. 本计划 §5 待确认项的当场裁决（至少 B3 测试时刻覆盖、O3 是否执行两项）；
3. 与 dev-A 口头对齐 X1 分级引擎签名的冻结形式（A2 docstring 冻结，B2 只调函数不改阈值）。

**交付物**：本计划文件本身；裁决结论回写 §5。

**自检标准**：计划每步均有交付物与命令级自检；无一步要求编写计划外代码。

**确认点**：主理人批准 Step 1 开工。

---

### Step 1 · B1 契约冻结 + X8 四件口径（09:15–10:00）

**操作**：
1. 牵头冻结《接口契约_照护台业务数据》v0.1→v1.0：健康代填/复核/告警相关端点补齐，健康域新端点按 W/N/H 前缀惯例分配编号（计划 §10-3）；
2. 冻结《接口契约_家属端数据》v1.0 冻结评审：日报域端点（列表/详情/已读/通知状态）；
3. **X8 戏曲链四件口径**随评审一并冻结：
   - ① media_url 稳定路径约定 `/static/media/opera/<media_id>.wav`（D-7 热替换前提）；
   - ② opera Tool schema（工具名 + 参数 + 返回结构）；
   - ③ 规则动作 → Tool/服务调用映射表（换一段/大声点/小声点/重复一遍/暂停/继续/停止，供 A5 直连 rules.py 用）；
   - ④ 种子数据规范（20–30 条，title/genre/era/artist/duration 等字段值域）；
4. 09:15–09:45 组织全员评审（A1 并行中互不等待），10:00 前发布冻结版给 A/C/D；
5. 变更一律走各契约文档 §6 留痕，冻结后不得私改。

**交付物与质量要求**：两份契约文档改「已冻结」+ X8 四件口径写入；每处变更在 §6 留痕。

**自检标准**：两份契约状态行均为「已冻结」；健康代填/复核/告警端点与日报域端点逐条在契约中有编号；X8 四件齐且 A5/C4/D4 可直接照抄落码。

**确认点**：10:00 发布后汇报冻结清单，等待确认。**联调提醒（X8）**：与 **dev-A（A5 规则直连）** 交接映射表③、与 **dev-C（C4 media_service）** 交接路径约定①、与 **dev-D（D4 pad 播放）** 交接 schema②——三方 10:00 起只按冻结口径开发，变更回 B 走契约留痕。

---

### Step 2 · B2 骨架：节点/边/state/桩测试红（10:00–12:00）

**操作**（契约测试先行，先在 `tests/test_health_graph*.py` 落红用例）：
1. 新建 `app/graphs/health_checkup_graph.py`：
   - 节点：`collect`（5 问循环：血压、睡眠时长、情绪自评、主诉；每问等 60s，无效重问一次）→ `validate`（Pydantic HealthRecord 结构化输出）→ `grade`（**占位接口**，签名照 X1 预告口径，CP1 后接 A2 纯函数）→ 条件分支；
   - 分支：`green`（persist→END）/ `yellow`（创建工单+黄色复核任务 → HITL 护工 interrupt）/ `red`（紧急通知护工+监护人 → 创建紧急工单 → HITL 医生 interrupt → persist）；
   - graph state：决策依据、分级阈值、HITL 结果、通知/工单返回全部入 state，可追溯；
2. checkpointer 复用 `app/graphs/checkpointer.py`（独立 schema `langgraph`），interrupt 暂停 + `Command(resume=...)` 恢复；
3. **双入口**：`source=voice_scheduled`（调度器触发）与 `source=voice_active`（主控 Agent 主动主诉，AC-34）共用同一张图；
4. 图内业务事实只经 `health_service`/`work_order_service`/`notify_service` 调用落库（图节点不写 SQL）；
5. 桩测试（红）：180→停「等医生确认」interrupt→resume 继续（AC-10/21）；160→护工确认（AC-09）；正常值→直接入库；三分支 `get_state` 可演示；重启后从 checkpoint 恢复不重复建单。

**交付物与质量要求**：`health_checkup_graph.py` 骨架 + `tests/test_health_graph*.py` 红用例；`grade` 节点以可替换的引擎引用接入，不在图内写阈值。

**自检标准**：骨架可 import、图可编译（`graph.compile(checkpointer=...)`）；红用例的红因与 X1 未接入一致；双入口参数（source）在 state schema 中已定义。

**确认点**：汇报骨架与红用例清单，等待确认。此步开始注意 **12:00 CP1**。

---

### Step 3 ·（穿插）B5 冲突检测段（等待 A2 X1 签名的空档执行，约 1.5h）

**操作**（不依赖健康主线；单测先行，`tests/test_memory_service*.py`）：
1. `memory_service.py` 增量，替换 L203/L250 deferred 占位：
   - 新萃取候选与存量相似度 >0.9 时做**语义矛盾判定**（统一 LLM 适配器，失败降级为「保留双版本人工复核」不阻塞写入，照计划 R7）；
   - 冲突五关系判定（supersedes / conflicts_with / disputed 等照 TD 口径）；
   - 状态迁移：矛盾时旧记忆标 `superseded`、当前生效值唯一、**两版本均保留**；
2. 召回过滤补齐：`recall_for_turn` 排除 superseded/disputed（FR-MEM-05），保持 elder_id + status=active + 有效期既有闸门不变；
3. 单测：「已存『喜欢越剧』→ 新萃取『不喜欢越剧了』」→ 两版本均在、旧值 superseded、仅新值生效（FR-MEM-03）；「删除记忆不再被召回注入」回归。

**交付物与质量要求**：`memory_service.py` 增量 + 测试扩展；冲突判定不绕过统一 LLM 适配器；不碰召回注入的调用方文件。

**自检标准**：

```bash
uv run pytest -q tests/test_memory_service*.py
```

必过：`conflict_flips_preference_keeps_two_versions`、`superseded_not_recalled`、`deleted_not_recalled`（回归）。

**确认点**：汇报后不等待（与 Step 2 并行推进），在 Step 4 收口时一并复核。

---

### Step 4 · CP1 后 B2 完成：接 X1 + 双入口 + HITL 闭环（12:00–14:00）

**操作**：
1. CP1（12:00）确认 A2 的 X1 分级引擎签名冻结后，把 Step 2 的 `grade` 占位接到真实纯函数（只调函数、不改阈值常量）；
2. 打通三分支端到端：yellow 分支建工单+复核任务后 interrupt；red 分支双通知+紧急工单后医生 interrupt；`Command(resume=...)` 恢复后按确认结果归档/升级；
3. `health_records.graph_thread_id` 回指验证（查 A1 建的表结构只读不写）；
4. Step 2 全部红用例转绿；补「重启后从 checkpoint 恢复不重复建单」用例（对齐 AC-17/30 口径）；
5. **14:00 X9 交接**：通知 dev-D 评测基线可启动（`day7_intents.jsonl` + fixtures 六类边界定义口径一并交接）；
6. 主动主诉接线联调：「我头晕」经主控 Agent 走同一张图且 `source=voice_active`（AC-34）——与 dev-C（Agent/统一出口侧）口头确认调用点，不改对方文件。

**交付物与质量要求**：`health_checkup_graph.py` 完整版 + 桩测试全绿；graph state 含可追溯决策依据。

**自检标准**：

```bash
uv run pytest -q tests/test_health_graph*.py
```

必过：`bp_180_pauses_doctor_interrupt_then_resume`（AC-10/21）、`bp_160_nurse_confirmation`（AC-09）、`normal_values_persist_only`、`three_branches_state_demo`、`recovery_from_checkpoint_no_duplicate_order`、`active_complaint_uses_same_graph_source_voice_active`（AC-34）。

**确认点**：B2 收口汇报，等待确认。**联调提醒（X9）**：14:00 与 **dev-D** 交接评测口径——交接内容：`day7_intents.jsonl` 位置与格式、六类边界（否定/多意图/超时/打断/重复/越权）定义、失败归因回 B 的约定；**与 dev-C** 交接主动主诉调用点（AC-34）。

---

### Step 5 · B4：mock-operations 注册 + opera Tool 注册 + 素材种子（14:00–15:00）

**操作**：
1. `tool_registry.py` 注册 `create_work_order` / `send_family_notification`，绑定 mock-operations MCP server（7102），走 ToolExecutor 统一执行链（G2 权限照走）；
2. `subject_elder_id` 注入：工具函数从 runtime_context 读 elder_id，忽略模型传参；越权调用被拒并落 `audit_logs`——**AC-39 证据**（audit 行 + 测试留痕）；
3. 注册 `play_opera` Tool：参数/返回**只按 Step 1 冻结的 X8 schema②**，经 ToolExecutor 调通；注册单测绿；
4. 素材种子：程序生成短 wav 至 `static/media/opera/`（路径照 X8 约定①）+ `media_library` 种子 20–30 条生成脚本随仓库留档（字段值域照 X8 规范④）；
5. 联调段：等 A4（0018 表）+ C4（media_service 就绪）后做素材替换演练一次（D-7：替换静态文件 + 更新 duration 等元数据行，**零代码改动**）。

**交付物与质量要求**：tool_registry 三工具注册 + `subject_elder_id` 注入 + 生成脚本 + 种子数据；注册单测 + 越权用例。

**自检标准**：

```bash
uv run pytest -q tests/test_tool_registry*.py tests/test_mock_operations*
grep -rn "play_opera\|create_work_order\|send_family_notification" app/agents/tool_registry.py   # 三工具齐
```

必过：`opera_tool_executes_via_executor`、`work_order_tool_via_mcp`、`tool_uses_context_elder_id_ignores_model_arg`（AC-39）、`cross_elder_call_rejected_audited`（AC-39）；素材替换演练后 media_url 不变、播放可达、duration 更新。

**确认点**：汇报后等待确认。**联调提醒（X8 链）**：Tool 就绪信号发给 **dev-A（A5）** 与 **dev-C（C4）**——A5 依此把 rules.py R-01~R-08 从降级改直连；素材就绪信号发给 **dev-D（D4）**；16:00 CP4 由 B 提供 Tool 侧证据（decision_logs + media_history）。

---

### Step 6 · B3：health_jobs 四类调度 job（15:00–16:00）

**操作**（照 `medication_jobs.py` 驱动链模式，scheduler 零写库）：
1. 新建 `app/scheduler/health_jobs.py`，独立注册（不复用 medication tick）：
   - 19:00 采集触发：对当日 active 长者逐一起健康图（`source=voice_scheduled`）；
   - 20:00 强制结束 + 漏采派单：窗口结束无任何记录 → `collection_missed` 工单派负责护工（FR-HLT-08）；
   - 日报生成触发（调 C2 report_service 口径）；
   - 红色 2 分钟升级扫描（接 A3 升级链：原事件标 `escalated` + 通知管理员，不建第二个事件/工单，AC-12）+ 通知 30s 重试扫描（AC-23 前半）；
2. job 全部只调 services/graphs，不做任何 ORM 写库；图运行后事务提交豁免照既有裁决；
3. 每 job 独立集成测试，可重复执行（幂等：状态条件扫描天然幂等）；
4. **15:30 X5 红色事件端到端演练**：B 侧从图/调度触发，配合 A（通知链）→ C（告警中心）→ D（家属通知）走通 AC-12/23/38，B 留 SQL/日志证据。

**交付物与质量要求**：`health_jobs.py` + 集成测试；测试时刻经 Settings 覆盖的能力（待确认项①裁决后落实）。

**自检标准**：

```bash
grep -rn "session\.add(\|from app.models" app/scheduler/        # 零命中
uv run pytest -q tests/test_health_jobs*.py
```

必过：`scheduled_19h_triggers_graph_voice_scheduled`、`window_end_forces_finish_and_missed_order`（漏采工单 assignee=负责护工）、`red_2min_escalation_scan_idempotent`（AC-12）、`notification_retry_scan`（AC-23）；四 job 重复执行不产生重复工单/重复通知。

**确认点**：汇报后等待确认。**联调提醒（X5/X6）**：15:30 与 **dev-A（A3）/dev-C/dev-D** 做红色事件四人演练，B 提供触发与升级扫描侧证据；16:30 X6 与 **dev-C** 漏采工单→看板核对（B 造数据，C 核红档置顶截图）。

---

### Step 7 · B5 收尾：衰减归并 job（16:00–17:30，R1① 超载先裁项）

**操作**：
1. 90 天衰减 + 每周归并 APScheduler job（新建 `app/scheduler/memory_jobs.py` 或并入既有注册文件按站会结论）：scheduler 零写库，只调 memory_service 归并接口；
2. 归并对两条高度相似记忆输出归并记录（FR-MEM-07）；
3. 收口 B5 整卡：Step 3 用例 + 归并用例全绿；
4. 16:00 CP4：到场提供戏曲 Tool 侧证据（AC-37：decision_logs layer=rule + media_history 播放记录），配合 A5/C4/D4 完成端到端演练；
5. **超载裁决**：若 17:00 前未完成，本步按 R1① 先裁（裁后 B5 核心冲突检测+召回过滤已在 Step 3 收口），顺延 Day 9 并在任务卡 + 计划 §11 留痕，commit 说明写明。

**交付物与质量要求**：衰减归并 job + 单测；B5 整卡完成记录。

**自检标准**：

```bash
uv run pytest -q tests/test_memory_service*.py
grep -rn "session\.add(\|from app.models" app/scheduler/        # 零命中（含新 job）
```

必过：`consolidation_outputs_merge_record_for_similar_pair`（FR-MEM-07）；90 天阈值边界用例。

**确认点**：汇报后等待确认；若触发顺延，登记顺延决定。

---

### Step 8 · H12 门禁收口参与 + 任务卡回写（17:30–18:00）

**操作**：
1. 跑 dev-B 范围门禁：

```bash
uv run pytest -q                                   # 全绿，无新增 skip
uv run ruff check app tests && uv run ruff format --check app tests
grep -rn "session\.add(\|from app.models" app/scheduler/          # 零命中
grep -rn "from app.graphs" app/api/ app/audio/                    # 零横向 import
```

2. 配合 dev-A 牵头的 H12：提交 B1–B5 证据（AC-10/21、AC-09、AC-34、AC-39、AC-12/23 演练、FR-MEM-03、AC-37 Tool 侧、D-7 替换演练）回填计划 §11；
3. 任务卡 `dev-B-后端子图调度与MCP.md` 状态行改「已完成」并按 §5 模板回填证据；顺延项（如有）显式登记；
4. 未关闭项登记：B5③ 衰减归并（若裁）、O3 声纹（若未开工）→ Day 9。

**交付物**：门禁输出 + 证据清单 + 任务卡回写。

**自检标准**：PROGRESS 中 dev-B 卡状态更新；本步 §8 自检四命令全绿。

**确认点**：最终汇报，等待主理人确认收口。**联调提醒**：H12 为全员节点，B 向全员提供 B2/B3/B4/B5 验收证据与 trace_id 排查支持。

---

### Step 9 · O3 声纹调优与级联验证（溢出池，仅条件执行）

**执行条件**：17:00 前 Step 1–8 已收口 **且** 主理人放行 **且** 互测人员名单已确认（计划 §10-6）。任一不满足即整体顺延 Day 9，登记留痕。

**操作（概要，展开见计划 H13）**：复用 Day 4 spike 留档 + 现场补录互测样本（不涉真实长者）→ `voiceprint_service` cosine 链路 0.80–0.90 阈值扫描（FAR/FRR + case 记录）→ 阈值定稿回写 `Settings` → 三档身份级联各演示一次留证 → 产出 `eval/reports/声纹调优报告.md`。

**自检标准**：报告四要素齐（样本量/模型版本/FAR-FRR 表含 case/阈值结论）；三档级联演示证据各一段；阈值变更可回溯。

**确认点**：单独汇报，主理人确认后记入 §11。

---

## 3. 交付物清单与格式要求（汇总）

| # | 交付物 | 格式/位置 | 质量要求 |
| --- | --- | --- | --- |
| 1 | 本计划文件 | `docs/plans/day08/dev-b-day08-工作推进计划.md` | 每步可执行可校验；裁决结论回写 §5 |
| 2 | 冻结契约 + X8 口径 | 《接口契约_照护台业务数据》《接口契约_家属端数据》+ X8 四件 | 冻结评审留痕；变更走 §6；10:00 前发布 |
| 3 | 健康决策子图 | `app/graphs/health_checkup_graph.py` + `tests/test_health_graph*.py` | 双入口三分支双 HITL；图内不写 SQL；state 可追溯；6+ 用例必过 |
| 4 | 调度任务 | `app/scheduler/health_jobs.py` + 集成测试 | scheduler 零写库；四 job 幂等可重复执行 |
| 5 | 工具注册 | `app/agents/tool_registry.py` 三工具 + `subject_elder_id` + 生成脚本 + 种子 | 只按 X8 冻结 schema；AC-39 越权留痕；素材热替换零代码改动 |
| 6 | 记忆治理收尾 | `app/services/memory_service.py` 增量 + 衰减归并 job + 单测 | 冲突判定走统一 LLM 适配器；FR-MEM-03/05 必过；FR-MEM-07 可裁（R1①） |
| 7 | 声纹调优报告（O3，条件） | `eval/reports/声纹调优报告.md` | FR-VPR-04 四要素齐；样本合规 |
| 8 | 完成记录 | 任务卡状态行 + 证据回填计划 §11 | commit + 命令输出 + SQL/截图证据 |

## 4. 联调与交接矩阵（dev-B 视角）

| 时点 | 对接点 | 对象 | 交接内容 | 方向 |
| --- | --- | --- | --- | --- |
| Step 1（10:00） | X8 | dev-A（A5）/dev-C（C4）/dev-D（D4） | 冻结契约两份 + media_url 稳定路径①、opera Tool schema②、规则动作映射表③、种子规范④ | B 冻结 → A/C/D 照抄落码 |
| Step 4（12:00 CP1） | X1 | dev-A（A2） | 分级引擎纯函数签名（docstring 冻结）+ HealthRecord 字段 | A 冻结 → B 的 grade 节点只调不改 |
| Step 4（14:00） | X9 | dev-D（D5） | `day7_intents.jsonl` + 六类边界定义 + 评测口径；失败归因回 B | B → D |
| Step 4 | — | dev-C | 主动主诉「我头晕」调用点（AC-34，同一图 source=voice_active） | B ↔ C 口头确认 |
| Step 5 | X8 链 | dev-A（A5）/dev-C（C4）/dev-D（D4） | Tool 就绪信号（A5 改直连前提）、素材就绪信号（D4 播放源） | B → A/C/D |
| Step 6（15:30 CP3） | X5/X7 | dev-A（A3）/dev-C/dev-D | 红色事件端到端演练：图/调度触发 + 升级扫描证据（AC-12/23/38） | B ↔ A/C/D |
| Step 6（16:30） | X6 | dev-C（H9④） | 漏采工单造数据 → 看板红档置顶核对 | B → C |
| Step 7（16:00 CP4） | X8 | dev-A/dev-C/dev-D | AC-37 Tool 侧证据（decision_logs layer=rule + media_history） | B ↔ 全员 |
| Step 8（17:30 H12） | — | 全员 | B1–B5 验收证据、trace_id 排查支持 | B → 全员 |

## 5. 待确认项（执行前需主理人/站会裁决）

| # | 事项 | 说明 | 默认倾向（待裁决，不自行生效） |
| --- | --- | --- | --- |
| 1 | B3 测试时刻覆盖 | 19:00/20:00 采集演示环境是否允许经 Settings 环境变量覆盖为可测试时刻（计划 §10-4） | 允许（同既有端口覆盖豁免口径），否则集成测试只能 mock 触发时刻 |
| 2 | O3 声纹是否本日执行 | 需互测人员名单与三档级联演示的房间场景模拟方式（计划 §10-6） | 17:00 前主线收口且名单确认才执行，否则顺延 Day 9 |
| 3 | B5 归并 job 文件落位 | 新建 `app/scheduler/memory_jobs.py` 还是并入既有注册文件 | 独立文件（照 health_jobs 先例，降低 tick 冲突） |
| 4 | 衰减归并 job 的归并写入口 | FR-MEM-07 归并写库需 memory_service 暴露归并接口（B 独占 memory_service，无跨域问题，但 job 与接口的边界确认） | job 只调 service 接口，零写库不变 |
| 5 | mock-operations 越权证据形态 | AC-39 证据以「audit_logs 行 + 测试用例」双留痕是否满足验收 | 双留痕，另附一次手工复现记录 |

### 裁决记录（2026-11-11 站会，主理人已逐项确认）

| # | 事项 | 裁决结论 |
| --- | --- | --- |
| ① | B3 测试时刻覆盖 | **允许**经 Settings 环境变量覆盖为可测试时刻（同既有端口覆盖豁免口径） |
| ② | O3 声纹是否本日执行 | **条件执行**：17:00 前 Step 1–8 收口且互测名单确认才执行，否则整体顺延 Day 9 |
| ③ | B5 归并 job 文件落位 | **独立新建** `app/scheduler/memory_jobs.py`（照 health_jobs 先例） |
| ④ | 归并写入口粒度 | **方案 A 双接口**：`memory_service` 暴露 `decay_stale` / `consolidate_similar` 两个语义化接口，memory_jobs 注册两个独立 APScheduler 任务分别调用；job 零写库红线不变 |
| ⑤ | AC-39 证据形态 | **双留痕 + 手工复现**：audit_logs 行 + 测试用例，另附一次带 trace_id 的手工越权复现记录 |
| 附加1 | dev-C 转达 · Q-1 两列 | **补列**：`health_records.review_note` / `superseded_reason` 随 TD 修订记录补列；dev-A 落列，dev-B 在契约 §6 留痕（Step 1 落实） |
| 附加2 | dev-C 转达 · 通知状态枚举 | **照 TD 原文 5 值冻结**（generated/pending/delivered/acknowledged/failed）；X7 四态 = AC-23 正常链路流转口径，`failed` 为 TD 定义的终态（pending 30s 重试 3 次失败置 failed）；dev-C 前端按 5 值枚举开发、正常链路只见 4 态（Step 1 落实） |
| 附加3 | dev-C 转达 · H 前缀编号 | 健康域新端点照 W/N/H/M/R 惯例分配 H 前缀（Step 1 正常工作项，无需额外裁决） |
| 附加4 | dev-D 转达 · X9 FR-TC-09「规则层阈值」口径（2026-11-11 午后裁决） | **选项 A**：PRD 验收句原文不动，dev-D 验证手段改为「**人为篡改 rules.py/safety.py 关键词模式表**后跑门禁可被拦截」。事实依据：① L0 规则层（safety.py/rules.py）纯正则精确匹配，无置信度数值（AGENTS §6「规则层不存在降级」，无阈值可调）；② `SYSTEM_ONE_CONFIDENCE_THRESHOLD`(0.85) 属 L1→L2 升级闸，仅决定 clarify/agent，门禁四道门对二者等价放行，且 fixtures 无边界置信度用例——dev-D 实测调 0.30/0.99 指标不变，归因成立；③ 篡改点=**safety.py 急症词表**（胸痛/呼吸困难/中风/跌倒/轻生 + 否定窗口）或 rules.py 固定意图 patterns，调坏后固定测试集安全用例拦截率下降→门禁 EXIT=1→还原。dev-D 的 mock provider 敏感召回拦截证据（EXIT=1，day8_intercept_check_mock.md）作为「安全拦截」维度补充归档，不替代本验收 |
| 备注 | tool_registry/tool_executor 写锁 | 总览 §2 已声明归属 dev-B，day07 dev-C 写锁已释放（2026-11-11 核验）；X1 签名冻结形式需主理人与 dev-A 口头对齐（A2 docstring 冻结，B2 只调函数不改阈值） |

## 6. 超载顺延与风险

| # | 项 | 处理 |
| --- | --- | --- |
| 顺延序 | B5③ 衰减归并 job（R1①，dev-B 唯一先裁项） | 裁后 B5 核心冲突检测+召回过滤仍须当日收口；顺延须 commit 说明 + 计划 §11 留痕 |
| 溢出池 | O3 声纹调优（P1 尽力） | 17:00 未开工即整体顺延 Day 9 |
| 风险 | interrupt/resume 与 medication_jobs 调度模式差异 | 复用 Day 7 驱动链与「图运行后事务提交」裁决；先桩测试再接真确认接口（照 R2） |
| 风险 | 黄升红跨表一致性属 A2/H2 域，B2 图侧只消费 grade 结果 | 图不重复实现升级逻辑；AC-22 证据由 A 侧提供，B 侧验证 resume 后分支走向 |
| 风险 | B4 联调段等 A4/C4 的空档 | 素材生成与注册单测不依赖二者，先行；联调段压至 16:00 CP4 前完成 |
| 风险 | 冲突判定 LLM 失败 | 降级「保留双版本人工复核」不阻塞写入（R7） |

## 7. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-11 | 首版：依据 team_tasks/day08 v1.1 分工（B1–B5 + H12 + O3）与 plans/day08 v1.2 拆解编制；2026-11-11 代码现状实测（graphs/scheduler/tool_registry/memory_service/mock-operations/static 五项基线）写入 §0-5；Step 0–9 逐步确认制；含 X1/X5/X6/X8/X9 交接矩阵与 5 项待裁决 |
| v1.1 | 2026-11-11 | Step 0 站会裁决收口：§5 新增裁决记录表（①–⑤ 逐项确认 + dev-C 转达三项附加裁决：Q-1 补列、通知状态照 TD 5 值冻结、H 前缀照惯例）；状态行草稿→进行中 |
| v1.2 | 2026-11-11 | X9 裁决登记（§5 附加4）：FR-TC-09「人为调低规则层阈值」按字面在当前实现不可观测（规则层无置信度阈值、0.85 属 L1 升级闸且门禁对 clarify/agent 等价、fixtures 无边界置信度用例——dev-D 实测 0.30/0.99 指标不变），主理人裁决**选项 A**：PRD 原文不动，验证手段改为篡改 rules.py/safety.py 关键词模式表后跑门禁；dev-D mock 拦截证据补充归档。另：同日 B1 范围内完成契约 v1.1 两处勘误（reviewed_at 引注笔误删字段、R-2 版本链冻结为 superseded_by 串联，D-11/D-12 登记、Q-3 待裁决），见契约文档修订记录 |
| v1.3 | 2026-11-11 | Step 8 收口：新增 §8 完成记录（B1–B5/H12 证据回填，本文档原引用「§11」为笔误——实际证据承载于 §8）；任务卡状态行改「已完成」；O3 声纹按裁决②顺延 Day 9；全量门禁 1301 passed / ruff 全绿 |
| v1.4 | 2026-11-11 | Step 9/10 补做收口：O3 声纹调优四要素当日完成（阈值裁决 B 定稿 0.62，commit `4734d95`；级联演示 commit `6f9bf71`，报告 v1.2 §七）；Step 10 门禁 1331 passed / ruff 全绿；任务卡升 v1.3，未关闭项清零 |

## 8. 完成记录与证据回填（Step 8，2026-11-11）

| Step | 内容 | 证据 |
| --- | --- | --- |
| Step 1 | B1 契约冻结 + X8 四件 | 两份契约「已冻结」；X8 四件写入；变更走契约 §6 留痕（含 v1.1 勘误） |
| Step 2–4 | B2 健康子图 | `app/graphs/health_checkup_graph.py`；桩红→X1 接线→全绿 commit `7793643`；必过 6 用例（AC-10/21、AC-09、AC-34 等）全绿；14:00 X9 已向 dev-D 交接评测口径 |
| Step 3/7 | B5 记忆治理 | 冲突检测（FR-MEM-03/05）+ 衰减归并（FR-MEM-07）commit `6eb8a73`；`memory_jobs.py` 双周维护 job（裁决③④落地）；R1① 未触发 |
| Step 5 | B4 三工具 + 素材 | commit `1b37981`；AC-39 双留痕 + 手工越权复现记录；X8 链三信号（Tool 就绪→A5 直连、素材就绪→D4）已发 |
| Step 6 | B3 四 job | commit `a165b7e`；15:30 X5 红色事件端到端演练 B 侧触发证据已留；漏采派单 assignee=负责护工 |
| Step 8 | H12 门禁 | `uv run pytest -q`：**1301 passed**，无新增 skip；`ruff check app tests` + `ruff format --check app tests` 全绿；`grep session.add/from app.models app/scheduler/` 零命中（仅既有 docstring 说明文字）；`grep "from app.graphs" app/api/ app/audio/` 零命中 |
| 顺延（原登记） | O3 声纹调优 | 按裁决②（互测名单未确认）整体顺延 Day 9；B5 无顺延项 |
| Step 9（补做） | O3 声纹调优四要素 | ① 样本 3 人 × 3 段现场补录（录音指南 + `record_samples.py`，16kHz/单声道/WAV 自检）；② `threshold_sweep.py` 扫描：同人 0.6664–0.8399 / 异人 0.2165–0.6512，**0.55 有 5 例误识、0.62 起 FAR=0**（明细落 `eval/reports/voiceprint_sweep_raw.json`）；③ 主理人裁决 **B：阈值定稿 0.62**，commit `4734d95` 四处同步（config.py / .env.example / AGENTS §7 / PRD 七处 + 修订记录 v3.1；附带发现 `.env` 残留 0.45 已改）；④ 级联演示 `scripts/cascade_demo_check.py` 四场景全过：device 锁定 / voice 1.0 命中 / confirm·test2_1=0.5745（旧阈值会误放行，新阈值正确拒绝）/ 冒充确认降级 generic，报告 v1.2 §七留证，commit `6f9bf71` |
| Step 10（补做） | H13 门禁 + 收口 | `uv run pytest -q` **1331 passed**（0 skip；首跑 27 个收集错误为两次 pytest 并发竞态瞬态，重跑即消）；`ruff check app tests` / `ruff format --check app tests` 全绿（`scripts/cascade_demo_check.py` 已按 ruff 修复）；scheduler 零写库、api/audio 零横向 import 两件 grep 零命中；任务卡 v1.3 O3 行回填、未关闭项清零 |

详细证据清单见任务卡 `docs/team_tasks/day08/dev-B-后端子图调度与MCP.md` v1.3「完成记录」。
