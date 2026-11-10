> 状态：草稿　｜　版本：v1.0　｜　日期：2026-11-10
> 关联：`docs/team_tasks/day07/dev-c-day07.md`（任务卡）｜`docs/team_tasks/day07/dev-c2-用药到点触发接线.md`（C2 明细）｜`docs/plans/day07/handoff-A-to-C-用药到点接线.md`（接线契约）｜`docs/team_tasks/day07/README.md`（Day 7 DAG 总表）
> 上游依据：PRD FR-MED-02/05/06、FR-DLG-07/09、FR-DLG-10｜技术设计文档 §6/§7/§8.3｜AGENTS.md §4/§5/§6/§7/§8
> 变更记录见文末

---

# dev-C · Day 7 · 执行计划

## 0. 整体认知

### 0.1 项目架构三句话
- **后端**：FastAPI + SQLAlchemy 2.x + Alembic + Redis + PostgreSQL；`app/services/` 是唯一写库层；依赖方向严格单向 `api/audio/scheduler/graphs → services → models`
- **前端**：React + Vite MPA 三入口（pad/console/family）；pad 用 three-vrm + WebRTC；console/family 复用 token 变量不引组件库
- **核心业务链**：ASR → G0 安全一票否决 → 三级路由（规则层 → System One Chinese-Jev → Agent）→ G2 工具权限校验 → 执行

### 0.2 Day 7 dev-C 全景（2.75 人日）

| 子任务 | 人日 | 核心目标 | 依赖 |
|-------|-----|---------|------|
| **C1 用药确认工具绑定** | 1.0 | 关闭 `medication_confirm` / `memory_recall` executor=None 悬空；Agent 工具清单从 1 个扩到 3 个 | B2 `recall_for_turn`（可用 stub） |
| **C2 用药到点调度接线** | 1.0 | 新建 scheduler 模块 + 到点查询只读服务；提供 `install_scheduler(app)` 由 A3 装配；不碰 main.py | 无硬依赖（新文件可先行） |
| **C3 Agent/安全边界收口** | 0.75 | generic identity 修正、四类失败回传话术、第 9 次调用拒绝、safety.py 四类边界、医疗拒答固定话术 | **C1 必须先完成** |

### 0.3 现状已完成度盘点

| 组件 | 现状 | 评价 |
|-----|------|------|
| `tool_registry.py` | ✅ 三条工具 spec 全注册，schema 完备 | C1 只需要写 executor 函数 + `bind()` |
| `tool_executor.py` | ✅ 完整实现：身份消毒、参数校验、幂等查重、四类失败、审计 | 执行器入口已就绪，C1 只需挂业务函数 |
| `companion.py` | ⚠️ 桩期：`DEFAULT_TOOLS` 只有 `WEATHER_TOOL` | C1 把另外两个 ToolSpec 加进去 |
| `safety.py` | ✅ 已有四类边界词表（否定/模糊/他人/缓解）+ 双命中判定 | C3 补医疗拒答话术即可 |
| `app/scheduler/__init__.py` | ❌ 3 行 docstring | C2 从零建 `medication_jobs.py` |
| TD-1 触达语音接线 | ✅ 已收口（commit 6c32846） | C2 直接复用子图触达 |
| TD-2 played 闭环 | ✅ 已收口（迁移 0013） | C2 不需要改 |
| N10 用药子图 | ✅ A 线 Day 6 收官 | **绝对不能改 `app/graphs/**`** |

### 0.4 关键接口契约（来自 handoff-A-to-C）

```python
# ⚠️ 子图工厂签名（最容易踩空！）
def build_medication_graph(
    *, session, checkpointer,
    deliver_fn=None,      # 信令层投递
    tts_fn=None,           # ★ 是 tts_fn，不是 speak_fn
)

# A 线公开入口
async def start_medication_task(graph, *, reminder_id, plan_id, elder_id, deadline, triggered_by="scheduler")
async def resume_medication_task(graph, *, reminder_id, answer)
def graph_config(reminder_id)  # → {"configurable": {"thread_id": f"medication:{reminder_id}"}}
MAX_ATTEMPTS = 4  # T0/+5/+10/+15

# 服务层（只读查询 + 写库都经 services）
medication_service.create_event()       # 幂等键 (plan_id, scheduled_at)
medication_service.get_attempt()        # 先查后发
medication_service.set_terminal()       # 终态互斥
work_order_service.open_missed_medication()  # 确定性 order_no
```

### 0.5 写作用域与禁触清单

**独占白名单**（本卡唯一写者）：

```
app/agents/companion.py
app/agents/tool_registry.py
app/services/tool_executor.py
app/services/medication_due_service.py       # C2 新增
app/scheduler/__init__.py                    # C2 改写
app/scheduler/medication_jobs.py             # C2 新增
app/router/safety.py                         # C3 仅边界规则/话术
app/prompts/personas/                        # C3 医疗拒答固定话术
tests/test_companion.py
tests/test_tool_executor.py
tests/test_scheduler_medication.py           # C2 新增
tests/test_safety.py
```

**禁触**（他人文件 / 已收官 / 高风险）：

