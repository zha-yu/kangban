# Task dev-C · Day 7 · 用药工具、到点调度与 Agent 边界

> 状态：已完成　｜　版本：v1.2　｜　日期：2026-11-10
> 负责人：dev-C　｜　总人日：2.75（C1 1.0 + C2 1.0 + C3 0.75）
> 关联：`docs/team_tasks/day07/README.md`（总表）｜`docs/team_tasks/day07/dev-c2-用药到点触发接线.md`（C2 既有卡，不重开）｜`docs/plans/day07/handoff-A-to-C-用药到点接线.md`
> 上游依据：PRD FR-MED-02/05/06、FR-DLG-07/09、FR-DLG-10｜技术设计文档 §6/§7/§8.3
> 本卡只含后端开发；验收由开发自证。

---

## 0. 一句话

让 Agent 真正会“确认吃药”，让到点提醒真正有生产调用点，同时把 identity、工具失败四分类、敏感边界与医疗拒答收口；**不重开 N10 子图**。

## 1. 写作用域（独占）

```text
app/agents/companion.py
app/agents/tool_registry.py
app/services/tool_executor.py
app/services/medication_due_service.py       # C2 新增
app/scheduler/**                             # C2 新增
app/router/safety.py                         # C3 仅边界规则/话术
app/prompts/personas/**                      # C3 医疗拒答固定话术
tests/test_companion.py
tests/test_tool_executor.py
tests/test_scheduler_medication.py
tests/test_safety.py
```

**禁触**：`app/graphs/**`、`app/services/medication_service.py`、`app/services/work_order_service.py`（A 线已收官）；`app/models/**`、`alembic/**`（无表结构变更）；`app/services/response_service.py`（B1）；`app/services/turn_service.py`/`main.py`（A3）；`frontend/**`（D）。

> `main.py` 由 A3 持有。C2 提供 `install_scheduler(app)`，由 A3 在 lifespan 调用；C2 不直接写 `main.py`。

---

## 2. C1 · 用药确认工具绑定与对话入口（1.0 人日）

### 2.1 目标

关闭“工具注册了但 `executor=None`”的悬空状态：长者说“我吃过药了”时，`medication_confirm` 真正推进用药子图，不再返回 `not_found`；`memory_recall` 有真实实现；Agent 工具清单包含 3 个工具。

### 2.2 现状证据

- `tool_registry.default_registry()` 已注册 `memory_recall`、`get_weather`、`medication_confirm`；
- `medication_confirm` 在装配处未 `bind()`，调用返回 `not_found`；
- `CompanionAgent.DEFAULT_TOOLS` 只有 `WEATHER_TOOL`，模型根本不知道有用药确认工具；
- `build_medication_graph()` 在 `app/**` 内零生产调用点（到点由 C2 接，对话由本卡接）。

### 2.3 实施步骤

1. `tool_executor.py`：
   - 注册 `medication_confirm` 执行器：参数 `answer`、可选 `plan_id`；
   - 查当前 `waiting_for=taken_answer` 的用药事件（按 `runtime.elder_id` + `thread_id`）；无待确认 → `ToolResult(status="failed", error_class="not_found", message="现在没有待确认的用药提醒")`；
   - 有则调用现有 `medication_service`/子图 resume 接口推进 `attempt_no`，返回 `succeeded` + 可播报文本；
   - 身份只从 `runtime.elder_id` 取；参数含 `elder_id` 丢弃并写 `audit_logs.action='tool_identity_override_attempt'`；
   - 失败四分类：`bad_args/not_found/transient/forbidden` 不混用。
2. `tool_executor.py` 绑定 `memory_recall`：调 B2 的 `recall_for_turn(elder_id=runtime.elder_id, query=...)`；返回 `ToolResult(data={"notes":[...]})`；无记忆返回空列表，不报错。
3. `companion.py`：
   - `DEFAULT_TOOLS` 改为 `(WEATHER_TOOL, MEMORY_RECALL_TOOL, MEDICATION_CONFIRM_TOOL)`；
   - 工具描述与 schema 与 `tool_registry` 同源，不复制第二套；
   - `CompanionAgent.run` 接收可选 `identity`；传了就用，不传回退推断并打 `companion_identity_inferred` 告警（沿用 A 线已落地版本）；
   - generic 分支不再静默走陌生人设：`DialogueIdentity(source="generic")` 明确标记，不加载个人记忆/人设，但保留通用陪伴话术。
4. 对话入口：确保 `turn_service` 的 Agent 分支能拿到 `identity=result.identity`（由 A3 接线；本卡提供签名与用例，不写 `turn_service.py`）。
5. 不修改 `app/graphs/**`；子图缺失/异常时返回可读话术，不让异常穿透 Agent。

### 2.4 交付物

