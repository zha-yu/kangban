> 状态：草稿　｜　版本：v1.4　｜　日期：2026-11-11
> 关联：docs/team_tasks/day08/dev-A-后端数据与分级.md v1.2（药单管理 API 为 dev-A Day 8 **追加项**，主任务书 A1–A5 已合入，本计划为 H+1 补卡）
> 上游依据：PRD FR-MED-01 / §4.5（医生增改停查、护工/管理员只读）｜技术设计文档 §3.3 medication_plans 表｜AC-27（version+1、历史快照不受影响）
> 变更记录见文末

---

# dev-A（Day 8·追加）药单管理后端接口实施计划

## 0. 现状基线（代码实测，2026-11-11）

| 项 | 现状 | 对本计划的含义 |
| --- | --- | --- |
| ORM | `app/models/medication.py` 已建 `MedicationPlan`（迁移 0010，含 `version` 列 default=1，但**零引用**） | version 列是预建但未落地的能力——本计划让它真正工作；**不改迁移**（0010 已合入） |
| Plan Service | **完全不存在**——`medication_service.py` 只管 events/attempts，`medication_due_service.py` 只扫描 active plans（只读） | 需新建 `medication_plan_service.py`，作为 MedicationPlan 的**唯一写库出口** |
| REST API | `app/api/` 无 medication 路由；`app/api/__init__.py` ROUTERS 注册表无此条目 | 需新建 `app/api/medication.py`，追加一行到 ROUTERS |
| Schema | `app/api/schemas/` 仅 auth/org/staff 三个文件，无 medication | 在 `app/api/medication.py` 内部定义 Pydantic 请求/响应模型（对齐 workorders.py 风格：就地定义 + 序列化函数） |
| 鉴权 | `STAFF_ROLES = ("admin", "care_worker", "doctor")`；`require_roles` 依赖自动 403（依赖注入阶段，service 层不被调用） | 权限门禁**下沉到 service 层**：API 层放行所有 staff 到 service，service 内写分支读/写+越权审计 |
| 审计 | `record_audit(session, actor_type, actor_id, action, resource_type, resource_id, detail)` 与业务同事务、只 flush 不 commit | 三处写操作（create/update/stop）都必须记录审计；越权也要记审计 |
| 前端契约 | `frontend/src/console/medication-api.ts` Mock store，四个导出函数签名已冻结：`listPlansByElder / createPlan / updatePlan / stopPlan` | 后端 REST 必须让前端 `medication-api.ts` 内部替换为 fetch 后**页面层零改动**（导出签名不变） |
| events 唯一键 | `UNIQUE(plan_id, scheduled_at)` + `payload_hash` 判定同键异内容 → `MedicationConflict`（409 语义） | **G1 风险**：医生改 drug_name/dose 但 cron 不变 → 下次 scheduler 命中同键异 hash → conflict 被跳过（warning 后不重试） |
| events.drug_snapshot | JSONB 列，由 scheduler 在 `_trigger_plan` 里用**当前 plan 字段**构造 `{"drug_name":..., "dose":...}` 写入 | 已满足 AC-27「历史快照不受影响」——老事件的 snapshot 冻结在事件提交时；plan 改版后新事件带新快照 |

**G1 详细说明**（唯一键与 version 未联动）：
- events 唯一键不含 version，且 medication_plan_service 更新 plan.version+1 后**不会自动让 scheduler 知道版本变了**
- scheduler 扫描 active plans 时用的是**最新 plan 字段**（已改后的 drug_name/dose）构造 snapshot
- 如果 cron 没变（同一分钟），同 `(plan_id, scheduled_at)` 但 `payload_hash` 不同 → `MedicationConflict`
- scheduler 当前处理：warning 后 `return`（跳过），不降级不重试

**✅ 主理人裁决 G1（2026-11-11）：数据库唯一约束 + Service 层校验 + Scheduler 幂等性，三者组合**：

| 层面 | 做什么 | 改什么 |
| --- | --- | --- |
| **数据库（0019 迁移）** | 给 `medication_events` 加 `plan_version INT NOT NULL DEFAULT 1`；删旧唯一键 `uq_medication_events_plan_id_scheduled_at`；建新唯一键 `UNIQUE(plan_id, scheduled_at, plan_version)` | 新建 `alembic/versions/0019_day8_medication_plan_version.py` |
| **ORM** | `MedicationEvent` 模型加 `plan_version` 列（Integer, NOT NULL, default=1） | `app/models/medication.py` 增量（§4 写作用域更新） |
| **Service 层校验** | `update_plan` 时比对 old/new schedule_cron；**不变则**审计 detail 里打 `{"cron_unchanged": true}` 标记 + API 响应里追加 `cron_unchanged=true` 提示前端"无需调整调度" | `medication_plan_service.update_plan` 增量 |
| **Scheduler 幂等** | `_trigger_plan` 构造 `drug_snapshot` 时同时取 `plan.version` → 传给 `create_event(plan_version=plan.version)`；新唯一键 `(plan_id, scheduled_at, plan_version)` 让 plan 改版后**天然不冲突**（不同 version 建新行） | `medication_jobs._trigger_plan` 增量 |
| **medication_service 幂等** | `create_event` 签名加 `plan_version: int` 参数，写入 events.plan_version；唯一键查重改为 `(plan_id, scheduled_at, plan_version)` | `medication_service.create_event` 签名变更（影响 scheduler + graphs 两处调用点） |

**三者分工不重叠**：数据库约束是最终防线，Service 层给用户提前提示，Scheduler + medication_service 保证幂等。cron 不变时的前端提示是**额外业务规则**，与唯一约束独立。

---

## 1. 任务拆解 · 时间分配

**总估算 4.0h**（dev-A Day 8 主任务已收，G1 裁决落地追加 1.5h，合计 4.0h）。

