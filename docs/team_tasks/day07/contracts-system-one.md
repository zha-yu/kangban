# Day 7 契约增量 · System One（Laya 本地 sidecar）v1.1

> 状态：已冻结（J1 待签）　｜　版本：**v1.1**（A3 落地回填）　｜　日期：2026-11-10
> v1.1 只补「实测后必须改的口径」，不新增消息/字段：内部预算 180→195ms、shadow 计入路由耗时、
> 澄清按意图分流（chat/other 免澄清）、G1 先采信 safety.py 的四类边界、G1 升级落库的边界。
> 变更记录见 §7。
> 关联：`docs/team_tasks/day07/dev-a-day07.md`（A1 产出）｜`docs/plans/day07/dev-a-三级路由与决策日志.md` §5.2.0/§10
> 上游依据：PRD FR-DLG-02/03/06/07（line 264–274）、§5.3 性能（line 543–550）｜技术设计文档 §3.4 `decision_logs`（line 686–706）、§6（line 897）
> 实测依据：`spikes/chinese_jev_hello/README.md`（权重/许可/延迟/20 条准确率）

---

## 0. 一页结论

| 项 | 冻结口径 |
| --- | --- |
| 模型 | `convaiinnovations/laya-multilingual`（322M / F16 / 615 MB / `apache-2.0` / 含 commercial-use） |
| 推理入口 | 模型仓库自带 `rl_agent_api.RLAgent.system_one(state, questions)`，**不使用 pip `laya` 包** |
| 服务形态 | 本地 FastAPI sidecar，端口 **7001**（不变，避免动 nginx/防火墙/前端） |
| 路由预算 | 客户端 **200 ms**（PRD §5.3，不动）；**sidecar 内部 195 ms**（v1.1：180→195，仍在 200 内）；实测单问 p95 167 ms（空闲机） |
| 快路径 | **只问 `intent` 一题**；`is_sensitive` 走 shadow，不阻塞路由（§6.3） |
| 置信阈值 | 0.85（`system_one_confidence_threshold`），低于此只澄清一次 |
| 降级 | `degraded=true` 必须同时 `intent=other` / `confidence=0.0` / `is_out_of_domain=true`（schemas 强校验） |
| 澄清策略（v1.1） | **动作型意图**（weather/medication/opera/movie/health_checkup）低置信才澄清：首次 `clarify`、二次 `agent`；**`chat`/`other` 低置信不澄清、直接 `agent`**；域外一律澄清一次 |
| 意图枚举 | 七类：`chat / medication / opera / movie / weather / health_checkup / other` |
| 日志 | 每轮恰好 1 行 `decision_logs`，`route_layer ∈ rule/system_one/agent/fallback`（`laya` 仅历史只读） |

---

## 1. `POST /v1/decide` 请求 / 响应

### 1.1 请求

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `request_id` | str | 否 | 调用方生成，响应原样回带（排障用） |
| `trace_id` | str | 是 | 全链路 trace；sidecar 只写日志，不落库 |
| `state` | str / object / list | 否 | 判断材料。**必须带上下文**：`asr_final` + `current_task` + `media_status`（缺上下文时长指令会判错，见 spike §5） |
| `questions` | `{qid: QuestionSpec}` | 否 | ≥1 题；题面由 `app/system_one/questions.py` 唯一构造 |
| `model_version` | str | 是 | 请求侧声明的模型版本；响应以 sidecar 实际加载的为准 |

**QuestionSpec**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | `choice` / `noul` / `score` | 三种原语，不得扩展 |
| `instructions` | str | 任务指令，中文 |
| `criteria` | `{键: 描述}` / `{"false":..,"true":..}` / `[等级0, 等级1, ...]` | 形状随 `type` 变，`schemas.QuestionSpec` 强校验 |

> ⚠️ 约束：`choice` 的候选描述要短（`head_max_len=256`，7 个候选时每个只剩 ~20 token）；
> **候选顺序写死在 `INTENT_CRITERIA` 里，禁止运行时重排**（模型对顺序敏感）。

### 1.2 响应