```
app/graphs/**                                # N10 已收官
app/services/medication_service.py           # A 线已收官
app/services/work_order_service.py           # A 线已收官
app/services/response_service.py            # B1
app/services/turn_service.py / main.py      # A3；C2 只提供 install_scheduler
app/models/**                                # 无表结构变更
alembic/versions/**                          # 禁新增迁移（0013 已落地）
frontend/**                                  # D 线
```

---

## 1. 总体执行顺序与时间分配

```
Day 7 上午 09:00 ──────────────────────────────────────────────────────────────→ 晚上
│                                                                                │
│  ┌─阶段 1·准备 (30min)──┐  ┌─阶段 2·C2 (1.0d)────────┐  ┌─阶段 3·C1 (1.0d)─────┐  ┌─阶段 4·C3 (0.75d)──┐
│  │ 通读 handoff 确认签名 │  │ medication_due_service │  │ medication_confirm  │  │ identity 修正      │
│  │ 与 A3 对齐安装接口    │  │ medication_jobs 核心   │  │ memory_recall 执行器│  │ 四类失败话术验证   │
│  │                      │  │ 双驱动守卫             │  │ bind + DEFAULT_TOOLS│  │ safety.py 边界     │
│  └──────────────────────┘  │ 重启恢复 + install_fn │  │ identity 修正      │  │ 医疗拒答           │
│                            │ 8 条单测               │  │ 6 条单测            │  │ 13 条边界用例      │
│                            └────────────────────────┘  └───────────────────────┘  └────────────────────┘
└────────────────────────────────────────────────────────────────────────────────┘
```

**排期理由**：
1. C2 是全新文件、无硬依赖，写完立刻单测验证，不阻塞任何人，先啃"独立大块"
2. C1 依赖理解子图接口（C2 做完刚好加深了理解），memory_recall 可用 stub
3. C3 是 C1 的硬前置，C1 完成后立即开始

**关键路径**：`C2(1.0d) → C1(1.0d) → C3(0.75d) = 2.75d`
**浮时**：C2 Day 7 上午 9:00 可直接开写；C1 的 memory_recall 可先用 stub 写；C3 只有 C1 一个硬前置

---

## 2. 阶段 1 · 准备与契约对齐（Day 7 上午，30 分钟）

| # | 动作 | 产出 | 验收命令 |
|---|------|------|---------|
| 1.1 | 通读 `handoff-A-to-C-用药到点接线.md`，确认子图工厂签名确实是 `tts_fn` 不是 `speak_fn` | 确认笔记 | `grep -n "def build_medication_graph" app/graphs/medication_confirmation_graph.py` |
| 1.2 | 核实子图完整 API（start / resume / graph_config / checkpointer / MAX_ATTEMPTS） | 接口速查表 | 能列全 5 个公开函数签名 |
| 1.3 | 与 A3 口头对齐 `install_scheduler(app)` 的 lifespan 调用位置与关闭顺序 | 口头确认 | A3 不写 scheduler、C2 不写 main.py |
| 1.4 | 核实 `medication_service` / `work_order_service` 公开函数签名已稳定 | 调用清单 | grep 确认 6 个函数均已存在 |

---

## 3. 阶段 2 · C2 用药到点调度接线（1.0 人日）

### 3.1 任务拆解

| # | 子任务 | 耗时 | 优先级 | 依赖 |
|---|-------|-----|-------|------|
| **2.1** | 新建 `app/services/medication_due_service.py` | 1h | P0 | 无 |
| **2.2** | 新建 `app/scheduler/medication_jobs.py` 核心驱动链 | 3h | P0 | 2.1 |
| **2.3** | 双驱动先到先得守卫 | 1h | P0 | 2.2 |
| **2.4** | 重启恢复验证 | 0.5h | P1 | 2.2 |
| **2.5** | 提供 `install_scheduler(app)` | 0.5h | P0 | 2.2 |
| **2.6** | 8 条单测 | 1h | P0 | 2.1–2.5 全部完成 |

### 3.2 子任务详细设计

#### 2.1 `medication_due_service.py` · 到点只读查询

```python
# 职责：只读查询"当前时刻到点的 active 计划"
# 硬约束：不写库、不 import app.models
#
# 公开接口：
async def list_due_plans(
    session: AsyncSession,
    *,
    now: datetime | None = None,      # 测试可注入
    timezone: str = "Asia/Shanghai",
) -> list[DuePlan]:
    """返回当前时刻所有"应该触发"的 active 药单。
    
    过滤条件：
    - status = 'active'
    - schedule_cron 命中当前时刻（CronTrigger.from_crontab(expr, timezone) 匹配）
    - start_date ≤ 今天 ≤ end_date
    """

@dataclass(frozen=True)
class DuePlan:
    plan_id: uuid.UUID
    elder_id: uuid.UUID
    scheduled_at: datetime        # UTC，当前时刻
    drug_name: str
    dose: str | None
    cron_expr: str
```

**实现要点**：
- 用 SQLAlchemy 查表 + Python 侧 `croniter` 或 APScheduler 的 `CronTrigger` 做 cron 匹配（只匹配 expr 和分钟/小时/日/月/周，时区按 Asia/Shanghai）
- **零 `session.add/commit/execute`**，零 `from app.models`（靠 SQL 字符串或 ORM select + flush-only）
- 返回值包含驱动链所需全部字段，scheduler 层不再需要二次查计划

#### 2.2 `medication_jobs.py` · 核心驱动链