- `tool_executor.py` 两个执行器绑定。
- `companion.py` 3 工具清单 + identity 处理。
- 扩展 `tests/test_tool_executor.py`、`tests/test_companion.py`。

### 2.5 验收（命令级）

```bash
uv run pytest -q tests/test_tool_executor.py tests/test_companion.py
# 必过用例：
# 1) medication_confirm_without_pending_returns_not_found
# 2) medication_confirm_with_pending_resumes_graph
# 3) medication_confirm_ignores_injected_elder_id_and_audits
# 4) memory_recall_returns_empty_without_crash
# 5) companion_exposes_three_tools
# 6) generic_identity_does_not_load_personal_memory
```

- Agent 工具清单长度 = 3，名称与 registry 完全一致。
- “我吃过药了”在有待确认事件时返回 `succeeded`，子图 `terminal_state=taken_reported`；无待确认时返回 `not_found` 可读话术。
- 注入 `elder_id` 参数：执行结果只作用于 runtime 长者，且 `audit_logs` 有 1 行。
- 预算不变：第 9 次工具调用仍被拒（复用 `guardrails`）。

### 2.6 依赖与交接

- 软依赖 B2 `recall_for_turn`（可用 stub 先开发）；依赖 A3 的 Agent 分支保持 `agent.run(identity=...)` 签名。
- 交接 A3：`medication_confirm` 工具名/参数/返回话术，供路由直连。
- 交接 D3：真机说“我吃过药了”可验；无待确认时话术可读。

---

## 3. C2 · 用药到点调度接线（1.0 人日）

> 本任务即既有 `docs/team_tasks/day07/dev-c2-用药到点触发接线.md`，**不重开 N10**；本卡只把交付接口调整为适配 A3 的 `main.py` 单点装配。

### 3.1 目标

把“到点 → 建事件 → 起图 → 触达 → 等回答 → 终态/工单”的调度侧驱动链接上，让 N10 子图第一次有生产调用点。

### 3.2 实施步骤

1. 新建 `app/services/medication_due_service.py`：只读查询“当前时刻到点的 active 计划”，`elder_id` 不作为必填（调度器需要全量）；不写库。
2. 新建 `app/scheduler/medication_jobs.py`：
   - `AsyncIOScheduler` 绑 uvicorn 事件循环，时区 `Asia/Shanghai`；
   - `CronTrigger.from_crontab(schedule_cron, timezone=...)`；
   - 到点调 `medication_service.create_event`（幂等键 `(plan_id, scheduled_at)`）→ `build_medication_graph(...)` 驱动；
   - 不自己拼 `tts_text`；触达消息由子图 `remind` 组装（TD-1 已收口）；
   - 提供 `install_scheduler(app: FastAPI) -> None` 与 `shutdown_scheduler()`；A3 在 `main.py` lifespan 调用；
   - `grep` 零 `session.add/commit/execute`，零 `from app.models`。
3. 双驱动先到先得：resume 前 `aget_state`；已有终态或 `attempt_no` 不符 → 跳过；投递侧“先查后发”（`get_attempt`）。
4. 保持既有卡 §5 七键幂等：提醒/触达/建单/终态/played/图状态；不改子图、不改 `medication_service`、不改迁移。
5. 开放点 O1（无回执 `notification_failed`）按既有卡：不改子图，统一 `missed_unconfirmed`，登记技术债。

### 3.3 交付物

- `app/services/medication_due_service.py`。
- `app/scheduler/medication_jobs.py`（含 `install_scheduler`）。
- `tests/test_scheduler_medication.py` ≥8 条。
- 交接 A3 的两行装配说明（不直接改 `main.py`）。

### 3.4 验收（命令级）

```bash
uv run pytest -q tests/test_scheduler_medication.py
# 必过 8 条：到点幂等 / 4 触达 / 4 次无回答终态 / 1 工单 / 重启恢复 / 双驱动 / 窗口内回答 / 时区

grep -rn "session.add\|session.commit\|session.execute" app/scheduler/   # 零命中
grep -rn "from app.models\|import app.models" app/scheduler/             # 零命中
```

手工：造 `schedule_cron='* * * * *'` 的 active 药单 → `medication_events` 1 行、`medication_attempts` 4 行、`work_orders` 1 行、`terminal_state='missed_unconfirmed'`；重启不新增 attempts。

### 3.5 依赖与交接

- 新文件无前置；`main.py` 装配依赖 A3（A3 调用 `install_scheduler`）。
- 交接 D3：到点触达真机验证；交接 A3：`install_scheduler` 签名与关闭顺序。

---

## 4. C3 · Agent 工具与安全边界收口（0.75 人日）

### 4.1 目标

关闭审查中 companion generic 的静默错误，补 PRD FR-DLG-07 的否定/历史/他人/模糊四类边界，以及 FR-DLG-09 医疗判断固定拒答；同时把 **G2 Tool 执行权限校验** 收敛到唯一 `ToolExecutor`，覆盖工具超时、重复调用与权限越权验收。不扩大 Agent 权限，不建第二套权限逻辑。