```jsonc
{
  "request_id": "r-…",
  "result": {
    "intent": "weather",            // 七类之一
    "confidence": 0.87,             // 0~1，来自候选概率分布的熵（不是 max prob）
    "is_sensitive": false,
    "sensitive_probability": 0.03,  // noul 题只有概率，没有置信度；无 shadow 结果时为 null
    "privacy_scope": "shared",      // personal | shared | none
    "is_out_of_domain": false,
    "degraded": false,
    "latency_ms": 168,
    "model_version": "laya:multilingual:q1",
    "multi_intent": false,
    "sub_intents": []
  }
}
```

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `intent` | 枚举 | 七类之一；**不做工具选择**，只给路由层做参考 |
| `confidence` | float | 熵归一化置信度；`<0.85` 触发"只澄清一次" |
| `is_sensitive` | bool | 由 shadow 的 noul 概率经阈值（默认 0.80）判定；**shadow 未返回时为 false**，此时安全判定只靠 G0 规则 |
| `sensitive_probability` | float\|null | shadow 原始概率，落 `decision_logs.is_sensitive` 的依据留痕 |
| `privacy_scope` | 枚举 | 双人间隐私收敛用（P2 说明项） |
| `is_out_of_domain` | bool | 域外检测；降级时强制 true |
| `degraded` | bool | 见 §1.3 不变量 |
| `latency_ms` | int | sidecar 内部推理耗时（不含网络） |
| `model_version` | str | `laya:multilingual:<questionset_version>` |
| `multi_intent` / `sub_intents` | bool / 数组 | 多意图信号；`sub_intents` 为空即单意图 |

### 1.3 降级不变量（强校验，违反即 422）

```
degraded == true  ⇒  intent == "other"  ∧  confidence == 0.0  ∧  is_out_of_domain == true
                     ∧  is_sensitive == false  ∧  sub_intents == []
```

理由：半套降级值会让下游"既走降级分支又执行工具"。`schemas.SystemOneResult` 用 `model_validator` 强制，
`degraded_result()` 是唯一推荐的构造入口。

### 1.4 `GET /health`

```jsonc
{"status": "ok", "model_id": "laya-multilingual", "model_version": "laya:multilingual:q1",
 "model_loaded": true, "device": "cpu", "provider": "laya"}
```

- 模型缺失/加载失败 → **HTTP 503** + `{"status":"model_missing","model_loaded":false, ...}`。
- **禁止**返回 200 + `model_loaded=false`（那等于假 ok）。

---

## 2. `RouteDecision`（Router → turn_service）

```python
@dataclass(frozen=True, slots=True)
class RouteDecision:
    route_layer: Literal["rule", "system_one", "agent", "fallback"]
    action: Literal["tool", "agent", "clarify", "escalate"]
    intent: Intent
    confidence: float
    is_sensitive: bool
    escalated_to_s2: bool
    reason: str | None            # capability_not_ready / low_confidence / out_of_domain / sidecar_failed / sensitive_g1
    tool_name: str | None
    tool_args: dict[str, Any] | None
    clarify_text: str | None
    multi_intent: bool
    sub_intents: tuple[Intent, ...]
    latency_ms: int
```

| 字段 | 谁能填 | 约束 |
| --- | --- | --- |
| `route_layer` | Router | 四值；`fallback` 表示分类器不可用，**不计入 system_one 命中率** |
| `action` | Router | `tool`=直连一个工具（必须经 `ToolExecutor`）；`agent`=下沉主控；`clarify`=只澄清一次；`escalate`=G1 安全升级 |
| `tool_name` / `tool_args` | 规则层或意图映射 | `action != "tool"` 时必须为空 |
| `clarify_text` | Router | `action == "clarify"` 时必须非空，且同一个 `thread_id` **只允许出现一次** |
| `multi_intent` / `sub_intents` | Router | 简单顺序多意图展开为有序动作；动态组合直接 `agent`；任一子句敏感 → 整轮 `escalate` |
| `latency_ms` | Router | 路由层自身耗时，**含快路径与 shadow 等待**（v1.1 定口径），写进 `decision_logs.latency_ms`；不做「剔除 shadow」的重新定基线 |

