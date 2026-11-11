> 状态：草稿　｜　版本：v1.1　｜　日期：2026-11-11
> 关联：docs/team_tasks/day08/dev-A-后端数据与分级.md v1.1
> 上游依据：PRD §4.6（验收）｜技术设计文档 §3.4/§3.5/§7.2（实现）
> 变更记录见文末

---

# dev-A（Day 8 · 数据层 / 分级引擎 / 通知规则 / 戏曲直连）个人执行计划

## 0. 全局认知基线（一页版）

| 项 | 现状（2026-11-11 实测） | 对 Day 8 dev-A 的含义 |
| --- | --- | --- |
| 迁移基线 | 止于 **0016**（decision_logs）；0011 已建 `notifications`、0010 已建 work_orders/risk_events | A1 新建 0017 两表、A4 新建 0018 两表，**均不碰已合入迁移**；迁移写锁由 A 独占，避免并行 autogenerate 冲突 |
| 模型层 | `app/models/` 共 14 个文件；无 `health.py` / `report.py` / `media.py` | A1/A4 新建，不追加到既有文件；风格对齐 `app/models/notifications.py`（枚举一律 CHECK、不用 PG ENUM、列注释引 TD 行号） |
| 通知服务 | `notify_service.py` 仅监护人侧（send_guardian_notifications + 查询面），**缺 staff 侧**、状态机只支持 generated 初始态 | A3 增量扩展：新建 staff 侧通知入口 + **状态机五态驱动**（generated→pending→delivered→acknowledged + 失败终态 failed）+ 2 分钟升级扫描逻辑 + 30s 重试逻辑（retry_count≥3→failed） |
| 工单服务 | `work_order_service.py` 已含 `open_work_order` / `open_missed_medication` / 流转 + 只读查询面 | health_service 分级后调它建黄/红工单，调用方式参照漏服模式（`dedupe_token` 确定性幂等） |
| 规则层 | `rules.py` ≤10 条；R-01~R-08 播放控制**全部 action="agent" + reason="capability_not_ready"**；仅 R-09（天气）是真直连（ToolExecutor→MCP weather） | A5 只做「降级→直连」升级：R-01~R-08 的 action 改 `tool`、tool_name 从 X8 冻结口径取；不碰阈值/正则/ASR 链路（D5 评测门禁依赖稳定） |
| 决策日志 | 0016 已建 `decision_logs` 表；`decision_log_service` 已实现 record_decision 写入 | A5 规则直连执行时 decision_logs 会自动写 `route_layer="rule"`（已在 turn_service 第 3 步打点）；AC-37 验收用它留痕 |
| write 锁（互斥） | migration=A、models=A、notify_service 增量=A、rules.py=A | 零文件重叠；graphs/scheduler/api/tool_registry/memory_service/media_service 分别归 B/B/C/B/B/C |

## 1. 任务拆解 · 时间分配 · 优先级

**总估算 9.0h**（含 H12 门禁 0.5h）——按 Day 8 9 小时工作日有 0.5h 浮动空间。

| # | 子任务 | 对应主计划 | 优先级 | 预估 | 前置 | 核心交付物 |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | 两表迁移 0017 + ORM | H1 | P0 | 2.0h | 无（与 B1 并行） | `0017_day8_health_reports.py` + `app/models/health.py` + `app/models/report.py` |
| A4 | 戏曲迁移 0018 | H14① | P0 | 0.5h | **A1 完成**（迁移写锁顺做） | `0018_day8_media.py` |
| A2 | health_service（校验+分级+版本链+代填） | H2 | P0 | 3.5h | A1 完成 | `app/services/health_service.py` + 六类分级单测 |
| A3 | 通知双轨+2分钟升级+状态机 | H7 | P0 | 2.0h | A2 签名冻结 + C2 对齐 X2 | `notify_service.py` 增量 + 通知状态机单测（五态含 failed） |
| A5 | 规则直连 R-01~R-08 | H14④ | P0 | 1.5h | B1/X8 冻结口径 + B4 Tool 就绪（骨架可先行） | `app/router/rules.py` 增量（仅规则动作升级） |
| H12 | 门禁收口（牵头） | H12 | P0 | 0.5h | 全员任务完成 | 四件套全绿 + 证据回填 |
| **合计** | | | | **10.0h** | | |

> ⚠️ 超载裁序（按 R1 顺位，**包内无先裁项**）：A1–A4、A2、A3、A5 均为健康主线/欠债核心；溢出池 O4 聚合预警（P2）仅产能富余才启动。若 CP4 联调阻塞→A5 联调段顺延 Day 9（骨架必须当日完成）。

## 2. 逐子任务详细执行步骤

### 2.1 A1：两表迁移 0017 + ORM（2.0h）

**前置**：无，与 B1 契约冻结并行开工。

#### Step 1.1（30min）：核对 TD 表定义字段清单

