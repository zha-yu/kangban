# dev-B Day 7 工作推进计划 · 回合终态、上下文与状态收口

> 状态：待主理人确认　｜　版本：v1.0　｜　日期：2026-11-10
> 负责人：dev-B　｜　性质：**执行计划文档（本文件不含任何业务代码，本轮未修改任何代码文件）**
> 任务来源：`docs/team_tasks/day07/dev-b-day07.md`（B1/B2/B3 任务卡）｜`docs/team_tasks/day07/README.md`（总表与 DAG）
> 上游依据（事实只引这两份）：`docs/康伴3D数字人_PRD.md`（FR-DLG-01/03/05/06）｜`docs/康伴3D数字人_技术设计文档.md`（§3.4/§6/§8.3）
> 遗留问题源：`docs/plans/adhoc-20261110-day6全面审查.md`（经总表 §7 追踪矩阵核对，本计划不重复展开）
> 执行纪律：**逐步执行，每一步完成后自检、向主理人汇报，经确认后再进入下一步。**

---

## 0. 项目认知摘要（计划的前提基线）

1. **本日定位**：实际 Day 7 = 原大纲 Day 6 内容（三级路由 + System One），同时收口实际 Day 6 审查的 P1/P2/P3 共 40 条；dev-B 独占其中 B1（回合终态与打断，1.25 人日）、B2（上下文 NULL/预算 + 状态降级 + 记忆生产接线，1.25 人日）、B3（metrics 白名单 + E20 并发 + API 边界，0.5 人日），合计 3.0 人日。
2. **dev-B 写锁（独占）**：`app/services/response_service.py`、`history_service.py`、`app/core/context.py`、`state_service.py`、`memory_service.py`、`metrics_service.py`、`staff_service.py`、`app/api/staff.py`、`app/api/schemas/staff.py`、`app/audio/signaling.py`（仅 interrupt/ack 分支与异常包裹）、对应 5+1 个测试文件。
3. **禁触**：`app/services/turn_service.py`（A3 独占，P2-10 的双写口收敛也由 A3 在其侧删第二写口，B1 只在 `commit_response` 侧做"已存在则只补投影"的防御）、`app/system_one/**`、`app/agents/**`、`app/scheduler/**`、`frontend/**`、`0013` 及以前迁移。dev-B 本日**不新增任何迁移**。
4. **前置状态核查结果（2026-11-10 实测）**：
   - `docs/**/contracts-system-one.md` **尚不存在**，dev-A 四卡均未开始；
   - 但总表 §5.4 已给出冻结口径的 C→S 打断契约：`{ "type": "interrupt", "response_id": "...", "reason": "barge_in" }`，含幂等、归属校验、`response_interrupted` + `finish_turn("interrupted")` 语义；
   - `tests/test_response_service.py`、`test_history_store.py`、`test_context_builder.py`、`test_state_service.py`、`test_memory_service.py`、`test_metrics_service.py` 已存在可扩展；`tests/test_staff_reassign.py` **不存在，需新建**。
5. **总表既定启动顺序（§4.1）**：dev-B 先开 **B2**（无依赖、最影响上下文崩溃）；**B1 等 A1 的 interrupt 字段确认后立即开工**；B3 与 B1/B2 同人串行、无前置。
6. **本日不做**：多 worker ack 归属（P2-9 多 worker 部分，登记 Day 9）、断连完整重放（P2-11 重放部分，登记 Day 9）、记忆冲突治理/遗忘（登记 Day 8）、家属端、健康采集、`played` 纳入用药重试判定。

---

## 1. 执行顺序与依赖图

```text
Step 0  启动确认（本计划裁决，主理人点头）
  └─ Step 1~3  B2（无硬依赖，先行）：context → state_service → memory + B2 收口验收
       └─ Step 4  B1 契约前置确认（interrupt 契约来源裁决）
            └─ Step 5  B1：契约测试(红) → response_service 终态重构 → signaling interrupt 分支 → history_service → B1 收口验收
                 └─ Step 6  B3：metrics 白名单 → E20 并发 → API 边界 → B3 收口验收
                      └─ Step 7  总收口：静态门 + 全量测试 + 完成记录回写任务卡
```