```python
# 公开接口：
def install_scheduler(app: FastAPI, *, session_factory: AsyncSessionFactory) -> None:
    """注入 FastAPI lifespan，在 startup 时 start、shutdown 时 shutdown。
    
    不直接改 main.py；A3 调用本函数即可。
    """

async def _tick_due_plans(session_factory: AsyncSessionFactory) -> None:
    """核心任务：每分钟触发一次（CronTrigger('* * * * *', timezone='Asia/Shanghai')）。
    
    对每条 due plan：
    1. medication_due_service.list_due_plans() 拿待处理列表
    2. 对每条 plan：
       a. create_event(plan_id, elder_id, scheduled_at, drug_snapshot, triggered_by="scheduler")
          → 幂等键 (plan_id, scheduled_at)，同键已存在返回原 reminder_id
       b. build_medication_graph(session, checkpointer) → graph
       c. start_medication_task(graph, reminder_id, ...) → 停在 wait_confirm (interrupt)
       d. 等 60 秒窗口（A 线子图内部控制，scheduler 不自己实现循环）
    """
```

**驱动链顺序**（照抄 handoff-A-to-C §2.2）：

```
APScheduler 到点（cron 命中，Asia/Shanghai）
  └─ ① list_due_plans() 拿待处理
        └─ ② create_event(plan_id, elder_id, scheduled_at, drug_snapshot, triggered_by="scheduler")
              │    ↳ 同键已存在 → 返回原 reminder_id（不新建行）
              └─ ③ build_medication_graph(session, checkpointer)
                    └─ ④ start_medication_task(...) → 停在 wait_confirm（interrupt）
                          │   ↳ remind 节点：先查 attempt → 无记录才投递
                          │     投递消息由子图组装（含 touch_id/touch_attempt_no/audio），
                          │     scheduler **不自己拼**
                          └─ ⑤ 等 60 秒窗口 → 子图内部处理 resume / 重试 / 终态
```

**四个触达时点**（照抄 handoff-A-to-C §2.3）：

| 轮次 | 时刻 | attempt_no | 无回答时 |
|-----|------|-----------|---------|
| 1 | T0 | 1 | → 轮次 2 |
| 2 | T0 + 5min | 2 | → 轮次 3 |
| 3 | T0 + 10min | 3 | → 轮次 4 |
| 4 | T0 + 15min | 4 | → missed_unconfirmed + 升级工单 |

**硬约束**：
- scheduler 层**零 `session.add/commit/execute`**；零 `from app.models`
- scheduler **不自己拼触达消息**（那是子图 remind 节点的职责）
- 时区统一 `Asia/Shanghai`；`medication_events.scheduled_at` 存 UTC
- `MAX_ATTEMPTS = 4` 是 A 线常量，scheduler **不要**叠自己的上限

#### 2.3 双驱动先到先得守卫

```python
# resume 之前必须做的事（照抄 handoff-A-to-C §3.1）
async def _safe_resume(graph, reminder_id, answer) -> dict | None:
    state = await graph.aget_state(graph_config(reminder_id))
    # 已有终态 → 跳过，不 resume
    if "terminal_state" in state.values:
        return None
    # attempt_no 与预期不符（对话侧已推进）→ 跳过
    if state.values.get("attempt_no", 0) >= expected_attempt:
        return None
    # 安全：投递侧还有子图的"先查后发"（get_attempt 已有记录则跳过投递）兜底
    return await resume_medication_task(graph, reminder_id=reminder_id, answer=answer)
```

#### 2.5 `install_scheduler(app)` 提供方式

```python
# 方式：用 app.state 或 lifespan contextvar 传递 session_factory
def install_scheduler(app: FastAPI, *, session_factory) -> None:
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    
    # 每分钟触发一次 due plans 扫描
    scheduler.add_job(
        _tick_due_plans,
        CronTrigger.from_crontab("* * * * *", timezone="Asia/Shanghai"),
        args=[session_factory],
        id="medication_due_tick",
        replace_existing=True,
    )
    
    # FastAPI lifespan 注册
    @app.on_event("startup")
    async def _start():
        app.state.scheduler = scheduler
        scheduler.start()
    
    @app.on_event("shutdown")
    async def _stop():
        scheduler.shutdown(wait=False)
```

> **注意**：`main.py` 是 A3 独占的高风险文件。C2 不直接写 `app/main.py`，只提供 `install_scheduler(app, session_factory)` 函数。A3 在其 lifespan 内调用一次即可。如果双方对"谁来加 startup/shutdown"有分歧，J1 对齐时必须拍板。

### 3.3 C2 验收

#### 静态门禁
```bash
grep -rn "session.add\|session.commit\|session.execute" app/scheduler/             # 0
grep -rn "from app.models\|import app.models" app/scheduler/                       # 0
grep -rn "apscheduler" pyproject.toml                                              # 已存在即可
```

#### 单测 8 条（`tests/test_scheduler_medication.py`）