| 表 | 来源章节 | 关键字段 |
| --- | --- | --- |
| `health_records` | TD §3.4 + **Q-1 两列补入** | **19 列**（TD 17 列 + 2 列裁决补入）：record_id(UUID PK), elder_id(FK), recorded_at(timestamptz), source(text 四值), operator_id(FK staff 可空), systolic/diastolic(int 可空), sleep_hours(numeric(3,1) 可空), mood_score(int 可空), mood_note(text 可空), complaint(text 可空), severity(text 三值), rule_version(text), review_status(text 三值), reviewed_by(FK staff 可空), **review_note(text 可空——D-2/Q-1 主理人裁决补列，复核操作备注，承载前端 review-api `review_note` 字段，TD 走修订记录)**，version(int), superseded_by(UUID 自引用可空), **superseded_reason(text 可空——D-1/Q-1 主理人裁决补列，CHECK 枚举 `review_correction/backfill`，承载前端 review-api `superseded_reason` 字段，TD 走修订记录)**，graph_thread_id(text 可空) |
| `daily_reports` | TD §3.5 | 9 列：report_id(UUID PK), elder_id(FK), report_date(date), version(int), status(text), summary_text(text), medication_stats(jsonb), audio_url(text 可空), pushed_at(timestamptz 可空) |

#### Step 1.2（45min）：编写迁移 `0017_day8_health_reports.py`

**down_revision** = 0016_decision_logs

**CHECK 约束**（全部照 TD §3.6/§3.4 数值口径，用 PG CHECK 不用 ENUM）：

| 约束 | SQL 片段 | 来源 |
| --- | --- | --- |
| 血压范围 | `systolic BETWEEN 0 AND 300 OR systolic IS NULL` / `diastolic BETWEEN 0 AND 300 OR diastolic IS NULL` | TD §3.6 第 1/2 条 |
| 睡眠范围 | `sleep_hours BETWEEN 0 AND 24 OR sleep_hours IS NULL` | TD §3.6 第 3 条 |
| 情绪范围 | `mood_score BETWEEN 1 AND 5 OR mood_score IS NULL` | TD §3.6 第 4 条 |
| 来源枚举 | `source IN ('voice_scheduled','voice_active','staff_scheduled','staff_active')`（四值，不含旧口径 `staff_report`/`resident`） | TD §3.6 第 5 条（裁决 B1） |
| 严重度三值 | `severity IN ('green','yellow','red')` | TD §3.4 |
| 复核状态三值 | `review_status IN ('pending','confirmed','corrected')` | TD §3.4 |
| 日报唯一 | `UNIQUE(elder_id, report_date, version)` | TD §3.5 |
| 日报状态 | `status IN ('normal','away_notice')` | TD §3.5 |
| **superseded_reason 枚举** | `superseded_reason IN ('review_correction','backfill') OR superseded_reason IS NULL` | D-1/Q-1 主理人裁决补列，TD 走修订记录，CHECK 口径与枚举值对齐契约 v0.1 |

**索引**：
- `ix_health_records_elder_id_recorded_at` ON health_records(elder_id, recorded_at DESC) —— 医生健康趋势查询 FR-HLT-09
- `ix_daily_reports_elder_id_report_date_version` ON daily_reports(elder_id, report_date, version DESC) —— 家属日报列表查询

**外键**：
- health_records.elder_id → elders.elder_id CASCADE
- health_records.operator_id → staff_accounts.staff_id SET NULL
- health_records.reviewed_by → staff_accounts.staff_id SET NULL
- daily_reports.elder_id → elders.elder_id CASCADE

**迁移风格**：完全照搬 0016 的三段结构（`_in_clause` 辅助函数 + 逐列 `create_table` + CHECK + 索引 + 注释），不写 ORM 自动生成。

**downgrade**：先 drop 4 个索引 → drop health_records 表 → drop daily_reports 表。

#### Step 1.3（45min）：编写 ORM

**`app/models/health.py`**：

```python
"""health_records ORM——0017 迁移镜像。列注释引 TD §3.4 行号。"""

class HealthRecord(Base):
    __tablename__ = "health_records"
    __table_args__ = (
        Index("ix_health_records_elder_id_recorded_at", "elder_id", "recorded_at"),
        ...  # 全部 CHECK 逐字段
    )
    # 19 列（TD 17 列 + review_note + superseded_reason 两列裁决补入），逐列映射
```

**`app/models/report.py`**：

```python
"""daily_reports ORM——0017 迁移镜像。列注释引 TD §3.5 行号。"""

class DailyReport(Base):
    __tablename__ = "daily_reports"
    __table_args__ = (
        UniqueConstraint("elder_id", "report_date", "version", name="uq_daily_reports_unique"),
        ...  # 全部 CHECK
    )
    # 9 列
```

**自检命令**：

```bash
uv run alembic upgrade head                      # 0017 建表
uv run alembic downgrade -1 && uv run alembic upgrade head   # 可逆
psql -d silvermate -c "\d health_records"         # 核对 CHECK + 索引
psql -d silvermate -c "\d daily_reports"         # 核对 CHECK + UNIQUE
```

**验收标准**：
- ✅ 两表 CHECK 全部存在（血压 0–300、睡眠 0–24、mood 1–5、source 四值、severity 三值、review_status 三值）
- ✅ 索引齐全（(elder_id, recorded_at DESC)、(elder_id, report_date, version DESC)）
- ✅ downgrade → upgrade 可回滚
- ✅ ORM 列名/类型/可空与迁移逐字一致

---

### 2.2 A4：戏曲迁移 0018（0.5h）