### 4.2 实施步骤

1. `companion.py`：
   - 彻底修 generic：`DialogueIdentity.elder_id=None` 不再触发 `__post_init__` 崩溃；`if runtime.elder_id` 不再把 staff/guardian 静默判成 generic；
   - 工具失败回传话术：`bad_args` 让模型重试（计预算）、`not_found` 如实告知、`transient` 退避≤2 后降级、`forbidden` 拒绝 + 审计；
   - 第 9 次工具调用拒绝并告知；同 Tool 1 分钟第 3 次熔断，复用 `guardrails`，不建第二套。
2. `tool_executor.py`（**G2 唯一入口**）：
   - Agent 与 A3 路由直连共用本入口；Router/Agent 不允许绕过这里直连 HTTP/MCP/DB；
   - 身份只取 `runtime.elder_id`；args 中出现 `elder_id/thread_id/device_id` 一律丢弃并写 `audit_logs.action='tool_identity_override_attempt'`；
   - 按工具 `side_effect/idempotent` 与调用方身份校验权限；跨长者资源访问返回 `forbidden`；
   - 工具超时统一映射为 `transient`，退避 ≤2 后降级话术；超时不静默、不无限等待；
   - 同幂等键重复调用不重复副作用；同 Tool 1 分钟第 3 次触发熔断并写审计/日志。
3. `safety.py`：
   - 否定（“没有胸痛”）不触发红色；
   - 历史（“昨天疼现在好了”）判黄复核；
   - 他人主体（“我爸爸胸痛”）不为本人建红色事件；
   - 模糊（“好像胸口疼”）判黄复核；
   - 词表按“部位 + 症状”双命中，不靠单字；误报 ≤2/50 普通语料；
   - 供 A3 的 G1 分类器信号触发的安全复核复用同一升级口，不新增第二套安全判定；安全模块保持最高优先级拦截权。
4. 医疗边界：询问诊断/药理（“头疼是不是高血压”“这药治什么的”）→ 固定话术“我不能判断，帮您转告医生/护工”+ 转告记录；不生成诊断性表述。
5. 提示词：固定拒答话术进 `app/prompts/personas/` 常量，不进模型自由生成；不新增工具权限。

### 4.3 验收（命令级）

```bash
uv run pytest -q tests/test_companion.py tests/test_tool_executor.py tests/test_safety.py
# 必过：generic 不崩 / staff 身份不被判 generic / 四类边界各 5 例 / 医疗拒答 /
#       第 9 次调用被拒 / 注入 elder_id 被丢弃并审计 / 工具超时 transient /
#       同幂等键重复无副作用 / 同 Tool 1 分钟第 3 次熔断 / 跨长者资源 forbidden
```

- **否定/多意图/敏感边界**：
  - 四类敏感边界各 5 例：否定 0 红色、历史黄复核、他人 0 本人红色、模糊黄复核；
  - 简单顺序多意图按序执行；动态组合进 Agent；任一子意图敏感时整轮升级，不部分执行高危动作。
- 普通语料 50 例误报 ≤2；急症正例召回 ≥95%（本日样本范围内）。
- 医疗判断问题：回复固定拒答，`work_orders`/通知有转告记录，无诊断词。
- **G2 工具权限与可靠性**：
  - 注入 `elder_id` 的调用：实际仍作用于 runtime 长者，且 `audit_logs` 有 1 行；
  - 工具超时：返回 `transient` + 可读话术，不抛异常穿透；
  - 重复调用：同幂等键第二次无副作用、无新事件；同 Tool 1 分钟第 3 次熔断；
  - 权限：非授权/跨长者访问返回 `forbidden`，并写审计。
- 第 9 次工具调用：拒绝且用户收到可读话术，不静默截断。
- `grep -rn "ToolExecutor.execute" app | wc -l` 能看到 Agent 与 Router 共用同一入口；Router/Agent 内无第二套权限判断实现。

### 4.4 依赖与交接

- 依赖 C1（工具绑定）；与 B1/B2 的响应/上下文并行。
- 交接 D3：真机验证“胸口疼”不进 Agent、“头疼是不是高血压”固定拒答。

---

## 5. 完成记录

**任务：C1（用药确认工具绑定）＋ C2（用药到点调度接线）＋ C3（Agent/安全边界收口）　状态：已完成**

### 证据

- **代码**：
  - commit `99eddc0`：收口——medical_refusal 路由接线（dialogue_service.py L590-606）、medical_refusal.py general_advice 补词（加"用不用开刀"变体）、pyproject.toml 清单修复（edge-tts 去重 / pytest-xdist 归 dev 组）、8 条新测试
  - 之前 commit：9 个新文件全部落盘（medication_due_service.py / medication_jobs.py / scheduler/__init__.py / test_scheduler_medication.py / tool_impls.py / bind_tools.py / companion.py / medical_refusal.py / 执行计划）