- B2 内部顺序：先 context（P1-2 最伤）→ state（P2-13~15）→ memory（接线收口）。
- B1 唯一外部硬前置是 interrupt 契约；按总表口径 B2 期间 A1 契约应已冻结（J1 第 1 天 17:00）。
- B3 无外部依赖，放在最后因人日最小且可与本人其他步骤串行。

---

## 2. 分步计划（每步：操作 → 交付物与质量要求 → 自检标准 → 确认点）

### Step 0 · 启动确认（0.25h，等待主理人裁决）

**操作**：向主理人提交本计划，并请裁决两个问题（见 §5 待裁决 O-B1/O-B2）。

**交付物**：本计划文件本身；裁决结论将回写至本文件 §5。

**自检标准**：计划中每一步都有明确的交付物格式与命令级自检标准；无一步要求编写计划外代码。

**确认点**：主理人批准 Step 1 开工。

---

### Step 1 · B2-1 上下文修复（`app/core/context.py`，约 0.4 人日）

**操作**（全部为"契约测试先行"：先在 `tests/test_context_builder.py` 加红用例，再改实现）：

1. NULL 防御：`assistant_delivered_text` / `user_text` 统一 `or ""`，消除 `None + str`；
2. `status=="interrupted"` 且文本非空时才追加打断标记，空文本不拼；
3. generic 轮短路 `waiting_for` 任务绑定注入（`is_generic` 时不注入"优先解释为用药确认"）；
4. 四处裸 `except Exception: return ""` 改为捕获具体异常 + `logger.warning` + `degraded` 标记；
5. 预算口径：`history_turns` 与 `waiting_for` 计入 `budget_used`；新增 `estimate_tokens` 单元测试（中文 2 字符≈1 token 仅作粗略口径，但必须可复算）；
6. 每轮 `read_turns` 只调 1 次；memories/facts 去重查询；摘要与历史去重；
7. 已知 dataclass 直接属性访问（去防御式 `getattr`）；零记忆不误报 `degraded`。

**交付物与质量要求**：`app/core/context.py` 修改 + `tests/test_context_builder.py` 扩展；代码过 `ruff check` 与 `mypy app`；不引入新依赖。

**自检标准**：

```bash
uv run pytest -q tests/test_context_builder.py
```

必过新用例：`interrupted_with_null_delivered_does_not_crash`、`llm_messages_never_content_null`、`generic_turn_skips_waiting_for_binding`、`budget_counts_history_and_waiting_for`、`estimate_tokens_consistent`。
手工口径：构造 `assistant_delivered_text=NULL + status=interrupted` 的历史行，上下文构建不抛异常，LLM 消息 `content` 绝不为 `None`。

**确认点**：向主理人汇报测试输出后等待确认，再进入 Step 2。

---

### Step 2 · B2-2 状态降级与故障语义（`app/services/state_service.py`，约 0.4 人日）

**操作**（同样测试先行，`tests/test_state_service.py`）：

1. `snapshot()` 增加 `strict` 参数（或 `_snapshot_raw`）：RedisError 上抛不再吞；`apply(is_medication_task=True)` 真正抛 `StateUnavailable`（而非 `StateVersionConflict`）；
2. Redis 写失败降级内存后，恢复时把内存版本回写/合并到 Redis；`snapshot` 读 Redis 前先比较内存版本，防旧 Redis 覆盖；
3. `from_dict` 加 schema 校验（版本、必填 dict、字段类型）；Lua 返回 -2 改为冲突/重试，不走无条件 SET；`_memory_store` 加 LRU/TTL 上限；
4. `state.apply` 失败时保留已写事实，写 `turn_failed` 兜底，不做全轮回滚。