**前置**：A1 完成（迁移写锁顺做，避免并行 autogenerate 冲突）。

**down_revision** = 0017_day8_health_reports

**两表**：
- `media_library`（7 列）：media_id(UUID PK), type(text CHECK opera/movie), title(text), genre(text 可空), era(text 可空), artist(text 可空), media_url(text), duration(int 可空), copyright_status(text)
- `media_history`（5 列）：history_id(UUID PK), elder_id(FK elders), media_id(FK media_library), started_at(timestamptz), finished_at(timestamptz 可空), completion_rate(numeric(4,3) 可空)

**CHECK**：type IN ('opera','movie')、copyright_status 枚举（教师授权/公有领域样例）

**索引**：`ix_media_library_type_genre` ON media_library(type, genre) —— 剧种搜索

**与 health 两表零触碰**：0018 只建 media_library + media_history，不删改 health_records / daily_reports 任何行。

**验收标准**：
- ✅ `alembic upgrade head` 0017+0018 依次执行
- ✅ `alembic downgrade -2` 回退到 0015（或 `downgrade -1` 依次回退）
- ✅ 两表 CHECK + 索引齐全

---

### 2.3 A2：health_service 分级引擎（3.5h）

**前置**：A1 完成（ORM 字段就绪）；**12:00 前签名冻结通知 B/C（X1）**。

#### Step 2.1（30min）：定义分级常量与阈值（PRD §4.6 表写死代码不写 prompt）

```python
#: 分级规则版本（改阈值必须 +1，rule_version 写进 health_records）
RULE_VERSION: Final[str] = "g1"

#: 严重度三值
SEVERITY_GREEN: Final[str] = "green"
SEVERITY_YELLOW: Final[str] = "yellow"
SEVERITY_RED: Final[str] = "red"

#: 红色阈值（单分量即判红，AC-21）
SYSTOLIC_RED: Final[int] = 180      # 收缩压 ≥180
DIASTOLIC_RED: Final[int] = 120     # 舒张压 ≥120 或 ≤90（低血压）
SYSTOLIC_HYPOTENSION: Final[int] = 90  # 收缩压 ≤90 也判红
SYSTOLIC_HYPOTENSION_STRICT: Final[int] = 90  # 统一用

#: 黄色阈值（AC-09 口径：收缩压 140–179 或 90 以下正常）
SYSTOLIC_YELLOW_LOWER: Final[int] = 140  # 140–179 = 黄
SYSTOLIC_YELLOW_UPPER: Final[int] = 179
DIASTOLIC_YELLOW_LOWER: Final[int] = 90   # 90–119 = 黄
DIASTOLIC_YELLOW_UPPER: Final[int] = 119
SYSTOLIC_LOW_NORMAL: Final[int] = 90      # 收缩压 ≤90 触发判红阈值（SYSTOLIC_HYPOTENSION）

#: 睡眠阈值
SLEEP_RED_HOURS: Final[float] = 0.0      # 无睡眠数据 → 黄（数据质量）
SLEEP_YELLOW_HOURS: Final[float] = 5.0   # <5h = 黄

#: 情绪阈值
MOOD_YELLOW: Final[int] = 2               # mood_score ≤2 = 黄
```

**⚠️ 12:00 X1 签名冻结** —— 分级函数签名以 docstring 形式在 commit 里显式写出，B/C 据此调用：

```python
def grade_health(
    *,
    systolic: int | None,
    diastolic: int | None,
    sleep_hours: float | None,
    mood_score: int | None,
    complaint: str | None,
) -> HealthGradeResult:
    """纯函数 · 无 I/O · 无 DB 依赖。
    返回分级结果（severity + rule_version + 各分量判定详情）。
    单分量达红即红（AC-21），多项并存取最高。"""
```

#### Step 2.2（60min）：校验 + 分级纯函数实现

**校验规则**：
1. 非数字/负值 → 无效，标记该字段为缺失
2. 睡眠 >24h → 无效，标缺失
3. 两分量均有时收缩压 > 舒张压 → 记录警告（健康数据口径自裁）
4. 连续两次同一字段解析失败 → 最终标缺失不阻断（由调用方决定是否追问）

**分级逻辑**（按最高优先规则匹配）：
1. **红（AC-10/21）**：收缩压 ≥180 OR 收缩压 ≤90 OR 舒张压 ≥120 OR 舒张压 ≤60
2. **红（AC-10 单分量）**：只要一个分量达红 → 立即红，不等另一分量（AC-21 核心）
3. **黄（AC-09）**：收缩压 140–179 OR 91–139 但舒张压 90–119 OR 睡眠 <5h OR mood ≤2 OR 主诉非急症 OR 任一字段缺失
4. **绿**：收缩压 90–139 且 舒张压 60–89 且 睡眠 5–12h 且 mood 3–5 且 无缺失

> 多项并存取最高（红 > 黄 > 绿）；AC-22 黄升红：**同一 event_id 下补录 → severity 升级 → 原 risk_events.status 保持 open 不新建第二事件（通知升级见 A3）**，工单 severity 是建单快照不随升级漂移（TD 明确）。

#### Step 2.3（60min）：health_service 写库入口