| # | 子任务 | 优先级 | 预估 | 前置 | 核心交付物 |
| --- | --- | --- | --- | --- | --- |
| M0 | **G1 裁决落地**：0019 迁移 + ORM plan_version + service 签名变更 | P0 | 1.5h | 无（与 M1 并行开工） | `alembic/versions/0019_day8_medication_plan_version.py` + `medication.py` ORM 增量 + `medication_service.create_event` 签名加 `plan_version` + `medication_jobs._trigger_plan` 传 version |
| M1 | MedicationPlan Schema（Pydantic 模型） | P0 | 0.3h | 无 | 就地定义在 `app/api/medication.py` 顶部 |
| M2 | `medication_plan_service.py`（Plan CRUD + 版本 + 权限 + 审计 + cron_unchanged 提示） | P0 | 1.2h | M1 字段冻结 | `app/services/medication_plan_service.py` + 单测 |
| M3 | REST 路由 `medication.py` + ROUTERS 注册表追加 | P0 | 0.5h | M2 签名冻结 | `app/api/medication.py` + `app/api/__init__.py` 增量 |
| M4 | 联调 + 前端 Mock 内部替换 + 单测全绿 | P0 | 0.5h | M3 路由可访问 | 后端 pytest 绿 + 前端手动验证 |
| **合计** | | | **4.0h** | | |

> 超载处理：M0（G1 裁决落地）是数据库 schema 变更，**必须最先做**（迁移写锁 dev-A Day 8 主任务已释放）。M0 先于 M1–M4 完成，后续任务不受 G1 影响。

---

## 2. 逐子任务详细步骤

### 2.0 M0：G1 裁决落地 — 0019 迁移 + ORM + service 签名变更（1.5h）

**前置**：dev-A Day 8 主任务 A1–A5 已合入（迁移写锁释放）。

#### Step 0.1（0.5h）：0019 迁移 `medication_events` 加 plan_version + 改唯一键

**down_revision**：`0018_day8_media`（当前最新）

**SQL**：
```sql
-- ① 加 plan_version 列
ALTER TABLE medication_events ADD COLUMN plan_version INTEGER NOT NULL DEFAULT 1;

-- ② 回填历史行（从 plan 表查当前 version）
UPDATE medication_events e
SET plan_version = p.version
FROM medication_plans p
WHERE e.plan_id = p.plan_id;

-- ③ 删旧唯一约束
ALTER TABLE medication_events DROP CONSTRAINT uq_medication_events_plan_id_scheduled_at;

-- ④ 建新唯一约束（三列：plan_id + scheduled_at + plan_version）
ALTER TABLE medication_events ADD CONSTRAINT uq_medication_events_plan_id_scheduled_at_plan_version
    UNIQUE (plan_id, scheduled_at, plan_version);
```

**downgrade**：反向操作——删新唯一约束 → 删 plan_version 列 → 还原旧唯一约束（回填历史时保留默认 1 即可）。

**验收**：`alembic upgrade head` → `downgrade -1` → `upgrade head` 可逆；新唯一约束生效；历史行 plan_version 回填正确。

#### Step 0.2（0.3h）：ORM `MedicationEvent` 增量

**`app/models/medication.py`** 加一列（增量，不动既有代码）：
```python
plan_version: Mapped[int] = mapped_column(
    Integer(),
    server_default=text("1"),
    nullable=False,
    comment="药单版本（与 medication_plans.version 联动；改药单后新事件自动升级到新版本）",
)
```

唯一约束从 `UniqueConstraint("plan_id", "scheduled_at", ...)` 改为 `UniqueConstraint("plan_id", "scheduled_at", "plan_version", ...)`。

#### Step 0.3（0.3h）：`medication_service.create_event` 签名变更

**`app/services/medication_service.py`**：
```python
async def create_event(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    elder_id: uuid.UUID,
    scheduled_at: datetime.datetime,
    drug_snapshot: dict[str, Any],
    triggered_by: str,
    plan_version: int,  # ← 新增参数
) -> uuid.UUID:
    # 唯一键查重改为 WHERE plan_id=? AND scheduled_at=? AND plan_version=?
    # 写入 MedicationEvent(..., plan_version=plan_version)
    # 其余不变（payload_hash 判定同键异 hash 抛 MedicationConflict）
```

**影响面**：
- `medication_jobs._trigger_plan`（scheduler 层）—— 当前调用要加 `plan_version=plan.version` 参数
- `medication_confirmation_graph.py`（graphs 层）—— 调用点同样加 `plan_version`
- 两者都是**加参数**不删既有逻辑，改动量极小

#### Step 0.4（0.4h）：scheduler + due_service + graphs 调用点增量

**连锁改动**（DuePlan dataclass 加 version 字段）：
1. `DuePlan`（`medication_due_service.py`）：加 `version: int` 字段
2. `list_due_plans`：扫描 active plans 时 `select(MedicationPlan.plan_id, ..., MedicationPlan.version)` 一并取回
3. `medication_jobs._trigger_plan`：构造 snapshot 时同时传 `plan_version=plan.version`
4. `medication_confirmation_graph.py`：搜 `create_event` 调用点，加 `plan_version=` 参数（如果图内只有 plan_id 没有 version，需补查一次 plan.version）

**自检**：
```bash
uv run alembic upgrade head
psql -d silvermate -c "SELECT plan_version, COUNT(*) FROM medication_events GROUP BY plan_version"
uv run alembic downgrade -1 && uv run alembic upgrade head
uv run pytest tests/test_medication_service*.py -q
uv run pytest tests/test_scheduler*.py -q
```

**验收**：
- ✅ 0019 迁移 upgrade → downgrade → upgrade 可逆
- ✅ medication_events 新唯一键生效
- ✅ medication_service + scheduler + due_service + graphs 四处改动编译通过、pytest 绿
- ✅ scheduler 扫描 active plans 时同时拿到 version 并传给 create_event
- ✅ 改 plan 后 version+1 → 新事件自动用新版本号、不冲突

---

### 2.1 M1：Pydantic Schema（0.3h）

**位置**：就地定义在 `app/api/medication.py` 顶部（对齐 workorders.py 风格，不新建 schemas/medication.py 文件）。