**交付物与质量要求**：`app/services/state_service.py` 修改 + `tests/test_state_service.py` 扩展；错误类型语义变化（`StateUnavailable` vs `StateVersionConflict`）在 docstring 写清，供 C1 用药工具消费方对齐。

**自检标准**：

```bash
uv run pytest -q tests/test_state_service.py
```

必过新用例：`redis_down_medication_raises_state_unavailable`、`redis_recovery_merges_memory_version`、`from_dict_rejects_bad_schema`、`apply_failure_keeps_written_facts`。
手工口径：停 Redis + `is_medication_task=True` → 抛 `StateUnavailable`，不是 `StateVersionConflict`。

**确认点**：汇报后等待确认。

---

### Step 3 · B2-3 记忆生产接线 + B2 收口（`app/services/memory_service.py`，约 0.45 人日）

**操作**：

1. `memory_service.py` 暴露 `async extract_after_turn(*, elder_id, thread_id, turn_id, user_text, assistant_text) -> int`；
2. 用 `asyncio.create_task` 或后台队列在回合结算后执行，不阻塞首包；失败只记日志；
3. `insert_candidate` 必须带 `source_turn_ids`；召回强制 `elder_id + status='active' + 有效期` 过滤；
4. 本日只做"偏好/事实"一类低风险候选，不做冲突治理/遗忘；
5. **边界纪律**：B1 侧调用此端口时，若 B1 尚未开工，使用 `MemoryExtractionPort` stub 解耦，不反向等待；
6. 跑 B2 整卡验收（下方命令），收口 B2，更新任务卡状态行。

**交付物与质量要求**：`memory_service.py` 修改 + `tests/test_memory_service.py` 扩展；`extract_after_turn` 函数签名与 `recall_for_turn` 签名写入本计划交付说明，作为与 C1 的交接件（见 §4）。

**自检标准（B2 整卡收口）**：

```bash
uv run pytest -q tests/test_context_builder.py tests/test_state_service.py tests/test_memory_service.py
```

必过 7 用例（Step 1/2 全部 + `memory_extract_after_turn_persists_candidate`）。
口径：一轮对话后 `memory_chunks` 至少 1 条候选且 `source_turn_ids` 非空；首包延迟不因提取增加 >50ms（本地测）；超预算用例低优先级先裁，`safety/policy`、`identity`、`active_task`、`current_turn` 不裁。

**确认点**：B2 收口汇报（含测试输出），等待确认。此步完成后即可支援 C1 的 `memory_recall` 联调。

---

### Step 4 · B1 契约前置确认（0.25h，等待裁决/交接）

**操作**：确认 interrupt 契约来源（裁决项 O-B1）。无论选哪条路径，先在 `tests/test_response_service.py` 落契约测试（红）：

1. 末段 `played` → `conversation_turns.status=completed`、`assistant_delivered_text` 非 NULL；
2. 打断 → `status=interrupted`、`delivered_text` 为 `[0..last_played]` 连续前缀；
3. 重复 ack / 迟到 ack → 不重复写事件；
4. `response_completed` 每轮恰好 1 条。

**交付物**：红色契约测试 4 组；裁决结论记录。

**自检标准**：测试当前为红（因生产零调用 `finish_turn`、`interrupt` 无信令分支），且红的原因与任务卡 §2.2 现状证据一致。

**确认点**：确认测试红 + 契约口径后开工 Step 5。

---

### Step 5 · B1 回合终态与打断闭环（约 1.0 人日，4 个子步，每子步内部即改即测）

#### Step 5a · `response_service.py` 终态/打断重构