```python
async def create_health_record(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    source: str,
    systolic: int | None,
    diastolic: int | None,
    sleep_hours: float | None,
    mood_score: int | None,
    mood_note: str | None,
    complaint: str | None,
    operator_id: uuid.UUID | None,
    graph_thread_id: str | None = None,
) -> HealthRecordRef:
    """写库入口：校验→分级→写 health_records→按需建 risk_events + work_order。
    越权保护：source=staff_* 时 operator_id 必填 且 operator 必须是该长者的 primary_care 护工。
    """
```

**版本链逻辑**（Q-1 主理人裁决：superseded_reason 服务端固定枚举写入，前端请求体不传）：
- version 从 1 起，补录 version+1
- superseded_by 指向上一版本
- 最新版本不设 superseded_by（或 superseded_by=NULL）
- **superseded_reason**（主理人澄清 2026-11-11）：纠错写 `review_correction`、代填补录写 `backfill`、正常采集或最新版本写 `NULL`。纯服务端写入，前端 review 端点请求体不传该值（主理人明确）

**黄升红逻辑（AC-22）**：
- 同一 elder_id + 当日已有未关闭黄色 risk_event → 补录数据重算分级 → 若从黄升红：原 risk_event.severity=red + 原事件 status 保持 open（不新建第二事件）+ 立即发红色双通知
- **不重复建单**（TD 明确：建单快照 severity 不随后同步）

#### Step 2.4（60min）：六类分级单测

```python
def test_grade_red_systolic_only_182():
    """AC-21：单分量达红（182/null）→ red 立即，不等另一分量"""

def test_grade_yellow_165_85():
    """AC-09：165/85 → yellow"""

def test_grade_green_120_80():
    """正常值 → green"""

def test_grade_invalid_data_twice_fail():
    """AC-11：连续两次无效 → 标缺失继续 → 生成 yellow 复核"""

def test_grade_missing_field_yellow():
    """任一字段缺失 → yellow"""

def test_upgrade_yellow_to_red_huang_sheng_hong():
    """AC-22：黄档未关闭补录 185 → 升级红、不重复建单"""

def test_staff_proxy_unauthorized_403():
    """AC-32：非负责护工代填 → 403 + audit_logs"""
```

#### Step 2.5（30min）：复核入口（C1 调用）

```python
async def review_health_record(
    session: AsyncSession,
    *,
    record_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    action: Literal["confirm", "correct"],
    corrected_values: dict | None = None,
) -> HealthRecordRef:
    """黄档护工可确认/纠错；红档需医生确认（scope filter 在外层 API 做）。
    纠错→新版本（version+1、superseded_by 指旧版本、superseded_reason="review_correction"、review_note=前端传入值）。
    前端请求体**不传** superseded_reason，纯服务端写入（主理人澄清 2026-11-11）。"""
```

**自检命令**：

```bash
uv run pytest tests/test_health_service*.py -q       # 六类分级 + 黄升红 + 越权
# 手工复跑 AC-22：先建黄档 → 补 185 → 查 risk_events 仍是一条且 severity=red
psql -d silvermate -c "SELECT event_id, severity FROM risk_events WHERE elder_id=... ORDER BY created_at DESC LIMIT 5"
```

**验收标准**：
- ✅ 分级纯函数无 DB 依赖（单测不 mock session 即可跑）
- ✅ 六类分级单测全绿（AC-09/10/11/21/22 全覆盖）
- ✅ 黄升红不重复建单（risk_events 行数不变、severity 从 yellow → red）
- ✅ 代填越权 403 + audit_logs 有记录
- ✅ 12:00 前签名冻结、通知 B/C（X1）

---

### 2.4 A3：通知双轨 + 2 分钟升级 + 状态机（2.0h）

**前置**：A2 签名冻结；**11:30 与 C 对齐 X2（dedupe_key 口径）**。

#### Step 3.1（30min）：X2 对齐 dedupe_key 口径

C2 日报侧 dedupe_key 格式约定（双方对齐后各自落码）：
- **日报更新通知**（C2 触发）：`daily_report:<report_id>:update`
- **红色事件通知**（A3 触发）：`risk_event:<event_id>:staff`（护工侧） / `risk_event:<event_id>:guardian`（家属侧）
- **红色升级通知**（2 分钟后）：`risk_event:<event_id>:escalated`
- **通知重试**：同一 dedupe_key 只发一次（pending 内部重试不改 dedupe_key）

#### Step 3.2（60min）：notify_service 增量扩展

**Staff 侧通知入口**（新增）：

```python
async def send_staff_notifications(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    ntype: str,
    payload: dict,
    dedupe_key: str | None = None,
    channel: str | None = None,
) -> list[NotificationRef]:
    """给负责护工（primary_care relation）发通知；找不到护工返回空列表不报错。"""
```

**状态机驱动**（新增状态扫描逻辑）：