```python
# ==== 请求模型 ====

class PlanCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    elder_id: uuid.UUID
    drug_name: str = Field(min_length=1, max_length=200)
    dose: str = Field(min_length=1, max_length=200)
    schedule_cron: str = Field(min_length=1, max_length=100)
    start_date: datetime.date
    end_date: datetime.date | None = None

class PlanUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drug_name: str = Field(min_length=1, max_length=200)
    dose: str = Field(min_length=1, max_length=200)
    schedule_cron: str = Field(min_length=1, max_length=100)
    start_date: datetime.date
    end_date: datetime.date | None = None

# ==== 响应模型 ====

class PlanOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: uuid.UUID
    elder_id: uuid.UUID
    drug_name: str
    dose: str
    schedule_cron: str
    start_date: datetime.date
    end_date: datetime.date | None
    version: int
    status: Literal["active", "stopped"]
    created_by: uuid.UUID              # ← 返回 UUID（前端展示层再查姓名；或后端 JOIN 回填 created_by_name）
    created_by_name: str | None         # ← 后端 JOIN staff_accounts.name 回填，对齐前端 Mock 的 created_by_name
    created_at: str
```

**设计决策**：
- `PlanOut.created_by` + `created_by_name` 都返回：UUID 给审计/追溯用，姓名给前端展示
- 前端 Mock `MedicationPlan` 类型里**只定义了 `created_by_name`**（没有 UUID 字段），所以后端**必须返回 `created_by_name`**——service 层需 JOIN `staff_accounts`
- 前端写操作 `doctorName` 参数在真实接口里**不再需要**：后端从 JWT 注入 `created_by`，前端不传

---

### 2.2 M2：MedicationPlan Service（1.2h）

**位置**：新建 `app/services/medication_plan_service.py`——MedicationPlan 的**唯一写库出口**。

#### 2.2.1 模块头部常量与异常

```python
#: 业务异常
class PlanError(ValueError): ...
class PlanNotFound(PlanError): ...
class PlanStopped(PlanError): ...           # 已停用不可修改
class PlanInvalidCron(PlanError): ...
class PlanPermissionDenied(PlanError): ...  # 非医生尝试写入

#: CronTrigger 构造器（APScheduler 已在 pyproject 依赖里）
from apscheduler.triggers.cron import CronTrigger

#: status 枚举（与 medication_plans 表 CHECK 同源）
STATUS_ACTIVE: Final[str] = "active"
STATUS_STOPPED: Final[str] = "stopped"
```

#### 2.2.2 只读查询（所有 staff 角色可用）

```python
async def list_plans_by_elder(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    include_stopped: bool = True,
) -> list[PlanRow]:
    """按长者查药单（JOIN staff_accounts 回填 created_by_name + scope 过滤）。"""
    # ① _filter_by_scope 加 WHERE elder_id IN allowed_elder_ids（admin 跳过）
    #    越权读时 allowed_elder_ids 不包含请求 elder_id → list 自动返回空
    #    + 审计 action=medication_plan_read_forbidden, detail.reason="read_scope_outside_assignment"
    # ② 主查询 + JOIN staff_accounts AS creator ON plan.created_by = creator.staff_id
    # ③ 过滤 status!='stopped' 若 include_stopped=False
    # ④ 返回 list[PlanRow]
    #    → 空列表是合法返回（员工查非负责长者），不抛异常

async def get_plan(session: AsyncSession, plan_id: uuid.UUID) -> PlanRow | None:
    """查单条药单（scope 校验 + 越权读记审计）。"""
    # ① 先查 plan（拿 elder_id 用于 scope 校验）
    # ② _filter_by_scope 判定：该 plan.elder_id 是否在当前 staff 的允许集合里
    #    → admin 直接通过
    #    → doctor/care_worker 且 elder_id 不在集合 → 记审计后返回 None（API 层转 404）
    # ③ JOIN staff_accounts 回填 created_by_name
    # ④ 返回 PlanRow 或 None
```

**PlanRow** 结构（dataclass 或 namedtuple）：
```python
@dataclass
class PlanRow:
    plan: MedicationPlan
    created_by_name: str | None  # JOIN staff_accounts.name 结果，可能为 None（医生账号被删）
```

#### 2.2.3 写入入口（权限下沉 + 版本 + 审计）

**权限门禁方案**（主理人裁决 2026-11-11：API 层全放行 + Service 层双重门禁——角色 + 数据范围，与 `elder_staff_assignments` 表联动）：

| 接口类型 | API 层 | Service 层门禁 |
| --- | --- | --- |
| 查询药单（list/get） | `require_roles("doctor","care_worker","admin")` 放行 | **范围收紧**（三分支）：admin 看全量；doctor/care_worker 只能看自己有 active assignment 的长者（`elder_staff_assignments WHERE staff_id=? AND elder_id IN (...)`——relation 不限，primary_care 或 doctor 都是"负责"）；越权读（员工查非负责长者）返回空集合（list）或 404（get）+ 审计 detail.reason="read_scope_outside_assignment" |
| 新增药单 | `require_roles("doctor","care_worker","admin")` 放行到判定流程 | ① role=doctor；② 该 staff 是否为该长者的负责医生（`elder_staff_assignments WHERE elder_id=? AND staff_id=? AND relation='doctor'`） |
| 修改药单 | 同上 | 同上（role + 该 staff 是否为该长者的负责医生） |
| 停用药单 | 同上 | 同上 |