**操作**：
1. 抽出 `_settle_turn(state, *, status, delivered_text)`：调用 `HistoryStore.commit_response` + `finish_turn`；
2. 末段 `played`：先写 `response_segment_delivered`，再写**唯一** `response_completed`，再 `commit_response(delivered_text=..., status="completed")`、`finish_turn("completed")`；
3. `canceled`：校验 ①②③ 后置 `PHASE_ENDED`、`delivery_status="interrupted"`、写 `response_interrupted`，按水位截断 `delivered_text`，`commit_response` + `finish_turn("interrupted")`；`segment_id` 与水位不一致时告警并以水位为准（P2-8）；同代 `played` 在 canceled 后被拒绝（P2-7）；
4. `interrupt()`：幂等；已结束 no-op；bump epoch；取消 cancellable；写事件；结算落库；
5. `response_completed` 双写防御：`commit_response` 侧检测已存在则只补投影（P2-10，A3 侧删第二写口不归本卡）；
6. epoch/水位回写 Redis：`session:{thread_id}:epoch` 与 `response:{response_id}` 最小字段，TTL 2h；重启后从 Redis 恢复（P2-9 单进程范围，多 worker 登记 Day 9）；
7. 公开方法改 `.get()`/显式错误（P3-30）；`_safe_append_event` 不吞 `CancelledError`（P3-27）；
8. `start()` 失败重试 1 次 + `response_delivery_failed` 事件（P2-11 本日范围）。

**自检**：`uv run pytest -q tests/test_response_service.py` 中 `played_last_segment_finishes_turn`、`canceled_same_epoch_played_rejected`、`interrupt_idempotent`、`redis_epoch_restore` 等转绿。

#### Step 5b · `signaling.py` interrupt 分支与异常包裹

**操作**：
1. 新增 C→S `interrupt` 分支：`{type, response_id, reason}`；校验设备/会话归属；调 `ResponseService.interrupt()`；异常包裹不炸连接；
2. `identity_confirm/client_diag/media` 等 handler 统一 try/except（P3-28）；
3. `segment_ack` 现有四步校验不动，只补 `canceled` 终态语义。

**自检**：interrupt 分支有归属校验失败用例（非法 `response_id` 不落库、不炸连接）；`interrupt` 生产调用点 ≥1。

#### Step 5c · `history_service.py` limit 与幂等

**操作**：`read_turns` limit 上限 50、超限截断 + 日志（P3-32）；`finish_turn` 幂等：已终态不覆盖，`completed` 不得覆盖 `interrupted`。

**自检**：`tests/test_history_store.py` 新用例转绿。

#### Step 5d · B1 整卡收口

**交付物**：`response_service.py` 终态/打断重构 + Redis 回写；`signaling.py` interrupt 分支 + 异常包裹；`history_service.py` limit/幂等；测试扩展。

**自检标准**：

```bash
uv run pytest -q tests/test_response_service.py tests/test_history_store.py
```

必过 6 用例：`played_last_segment_finishes_turn`、`interrupted_turn_truncates_delivered_text`、`duplicate_played_does_not_duplicate_completed`、`canceled_same_epoch_played_rejected`、`interrupt_idempotent`、`redis_epoch_restore`。

SQL 抽查（真实一轮后）：

```sql
SELECT status, assistant_delivered_text IS NOT NULL AS has_delivered
FROM conversation_turns ORDER BY started_at DESC LIMIT 1;   -- 期望 completed | t

SELECT count(*) FROM conversation_events
WHERE turn_id = :turn_id AND event_type = 'response_completed';  -- 期望 1
```

口径：打断后 200ms 内服务端状态 `interrupted`、`delivered_text` 只含已 `played` 前缀；重复/迟到 ack 不产生第二条 `response_completed`。

**确认点**：B1 收口汇报，等待确认。**联调提醒（J3）**：此处须与 **dev-D（D1）** 交接——交接内容：`interrupt` 消息形状（§5.4）、`response_interrupted` 事件、`delivery_status` 取值；与 **dev-A（A3）** 交接——`TurnOutcome` 可带 `route_layer` 但不改响应流字段、`commit_response` 双写口由 A3 在 `turn_service.py` 侧删除（A3 独占文件，dev-B 只提 patch 需求）。

---

### Step 6 · B3 metrics 白名单 + E20 并发 + API 边界（约 0.5 人日）

**操作**（测试先行；`tests/test_staff_reassign.py` 需新建）：