```python
async def mark_notification_delivered(session: AsyncSession, *, notification_id: uuid.UUID) -> None:
    """客户端回执 → pending → delivered（AC-23）。"""

async def acknowledge_notification(session: AsyncSession, *, notification_id: uuid.UUID, *, staff_id: uuid.UUID) -> None:
    """人工确认 → delivered → acknowledged（AC-23）。"""

async def escalate_red_event(session: AsyncSession, *, event_id: uuid.UUID) -> None:
    """2 分钟无人确认 → risk_events.status=escalated + 通知管理员（AC-12）。"""
    # 升级计时从 risk_events.created_at 起，不因通知失败暂停
    # 只改 risk_events.status，不建第二个 risk_event / work_order

async def scan_pending_notifications(session: AsyncSession, *, now: datetime) -> list[Notification]:
    """pending 每 30s 重试、3 次失败 → failed。返回需重试列表供 scheduler 驱动。"""

async def scan_escalations(session: AsyncSession, *, now: datetime, *, window_minutes: int = 2) -> list[uuid.UUID]:
    """扫描 open 且 created_at < now-2min 且无 acknowledged_at 的红色 risk_events。
    返回需升级的 event_id 列表供 scheduler 驱动。"""
```

**红色事件双通知（AC-38）**：health_service 分级判红后调：
1. `send_staff_notifications` → 负责护工
2. `send_guardian_notifications` → 所有 active 家属

#### Step 3.3（30min）：通知状态机五态单测

```python
def test_notification_lifecycle_success():
    """AC-23 成功链路：generated → pending → delivered → acknowledged 依次执行。"""

def test_notification_retry_fail_three_times():
    """pending 重试 3 次失败 → status=failed（失败终态）。"""

def test_red_event_dual_notification():
    """AC-38：红色事件 → 护工 + 家属各一条 generated。"""

def test_red_escalation_no_second_event():
    """AC-12：2 分钟无人确认 → 原事件 escalated、无第二 risk_event。"""

def test_dedupe_key_prevents_duplicate_notifications():
    """重复触发 → 只有一条通知记录，created=False。"""
```

#### Step 3.4（20min）：15:30 X5 演练准备

**X5 演练脚本（AC-12/23/38）**：
1. 造红事件：185/90 → risk_events(severity=red, status=open)
2. 立即查 notifications：应看到两条 generated（staff + guardian）
3. 等待 2 分钟（或直接调 scan_escalations 加速）→ risk_events.status=escalated
4. 查 notifications：仍只有两条（无 escalation 新事件，升级通知是**第三条** escalation 类型）
5. 模拟回执 → pending → delivered；护工点确认 → acknowledged

**自检命令**：

```bash
uv run pytest tests/test_notification*.py -q
# 手工 SQL 核对
psql -d silvermate -c "SELECT notification_id, target_type, status, dedupe_key FROM notifications ORDER BY created_at DESC LIMIT 10"
psql -d silvermate -c "SELECT event_id, severity, status FROM risk_events WHERE elder_id=... ORDER BY created_at DESC LIMIT 3"
```

**验收标准**：
- ✅ AC-38：红色事件双通知记录齐（staff + guardian 各一条）
- ✅ AC-12：2 分钟升级后 risk_events.status=escalated 且无第二事件
- ✅ AC-23：generated→pending→delivered→acknowledged 四态依次可见 + **pending 重试 3 次失败→failed**（五态含失败终态）
- ✅ 幂等：同 dedupe_key 不重复发

---

### 2.5 A5：规则直连 R-01~R-08（1.5h）

**前置**：B1 冻结 X8 戏曲链四方契约（media_url 稳定路径 + opera Tool schema + 规则动作→Tool/服务映射表）；**B4 opera Tool 注册就绪**（骨架先行，联调段等 B4）。

#### Step 4.1（B1 前先行 · 30min）：骨架先行（不碰 Tool，改 action + 降级逻辑）

**只改 rules.py 的 R-01~R-08 八规则**：
1. 把每条 `action="agent"` 改为**按 X8 冻结映射表**填 action + tool_name
2. 把 `reason=REASON_CAPABILITY_NOT_READY` 移除（不再降级）
3. `capability_not_ready` 只保留 R-10（用药查询），不动 ASR/分类阈值

**⚠️ 硬约束**：rules.py 仍是纯函数层——不 import ToolExecutor、不调 API。action="tool" 后的实际执行由**上层 turn_service 第 4 步**（既有流程）负责：命中 RuleHit.action="tool" → ToolExecutor.call(RuleHit.tool_name, RuleHit.tool_args)。

**X8 冻结口径（B1 10:00 发布后 A5 按此落码）**：

| 规则 | action 改为什么 | tool_name | 调用方式 |
| --- | --- | --- | --- |
| R-01 暂停 | tool | play_opera | `ToolExecutor.call("play_opera", {"action":"pause"})` |
| R-02 继续 | tool | play_opera | `ToolExecutor.call("play_opera", {"action":"resume"})` |
| R-03 停止 | tool | play_opera | `ToolExecutor.call("play_opera", {"action":"stop"})` |
| R-05 换一段 | tool | play_opera | `ToolExecutor.call("play_opera", {"action":"next"})` |
| R-06 调大音量 | tool | play_opera | `ToolExecutor.call("play_opera", {"action":"volume_up"})` |
| R-07 调小音量 | tool | play_opera | `ToolExecutor.call("play_opera", {"action":"volume_down"})` |
| R-08 重复一遍 | tool | play_opera | `ToolExecutor.call("play_opera", {"action":"repeat"})` |

> R-04 打断播报：intent=chat、action=agent（保持，它不属于戏曲域，D5 评测门禁依赖它走 Agent 链路）