| # | 用例 | 断言 |
|---|------|------|
| A2-1 | 到点幂等 | 同一 (plan_id, scheduled_at) 触发两次 → events 仍 1 行、reminder_id 相同 |
| A2-2 | 四次触达 | attempts 恰好 4 行、attempt_no=1/2/3/4、receipt 合理 |
| A2-3 | 四次无回答终态 | terminal_state='missed_unconfirmed'、status='escalated'、retry_count=3 |
| A2-4 | 建单恰好一张 | work_orders 1 行、order_no 确定性 |
| A2-5 | 重启恢复 | 新 checkpointer 实例能读等待节点、attempts 行数不增 |
| A2-6 | 双驱动先到先得 | 对话侧先 resume 到 taken_reported → 调度侧 resume 不抛 TerminalStateConflict |
| A2-7 | 窗口内回答 | 60s 窗口内回答 → 后续三个触达时点不再触发 |
| A2-8 | 时区 | schedule_cron 按 Asia/Shanghai 解释、events.scheduled_at 存 UTC |

#### 手工联调（一次跑通）
```sql
-- 造一条 schedule_cron='* * * * *' 的 active 药单
SELECT terminal_state, status, failure_reason, retry_count
FROM medication_events ORDER BY created_at DESC LIMIT 1;
-- 期望：missed_unconfirmed | escalated | no_answer | 3

SELECT attempt_no, receipt, tts_result
FROM medication_attempts ORDER BY attempt_no;
-- 期望：4 行、attempt_no 1-4、receipt 合理

SELECT order_no, status FROM work_orders ORDER BY created_at DESC LIMIT 1;
-- 期望：1 行

-- 重启服务后再查 attempts：行数不增
```

---

## 4. 阶段 3 · C1 用药确认工具绑定（1.0 人日）

### 4.1 任务拆解

| # | 子任务 | 耗时 | 优先级 | 依赖 |
|---|-------|-----|-------|------|
| **3.1** | `medication_confirm` 执行器函数 | 2h | P0 | C2 完成（需理解子图接口） |
| **3.2** | `memory_recall` 执行器函数 | 1h | P0 | B2 `recall_for_turn`（可 stub） |
| **3.3** | 装配层 `bind()` 两个执行器 | 0.5h | P0 | 3.1 + 3.2 |
| **3.4** | `companion.py` DEFAULT_TOOLS 扩展 | 0.5h | P0 | 3.3 |
| **3.5** | 身份处理修正（generic 不崩 + staff 不误判） | 1h | P1 | 3.4 |
| **3.6** | 扩展 test_companion + test_tool_executor | 3h | P0 | 3.1–3.5 |

### 4.2 子任务详细设计

#### 3.1 `medication_confirm` 执行器

```python
# 执行器函数签名（照 tool_registry 定义）
async def medication_confirm_executor(args: dict, runtime: Identity) -> dict:
    """工具执行器：记录长者对服药提醒的回答。
    
    核心逻辑：
    1. elder_id 只从 runtime.elder_id 取（args 里的 elder_id 已在 ToolExecutor._strip_identity 被丢弃）
    2. 查该长者是否有 waiting_for=taken_answer 的 medication_event
       → 无 → 抛 ToolNotFoundError（error_class='not_found'）
       → 有 → 调 resume_medication_task(reminder_id, answer)
    3. 返回 ToolResult.succeeded + 可播报文本
    """
    # 1. 身份收敛
    elder_id = runtime.elder_id
    if elder_id is None:
        raise ToolForbiddenError("无法识别长者身份")
    
    # 2. 查待确认事件（只读查询，不写库）
    #    用 medication_service 提供的只读查询口（若 A 线已提供）
    #    或自己写一个 select（只读，不碰 session.add）
    pending = await _find_pending_taken_answer(session, elder_id)
    if pending is None:
        raise ToolNotFoundError("现在没有待确认的用药提醒")
    
    # 3. resume 子图推进
    from app.graphs.medication_confirmation_graph import graph_config, resume_medication_task
    result = await resume_medication_task(
        pending.graph,
        reminder_id=pending.reminder_id,
        answer=args["answer"],
    )
    
    # 4. 组装可播报文本
    terminal_state = result.get("terminal_state", "")
    message = _terminal_to_speech(terminal_state)
    
    return {"terminal_state": terminal_state, "message": message}
```

**失败四分类**（AGENTS §6）：
- `bad_args`：answer 缺失或类型错 → 参数校验层已处理，回灌模型重试
- `not_found`：无待确认事件 → 如实告知"现在没有待确认的用药提醒"
- `transient`：子图调用超时 / DB 临时故障 → 退避 ≤2 后降级话术
- `forbidden`：runtime.elder_id 为空 / 跨长者访问 → 拒绝 + 审计

#### 3.2 `memory_recall` 执行器

```python
async def memory_recall_executor(args: dict, runtime: Identity) -> dict:
    """工具执行器：召回长者的历史偏好/事实记忆。
    
    - 调 B2 的 recall_for_turn(elder_id, query, limit=5)
    - 无记忆返回空列表（ToolResult.succeeded + data.notes=[]），不报错
    - 身份收敛同 medication_confirm
    """
    elder_id = runtime.elder_id
    if elder_id is None:
        raise ToolForbiddenError("无法识别长者身份")
    
    # 软依赖 B2：先用 stub 硬返回空列表，B2 完成后替换一行
    try:
        from app.services.memory_service import recall_for_turn
        notes = await recall_for_turn(elder_id=elder_id, query=args.get("query", ""))
    except ImportError:
        notes = []  # stub 阶段兜底
    except Exception:
        raise ToolTransientError("记忆召回暂时不可用")
    
    return {"notes": [{"content": n.content, "category": n.category} for n in notes]}
```