1. `metrics_service.py`：DB 白名单加入 `response_segment`；`record_stage` 不再每段 WARNING；补落库断言；
2. `staff_service.py`：`reassign_elders` 对 `elder_id` 排序后 `SELECT ... FOR UPDATE` 行锁（或同事务锁 `elder_staff_assignments`）；两个 admin 并发摘不同护工只允许一个成功；用 `elders.updated_at`/`version` 做乐观并发 CAS，后提交者 409；409/403/404 失败路径写 `audit_logs`（操作者、trace_id、原因、差集）；admin 不能停用自己/最后一个 admin；
3. `api/staff.py` / `api/schemas/staff.py`：`role/status` 用 `Literal`/枚举校验，非法值 422；`elder_ids` 上限 200 超出 422；409/403 映射明确不返回 500。

**交付物与质量要求**：上述 4 文件修改 + 新建 `tests/test_staff_reassign.py` + 扩展 `tests/test_metrics_service.py`；错误码契约与 §4 D2 交接件一致。

**自检标准**：

```bash
uv run pytest -q tests/test_metrics_service.py tests/test_staff_reassign.py
uv run pytest -q tests/test_response_service.py tests/test_history_store.py tests/test_context_builder.py tests/test_state_service.py tests/test_memory_service.py
```

必过：`response_segment` 落库；并发摘护工 1 成功 1 409；最后 admin 保护；`elder_ids` 201 个返回 422；`GET /api/staff?role=superman` 返回 422 而非 200 空列表。

```sql
SELECT count(*) FROM metrics WHERE stage='response_segment' AND created_at > now() - interval '5 min';
-- 期望 > 0（一轮分段后）
```

并发口径：两个事务同时移除不同 primary_care，最终该长者至少 1 位责任护工；失败方 409 且 `audit_logs` 有行。

**确认点**：B3 收口汇报，等待确认。**联调提醒**：与 **dev-D（D2）** 交接——409/403/422 错误码、错误字段与分页字段契约（`PUT /api/staff/{staff_id}/elders` 全量覆盖 + 差集提交口径见总表 §5.7）。

---

### Step 7 · 总收口（0.25h）

**操作**：
1. 跑静态门与全量测试：`uv run ruff check app tests`、`uv run mypy app`、`uv run pytest -q`（全绿，无新增 ignore）；
2. 按 `docs/team_tasks/day07/dev-b-day07.md` §5 完成记录模板回写 B1/B2/B3 状态与证据（commit、pytest 输出、turn_id/SQL 结果）；改任务卡首部 `> 状态：` 行（pre-commit 自动刷新 PROGRESS.md）；
3. 汇总未关闭项与登记项：P2-9 多 worker、P2-11 完整重放（Day 9）；记忆冲突治理（Day 8）；
4. 支援 G7 集成门禁（§8.1 G1/G2 两道门的 B 侧证据）。

**交付物**：任务卡状态更新 + 完成记录；收口汇报。

**自检标准**：PROGRESS.md 中 dev-B 卡状态为已完成；五道门中 G1/G2 在 dev-B 范围全绿。

**确认点**：最终汇报。**联调提醒（J2/J3/J6）**：D3 真机联调（第 3 天）需要 dev-B 在场，提供终态/打断行为证据与 `trace_id` 排查支持。

---

## 3. 交付物清单与格式要求（汇总）