#### Step 4.2（60min）：16:00 CP4 联调段（等 B4 opera Tool + C4 media_service）

**AC-37 端到端验收脚本**：

```
前置：pad 正在播放戏曲（mock 素材），media_history 有记录

1. 说「换一段」→ 规则层命中 R-05 → ToolExecutor.call("play_opera", {"action":"next"})
   → decision_logs 新行 route_layer="rule"、model_version="ruleset:r1"
2. 依次「大声点」「小声点」「重复一遍」→ 各层命中留痕
3. 查 decision_logs：四条新行 route_layer 全部是 rule、latency_ms 全部 ≤200
4. 查 media_history：播放记录写完播率（P1，有就行）
5. 说「我想听一个不存在的戏曲《蓝色多瑙河》」→ 规则层不命中任何戏曲播放控制（FR-CON-04）
   → 天气仍然可问（FR-CON-04 要求：请求不存在剧目不影响天气）
```

**联调阻塞预案**：若 B4 opera Tool 16:00 未就绪→**A5 骨架必须当日完成**（rules.py 纯函数改完、单测绿），联调段顺延 Day 9，commit 说明写明。

**测试增量**：
```python
def test_rule_r05_next_opera_direct_tool():
    """R-05 换一段：action=tool、tool_name=play_opera、args={"action":"next"}。"""

def test_rule_r01_pause_direct_tool():
    """R-01 暂停：同上模式。"""

def test_rule_control_rules_not_weather_blocked():
    """FR-CON-04：规则直连不影响 R-09 天气。"""

def test_no_false_positive_for_nonexistent_opera():
    """请求不存在曲目 → 规则层不命中播放控制。"""
```

**自检命令**：

```bash
uv run pytest tests/test_rules_opera*.py -q
uv run pytest tests/test_router_rules.py -q              # 既有规则测试不能回退
# 手工 SQL 核对 AC-37
psql -d silvermate -c "SELECT trace_id, route_layer, latency_ms FROM decision_logs WHERE intent='opera' AND route_layer='rule' ORDER BY created_at DESC LIMIT 10"
```

**验收标准**：
- ✅ AC-37：规则层直连执行换一段/大声点/小声点/重复一遍，decision_logs 留痕 route_layer=rule，路由耗时 ≤200ms
- ✅ FR-CON-04：不存在曲目 → 提示 + 天气仍可用
- ✅ 每动作经 ToolExecutor（G2 权限照走，不在 rules.py 内直接写库）
- ✅ 既有规则测试 test_router_rules 不回退

---

### 2.6 H12：门禁收口（0.5h · 牵头）

**时间**：17:30–18:00

**四件套必须全绿**：

```bash
# 1. pytest 全绿（dev-A 新增 + 全员）
uv run pytest -q

# 2. ruff 静态
uv run ruff check app tests && uv run ruff format --check app tests

# 3. 迁移可逆
uv run alembic downgrade -1 && uv run alembic upgrade head   # 0017
uv run alembic downgrade -2 && uv run alembic upgrade head   # 0017+0018 各一遍

# 4. 分层硬约束（A 线自检）
grep -rn "session\.add(\|from app.models" app/scheduler/     # 零命中（配合 B3）
grep -n "route_layer.*rule\|route_layer=\"rule\"" docs/...  # 或 SQL 查 decision_logs 有新行
```

**dev-A 必须收集的证据**：
1. 六类分级单测通过率（test_health_service* -v 输出）
2. AC-22 黄升红 SQL 证据
3. AC-38 双通知 + AC-12 升级 SQL 证据
4. AC-37 decision_logs 留痕（route_layer=rule 且 latency ≤200ms）
5. 迁移 downgrade/upgrade 日志

**交付**：所有证据附在 commit message 里；或汇总进 `docs/plans/day08/` 的执行记录。

## 3. 跨成员对接点（dev-A 消费 / 提供）

| 对接点 | 类型 | 提供方→消费方 | 接口内容 | dev-A 动作 |
| --- | --- | --- | --- | --- |
| **X1**（12:00） | A→B,C | dev-A→dev-B,C | 分级纯函数签名 + HealthRecord ORM 字段 | A2 commit 时在 docstring 冻结 + IM 通知 |
| **X2**（11:30） | C→A | dev-C→dev-A | dedupe_key 构造规则 + 日报更新通知口径 | A3 调 IM 对齐后在 commit 里写死 |
| **X5**（15:30） | 全员联调 | B→A→C→D | 红色事件端到端演练 | A 负责通知侧 SQL 证据（2 条 generated + 升级后 escalated） |
| **X7**（15:30） | A→D | dev-A→dev-D | 通知四态 generated/pending/delivered/acknowledged/failed | A3 commit 后 IM 通知 D 按此枚举开发 |
| **X8**（B1 10:00 发布） | B→A,C,D | dev-B→dev-A,C,D | media_url 稳定路径 + opera Tool schema + 规则映射表 | A5 骨架先行；B1 发布后补全 action/tool_name |
| **X9**（14:00） | B→D | dev-B→dev-D | 路由基线（B2 桩测试绿后 D 启动 D5） | A 不直接参与，但 A5 规则直连后 decision_logs 会多一层 rule 留痕，影响 D 的归因报告分层 |