#### 3.3 `bind()` 两个执行器

**绑定位置**：在装配层（`app/main.py` 或单独的装配模块）构造 `ToolRegistry` / `ToolExecutor` 时。C1 不写 main.py（A3 独占），只提供绑定函数。

```python
# C1 提供的装配辅助函数（不写 main.py）
def bind_dev_c_tools(registry: ToolRegistry, executor_factory) -> None:
    """把 C1 实现的两个工具执行器绑定到 registry。
    
    A3 在装配层调用本函数，C1 不碰 main.py。
    """
    registry.bind("medication_confirm", medication_confirm_executor)
    registry.bind("memory_recall", memory_recall_executor)
```

#### 3.4 `companion.py` DEFAULT_TOOLS 扩展

```python
# 在 companion.py 新增两个 ToolSpec 常量（与 WEATHER_TOOL 并列）
MEDICATION_CONFIRM_TOOL: Final[ToolSpec] = ToolSpec(
    name="medication_confirm",
    description="记录长者对服药提醒的回答，帮长者确认是否已经服药。",
)

MEMORY_RECALL_TOOL: Final[ToolSpec] = ToolSpec(
    name="memory_recall",
    description="查询这位长者此前提到过的偏好和事实，用于个性化回应。",
)

# 替换原来只有一个元素的 DEFAULT_TOOLS
DEFAULT_TOOLS: Final[tuple[ToolSpec, ...]] = (
    WEATHER_TOOL,
    MEDICATION_CONFIRM_TOOL,
    MEMORY_RECALL_TOOL,
)
```

**硬约束**：工具描述与 schema 必须与 `tool_registry.default_registry()` 同源，**不复制第二套**。如果 `companion.py` 和 `tool_registry.py` 各写一份 ToolSpec 描述，半年后一定出现两边不一致。

#### 3.5 身份处理修正

**三个要修的点**：

| 问题 | 根因 | 修法 |
|-----|------|------|
| `DialogueIdentity.elder_id=None` 触发 `__post_init__` 崩溃 | dataclass 校验过严 | 把 `elder_id` 改为可选；generic 时为 None 是合法值 |
| staff/guardian 身份被静默判成 generic | `_build_turn_context` 里 `if runtime.elder_id else "generic"` 把所有无 elder_id 的凭证都打成 generic | 改为：只有 `source="device"` 且 elder_id=None 才是 generic；其他凭证类型走各自人设路径 |
| generic 轮静默走陌生人设 | `persona=None` + `history=[]` 不报错但语义模糊 | generic 轮**明确标记** `DialogueIdentity(source="generic", elder_id=None)`；不加载个人人设/记忆，但保留通用陪伴话术 |

```python
# generic 不静默走陌生人设的修正
def _build_turn_context(..., identity: DialogueIdentity | None = None):
    if identity is None:
        inferred_source = "device" if runtime.elder_id else "generic"
        identity = DialogueIdentity(source=inferred_source, elder_id=runtime.elder_id)
        logger.warning("companion_identity_inferred ...")
    
    # generic 轮：不传 persona、不加载历史，但保留通用陪伴话术
    # 非 generic：正常拼人设 + 历史 + 记忆
    ...
```

### 4.3 C1 必过用例（6 条）

| # | 用例 | 断言 |
|---|------|------|
| 1 | `medication_confirm_without_pending_returns_not_found` | 无待确认 → ToolResult.failed + error_class='not_found' + 可读话术 |
| 2 | `medication_confirm_with_pending_resumes_graph` | 有待确认 → ToolResult.succeeded + terminal_state='taken_reported' |
| 3 | `medication_confirm_ignores_injected_elder_id_and_audits` | 参数注入 elder_id → 执行作用于 runtime 长者 + audit_logs 有 1 行 |
| 4 | `memory_recall_returns_empty_without_crash` | 无记忆 → ToolResult.succeeded + data.notes=[] |
| 5 | `companion_exposes_three_tools` | Agent 构造时工具清单长度=3、名称与 registry 完全一致 |
| 6 | `generic_identity_does_not_load_personal_memory` | generic identity → 不传 persona、不加载历史、保留通用话术 |

---

## 5. 阶段 4 · C3 Agent/安全边界收口（0.75 人日）

> **硬前置：C1 全部完成**

### 5.1 任务拆解

| # | 子任务 | 耗时 | 优先级 | 依赖 |
|---|-------|-----|-------|------|
| **4.1** | generic identity 彻底修正（C1 已做基础，C3 补完） | 1h | P0 | C1.5 |
| **4.2** | 四类失败回传话术验证与补全 | 1h | P0 | C1 全部 |
| **4.3** | 第 9 次调用 + 同 Tool 熔断验证 | 0.5h | P0 | 4.2 |
| **4.4** | safety.py 四类边界验证与补全 | 1.5h | P0 | safety.py 已实现 |
| **4.5** | 医疗拒答固定话术进 constants | 0.5h | P0 | 4.4 |
| **4.6** | 扩展用例（13 条） | 2h | P0 | 全部完成 |

### 5.2 子任务详细设计

#### 4.1 generic identity 彻底修正