**不变量**：`action=="escalate"` ⇒ `is_sensitive==True` ∧ `tool_name is None`（敏感命中不得执行工具）。

---

## 3. C→S `interrupt`（客户端打断）

```jsonc
{"type": "interrupt", "response_id": "…", "reason": "barge_in"}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `type` | 是 | 固定 `interrupt` |
| `response_id` | 是 | 要打断的那条回复；缺失 → 按 `invalid_payload` 回 error，不关连接 |
| `reason` | 否 | 默认 `barge_in`；预留 `user_exit` |

语义：服务端收到后停止该 `response_id` 的后续分段下发、把 response 置 `interrupted`、bump epoch；
**幂等**：同一 `response_id` 重复 `interrupt` 只生效一次，不报错。
打断不改变用药待确认状态（PRD FR-UI-04：业务状态保留）。

---

## 4. `decision_logs` 与 `route_layer`

| 字段 | 来源 | 备注 |
| --- | --- | --- |
| `log_id` / `created_at` | DB | uuid4 / 服务端时间 |
| `thread_id` / `trace_id` | 运行时 | `trace_id` 与请求头一致，用于 JOIN |
| `elder_id` | `runtime` | generic（无身份）轮次不写本表 |
| `input_text` | ASR final | **落库前脱敏**（手机号/身份证/房间号） |
| `intent` / `confidence` | `RouteDecision` | 七类 + 0~1 |
| `is_sensitive` | G0 或 shadow | G0 命中时恒 true |
| `route_layer` | `RouteDecision` | `rule` / `system_one` / `agent` / `fallback`；**`laya` 仅历史只读** |
| `escalated_to_s2` | `RouteDecision` | 是否下沉大模型 |
| `latency_ms` | `RouteDecision` | 路由层耗时 |
| `asr_confidence` | ASR | 可空 |
| `model_version` | sidecar | `laya:multilingual:<q1>` |

**写入纪律**：只 INSERT；每轮**恰好 1 行**；写失败不阻断对话（打 `decision_log_failed` + 指标）。

---

## 5. 低置信：按意图分流（v1.1 修订）

**v1.0 是"一律只澄清一次"；v1.1 改为"澄清只服务于工具执行"**——只有会**执行动作**的意图才值得
反问一句，闲聊/其他多问一次只会让长辈重复一遍（实测 50 条普通样本：一律澄清时澄清率 28%，
其中 7 条其实判对了；分流后降到 8%，准确率与高置信错误不变）。

```
intent ∈ {chat, other}（不映射任何工具）且 is_out_of_domain=false
  └─ 低置信 → action="agent"，reason="low_confidence:no_clarify_intent"
              （不查 was_clarified、不占"只澄清一次"的额度）

intent ∈ {weather, medication, opera, movie, health_checkup}（动作型）
  ├─ 读同 thread_id 最近一条 decision_logs：已澄清过 → action="agent"（reason 带 :clarified_once）
  └─ 否则 → action="clarify"，clarify_text 由 Router 常量提供（不调 LLM）

is_out_of_domain=true → 一律澄清一次（域外话术），第二次 action="agent"
```

- 判据只看**同 `thread_id` 的最近一条**，不回溯整段历史（避免长会话永远不再澄清）。
- `clarify` 轮**不调 Agent**、不执行工具。
- **阈值语义（v1.1）**：`≥0.85 走 system_one` 只约束**动作型意图**（决定是否执行工具）；
  `chat`/`other` 只要求「不误执行」，它们永不映射工具，这是安全边界本身。
- 「是否已澄清过」的判定不靠 `action` 列（`decision_logs` 没有该列），而是
  `confidence < 阈值 且 escalated_to_s2=false`（`decision_log_service.was_clarified` 唯一负责）。

---

## 6. 三层护栏与唯一工具入口

### 6.1 G0 · 入口安全（既有，不由本契约新增）

`app/router/safety.py: safety_check()` 在 Router **之前**执行，一票否决：命中即生成红色事件/工单，
`route_layer` 记为 `rule`、`escalated_to_s2=true`。否定/历史/他人/模糊四类边界由 `safety.py` 判定
（PRD FR-DLG-07），**不由分类器裁决**。

### 6.2 G2 · 工具权限（既有 `ToolExecutor`）

所有工具执行——**无论是规则直连、意图直连还是 Agent 调用**——只经 `ToolExecutor.execute()`：
白名单 → 身份消毒 → 预算/熔断 → 参数校验 → 幂等 → 审计。Router 内**不得**出现第二套权限判断、
HTTP 直连或 MCP 直连（`grep` 可证）。

### 6.3 G1 · 分类器信号触发的安全复核（本契约新增）

```
快路径：POST /v1/decide(questions={intent})         ← 200ms 预算内，返回 intent/confidence
shadow：POST /v1/decide(questions={is_sensitive})   ← 并行、独立预算（system_one_shadow_timeout_ms=400）
        ├─ 返回且 p ≥ 0.80 → 在"工具执行前"调用既有安全升级口（同一 `_escalate_safety`）
        └─ 超时/失败      → 当没问过（不阻塞、不报错），安全判定仍由 G0 兜底