## 4. 写作用域（dev-A 唯一写域）

**可写**（AGENTS §8 硬约束，违反即返工）：

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `alembic/versions/0017_day8_health_reports.py` | 新建 | 两表迁移，不碰已合入迁移 |
| `alembic/versions/0018_day8_media.py` | 新建 | 戏曲两表迁移 |
| `app/models/health.py` | 新建 | HealthRecord ORM |
| `app/models/report.py` | 新建 | DailyReport ORM |
| `app/services/health_service.py` | 新建 | 校验 + 分级 + 版本链 + 代填 + 复核 |
| `app/services/notify_service.py` | **增量** | 新增 staff 侧通知 + **状态机五态驱动**（generated/pending/delivered/acknowledged + failed 终态） + 2 分钟升级扫描 + 30s 重试扫描（retry_count≥3→failed）；不删改既有监护人家族侧代码 |
| `app/router/rules.py` | **增量** | 只改 R-01~R-08 八规则的 action/tool_name/reason；不碰 R-04/09/10、不碰阈值/正则/ASR |
| `tests/test_health_service*.py` | 新建 | 六类分级 + 黄升红 + 越权 |
| `tests/test_notification*.py` | 新建 | 状态机五态（含 failed）+ 双通知 + 升级 + 幂等 |
| `tests/test_rules_opera*.py` | 新建 | 规则直连 + FR-CON-04 |

**不可写（硬禁区）**：
- `app/graphs/`（归 B2）
- `app/scheduler/`（归 B3）
- `app/api/health_*.py` / `app/api/reports.py`（归 C1/D1）
- `app/services/report_service.py` / `media_service.py` / `memory_service.py`（归 C2/C4/B5）
- `app/agents/tool_registry.py` / `tool_executor.py`（归 B4）
- `app/router/safety.py`（不动阈值）
- 前端任何文件
- 任何已合入的 alembic/versions/0001–0016

## 5. 风险预案

| # | 风险 | 影响 | 概率 | 缓解/回退 |
| --- | --- | --- | --- | --- |
| R1 | H0–H16 合计 44h > 32h 容量 | dev-A 联调段可能阻塞 | 中 | dev-A 包内**无先裁项**；联调段（A5 CP4 16:00）若阻塞→骨架必须当日完成，联调顺延 Day 9 |
| R2 | 迁移 0017/0018 autogenerate 漂移 | CHECK 或索引缺失 | 低 | **手写 create_table**（0016 先例），不用 autogenerate；迁移写完后立即 downgrade→upgrade 验证 |
| R3 | health_service 分级阈值与 PRD 口径偏差 | AC-09/10/21 验收失败 | 中 | **阈值硬编码在常量区**（不在 prompt）；每个阈值常量旁写 PRD 行号注释；分级纯函数先写单测后写服务 |
| R4 | X8 戏曲链口径未冻结导致 A5 映射不清 | 16:00 CP4 联调失败 | 中 | A5 骨架先行（action/tool_name 用占位）；B1 10:00 发布后 5min 内替换占位；若仍阻塞 → 骨架当日交付、联调顺延 |
| R5 | 黄升红 AC-22 跨 risk_events/work_orders 两表一致性 | 工单 severity 漂移 | 低 | severity 是**建单快照不随同步**（TD 明确）；升级只改 risk_events.status + severity，工单保持原值；单测覆盖完整升级路径 |
| R6 | notify_service 增量后既有监护人侧回归 | family 通知中心不可用 | 低 | 增量**全部加新函数**、不改既有 send_guardian_notifications 签名与实现；单测先监护人家族侧全过再开发 staff 侧 |
| R7 | rules.py 改 action=tool 后 turn_service 第 4 步执行异常 | 规则层不触发 Tool | 低 | 改之前先读 turn_service 现有 ToolExecutor 调用逻辑；R-09 天气已走同一模式，是先例；联调段有 B4 现场排 |

## 6. 当日时间线（精确到时段）

```
09:00 ────────── A1 两表迁移 0017 开工（与 B1 契约冻结并行）
09:00–11:00    A1 迁移 + ORM 完成（2h）   ✅ 11:00 前交付
11:00–11:30    A4 戏曲迁移 0018 顺做（0.5h） ✅ 11:30 前交付
11:30           与 C 对齐 X2 dedupe_key 口径
11:00–12:00    A2 health_service 开发 + 12:00 前分级签名冻结（X1 通知 B/C）
12:00           ⭐ CP1（CheckPoint 1）— A1+A4 完成 + X1 签名冻结 + X2 对齐
12:00–14:30    A2 完整开发（校验+分级+版本链+代填+复核+六类单测）2.5h 剩余
14:30           A2 完整交付
14:30–16:00    A3 通知双轨 + 2 分钟升级 + 状态机 收口（2h）
                 ├── 14:30–15:00 A5 骨架先行（rules.py R-01~R-08 改 action/tool_name）
                 └── 15:30 ⭐ X5 红色事件端到端演练（A 通知侧 SQL 证据）
16:00           ⭐ CP4 — 戏曲端到端演练 AC-37（等 B4/C4 就绪）
                 ├── A5 联调段：rules.py → ToolExecutor → opera Tool → 路由耗时 ≤200ms
                 ├── B4 opera Tool 注册
                 ├── C4 media_service 就绪
                 └── D4 pad 播放就绪
                 ❗ 联调阻塞 → A5 骨架必须当日交付（rules.py 纯函数），联调顺延 Day 9
16:00–17:30    A3/A5 收尾 + 补测试 + 与 B3 核对 health_jobs 调用接口
17:30–18:00    ⭐ H12 门禁四件套（A 牵头）
                 ├── pytest -q 全绿
                 ├── ruff check + format
                 ├── alembic downgrade -1 && upgrade head
                 ├── 分层硬约束 grep
                 └── dev-A 证据回填 commit message
```