C1 已修 `_build_turn_context` 的推断逻辑，C3 补：
- `DialogueIdentity.elder_id=None` 在 dataclass 层面合法（不触发 `__post_init__`）
- staff/guardian 有 elder_id 的场景不被判 generic（验证已有逻辑覆盖）
- 全仓 `grep -rn "DialogueIdentity(" app/` 确认 elder_id=None 的场景只有 generic

#### 4.2 四类失败回传话术验证

`companion.py` 已有完整的四类处理逻辑（见文件 L496-503），C3 只需：
- 逐条验证执行器返回四类失败时的 Agent 行为
- 补 `_observation()` 对四类的指引文案（bad_args 让模型重试、not_found 指引如实告知、transient 指引降级、forbidden 直接拒绝）
- 确保 transient 的退避重试与 Guardrails 预算不叠加（重试不计预算，重试次数 ≤2 由 `_TRANSIENT_MAX_RETRIES` 控制）

#### 4.3 第 9 次调用 + 同 Tool 熔断

**复用 Guardrails，不建第二套计数器**：
```python
# 验证点：
# 1. Guardrails 已有 MAX_TOOL_CALLS_PER_TURN = 8（AGENTS §7）
#    → 第 9 次 check() 返回 decision.allow=False
#    → companion.py L411-425 已处理：返回 BUDGET_EXHAUSTED_TEXT
# 2. Guardrails 已有同 Tool 1 分钟第 3 次熔断（AGENTS §7）
#    → check() 返回 decision.allow=False + reason="circuit_break"
#    → companion.py 同路径处理
```

**验收命令**：
```bash
grep -rn "fuse\|circuit\|MAX_TOOL_CALLS" app/agents guardrails.py companion.py
# 期望：只有 guardrails.py 一处定义常量
grep -rn "fuse\|circuit" app/services/tool_executor.py
# 期望：零（熔断由 Guardrails 承载）
```

#### 4.4 safety.py 四类边界验证

**safety.py 现状已实现**，C3 补验证 + 医疗拒答：

| 边界 | safety.py 现状 | C3 动作 |
|-----|---------------|-------|
| 否定（"没有胸痛"） | ✅ `NEGATION_MARKERS` + 左窗口扫描 | 加 5 条测试用例，验证 0 红色 |
| 历史（"昨天疼现在好了"） | ✅ `RECOVERY_MARKERS` 全文扫描 | 加 5 条测试用例，验证判黄/不红 |
| 他人主体（"我爸爸胸痛"） | ✅ `OTHER_PERSON_MARKERS` + 左窗口扫描 | 加 5 条测试用例，验证不为本人建红 |
| 模糊（"好像胸口疼"） | ✅ `HEDGE_MARKERS` + 左窗口扫描 | 加 5 条测试用例，验证判黄/不红 |
| 双命中不靠单字 | ✅ `CHEST_PARTS` + `CHEST_SYMPTOMS` 必须邻近配对 | 验证 |
| 误报 ≤2/50 | — | 准备 50 条普通语料跑 safety_check，统计误报数 |

**双命中逻辑**（照抄 safety.py L208-230）：
```python
# 1. 先扫缓解护栏（全文）：带"已缓解"语义的不判红
for marker in RECOVERY_MARKERS:
    if marker in cleaned:
        return _not_sensitive("recovery", matched_rule=marker)

# 2. 胸部「部位 × 症状」双命中（邻近性 + 护栏）
for part in CHEST_PARTS:
    for part_start in _occurrences(cleaned, part):
        # 先查他人 / 模糊护栏
        if _window_has_marker(cleaned, part_start, OTHER_PERSON_MARKERS, 6):
            return _not_sensitive("other_person")
        if _window_has_marker(cleaned, part_start, HEDGE_MARKERS, 4):
            return _not_sensitive("hedge")
        # 再配对症状（邻近性窗口）
        for symptom in CHEST_SYMPTOMS:
            for sym_start in _occurrences(cleaned, symptom):
                if not (part_start <= sym_start <= part_end + 4): continue
                if _window_has_marker(cleaned, sym_start, NEGATION_MARKERS, 4):
                    blocked_reason = "negation"; continue
                return _sensitive(CATEGORY_CHEST_PAIN, ...)
```

#### 4.5 医疗拒答固定话术

**PRD FR-DLG-09**：医疗诊断/药理问题 → 固定话术 + 转告任务。

```python
# app/prompts/personas/medical_refusal.py（或同级 constants 文件）
from __future__ import annotations

MEDICAL_REFUSAL_TEXT = (
    "这个我不能判断，我帮您转告医生和护工，一会儿就来帮您看看。"
)
MEDICAL_REFUSAL_KEYWORDS: tuple[str, ...] = (
    # 诊断类
    "是不是高血压", "是不是糖尿病", "是不是冠心病", "是不是中风",
    "得了什么病", "什么病", "确诊", "诊断",
    # 药理类
    "这药治什么", "这药是干什么的", "这药有什么用",
    "这药有副作用吗", "这药安全吗",
    # 通用医疗建议
    "我该吃什么药", "我该怎么办", "需要动手术吗",
)
```