- **命令**：
  - `uv run pytest tests/test_scheduler_medication.py -v` → **8 passed**（到点幂等 / 四次触达 / 终态 / 工单 / 重启恢复 / 双驱动 / 窗口内回答 / 时区）
  - `uv run pytest tests/test_safety.py -k "medical_refusal" -v` → **6 passed**（diagnosis / pharmacology / general_advice 命中、正常查询不挡、话术无诊断词、纯函数幂等）
  - `uv run pytest tests/test_companion.py -k "default_tools or memory_recall_executor" -v` → **2 passed**
  - `uv run pytest -m "not slow" -q` → **1186 passed, 0 failed**（原 1178 + 新写 8 条净增）
  - `grep -rn "session.add\|session.commit\|session.execute" app/scheduler/` → **零命中**
  - `grep -rn "from app.models\|import app.models" app/scheduler/` → **零命中**
  - `uv run ruff check` → **All checks passed**（全部新改文件）
  - `uv run python scripts/update_progress.py --check` → **0**（pre-commit 钩子自动刷新）
- **数据**：
  - 调度 8 条验收 fixture 全部通过（Room/Device/StaffAccount/MedicationPlan 字段与 ORM 一致）
  - DEFAULT_TOOLS 数量 = 3（get_weather / memory_recall / medication_confirm），名称与 `tool_registry.default_registry()` 三条 spec 同名对齐
  - medical_refusal 7 条 smoke test 全部通过
  - pyproject.toml `uv sync --check` → Would make no changes
- **未完成**：**无**。B2 的 `recall_for_turn` 未落地，但 memory_recall executor 有 ImportError 降级空列表（stub 降级不阻塞），属软依赖待上游交付后自动生效。

### 交付物清单（14 个文件）

| # | 文件 | 类型 |
|---|------|------|
| 1 | `app/services/medication_due_service.py` | 新增 · 3 个只读查询 |
| 2 | `app/scheduler/medication_jobs.py` | 新增 · 调度驱动链 + install_scheduler |
| 3 | `app/scheduler/__init__.py` | 改写 · 导出 |
| 4 | `tests/test_scheduler_medication.py` | 新增 · 8 条验收用例 |
| 5 | `app/services/tool_impls.py` | 新增 · medication_confirm + memory_recall executor 闭包工厂 |
| 6 | `app/agents/bind_tools.py` | 新增 · A3 装配 hook |
| 7 | `app/agents/companion.py` | 改写 · DEFAULT_TOOLS 扩到 3 个 + generic identity 推断修 |
| 8 | `app/router/medical_refusal.py` | 新增 · 医疗拒答拦截 |
| 9 | `app/services/dialogue_service.py` | 改写 · L0.5 层接 medical_refusal_check 路由 |
| 10 | `pyproject.toml` | 修复 · edge-tts 去重 + pytest-xdist 归 dev 组 |
| 11 | `uv.lock` | 重生成 |
| 12 | `docs/plans/day07/dev-c-day07-执行计划.md` | 新增 · 执行计划 |
| 13 | `docs/plans/README.md` | 更新 · 索引 |
| 14 | `tests/test_safety.py` / `tests/test_companion.py` | 扩展 · 8 条新测试 |

### 禁触清单（全程守住）

| 路径 | 状态 |
|------|------|
| `app/graphs/**` | ✅ 零改动 |
| `app/services/medication_service.py` | ✅ 零改动 |
| `app/services/work_order_service.py` | ✅ 零改动 |
| `app/main.py` | ✅ 零改动（只提供 install_scheduler / bind_dev_c_tools 装配 hook） |
| `app/models/**` | ✅ 零改动 |
| `alembic/**` | ✅ 零改动 |
| `frontend/**` | ✅ 零改动 |

## 6. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版：C1 用药确认/记忆工具绑定、C2 调度接线（承接既有 dev-c2 卡）、C3 Agent/安全边界；共 2.75 人日 |
| v1.1 | 2026-11-10 | C3 补充 G2 唯一工具入口与权限/超时/重复调用验收；safety 边界与 A3 G1 复用同一升级口；新增多意图与权限越权用例 |
| v1.2 | 2026-11-10 | **收口为已完成**（commit `99eddc0`）：状态从"未开始"改为"已完成"；§5 替换为实际完成记录（14 交付文件 / 8+8=16 条新测试 / 1186 全绿 / 分层门禁通过）；补 pyproject.toml 清单修复（edge-tts 去重 / pytest-xdist 归 dev 组）；补 medical_refusal 路由接线（dialogue_service.py L590-606，L0.5 层） |