## 7. 对接矩阵（dev-A 视角）

| 时刻 | 事件 | dev-A 动作 | dev-A 依赖谁 | dev-A 给什么 |
| --- | --- | --- | --- | --- |
| 09:15 | 全员站会 | 确认迁移写锁归属 A、确认 tool_registry 写锁已归 B4（释放） | — | A1/A4 计划声明 |
| 10:00 | B1 发布 X8 冻结口径 | A5 骨架占位替换为冻结映射表 | B1 | — |
| 11:30 | X2 dedupe_key 对齐 | 与 C IM 对齐后落码 | dev-C（C2） | — |
| 12:00 | X1 分级签名冻结 | health_service commit 后 IM 通知 B/C | — | 分级纯函数 docstring |
| 14:30 | CP2 C1/D1 联调通过 | 无 dev-A 动作（对接 C/D） | — | — |
| 15:30 | X5 红色事件端到端演练 | 通知侧 SQL 证据 | dev-B（图触发） | 双通知记录 + escalated 状态 |
| 15:30 | X7 通知四态 | A3 commit 后通知 D 按此枚举开发 | — | 状态机枚举值 |
| 16:00 | CP4 戏曲端到端演练 | A5 规则直连联调 | dev-B（B4 Tool）、dev-C（C4 服务） | decision_logs 留痕 + 路由耗时 |
| 17:30 | H12 门禁 | 牵头全员自检 | — | pytest/ruff/alembic 验证 + 证据回填 |

---

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.4 | 2026-11-11 | 主理人澄清两则同步：① **Q-2 ≠ superseded_reason**：Q-2 针对 workorders.escalated 徽标（服务端推 risk_events.acknowledged_at IS NULL ∧ created_at+2min ≤ now），属 dev-D 写域，dev-A 完全不碰；superseded_reason 是 Q-1 补列，**服务端固定枚举写入**（纠错→review_correction、代填→backfill、正常采集→NULL），前端 review 端点请求体不传该值（主理人明确）。② **X8 映射表位置**：照护台契约无 §6.3（§6 是变更记录），冻结方仍 dev-B B1 任务，dev-A 预写骨架映射表风险预案 R4 保持不变 |
| v1.3 | 2026-11-11 | X2 dedupe_key 最终口径冻结（dev-A→dev-C 10:xx 对齐）：dev-C 草案 `health_red:{elder_id}:{YYYY-MM-DD}` 粒度过粗（同日长者可能两次独立红档各自独立 risk_event，按日期去重会吞第二条）；dev-A 三段式「领域对象 + 对象 id + 动作」直接落码：①红色事件·护工 `risk_event:{event_id}:staff`、②红色事件·家属 `risk_event:{event_id}:guardian`（复用 notify_service 既有 `dedupe_key:guardian_id` 逐家属幂等）、③红色升级 `risk_event:{event_id}:escalated`；AC-12「不建第二 risk_event」与 event_id 锚点天然对齐；notifications 表 `(dedupe_key, target_id)` 唯一索引（0011）兜底；dev-C C2 日报侧建议对齐同款三段式 `daily_report:{report_id}:update` |
| v1.2 | 2026-11-11 | X7 状态机五态对齐：dev-C 转达 09:15 契约评审会通知状态枚举对齐议题——契约 5 态（generated/pending/delivered/acknowledged/failed）vs 任务书 X7 写 4 态（无 failed）。dev-A 实现结论：pending 重试 3 次失败后置 `failed`（对齐 notifications 表 0011 CHECK 五值），任务书 4 态是写法疏漏、dev-C 按冻结契约 5 态开发。计划同步修正：状态机描述全部从「四态」→「五态（含 failed 终态）」，单测新增 `test_notification_retry_fail_three_times` |
| v1.1 | 2026-11-11 | Q-1 裁决修订：dev-C 转达主理人裁决 **0017 迁移补两列**（原 v1.0 计划补 review_note 一列、superseded_reason 留推导，现改为两列都补）：① health_records 字段清单 18→19 列（新增 superseded_reason text 可空）；② CHECK 约束表新增 superseded_reason 枚举（review_correction/backfill 可空，对齐契约 v0.1）；③ ORM 注释列数同步；④ TD 走修订记录补列口径 |
| v1.0 | 2026-11-11 | 首版：基于 dev-A 任务书 v1.1 + Day 8 总计划 v1.2 + PRD/TD + 代码现状实测编制；六类分级用例、A2 签名冻结（X1）、X2 dedupe_key 对齐、CP1/CP4 双关键检查点、H12 牵头门禁；超载无包内先裁项、联调阻塞顺延 Day 9 |