**核心设计**（三强调）：
1. **API 层一律不用 `require_roles("doctor")`**——护工/管理员的越权请求必须进入 Service 层才能被审计。`require_roles("doctor")` 在依赖注入阶段就抛 403，service 层不被调用 → 审计永远写不进去（AGENTS §4 红线）。
2. **Service 层写操作双重门禁**（`_assert_doctor_authorized` 辅助函数）：
   ```python
   async def _assert_doctor_authorized(
       session: AsyncSession, *, elder_id: uuid.UUID,
   ) -> None:
       """双重门禁：角色=doctor + 该医生是该长者的负责医生。"""
       from app.core.runtime_context import get_identity
       from fastapi import HTTPException
       identity = get_identity()
       staff_id = getattr(identity, "staff_id", None)
       
       # ① 角色门禁
       if identity is None or getattr(identity, "role", None) != "doctor":
           await record_audit(
               session, actor_type="staff", actor_id=staff_id,
               action="medication_plan_write_forbidden",
               resource_type="medication_plan", resource_id=None,
               detail={"reason": "role_not_doctor", "elder_id": str(elder_id)},
           )
           raise HTTPException(403, detail="药单管理仅负责医生可操作")
       
       # ② 数据范围门禁（查 elder_staff_assignments relation='doctor'）
       from sqlalchemy import select
       from app.models.relation import ElderStaffAssignment
       has_scope = (await session.execute(
           select(ElderStaffAssignment.id).where(
               ElderStaffAssignment.elder_id == elder_id,
               ElderStaffAssignment.staff_id == uuid.UUID(str(staff_id)),
               ElderStaffAssignment.relation == "doctor",
           )
       )).scalar_one_or_none() is not None
       if not has_scope:
           await record_audit(
               session, actor_type="staff", actor_id=staff_id,
               action="medication_plan_write_forbidden",
               resource_type="medication_plan", resource_id=None,
               detail={"reason": "not_attending_doctor_for_elder", "elder_id": str(elder_id)},
           )
           raise HTTPException(403, detail="您不是该长者的负责医生，无权操作药单")
   ```
3. **Service 层查询范围收紧**（`_assert_read_scope` / `_filter_by_scope`）：
   - **admin → 全量**：role=="admin" 直接返回所有匹配药单，不做 elder_id 过滤
   - **doctor / care_worker → 自己负责的长者**：先查 `elder_staff_assignments WHERE staff_id=当前 AND relation IN ('primary_care','doctor')` 拿到该员工负责的所有 elder_id 集合，再在 list 里加 `WHERE elder_id IN <集合>` 过滤
   - **越权读**（员工查非负责长者的药单）：list 返回**空集合**（不报错但也不泄漏），get 返回 404（"药单不存在"而非 403——不暴露"这个药单存在但你无权看"的事实）
   - **越权读也写审计**：detail.reason="read_scope_outside_assignment"，detail.elder_id / detail.plan_id 记录请求目标
   ```python
   async def _filter_by_scope(
       session: AsyncSession,
       *,
       query: Select[tuple[MedicationPlan]],
       elder_id: uuid.UUID | None = None,
   ) -> Select[tuple[MedicationPlan]]:
       """根据当前 staff 的范围过滤 query。admin 全量，doctor/care_worker 只返回负责长者的药单。"""
       identity = get_identity()
       role = getattr(identity, "role", None)
       staff_id = getattr(identity, "staff_id", None)
       if role == "admin":
           return query  # admin 全量，不加过滤
       # doctor / care_worker 查 elder_staff_assignments 拿负责的 elder_id 集合
       from sqlalchemy import select as sa_select
       from app.models.relation import ElderStaffAssignment
       rows = (await session.execute(
           sa_select(ElderStaffAssignment.elder_id).where(
               ElderStaffAssignment.staff_id == uuid.UUID(str(staff_id)),
               ElderStaffAssignment.relation.in_(["primary_care", "doctor"]),
           )
       )).all()
       allowed_elder_ids = {r[0] for r in rows}
       # 如果请求指定了 elder_id，但不在允许集合 → 记录审计（越权读）
       if elder_id is not None and elder_id not in allowed_elder_ids:
           await record_audit(
               session, actor_type="staff", actor_id=staff_id,
               action="medication_plan_read_forbidden",
               resource_type="medication_plan", resource_id=None,
               detail={"reason": "read_scope_outside_assignment", "elder_id": str(elder_id)},
           )
       return query.where(MedicationPlan.elder_id.in_(allowed_elder_ids))
   ```

**create_plan**：
```python
async def create_plan(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    drug_name: str,
    dose: str,
    schedule_cron: str,
    start_date: datetime.date,
    end_date: datetime.date | None,
    created_by: uuid.UUID,  # 从 JWT 注入（identity.staff_id），前端不传
) -> PlanRow:
    """新建药单。version=1、status=active。"""
    # ① 双重门禁：role=doctor + 负责医生 scope（_assert_doctor_authorized(session, elder_id=elder_id)）
    #    → 不通过时自动写审计（reason="role_not_doctor" 或 "not_attending_doctor_for_elder"）+ 403
    # ② 校验 cron 合法性（CronTrigger.from_crontab 试解析）
    # ③ 校验 start_date ≤ end_date（若 end_date 不为 None）
    # ④ INSERT MedicationPlan（version=1, status='active'）
    # ⑤ record_audit(action="medication_plan_create", resource_type="medication_plan", detail={...})
    # ⑥ flush 后返回 PlanRow
```

**update_plan**：
```python
async def update_plan(
    session: AsyncSession,
    plan_id: uuid.UUID,
    *,
    drug_name: str,
    dose: str,
    schedule_cron: str,
    start_date: datetime.date,
    end_date: datetime.date | None,
    updated_by: uuid.UUID,
) -> PlanRow:
    """修改药单。version+1。已停用不可修改。cron 不变则审计 detail 打 cron_unchanged 标记。"""
    # ① **先查 plan**（拿 elder_id 用于双重门禁 + 后续校验）→ 不存在抛 PlanNotFound
    # ② 双重门禁：role=doctor + 负责医生 scope（_assert_doctor_authorized(session, elder_id=plan.elder_id)）
    # ③ 校验 status!='stopped' → 已停用抛 PlanStopped
    # ④ 校验 cron 合法性、start_date ≤ end_date
    # ⑤ 比对 schedule_cron 是否变化：cron 不变则审计 detail 里打 {"cron_unchanged": true}
    # ⑥ UPDATE 所有业务字段 + version=plan.version+1
    # ⑦ record_audit(action="medication_plan_update", detail={"before": {...}, "after": {...}, "cron_unchanged": bool})
    # ⑧ flush 后返回最新 PlanRow
    #
    # ✅ G1 已裁决：plan.version+1 后，scheduler 下次扫到同一分钟时
    # 会因为 plan_version 不同而自动建新事件（唯一键已改为三列），
    # 不再触发 MedicationConflict。
```