**处理方式**（在 safety.py 或 companion.py 的早期拦截层）：
```python
# 医疗拒答拦截：在 safety_check 之后、Agent 之前
def medical_refusal_check(text: str) -> str | None:
    """命中医疗诊断/药理问题 → 返回固定拒答话术；否则 None"""
    for kw in MEDICAL_REFUSAL_KEYWORDS:
        if kw in text:
            return MEDICAL_REFUSAL_TEXT
    return None
```

**验收**：
- 问"头疼是不是高血压" → 回复固定拒答 + 转告任务（followup_required=True）
- 回复中**不出现**任何诊断词（高血压/糖尿病/冠心病/确诊/诊断）
- `work_orders` 有转告记录

### 5.3 C3 必过用例（13 条）

| # | 类别 | 用例 | 断言 |
|---|------|------|------|
| 1 | generic 不崩 | `generic_identity_full_turn_does_not_crash` | 完整跑完一轮、无 crash |
| 2 | staff 不误判 | `staff_with_elder_id_not_marked_generic` | staff 身份有 elder_id 时不走 generic 路径 |
| 3 | 四类·否定 | `negation_chest_pain_not_red` | "没有胸痛" → is_sensitive=False、reason='negation' |
| 4 | 四类·历史 | `historical_chest_pain_with_recovery_not_red` | "昨天疼现在好了" → reason='recovery' |
| 5 | 四类·他人 | `other_person_chest_pain_not_self_red` | "我爸胸痛" → reason='other_person'、不为本人建红 |
| 6 | 四类·模糊 | `hedged_chest_pain_not_red` | "好像胸口疼" → reason='hedge' |
| 7 | 医疗拒答 | `diagnosis_question_returns_fixed_refusal` | "头疼是不是高血压" → 固定话术 + followup_required |
| 8 | 第 9 次调用 | `ninth_tool_call_is_rejected` | Guardrails 返回 denied + 用户收到可读话术 |
| 9 | 身份注入被审计 | `injected_elder_id_is_dropped_and_audited` | 执行作用于 runtime 长者 + audit_logs 有行 |
| 10 | 工具超时 transient | `tool_timeout_returns_transient` | transient + 可读话术 + 不抛异常穿透 |
| 11 | 同幂等键无副作用 | `same_idempotency_no_duplicate_side_effect` | 第二次调用 ToolResult.idempotent_replay=True |
| 12 | 同 Tool 熔断 | `same_tool_third_call_in_1min_circuit_breaks` | 第 3 次 check() 返回 denied |
| 13 | 跨长者 forbidden | `cross_elder_resource_forbidden` | 跨长者访问 ToolResult.forbidden + 审计 |

### 5.4 C3 关键验收（命令级）

```bash
# G2 唯一工具入口验证
grep -rn "ToolExecutor.execute" app | wc -l         # Agent + Router 共用
grep -rn "permission\|forbidden" app/router app/agents | grep -v "ToolExecutor\|guardrails\|audit"
# 期望：零第二套权限逻辑

# 普通语料 50 例误报
# 人工准备 50 句普通对话，跑 safety_check，误报 ≤ 2/50

# 急症正例召回
# 所有急症正例胸痛/呼吸困难/跌倒/轻生 → 全部判 red

# 医疗拒答无诊断词
grep -rn "高血压\|糖尿病\|冠心病\|确诊\|诊断" 回复中
# 期望：零
```

---

## 6. 开放点与风险预案

### 6.1 开放点（开工前需主理人回话）

| # | 问题 | 建议裁决 | 不回话的后果 |
|---|------|---------|------------|
| **O1** | 无回执终态是 `missed_unconfirmed` 还是 `notification_failed` | C2 统一 `missed_unconfirmed`，登记技术债 | 验收按统一值走；AC-20 口径偏差留账 |
| **O2** | 到点查询只读服务放哪里 | C 侧新文件 `medication_due_service.py` | 不碰 A 线 `medication_service.py` |

### 6.2 风险预案

| # | 风险 | 概率 | 影响 | 预案 |
|---|------|-----|------|------|
| **R1** | 子图工厂签名踩空（旧代码用 speak_fn，现在是 tts_fn） | 高 | C2 驱动链 TypeError | **接线前先读合并后代码**；TypeError 好定位，不会静默失败 |
| **R2** | memory_recall 的 B2 接口还没完成 | 中 | C1 阻塞 | 先写 stub 硬返回空列表，B2 完成后替换一行 |
| **R3** | 双驱动同时 resume 导致 attempt_no 错位 | 中 | 重复投递 | resume 前必查 `aget_state`；投递侧另有子图"先查后发"兜底 |
| **R4** | `install_scheduler` 签名与 A3 lifespan 预期不对齐 | 低 | A3/C2 都要改 | J1 口头对齐；提供两个签名让 A3 选 |
| **R5** | generic identity 修复牵连 DialogueIdentity 其他用法 | 中 | C3 崩溃 | 改前 `grep` 全仓实例化点，确认 elder_id=None 场景只有 generic |
| **R6** | 工具超时 transient 退避 2 次后降级话术被误判为 not_found | 低 | Agent 行为混乱 | 严格区分四分类：超时=transient、资源不存在=not_found、越权=forbidden、参数错=bad_args |
| **R7** | 同 Tool 熔断在 Guardrails 里已有，C3 重复实现 | 中 | 熔断不生效或计两次 | C3 验收时 grep 确认只有 Guardrails 一处定义常量 |

### 6.3 回退策略