```

三条纪律：

1. **shadow 永不阻塞路由**：路由结果只依赖 `intent`；shadow 结果用于"执行前再拦一次"。
2. **快路径只问一题**是有实测依据的：两问 285ms、四问 668ms 均超 PRD 的 200ms（spike §4）。
3. **敏感升级必须走同一个口**：不得在 Router 内新建红色事件逻辑；重复触发按 `turn_id/thread_id` 幂等，
   不重复建红色事件（AC-04/FR-DLG-01）。

**v1.1 补充四条落地口径**（A3 实现时确定，均已在 `app/router/decision.py` 落地）：

4. **四类边界不由分类器裁决**（对齐 §6.1 / FR-DLG-07）：shadow 判敏感后，Router **先调
   `safety_check()`**；若它给出 `negation` / `recovery` / `hedge` / `other_person` 结论，
   则采信 G0、**不升级**。实测必要性：不采信时「我爸爸胸痛」「昨天疼现在好了」都会被 G1 误升级。
5. **shadow 的等待计入路由耗时**（`RouteDecision.latency_ms`）：它仍在本轮决策链上，就是用户真实等待。
6. **G1 升级的落库边界**：`escalate_safety_event()` 要求急症类别（`chest_pain`/`breathing`/`fall`/
   `stroke`/`self_harm`）才能建红色事件+工单，而分类器只给概率。**当前实现只在 `audit_logs`
   记 `system_one_sensitive_signal`（含 probability/model_version），不建单**——在 Router 里编一个
   类别等于伪造病历。要真建单需先给出「疑似急症」的类别口径（待裁，见 A3 报告 §6）。
7. **失败上限**：shadow 超时/失败即"当没问过"（`degraded=True`、`is_sensitive=False`），
   安全仍由 G0 兜底；**不得**因为 shadow 失败把本轮打成 `fallback`。

---

## 7. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版（A1 产出）：冻结 `/v1/decide` 请求响应与降级不变量、`RouteDecision` 字段表、`interrupt` 消息、`route_layer` 四值（`laya` 只读）、低置信只澄清一次的判据、G0/G1/G2 三层护栏与唯一工具入口；**新增快路径单问 + shadow 的预算切分**（依据 CPU 实测 1 问 167ms / 2 问 285ms / 4 问 668ms 与主理人 2026-11-10 裁决） |
| **v1.1** | 2026-11-10 | **A3 落地回填，不新增消息与字段**：①sidecar 内部预算 180→**195ms**（实测端到端 p50 188ms，180 正好压在 200ms 边界上，主理人裁决放宽；客户端 200ms 不动）；②`latency_ms` 明确**含 shadow 等待**；③§5 澄清改为**按意图分流**（`chat`/`other` 低置信不澄清直接下沉，澄清率 28%→8%，主理人裁决「方案 B」）；④§6.3 补 4 条落地口径：G1 先采信 `safety.py` 四类边界、G1 升级当前只落 `audit_logs`（缺「疑似急症」类别口径，不编造）、shadow 失败不得打 fallback、shadow 计入耗时 |