**stop_plan**：
```python
async def stop_plan(
    session: AsyncSession,
    plan_id: uuid.UUID,
    *,
    stopped_by: uuid.UUID,
) -> PlanRow:
    """停用药单。version+1。无删除语义。已停用不可重复停。"""
    # ① **先查 plan**（拿 elder_id 用于双重门禁 + 后续校验）→ 不存在抛 PlanNotFound
    # ② 双重门禁：role=doctor + 负责医生 scope（_assert_doctor_authorized(session, elder_id=plan.elder_id)）
    # ③ 校验 status!='stopped' → 已停用抛 PlanStopped
    # ④ UPDATE status='stopped' + version=plan.version+1（其他字段不变）
    # ⑤ record_audit(action="medication_plan_stop", detail={"prev_version": plan.version})
    # ⑥ flush 后返回最新 PlanRow
```

#### 2.2.4 审计 detail 字段设计

```python
# create
detail = {
    "elder_id": str(elder_id),
    "drug_name": drug_name,
    "dose": dose,
    "schedule_cron": schedule_cron,
    "start_date": str(start_date),
    "end_date": str(end_date) if end_date else None,
}

# update
detail = {
    "before": {
        "drug_name": plan.drug_name, "dose": plan.dose,
        "schedule_cron": plan.schedule_cron,
        "start_date": str(plan.start_date),
        "end_date": str(plan.end_date) if plan.end_date else None,
        "version": plan.version,
    },
    "after": {
        "drug_name": drug_name, "dose": dose,
        "schedule_cron": schedule_cron,
        "start_date": str(start_date),
        "end_date": str(end_date) if end_date else None,
        "version": plan.version + 1,
    },
}

# stop
detail = {"prev_version": plan.version}

# forbidden（越权——role 非 doctor）
detail = {
    "reason": "role_not_doctor",                  # 越权原因（role 不够）
    "elder_id": str(elder_id) if elder_id else None,
    "intended_action": "create/update/stop",
    "plan_id": str(plan_id) if plan_id else None,
}

# forbidden（越权——是 doctor 但不是该长者的负责医生）
detail = {
    "reason": "not_attending_doctor_for_elder",   # 越权原因（scope 不够）
    "elder_id": str(elder_id),
    "intended_action": "create/update/stop",
    "plan_id": str(plan_id) if plan_id else None,
}

# forbidden（越权读——员工查非负责长者的药单）
detail = {
    "reason": "read_scope_outside_assignment",    # 越权原因（读 scope 不够）
    "elder_id": str(elder_id),
    "intended_action": "list/get",
    "plan_id": str(plan_id) if plan_id else None,
}
```

审计**不回显**：staff 姓名、token、完整手机号、地址——detail 只放 ID、字段名、枚举值与变更前后快照（脱敏）。

#### 2.2.5 单测用例

```python
# ==== 权限（读——范围收紧）====
def test_admin_read_all_plans(): ...                        # admin 查任何 elder_id → 全量返回（不做 scope 过滤）
def test_staff_read_own_assigned_elder_plans(): ...         # doctor/care_worker 查自己负责的 elder_id → 返回该长者的药单
def test_staff_read_outside_scope_returns_empty_list(): ... # doctor/care_worker 查非负责的 elder_id → 返回 [] + 审计 action=medication_plan_read_forbidden, reason="read_scope_outside_assignment"
def test_staff_get_outside_scope_returns_404(): ...         # doctor/care_worker get 非负责的 plan → 404 "药单不存在"（不暴露存在性）+ 审计
def test_staff_with_no_assignments_reads_nothing(): ...      # 该 staff 在 elder_staff_assignments 里无任何 active assignment → list 恒返回 []

# ==== 权限（写——角色门禁）====
def test_create_plan_requires_doctor_role(): ...       # care_worker/admin 尝试 create → 403 + audit_logs detail.reason="role_not_doctor"
def test_update_plan_requires_doctor_role(): ...       # 同上（care_worker 尝试 update 自己无 scope 的 plan）
def test_stop_plan_requires_doctor_role(): ...         # 同上

# ==== 权限（写——数据范围门禁）====
def test_create_plan_unattending_doctor_forbidden(): ...  # 是 doctor 但不是该长者的负责医生 → 403 + audit_logs detail.reason="not_attending_doctor_for_elder"
def test_update_plan_unattending_doctor_forbidden(): ...  # 同上
def test_stop_plan_unattending_doctor_forbidden(): ...    # 同上
def test_create_plan_attending_doctor_allowed(): ...      # 是该长者的负责医生 → create 成功
def test_update_plan_attending_doctor_allowed(): ...      # 同上

# ==== 版本 ====
def test_create_plan_version_equals_1(): ...
def test_update_plan_increments_version(): ...
def test_stop_plan_increments_version(): ...
def test_update_stopped_plan_forbidden(): ...
def test_stop_already_stopped_forbidden(): ...

# ==== 审计 ====
def test_create_plan_writes_audit(): ...               # action=medication_plan_create
def test_update_plan_writes_audit_with_before_after(): ...  # detail 有 before/after 两快照
def test_stop_plan_writes_audit(): ...                 # action=medication_plan_stop
def test_forbidden_role_write_writes_audit(): ...      # action=medication_plan_write_forbidden, detail.reason="role_not_doctor"
def test_forbidden_scope_write_writes_audit(): ...     # action=medication_plan_write_forbidden, detail.reason="not_attending_doctor_for_elder"
def test_forbidden_read_scope_writes_audit(): ...      # action=medication_plan_read_forbidden, detail.reason="read_scope_outside_assignment"

# ==== 校验 ====
def test_invalid_cron_rejected(): ...
def test_start_date_after_end_date_rejected(): ...

# ==== 快照隔离（AC-27）====
def test_old_event_snapshot_not_affected_by_plan_update(): ...
    # 先造一条 plan + 建 event（scheduler 驱动）→ plan.update → 再查原 event 的 drug_snapshot 仍为旧值

# ==== cron_unchanged 提示 ====
def test_update_plan_cron_unchanged_marks_audit(): ...  # 新旧 cron 相同 → audit detail 有 cron_unchanged:true
def test_update_plan_cron_changed_no_mark(): ...        # 新旧 cron 不同 → audit detail 无 cron_unchanged 字段
```