如果某任务中途卡死：
- **C2 卡死**：撤掉 `app/services/medication_due_service.py` + `app/scheduler/` 新增文件，零副作用
- **C1 卡死**：执行器函数不注册 bind()，调用回到"not_found 可读话术"的降级路径（不崩）
- **C3 卡死**：回退 companion.py DEFAULT_TOOLS 到只有 WEATHER_TOOL，memory_recall 和 medication_confirm 继续 not_found

---

## 7. 验收 Checklist（门禁顺序）

```bash
# ========= 门禁 1 · C2 静态分层 =========
grep -rn "session.add\|session.commit\|session.execute" app/scheduler/             # 0
grep -rn "from app.models\|import app.models" app/scheduler/                         # 0

# ========= 门禁 2 · C2 单测 =========
uv run pytest -q tests/test_scheduler_medication.py                                  # ≥8 条全绿

# ========= 门禁 3 · C1 单测 =========
uv run pytest -q tests/test_tool_executor.py::medication_confirm_without_pending_returns_not_found
uv run pytest -q tests/test_companion.py::companion_exposes_three_tools              # 6 条全绿

# ========= 门禁 4 · C3 单测 =========
uv run pytest -q tests/test_companion.py tests/test_tool_executor.py tests/test_safety.py
# 13 条边界 + 权限 + 熔断用例全绿

# ========= 门禁 5 · 全量回归 =========
uv run pytest -m "not slow" -q                                                         # 全绿，零新增失败

# ========= 门禁 6 · 代码质量 =========
uv run ruff check app/agents app/services/tool_executor.py app/scheduler app/router/safety.py
uv run mypy app/agents app/services/tool_executor.py app/scheduler app/router/safety.py

# ========= 门禁 7 · 进度看板 =========
uv run python scripts/update_progress.py --check                                       # 0

# ========= 门禁 8 · 手动联调 SQL =========
SELECT terminal_state, status, retry_count FROM medication_events ORDER BY created_at DESC LIMIT 1;
SELECT attempt_no, receipt, tts_result FROM medication_attempts ORDER BY attempt_no;
SELECT order_no, status FROM work_orders ORDER BY created_at DESC LIMIT 1;
```

---

## 8. 交付物清单

| # | 文件 | 类型 | 归属 |
|---|------|------|------|
| 1 | `app/services/medication_due_service.py` | 新增 | C2 |
| 2 | `app/scheduler/__init__.py` | 改写（补 install_scheduler 导出） | C2 |
| 3 | `app/scheduler/medication_jobs.py` | 新增 | C2 |
| 4 | `app/agents/companion.py` | 改写（工具清单 + identity 修正） | C1 |
| 5 | `app/services/tool_executor.py` | 改写（bind medication_confirm + memory_recall 执行器） | C1 |
| 6 | `app/router/safety.py` | 改写（医疗拒答话术 + 四类边界完善） | C3 |
| 7 | `app/prompts/personas/medical_refusal.py`（或同级 constants） | 新增 | C3 |
| 8 | `tests/test_scheduler_medication.py` | 新增（≥8 条） | C2 |
| 9 | `tests/test_companion.py` | 扩展 | C1/C3 |
| 10 | `tests/test_tool_executor.py` | 扩展 | C1/C3 |
| 11 | `tests/test_safety.py` | 扩展或新建 | C3 |

---

## 9. 与其他 dev 线的交接

### 9.1 dev-A（A1/A2/A3）

| 交接项 | 内容 | 时间点 |
|-------|------|-------|
| → A3 | `install_scheduler(app, session_factory)` 签名 + lifespan 启停顺序 | J5（第 2 天结束） |
| → A3 | `medication_confirm` 工具名 / 参数 schema / ToolResult 话术，供路由直连 | J4（第 2 天下午） |
| → A3 | 工具清单 3 个：`get_weather` / `memory_recall` / `medication_confirm` | C1 完成后 |

### 9.2 dev-B（B1/B2/B3）

| 交接项 | 内容 | 时间点 |
|-------|------|-------|
| ← B2 | `recall_for_turn(elder_id, query, limit) -> list[MemoryNote]` 签名 | C1 写 memory_recall executor 时对齐 |

### 9.3 dev-D（D1/D2/D3）

| 交接项 | 内容 | 时间点 |
|-------|------|-------|
| → D3 | 真机说"我吃过药了"→ medication_confirm 可验（有待确认事件时推进子图） | D3 联调时 |
| → D3 | 无待确认时话术："现在没有待确认的用药提醒"（可读、不崩） | C1 完成后 |
| → D3 | 到点触达真机验证（C2 完成后 scheduler 会真正驱动子图） | D3 联调时 |

---

## 10. 完成记录模板

```text
任务：C1 / C2 / C3
状态：已完成 / 阻塞
证据：
- 代码：<commit>
- 命令：<pytest / ruff / grep 输出>
- 数据：<medication_events/attempts/work_orders 行数；safety 用例结果>
- 未完成：<无 / 具体项 + 原因 + 下一步>
```

---

## 11. 变更记录

| 版本 | 日期 | 说明 |
|------|------|------|
| v1.0 | 2026-11-10 | 首版：完整任务拆解 + 时间分配 + 依赖图 + 风险预案 + 验收 checklist + 交接清单 |