| # | 交付物 | 格式/位置 | 质量要求 |
| --- | --- | --- | --- |
| 1 | 本计划文件 | `docs/plans/day07/dev-b-day07-工作推进计划.md` | 每步可执行、可校验；裁决结论回写 §5 |
| 2 | B2 代码 + 测试 | `context.py`/`state_service.py`/`memory_service.py` + 3 个测试文件扩展 | 契约测试先行；7 用例必过；ruff/mypy 干净 |
| 3 | B1 代码 + 测试 | `response_service.py`/`signaling.py`/`history_service.py` + 2 个测试文件扩展 | 6 用例必过；`response_completed` 唯一；SQL 抽查达标 |
| 4 | B3 代码 + 测试 | `metrics_service.py`/`staff_service.py`/`api/staff.py`/`api/schemas/staff.py` + 新建 `test_staff_reassign.py` | 并发 1 成功 1 409；422/409/403 契约稳定 |
| 5 | 交接件 | `extract_after_turn`/`recall_for_turn` 签名；interrupt 消息口径；E20 错误码 | 先契约后编码，文字留痕于本文件 §4 |
| 6 | 完成记录 | `dev-b-day07.md` 状态行 + §5 模板 | 证据：commit + 命令输出 + 数据抽查 |

## 4. 联调与交接矩阵（dev-B 视角）

| 时点 | 对象 | 交接内容 | 方向 |
| --- | --- | --- | --- |
| Step 4（第 1 天 17:00，J1） | dev-A（A1） | C→S `interrupt` 消息字段确认（`type/response_id/reason`） | A 冻结 → B 确认 |
| Step 3 后 | dev-C（C1） | `recall_for_turn(elder_id, query, limit)` 函数签名与返回结构；`memory_recall` 只取 `runtime.elder_id`，参数身份丢弃并审计 | B → C |
| Step 5d（第 2 天下午，J3） | dev-D（D1） | `interrupt` 消息、`canceled` 终态口径、`response_interrupted` 事件、`delivery_status` 取值；联调判据：前端发 interrupt，服务端 200ms 内状态 `interrupted` | B → D |
| Step 5d | dev-A（A3，J2） | `commit_response` 双写口的收敛：A3 在 `turn_service.py` 删第二写口，B 侧只补投影；`TurnOutcome.route_layer` 不改响应流字段 | B ↔ A（patch 提案，A3 持锁） |
| Step 6 后 | dev-D（D2） | E20 `PUT /api/staff/{staff_id}/elders` 的 200/403/409/422 错误码与字段（总表 §5.7） | B → D |
| Step 7（第 3 天，J6） | dev-D（D3）+ 全员 | B1/B2/B3 验收证据（pytest 输出、SQL 抽查、turn_id/trace_id），真机联调排查支持 | B → 全员 |

## 5. 待裁决（2026-11-10 主理人已裁决）

- **O-B1（影响 Step 4/5 开工时点）**：`contracts-system-one.md` 目前尚不存在、dev-A 未开始。B1 的 interrupt 契约按哪条路径执行？
  - 方案 a（推荐）：以总表 README §5.4 已冻结口径为契约直接开工，A1 产出契约文件后若字段有差异再按"先改契约再改代码"对齐；
  - 方案 b：严格等 dev-A 产出 `contracts-system-one.md` 并经 J1 签字后再开工 B1（期间 dev-B 只做 B2/B3）。
  - **✅ 裁决（2026-11-10）：采用方案 a**——按总表 §5.4 冻结口径直接开工 B1；A1 契约文件产出后如有字段差异，先改契约再改代码对齐。
- **O-B2（B2 收口时点）**：B2 的记忆接线需要与 B1 的终态结算衔接（`extract_after_turn` 调用点）。若按方案 a 让 B1 提前开工，`MemoryExtractionPort` stub 的切换时点是"B1 收口前"还是"总收口统一接"？默认：B1 侧先用 stub，Step 7 总收口统一接真实端口。
  - **✅ 裁决（2026-11-10）：总收口统一接**——B1 全程使用 `MemoryExtractionPort` stub，Step 7 总收口时统一把真实 `extract_after_turn` 端口接到终态结算点，一次性验收记忆落库。

## 6. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版：B2 先行 → B1（契约确认后）→ B3 → 总收口，共 8 步逐步确认；含交接矩阵与两项待裁决 |
| v1.1 | 2026-11-10 | 主理人裁决回写：O-B1 采用方案 a（按 §5.4 直接开工 B1）；O-B2 总收口统一接真实记忆端口 |