---

### 2.3 M3：REST 路由 + ROUTERS 注册表（0.5h）

#### 2.3.1 `app/api/medication.py`

```python
router = APIRouter(
    prefix="/api/medication-plans",
    tags=["medication-plans"],
    route_class=ContractAPIRoute,  # 与 workorders.py 对齐
)

# 权限：所有 staff 角色可读写（service 层内部做写门禁）
_ALL_STAFF = ("doctor", "care_worker", "admin")

@router.get("", response_model=list[PlanOut])
async def list_plans(
    elder_id: Annotated[uuid.UUID, Query()],
    include_stopped: bool = Query(default=True),
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(require_roles(*_ALL_STAFF))],
) -> list[PlanOut]:
    """按长者查药单。"""
    rows = await medication_plan_service.list_plans_by_elder(
        session, elder_id=elder_id, include_stopped=include_stopped
    )
    return [_serialize(row) for row in rows]


@router.get("/{plan_id}", response_model=PlanOut)
async def get_plan(
    plan_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(require_roles(*_ALL_STAFF))],
) -> PlanOut:
    row = await medication_plan_service.get_plan(session, plan_id=plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="药单不存在")
    return _serialize(row)


@router.post("", response_model=PlanOut)
async def create_plan(
    body: PlanCreateIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_roles(*_ALL_STAFF))],
) -> PlanOut:
    """新建药单（仅医生可写，service 层门禁）。"""
    staff_id = getattr(identity, "staff_id", None)
    if staff_id is None:
        raise HTTPException(status_code=401, detail="仅员工会话可操作")
    row = await medication_plan_service.create_plan(
        session,
        elder_id=body.elder_id,
        drug_name=body.drug_name,
        dose=body.dose,
        schedule_cron=body.schedule_cron,
        start_date=body.start_date,
        end_date=body.end_date,
        created_by=uuid.UUID(str(staff_id)),
    )
    await session.commit()
    return _serialize(row)


@router.put("/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: uuid.UUID,
    body: PlanUpdateIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_roles(*_ALL_STAFF))],
) -> PlanOut:
    """修改药单（仅医生可写，已停用不可改，version+1）。"""
    staff_id = getattr(identity, "staff_id", None)
    row = await medication_plan_service.update_plan(
        session,
        plan_id=plan_id,
        drug_name=body.drug_name,
        dose=body.dose,
        schedule_cron=body.schedule_cron,
        start_date=body.start_date,
        end_date=body.end_date,
        updated_by=uuid.UUID(str(staff_id)) if staff_id else uuid.UUID(int=0),
    )
    await session.commit()
    return _serialize(row)


@router.post("/{plan_id}/stop", response_model=PlanOut)
async def stop_plan(
    plan_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_roles(*_ALL_STAFF))],
) -> PlanOut:
    """停用药单（仅医生可写，无删除语义，version+1）。"""
    staff_id = getattr(identity, "staff_id", None)
    row = await medication_plan_service.stop_plan(
        session,
        plan_id=plan_id,
        stopped_by=uuid.UUID(str(staff_id)) if staff_id else uuid.UUID(int=0),
    )
    await session.commit()
    return _serialize(row)
```

**异常映射**：`PlanNotFound → 404`、`PlanStopped → 422`（"已停用药单不可修改"）、`PlanInvalidCron → 422`（"cron 表达式不合法"）、`PlanPermissionDenied` 不抛（service 层直接 `raise HTTPException(403)`）。

#### 2.3.2 ROUTERS 注册表追加

在 `app/api/__init__.py` ROUTERS 元组**末尾追加一行**：
```python
(medication.router, ""),  # 自带 /api/medication-plans 前缀（H+1 药单管理）
```

并在文件顶部 import 列表里追加 `from app.api import medication`。

#### 2.3.3 序列化函数

```python
def _serialize(row: medication_plan_service.PlanRow) -> PlanOut:
    p = row.plan
    return PlanOut(
        plan_id=p.plan_id,
        elder_id=p.elder_id,
        drug_name=p.drug_name,
        dose=p.dose,
        schedule_cron=p.schedule_cron,
        start_date=p.start_date,
        end_date=p.end_date,
        version=p.version,
        status=p.status,  # type: ignore[arg-type]
        created_by=p.created_by,
        created_by_name=row.created_by_name,
        created_at=p.created_at.isoformat(),
    )
```

---

### 2.4 M4：联调 + 前端替换（0.5h）

#### 2.4.1 后端自测

```bash
# 1. pytest 全绿
uv run pytest tests/test_medication_plan*.py -q

# 2. 启动后端，用 curl/httpx 手工验证
# POST /api/medication-plans （拿 staff doctor token）
# GET /api/medication-plans?elder_id=xxx
# PUT /api/medication-plans/{plan_id}
# POST /api/medication-plans/{plan_id}/stop
# 换 care_worker token 写 → 预期 403 + audit_logs 有 forbidden 行

# 3. ruff
uv run ruff check app tests && uv run ruff format --check app tests

# 4. audit_logs 核对
psql -d silvermate -c "SELECT action, resource_type, resource_id, detail->>'intended_action' FROM audit_logs WHERE resource_type='medication_plan' ORDER BY created_at DESC LIMIT 10"
```

#### 2.4.2 前端 `medication-api.ts` 内部替换

**原则**：导出签名**完全不变**（`listPlansByElder / createPlan / updatePlan / stopPlan`），只换内部实现从 mock store → fetch 调用，页面层零改动。

**前端需对齐的响应映射**：
| 后端 PlanOut | 前端 MedicationPlan | 映射说明 |
|-------------|-------------------|---------|
| `plan_id: UUID` | `plan_id: string` | UUID 序列化为 string ✓ |
| `created_by_name: string \| null` | `created_by_name: string` | 后端保证非空（JOIN staff_accounts.name），前端可直接用 |
| `created_by: UUID` | —（前端不消费） | 忽略 |
| `created_at: string` | —（前端不消费） | 忽略 |
| `end_date: date \| null` | `end_date: string \| null` | ISO string ✓ |

**前端 fetch 替换骨架**（替换 `medication-api.ts` 里 store 相关代码，保留四个导出函数签名）：
```typescript
const API_BASE = (import.meta as any).env?.VITE_API_BASE || "http://localhost:8000";

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      // Authorization 从前端全局 token 取（复用现有 auth 逻辑）
      Authorization: `Bearer ${getAuthToken()}`,
      ...(options?.headers || {}),
    },
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

// listPlansByElder → GET /api/medication-plans?elder_id=xxx
export async function listPlansByElder(elderId: string): Promise<MedicationPlan[]> {
  return apiFetch<MedicationPlan[]>(`/api/medication-plans?elder_id=${elderId}`);
}

// createPlan → POST /api/medication-plans（**不再传 doctorName 参数**，后端从 JWT 注入）
export async function createPlan(input: PlanWriteInput, _doctorName?: string): Promise<MedicationPlan> {
  return apiFetch<MedicationPlan>("/api/medication-plans", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

// updatePlan → PUT /api/medication-plans/{plan_id}
export async function updatePlan(planId: string, input: PlanWriteInput, _doctorName?: string): Promise<MedicationPlan> {
  return apiFetch<MedicationPlan>(`/api/medication-plans/${planId}`, {
    method: "PUT",
    body: JSON.stringify(input),
  });
}

// stopPlan → POST /api/medication-plans/{plan_id}/stop
export async function stopPlan(planId: string, _doctorName?: string): Promise<MedicationPlan> {
  return apiFetch<MedicationPlan>(`/api/medication-plans/${planId}/stop`, {
    method: "POST",
  });
}
```

**关键**：前端 `PlanWriteInput` 类型定义**保持原签名不变**（不传 created_by / created_by_name / version）；后端返回的 PlanOut 序列化出的 JSON 自动匹配前端 `MedicationPlan` 类型结构（多出来的 `created_by`/`created_at` 字段 TS 会忽略）。

---

## 3. 风险预案

| # | 风险 | 影响 | 概率 | 缓解 |
| --- | --- | --- | --- | --- |
| ~~G1~~ | ~~events 唯一键不含 version → 改药单但 cron 不变时 scheduler 跳过提醒~~ | ~~高~~ | ~~中~~ | **✅ 已裁决（2026-11-11）**：0019 迁移加 plan_version + 改三列唯一键 + Service 层 cron_unchanged 提示 + Scheduler 传 version。三者组合消除风险。 |
| G2 | 权限下沉导致越权审计 | 零风险：service 层角色判定在所有写入入口第一行执行 | 低 | — |
| G3 | created_by_name JOIN 返回 None（医生账号已被删） | 前端 created_by_name 显示空白 | 低 | staff_accounts.created_by 是 `RESTRICT` FK——医生账号被删会被 FK 拦住；极端情况 service 层用 coalesce 兜底为空字符串 |
| G4 | CronTrigger.from_crontab 拒绝合法表达式（APScheduler 与其他 cron 实现的小差异） | 医生输入的 cron 被判非法 | 低 | 用 APScheduler 原生解析器（项目已有，scheduler 层同款）；校验失败时返回 422 "服药时间表达式不合法" 并附提示格式 |
| G5 | 新增 0019 迁移与 graphs 层调用点变更 | graphs 层 create_event 调用需加 plan_version 参数 | 低 | graphs 层改动量极小（加一个参数）；**不碰 graphs 核心逻辑**，只在 create_event 调用处增量；若图内没有 version 字段需补查一次 plan.version |
| G6 | DuePlan dataclass 加 version 字段 | scheduler 扫描增量 | 低 | 纯增量字段，不破坏既有调用；select 里多一列 |

---

## 4. 写作用域

**可写**（AGENTS §2 分层硬约束）：

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `alembic/versions/0019_day8_medication_plan_version.py` | **新建** | G1 裁决落地——events 加 plan_version 列 + 改唯一键为三列 |
| `app/models/medication.py` | **增量** | MedicationEvent 加 plan_version 列 + UniqueConstraint 改三列 |
| `app/services/medication_service.py` | **增量** | `create_event` 签名加 `plan_version: int`；唯一键查重改三列 |
| `app/services/medication_due_service.py` | **增量** | DuePlan dataclass 加 `version: int`；list_due_plans 扫描时取回 plan.version |
| `app/scheduler/medication_jobs.py` | **增量** | `_trigger_plan` 传 `plan_version=plan.version` |
| `app/graphs/medication_confirmation_graph.py` | **增量** | create_event 调用加 `plan_version=` 参数（**不碰图核心逻辑**） |
| `app/services/medication_plan_service.py` | **新建** | MedicationPlan 唯一写库出口（list/get/create/update/stop + 权限 + 审计 + 版本 + cron_unchanged） |
| `app/api/medication.py` | **新建** | REST 路由（就地定义 Pydantic 模型 + 序列化） |
| `app/api/__init__.py` | **增量** | ROUTERS 追加一行 + import 追加 |
| `tests/test_medication_plan*.py` | **新建** | 权限/版本/审计/校验/快照隔离全覆盖 |
| `tests/test_medication_service*_updated.py` | **增量** | create_event 新签名适配（plan_version 参数 + 三列唯一键查重） |
| `frontend/src/console/medication-api.ts` | **增量** | 内部 mock store → fetch 替换，**导出签名不变** |

**不可写（硬禁区）**：
- `app/services/medication_due_service.py` 之外的 scheduler 核心逻辑
- `app/graphs/medication_confirmation_graph.py` 之外的 graph 编排逻辑（LangGraph state machine、checkpointer）
- 任何已合入的 `alembic/versions/0001–0018`
- `app/main.py`（不动路由装配，走 ROUTERS）
- `app/router/safety.py` / `app/router/rules.py`
- `app/services/health_service.py` / `notify_service.py` / `work_order_service.py`（dev-A Day 8 主任务已合入）

---

## 5. 当日时间线（精确到时段）

```
17:00 ────────── M0 G1 裁决落地开工（迁移写锁已释放，最先做）
17:00–17:30    Step 0.1 0019 迁移（0.5h）—— ALTER TABLE + 回填历史行 + 删旧唯一约束 + 建新唯一约束
17:30–17:48    Step 0.2 ORM 增量（0.3h）—— MedicationEvent 加 plan_version + UniqueConstraint 改三列
17:48–18:06    Step 0.3 service 签名变更（0.3h）—— create_event 加 plan_version 参数 + 唯一键查重改三列
18:06–18:30    Step 0.4 调用点增量（0.4h）—— DuePlan 加 version + list_due_plans 取 version + _trigger_plan 传 version + graphs create_event 加 plan_version
                ✅ 18:30 前 M0 全量交付，后续任务不受 G1 影响
                ├── 18:00 与 dev-B 简短同步：due_service + medication_jobs + graphs 三处调用点需增量
                └── pytest tests/test_medication_service*.py + test_scheduler*.py 绿

18:30 ────────── M1 Schema + M2 Service 开工
18:30–19:06    M1 Schema 定义（0.3h）—— PlanCreateIn/PlanUpdateIn/PlanOut
19:06–20:18    M2 Service 开发（1.2h）——权限门禁 + cron 校验 + create/update/stop + cron_unchanged 标记 + 审计
                ├── 只读查询（list/get + JOIN 回填姓名）
                ├── create_plan（权限 + cron + INSERT + 审计）
                ├── update_plan（权限 + 停用拦截 + version+1 + cron_unchanged 提示 + 审计）
                └── stop_plan（权限 + 停用拦截 + version+1 + 审计）

20:18–20:48    M3 路由 + ROUTERS（0.5h）——就地 Pydantic + 序列化 + 异常映射
20:48–21:18    M4 联调（0.5h）——pytest 绿 + 后端 curl 验证 + 前端 fetch 替换
                ├── pytest tests/test_medication_plan*.py -q
                ├── ruff check + format
                ├── audit_logs SQL 核对（create/update/stop + forbidden）
                └── 前端页面手动验证（ListPlan/CreatePlan/UpdatePlan/StopPlan + 护工写 403）

21:18–21:30    提交 + 写 README 索引 + 时间线日志
```

---

## 6. 与 Day 8 其他任务的边界

| 对接点 | 方向 | 内容 |
| --- | --- | --- |
| **G1 裁决落地（M0）** | dev-A → dev-B（due_service + medication_jobs + graphs） | create_event 签名加 plan_version 参数 + DuePlan dataclass 加 version 字段。三处调用点都是**加参数增量**，不碰核心逻辑。18:00 简短同步后 dev-A 直接改代码（dev-B Day 8 已收，不再阻塞） |
| **前端契约** | 本计划 → dev-D（前端） | 后端 REST 上线后 dev-D 只需替换 `medication-api.ts` 内部 fetch 调用；**页面层零改动**（导出签名不变） |
| **scheduler 扫描** | 本计划 ← dev-B（due_service） | due_service.list_due_plans 扫描 active plans 时已取 plan.version（M0 增量）——plan 改版后 scheduler 自动拿到新版本号传给 create_event |
| **events drug_snapshot** | 本计划 → dev-B（medication_jobs） | scheduler 用**当前 plan 字段**构造 snapshot + plan_version 一起写事件——老事件 snapshot 冻结、新事件带新版本号 |
| **graphs create_event 调用** | dev-A → dev-B（medication_confirmation_graph） | graph 内 create_event 调用加 plan_version 参数。如果图内只有 plan_id 没有 version，需补查一次 `SELECT plan.version FROM medication_plans WHERE plan_id=?` |

---

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.4 | 2026-11-11 | **查询接口范围收紧**：不再放开全员看板口径。admin 看全量；doctor/care_worker 只能看自己在 `elder_staff_assignments` 里有 active assignment 的长者（relation IN ('primary_care','doctor') 都算"负责"）。越权读（员工查非负责长者）list 返回空集合、get 返回 404（不暴露存在性）+ 审计 action=medication_plan_read_forbidden, detail.reason="read_scope_outside_assignment"。新增辅助函数 `_filter_by_scope`（三分支逻辑 + 越权读审计）。单测清单扩充：新增 5 条读范围收紧用例 + 1 条越权读审计用例。同时确认保留 PlanOut.created_at（ISO string）。 |
| v1.2 | 2026-11-11 | **门禁裁决对齐主理人**：API 层一律放行全员 staff（`require_roles("doctor","care_worker","admin")`），写操作越权请求必须进入 Service 层才能被审计（禁止 API 层直接 `require_roles("doctor")` 提前拦截）。Service 层写操作**双重门禁**：① role=doctor；② 该 staff 是否为该长者的负责医生（查 `elder_staff_assignments WHERE elder_id=? AND staff_id=? AND relation='doctor'`——表已在迁移 0002 合入）。越权审计 detail 区分两种 reason：`role_not_doctor` vs `not_attending_doctor_for_elder`。查询接口放开全员（看板口径，对齐 workorders）。update_plan/stop_plan 步骤调整：先查 plan 拿 elder_id → 双重门禁 → 再校验 status。单测清单新增数据范围门禁相关用例。 |
| v1.1 | 2026-11-11 | **G1 裁决落地**：主理人裁决「数据库唯一约束 + Service 层校验 + Scheduler 幂等性」三者组合。新增 M0 任务（0019 迁移 events 加 plan_version + 改三列唯一键 + ORM + medication_service.create_event 签名加 plan_version + due_service DuePlan 加 version + medication_jobs 传 version + graphs 调用点增量）。update_plan 加 cron_unchanged 提示。写作用域扩充（medication.py/medication_service.py/medication_due_service.py/medication_jobs.py/graphs 增量）。时间线从 2.5h 扩到 4.0h。风险预案 G1 标记已消除、G5/G6 新增。§6 对接边界增加 dev-B scheduler + graphs。 |
| v1.0 | 2026-11-11 | 首版：基于代码实测（MedicationPlan ORM + medication_service + medication_due_service + 前端 Mock API）编制；G1 唯一键风险标记待裁决；权限门禁下沉到 service 层（API 层放行为全员、service 内写分支 + 越权审计）；零迁移变更（Plan 表已在 0010 合入，version 列闲置） |
