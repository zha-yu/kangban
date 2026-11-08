# Day 5 · dev-D（console / family 双端）工作推进计划

> 状态：草稿　｜　版本：v1.0　｜　日期：2026-11-07
> 关联：docs/team_tasks/day05/dev-d-前端console与family.md（任务唯一基线）｜docs/team_tasks/day05/dag-项目工作流总览与联调节点.md｜docs/plans/day05/day-05-延迟基线与护栏.md v1.3
> 上游依据：PRD FR-ADM-01/06、§3.1 权限矩阵、§6.1 Day 5 完成定义（验收）｜技术设计文档 §8.2/§8.3 Day 5、§3 表定义、§2.1 目录（实现）｜docs/前端页面方案.md §3.2/§3.3/§3.4/§5｜docs/接口契约_身份与权限.md v1.8（已冻结）｜docs/接口契约_家属端数据.md v1.0（草案，待冻结）
> 变更记录见文末

---

## 0. 本轮性质声明与红线

**本文件是计划件，不是执行件。** 本轮交付仅两个文档：本计划 + `docs/plans/day05/d-DAG.md`（独立执行路径文件），另按 `AGENTS.md §10.3` 同步登记 `docs/plans/README.md` 索引。

- **本轮不编写任何业务代码、功能实现代码，不修改任何现有代码文件**（`app/**`、`frontend/**`、`tests/**`、`alembic/**` 一律不动）。
- 下文所有「步骤 / 命令 / 改哪些文件」均为**后续执行阶段的指令**，不是本轮动作。
- 事实口径只取 PRD（验收）与技术设计文档（实现）；大纲「十、迭代计划」只取「Day 5 做哪几件事、当天产出物、先后顺序」，不取其中任何表名、字段、接口、阈值、页面口径（AGENTS 前置铁律 1/2/6）。
- 计划落位遵循 `AGENTS.md §10.1`：按天个人实施计划放 `docs/plans/day05/` 子目录；`docs/team_tasks/` 只回答「谁做/前置/写作用域/验收」，本计划只回答「怎么做/分几步/交付物与自检」，不越界改任务卡正文。

---

## 1. 项目认知（通读结论）

### 1.1 产品定位与三端

面向 CCRC 康养社区长者卧室的 **3D 数字人陪伴系统**，浏览器页面（模拟平板）上的 VRM 数字人 + WebRTC 全双工语音，承担情感陪伴、用药提醒、健康采集、戏曲/天气检索、人工告警五类职责。**不做医疗诊断、不替代 SOS 紧急呼叫、除天气 API 外全部外部系统走 Mock**（PRD §1.4/§1.5/§2.5；AGENTS §0/§4）。

| 端 | 入口 | 形态 | 本计划相关性 |
| --- | --- | --- | --- |
| `pad` | `frontend/index.html` → `src/pad` | 1920×1080 长者端，React + Tailwind + three-vrm | **不碰**（dev-C 负责） |
| `console` | `frontend/console.html` → `src/console` | 护工 / 医生 / 管理员按角色分流，React + 原生 CSS + `var()` token | **本计划主战场 D-1 / D-3** |
| `family` | `frontend/family.html` → `src/family` | 375px 移动优先，React + 原生 CSS + `var()` token | **本计划主战场 D-2** |

三端共用一套 Vite MPA 配置；**pad 的 `@pixiv/three-vrm` 不得进入 console/family 构建产物**（AGENTS §2 分层硬约束 4）。

### 1.2 技术架构与依赖方向

六层：前端层 → 接入层（nginx / REST / WebSocket / aiortc + coturn）→ 智能层（安全一票否决 → L0 规则层 ≤10 条 ≤200ms → L1 Laya 四信号 confidence≥0.85 → L2 主控 Agent ReAct）→ 能力层（流式 ASR/TTS、CAM++ 声纹、APScheduler）→ 数据层（PostgreSQL + pgvector，26 张业务表分 7 批迁移，LangGraph checkpoint 独立 `langgraph` schema、独立连接池；Redis 短期会话）→ 外部与 Mock（Weather MCP 唯一真实、Mock Operations MCP、Mock 家属通知、Langfuse + `/metrics`）。全链路统一 `trace_id`。

**分层硬约束**：`app/services/` 是唯一写库层；依赖单向 `api | audio | scheduler | graphs → services → models`；任何模块不得横向 import；`app/api/` 只做校验 + 范围过滤 + 调 services。本计划所有工作均在 `frontend/src/console`、`frontend/src/family` 内，**不触碰后端分层**。

### 1.3 需求边界（dev-D 直接相关）

| 依据 | 边界 |
| --- | --- |
| PRD §3.1 权限矩阵 | 护工查看药单**只读**；医生增/改/停/查药单；管理员只读药单、维护演示数据与配置；家属**只能查看健康日报与需关注事件摘要** |
| 接口契约_身份与权限 §6（已冻结 v1.8） | E12 员工维护、E13 家属绑定、E15 家属建号：仅 admin 200，其余四主体 403；E16/E17 设备建档/列表：admin + care_worker 200；E18 房间床位：员工 200、家属 403；E19 入床：admin + care_worker |
| 裁决 A-01（身份契约 §9） | **家属读任何长者档案一律 403 + 审计**；家属端展示名走 E3 `scope_elder_ids` / F0 返回字段，**不开档案接口** |
| 前端页面方案 §4.4 | 不在前端做健康分级；不静默猜人；不写业务事实；SOS 不给撤销入口 |
| 前端页面方案 §5 | console/family **不引 MUI 等组件库**、不引深色模式、不让 three-vrm 进其他入口、不用信号色做按钮底 |
| 非目标（PRD §1.4） | 真实 HIS / 微信 / 短信 / 支付、医疗诊断、生产级声纹、真实 Web Push（P1） |

### 1.4 Day 5 核心目标与验收数值

Day 5 主题为**延迟预算与 Agent 护栏**（PRD §6.1 Day 5 行；TD §8.2/§8.3）：

- 连问 20 次「今天天气」**≥19 次在 1.5 秒内开口**（PRD §5.3：简单指令 ≤1.5s，20 次中 ≥19 次；正式测量前试运行 3 次不计入）；
- 三档超时各有可读降级提示（TD §8.3：**ASR >2s、LLM 首 token >3s、TTS 首包 >1s**）；
- 同轮**第 9 次工具调用被拒**并告知用户（FR-DLG-10，`MAX_TOOL_CALLS_PER_TURN = 8`；同一 Tool 1 分钟内第 3 次熔断）；
- `/metrics` 埋点可用（FR-TC-08）；核心包 `pytest --cov` **≥70%**（FR-TC-06：核心 Tool 与权限场景 100%、接口 ≥70%）；
- 里程碑 **M5.4** Iter2 演示一次通过 + tag `iter2-demo`（`dag` §2.2），其中 **D-1 admin 页走查是 M5.4 的硬依赖**（`D1 --> M54`）。

> **dev-D 定位**：不承担延迟数字测量与 guardrails（属 dev-A/B/C），但 **D-1 是 M5.4 演示路径上的唯一前端交付物**，且 D-2/D-3 是 Day 8 三端后台对接的**前置契约对齐**。本日 dev-D 的完成定义见 §8。

### 1.5 约束条件（硬约束清单）

| 类别 | 约束 | 出处 |
| --- | --- | --- |
| 样式 | 原生 CSS，一律 `var()` 引用 `src/theme/tokens.css`；**零硬编码十六进制色值**；各入口 `index.css` 首行 `@import "tailwindcss"` 再 `@import "../theme/tokens.css"` | 前端页面方案 §1/§3/§5；dev-d 卡 |
| 样式 | 浅色同族侧栏 + 主色 8% 底选中态，**不用深侧栏**；徽标 `role="status"`、`aria-live="polite"`；console 表格行高 ≥40px、正文 14–16px；family **375px 无横向滚动、正文 ≥16px**；触控 ≥48×48px | 前端页面方案 §3.2/§4.1/§4.2；PRD FR-UI-02 |
| 构建 | `pnpm --dir frontend build`（`tsc --noEmit && vite build`）三入口全过；three-vrm 不进 console 产物 | dev-d 卡；AGENTS §2 |
| 工程 | console 与 family **独立同构，禁止互相 import**，也不 import `src/pad/**`；localStorage key 带入口前缀（`kb_console_` / `kb_family_`） | `console/api.ts:8`、`family/api.ts:8`；task-05 |
| 契约 | 《接口契约_家属端数据》为**草案 v1.0 待冻结**；**契约冻结评审前不擅自定稿**；差异只登记不私改 | dev-d 卡 N4；`dag` §2.3；契约 §0.4 |
| 能力唯一性 | **不新增第二种同类能力实现**；已有端点封装必须复用，不另造一套 | AGENTS §4 禁区 8 |
| 身份 | `elder_id` 不前端臆造；关系判定走 `GET /api/sessions/current`（E3 `scope_elder_ids`）/ F0 返回 | dev-d 卡 D-2 ③；AGENTS §4 禁区 9 |
| 隐私 | 不把密钥、真实长者数据写进代码/日志/夹具；403 越权必审计（服务端）；错误文案不回显 `trace_id` 与内部细节 | AGENTS §4 禁区 2/3；身份契约 §7 |
| 红线 | 不改后端、不改已合入迁移、不改基线文档正文（要改须同提交补「修订记录」）、不擅自引新框架 | AGENTS §4；dev-d 卡 D-3 ③ |

### 1.6 已登记的文档冲突与处置（本计划不重新裁决）

| # | 冲突 | 处置 |
| --- | --- | --- |
| C1 | TD §1.1/§2.1 写 console =「React + MUI」；PRD §2.1 v2.7、前端页面方案 §3/§5 v1.0 裁决「纯 React + 原生 CSS，不引组件库」 | 按已裁决口径执行纯 React + 原生 CSS；**不引 MUI**（MUI v9 颜色运算不支持 CSS 变量，实测白屏）。登记为已知冲突，本计划不改基线正文 |
| C2 | TD §8.2/§8.4.1 把 console/family 归 **Day 8**；Day 5 计划 §0 D4③ / §4 A3 裁决 **admin 管理页提前** | 属主理人已批的提前项，按 dev-d 卡 D-1 执行；**不改 TD 排期正文**，只在计划与卡内留痕 |
| C3 | TD §1.1 写「24 张业务表」，TD §3 / ER §1.1 写 **26 张**（7+3+10+6）；Day5 裁决 D2 再新增第 27 张 `metrics` | 以 26 张为准，metrics 属 dev-A 自裁并须完成「文档留痕三件套」；**dev-D 不参与、不在前端引用该表** |
| C4 | PRD FR-HLT-06 / AC-32 仍写 `source=staff_report`/`resident`；PRD §4.6 与 ER §5.3 为四值枚举 `voice_scheduled/voice_active/staff_scheduled/staff_active` | D-3 字段对齐时**以 TD §3 / ER §5.3 四值枚举为准**，差异登记回传 dev-A，不自行选一个 |
| C5 | **dev-d 卡 D-2 写「环境开关」；《接口契约_家属端数据》§6 S-11 明写「`api.ts` 内 `USE_MOCK` 常量 + 内置数据，不进环境变量体系」** | 见 §3.4 决策 K1：默认按 S-11 单一常量开关实现；改环境变量须走契约 §6 变更流程 + 冻结评审，**dev-D 不得自行改** |
| C6 | 《接口契约_照护台业务数据.md》（dev-A A-4）落点未定：day05 主计划 §6 #6 仍为「待主理人定」 | 见 §3.1 W0 启动门槛：落点确认前 **D-3 只做现状盘点，不做字段定稿** |

### 1.7 代码与文档现状盘点（dev-D 直接相关，只读结论）

**后端已就绪（Day 2 / 2026-11-06 建号接口交付）**：`POST /api/staff`(E12)、`POST /api/guardians`(E15)、`POST /api/elders/{id}/guardians`(E13)、`GET /api/rooms`(E18)、`POST /api/devices`(E16)、`GET /api/devices`(E17)、`POST /api/elders/{id}/bed`(E19)、`POST /api/elders`(E5)、设备绑定 E9；`app/api/staff.py`、`guardians.py`、`rooms.py`、`devices.py`、`elders.py` 均已注册。**D-1 无后端阻塞**。

**后端未交付（Day 7/8 批次，本日不接）**：`/api/family/*` 5 端点全部未在 `app/api/__init__.py` 注册；`work_orders*`、`notifications`、`health_records`、`medication_*`、`daily_reports` 无 ORM / 无迁移（ORM 现 12 张表，迁移 0001–0005）。

**前端现状**：
- `console/App.tsx:116-120`：admin / guardian 一律 `PlaceholderHome`；`layout/AppLayout.tsx:34-39`：admin 四项（用户与角色 / 教学数据重置 / 人设配置 / 审计日志）全部 `enabled:false`。
- `console/elders-api.ts` 已封装 `fetchRooms / createDevice / fetchDevices / bindDevice / createElder / assignBed`（E18/E16/E17/E9/E5/E19），**其中包含一次性 `device_secret` 展示能力**（`EldersPage` 四步注册向导）→ D-1 必须复用，不重复造。
- `console/api.ts` 提供 `apiGet / apiGetWithStatus / apiPost`，`ApiErrorKind` 仅 `"invalid" | "network"`（**无 `forbidden`**）；`family/api.ts` 已有 `"forbidden"` 与 `SCOPE_FORBIDDEN_TEXT`。
- `console` 五个业务页全为前端 Mock：`workorders-api.ts` / `alerts-api.ts` / `health-api.ts` / `medication-api.ts` / `review-api.ts`；`MedicationEditPage.tsx:24`、`ReviewQueuePage.tsx:105` 有硬编码演示身份。
- `family/api.ts:236` `export const USE_MOCK = true` 恒真；类型 `BoundElder / DailyReportSummary / DailyReportDetail / FamilyNotification / MedicationStats / AttentionEvent` 与函数 `fetchBoundElders / fetchReports / fetchReport / fetchNotifications / markNotificationRead` 已按契约草案 v1.0 落地，真实分支已写好 5 个端点形状但后端未实现。
- **已知渲染退化**（本轮只登记、不修）：`MedicationEditPage` / `ReviewQueuePage` 用到 `.rv-mask`、`.rv-dialog`、`.btn-ghost`、`.wo-dialog-title` 等 `console/index.css` 未定义类；`console/index.css` 多处引用 `tokens.css` 未定义的 `--color-border` / `--radius-lg` / `--color-surface-soft` / `--color-danger` / `--color-primary`。**D-1 新增样式不得沿用这些未定义变量**。
- 前端**无测试框架、无 lint 脚本**（`frontend/package.json` scripts 仅 dev/build/preview），因此本计划的自检以构建门禁 + 浏览器走查 + 静态 grep + 录屏为准，不虚构单测命令。
- `docs/reviews/` 目录当前为空（本轮 `ls` 实测）；Glob 索引中的两篇评审稿为幽灵条目，实体位于**工作区外** `D:\Desktop\reviews\`（见 `.workbuddy/memory/2026-11-07.md`「文档实际路径纠正」）。项目内无此事实源，**本计划不引用其内容作为依据**；后续如确需引用，先搬运入项目并同步更新互引路径。

---

## 2. Day 5 全部既定任务清单梳理

### 2.1 任务来源与效力

| 来源 | 提供什么 | 本计划如何使用 |
| --- | --- | --- |
| `docs/康伴3D数字人_大纲.md` 课时 5.1–5.4 | **仅流程**：口型 → 端到端延迟测量 + 超时熔断 → Iter2 验收演示 → 修债补测试 + 死循环防护；当天产出物：延迟基线表、三档超时演示、嘴型对齐录屏、演示脚本、覆盖 ≥70%、guardrails、`iter2-demo` tag | 只取「今天做哪几件事 + 先后顺序」 |
| `docs/康伴3D数字人_PRD.md` §6.1 Day 5 行 / §5.3 / FR-DLG-10 / FR-TC-06/08 | **验收口径**（AC 与数值） | §1.4、§2 各任务量化验收 |
| `docs/康伴3D数字人_技术设计文档.md` §8.2/§8.3 | **实现口径**：`app/core/metrics.py`、`app/agents/guardrails.py`、`tests/`；Day 5 无新建表 | 判定 dev-A/B 的落地位置 |
| `docs/plans/day05/day-05-延迟基线与护栏.md` v1.3 | 主理人裁决 D1–D5、D5-1–D5-7 拆解、S0–S7 阶段、A1–A4 提前项 | §2.2 全量清单主干 |
| `docs/team_tasks/day05/` 四张 dev 卡 + `dag-…md` | 谁做、前置、写作用域、验收、N1–N6 联调节点 | §2.3 / §2.4 / §3 全部 |

> 口径偏差已由主理人裁决并留痕（大纲 5.1 viseme → 维持能量近似；大纲 metrics → 建第 27 张表；guardrails → 只做骨架 Day6 生效），本计划**不翻旧稿重新裁决**。

### 2.2 全量任务拆解表（D5-1 – D5-7，主线视角）

| # | 任务 | 前置依赖条件 | 任务间先后依赖 | 具体执行步骤 | 量化验收标准 |
| --- | --- | --- | --- | --- | --- |
| D5-1 | a0 设备密钥轮换溯源（非开发项，主理人协调） | A2/E 组 11-06 上午设备绑定测试记录 | **独立可先行**，不阻塞任何开发项；结论回填 day05/a0 卡 | ①问人定位操作者 ②排查绕过 admin 直写 `devices` 的脚本 ③复核 `_clear_binding` 审计落库（`audit_logs` 属 Day9，当前按身份契约 S-08 落结构化日志）④给结论或收口 | 能说清轮换来源（人或脚本）；unbind/bind 有可查记录；卡 `> 状态：` 更新；必要时按卡内待办 4 的 sha256 方法重置 |
| D5-2 | 口型同步（已达标项） | Day3/4 TTS 下行链路 | 无 | 不开发；仅在 README「已知文档冲突」登记大纲 5.1 与 PRD FR-3D-01 冲突及 D1 裁决 | 播放任一 TTS 可观察口型开合（现状满足，录屏留证） |
| D5-3 | 延迟基线：metrics 表 + 打点收口 | Day3–4 打点单点已具备（`core/metrics.py`、`audio/_observe.py`） | **先于 D5-4**；dev-A `A1→A2→A3` | ①文档留痕三件套（TD §3.6 / ER / README 冲突表 + 修订记录同提交）②ORM + `0006_day5_metrics.py` ③打点写表经 `app/services/metrics_service.py` ④SQL 复算 | 六段打点全覆盖（wakeup/connection/asr/route/llm_first_token/tts_first_packet/playback）；20 次数字可复算；无一条含转写全文或隐私字段；0001–0005 一字未改 |
| D5-4 | P50/P95 基线测量 + 三档超时熔断 | D5-3 完成；day04/task-02 对话主链冒烟通过 | 依赖 D5-3；`A2→C2`、`B2→C1`、`A3→C2` | ①跑 20 次固定样本 ②出 `day-05-延迟基线表.md` 入档（Day6 对照分母）③三档降级演示 | P50/P95 由 SQL 复算、附查询语句；ASR>2s / LLM>3s / TTS>1s 逐个人为制造，**无一次白屏或静默**；本地 FunASR 基线（O-1 首包 2219ms）单独归档、不美化 |
| D5-5 | 死循环护栏骨架 | 无（不依赖 Agent） | **与 D5-3 并行**；`B1→M5.3`；预留 N6 给 Day6 | ①`app/agents/guardrails.py`：`MAX_TOOL_CALLS_PER_TURN=8`、同 Tool 1min 第 3 次熔断、重复调用检测 ②假工具单测 | 第 9 次调用被拒并如实告知；1min 第 3 次熔断；窗口过期计数复位；`pytest tests/test_guardrails.py -q` 全绿；常量与 AGENTS §7 一致 |
| D5-6 | 修债补测试 | 现有 27 个 `test_*.py` | 与 D5-3/D5-5 并行 | 核心包覆盖摸底 → 补缺口 → 清已裁决 TODO | `uv run pytest --cov=app --cov-report=term-missing` 核心包 ≥70%；`uv run pytest -q` 全量绿 |
| D5-7 | Iter2 验收演示收尾 | D5-2、D5-4 完成；**dev-D D-1 完成** | 收尾依赖项；`M5.2 / C3 / C1 / D1 → M5.4` | ①5 分钟演示脚本入档 `day-05-演示脚本.md` ②照脚本演练 ③`git tag iter2-demo` | 演示一次通过不翻车（录屏）；基线表已入档；tag 指向演示版 commit |

### 2.3 四人分工子任务逐项拆解（14 项，含 dev-D 三项）

**dev-A · 后端观测与数据**（`dev-a-后端观测与数据.md`，状态 未开始）

| 子任务 | 前置依赖 | 任务间依赖 | 执行步骤 | 量化验收标准 |
| --- | --- | --- | --- | --- |
| A-1 metrics 表 | 无 | `A-1→A-2→A-3` 起点 | ORM + `0006_day5_metrics.py` + `app/services/metrics_service.py` + ER/TD 修订记录 | `alembic upgrade head` 成功且不触碰 0001–0005；字段 `trace_id / elder_id(可空) / stage 七值枚举 / duration_ms / source / created_at`；索引 `(trace_id)`、`(stage, created_at)` |
| A-2 打点收口进库 | A-1 | `A-2 -.N1.-> C-2` | **只改** `app/core/metrics.py` 与 `app/audio/_observe.py` 单点收口函数体；写库经 services | 六段打点齐；`api/audio/scheduler/graphs` 未直接写库；抽 3 个 `trace_id` 日志/库内双轨一致；`test_audio_*`、`test_core_metrics.py` 全绿 |
| A-3 P50/P95 SQL + 基线表 | A-2、C-2 测量完成（N1） | `A-3 -.-> C-2`；`→M5.2` | 出 `docs/plans/day05/day-05-延迟基线表.md` + SQL | P50/P95 可复算；附查询语句；本地 FunASR 基线单独归档 |
| A-4 console 五 Mock 页接口契约草案 | 无 | `A-4 -.N3.-> D-3`（**dev-D 外部硬依赖**） | 对照 TD §3 表+字段起草，逐行标注出处；标注「待冻结」 | 每个字段行能回答「出处是 TD 哪一行」；无凭空字段；落点按主理人裁定（新建独立契约文件 vs 并入现有文件，**当前未定**） |

**dev-B · 后端护栏与降级**（`dev-b-后端护栏与降级.md`，状态 未开始）

| 子任务 | 前置依赖 | 任务间依赖 | 执行步骤 | 量化验收标准 |
| --- | --- | --- | --- | --- |
| B-1 guardrails 骨架 + 单测 | 无 | `B-1→M5.3`、`B-1 -.N6.-> Day6` | `app/agents/guardrails.py` + `tests/test_guardrails.py` | `check(tool_name) → allow/deny(原因)` 冻结；第 9 次被拒、1min 第 3 次熔断、窗口复位；`pytest -q` 全绿 |
| B-2 服务端三档超时降级 | 现有音频链路（M3/M4 已通） | `B-2 -.N2.-> C-1` | 超时降级实现 + `degrade` 事件常量 + 补测；阈值走 Settings 常量 | WS 下发 `{"stage":"asr"\|"llm"\|"tts","message":<常量话术>}`；话术进常量不进 prompt；三档逐个演示 |
| B-3 家属端契约技术校对 | 《接口契约_家属端数据》草案 v1.0 | `B-3 -.N4.-> D-2`（**dev-D 外部硬依赖**）；→ 冻结评审输入 | 逐端点核对 5 端点，出校对意见清单；**差异只登记不私改** | 5 端点（F0/F1/F2/N1/N4）逐条有结论；不擅自定稿；校对稿附在契约评审稿后 |
| B-4 Laya spike 预研 | 确认本地权重获取方式（主理人） | 独立，**不合并主线** | `spikes/laya_hello/` 独立目录 | 能装、能跑、记录 CPU 单句延迟实测值 |

**dev-C · 前端 pad**（`dev-c-前端pad语音与3D.md`，状态 未开始）

| 子任务 | 前置依赖 | 任务间依赖 | 执行步骤 | 量化验收标准 |
| --- | --- | --- | --- | --- |
| C-1 三档超时前端表现 | B-2 `degrade` 事件规范（N2） | `C-1→M5.4` | 按 `stage` 渲染、文案取事件 `message` | 三档逐个联演无一次白屏或静默；提示走 pad 大字规范（1920×1080） |
| C-2 20 次延迟测量执行 | A-2 完成通知（N1）、A-3 SQL 口径（N1）；S0 冒烟通过 | `C-2→M5.2` | 20 次「今天天气」× 分段原始记录 + 录屏 | 每次 `trace_id` 可在 metrics 表回查；20 次样本齐全，达标与否如实入档 |
| C-3 声纹闭环真人实测 | dev-A identity/voiceprint 后端（N5）；阈值 0.55（PRD v2.8） | `C-3→M5.4`，演示前必须完成 | 录入 3 段 → 同人命中（voice 终态 + cosine）→ 非同人「请问是哪位在说话？」→ 失败按 C4 降级放行 | 实测记录含 cosine 值并回填 day04/task-01 卡；录屏留证 |
| C-4 口型取证 | 无 | 无（D1 裁决维持现状） | 录屏留证，不开发 | 播放任一 TTS 可观察口型开合 |

**dev-D · 前端 console/family**（`dev-d-前端console与family.md`，状态 未开始）—— 本计划主体，逐项展开见 §3

| 子任务 | 前置依赖 | 任务间依赖 | 执行步骤（摘要） | 量化验收标准（原文） |
| --- | --- | --- | --- | --- |
| **D-1** admin 管理页 | **无**（后端端点 Day2 已就绪；console 纯 React + 原生 CSS，不引组件库） | **独立可立即开工**；`D-1→M5.4`（演示硬依赖） | 接 E12/E15/E13/E18/E16/E17 真实端点 → admin 角色分流接线 → 四流程走查 + 录屏 | ①admin 登录可见并走通建员工/建家属/绑定长者/设备建档四流程 ②非 admin 访问被拒（E1/E2 权限矩阵）③样式 `var()` 引用 tokens.css、无硬编码色值 ④`pnpm --dir frontend build` 通过且 three-vrm 不进 console 构建产物 |
| **D-2** family Mock→真实切换层 | **dev-B B-3 校对稿（N4）**；契约冻结评审（主理人） | `B-3 -.N4.-> D-2`；冻结前**不得定稿** | 为 HomePage/ReportsPage/NotificationsPage 建数据源抽象（Mock/真实双轨）→ 开关说明 | ①5 端点调用形状与《接口契约_家属端数据》一致（elders/reports/notifications + 已读）②Mock 现状不受影响，Day8 后端交付后仅切开关即可对接 ③`elder_guardians` 关系判定走 `GET /api/sessions/current`，不前端臆造 `elder_id` |
| **D-3** console 五 Mock 页字段对齐 | **dev-A A-4 草案（N3）**；契约草案落点确认（#6） | `A-4 -.N3.-> D-3`；**A-4 到手当天完成** | 逐页核对工单/告警/健康/药单/复核字段 → 差异清单回传 dev-A → 双向确认后修 Mock 结构 | ①每个页面字段能对上契约草案出处（TD §3 表+字段）②差异双向确认后修正 Mock 结构，页面渲染不回归 ③不改动后端，不新增第二套字段口径 |

### 2.4 联调节点、里程碑与非开发项

| 联调 | 双方 | 对接物 | 对接即检查单 | 时间窗 | dev-D 相关 |
| --- | --- | --- | --- | --- | --- |
| N1 | A-2 ↔ C-2 | 分段耗时打点 | `metrics_service.record_stage(trace_id, elder_id\|None, stage, duration_ms, source)`；七值 stage；端侧 `wake_ts` 优先、WS accept 兜底；不含转写全文与隐私字段 | A-2 完成后即时 | — |
| N2 | B-2 ↔ C-1 | 三档超时降级事件 | `degrade` 事件 `{"stage","message"}`；话术进常量；前端收到即展示不白屏 | B-2 定稿当天 | — |
| **N3** | **A-4 ↔ D-3** | console 契约草案 | **每个字段行标注 TD §3 表名 + 字段出处；Mock 字段与 TD 逐一对齐，差异清单双向确认** | **Day5 内，草案到手当天** | ✅ 输入 |
| **N4** | **B-3 ↔ D-2** | 家属端契约校对稿 | 对照《接口契约_家属端数据》草案逐端点核对 5 端点；**差异只登记不私改** | **Day5 内** | ✅ 输入 |
| N5 | dev-A 声纹后端 ↔ C-3 | 声纹闭环 | 阈值 0.55；录入 3 段 → 命中 → 主动确认 → 失败按 C4 降级放行 | S0 后立即，演示前必须完成 | — |
| N6 | B-1 ↔ Day6 Agent | guardrails 预留接口 | `check(tool_name) → allow/deny(原因)`；`MAX_TOOL_CALLS_PER_TURN=8`；Day6 零改动接入 | Day5 收口 | — |

| 里程碑 | 判定 | 依赖 |
| --- | --- | --- |
| M5.1 metrics 链路通 | 任取一行 metrics 能按 `trace_id` 串起一次对话各分段；日志/库内双轨一致 | A-1→A-2 |
| M5.2 基线表入档 | P50/P95 由 SQL 复算可复现；FunASR 段单独归档；三档超时降级录屏 | A-3+C-2、B-2+C-1 |
| M5.3 护栏用例全绿 | 第 9 次调用被拒 + 同 Tool 1min 第 3 次熔断，单测全绿 | B-1 |
| **M5.4 Iter2 演示** | 5 分钟脚本一次通过（对话/声纹/口型/降级），tag `iter2-demo` | M5.2、C-3、C-1、**D-1** |

**非开发项（不在四人分工内）**：a0 设备密钥轮换溯源（D5-1，主理人协调）；演示脚本与主持（主理人定节奏、dev-C 主操 pad）；Day4 卡状态回填（task-04/05、day04/a0）；**家属端契约冻结评审（A1，主理人组织，dev-B B-3 校对稿为输入）**；F-03 `tasks.ts` diff 复核。

### 2.5 dev-D 三项任务的依赖拓扑小结

- **D-1 无前置、无上游阻塞，且是 M5.4 唯一前端硬依赖 → 最高优先级，先开工**。
- **D-2 被 B-3（N4）+ 契约冻结评审双重阻塞** → 「先做不依赖口径的骨架，等待窗口内并行推进 D-1，校对稿到手后补口径」。
- **D-3 被 A-4（N3）+ 契约草案落点（主计划 #6 未闭环）双重阻塞** → 「先做现状字段盘点，草案到手当天完成对齐」。
- 三者**写作用域互不重叠**（D-1 改 console 路由/新页、D-2 改 family、D-3 改 console 五 Mock 页），可并行；唯一共享写锁是 `frontend/src/console/App.tsx` 与 `console/index.css`（D-1 与 D-3 可能同时改），见 §4.3。

---

## 3. dev-D 详细工作推进计划

> 执行顺序主线：**W0 →（W1 ∥ W2 骨架 ∥ W3 盘点）→ W4 联调与构建门禁 → W5 演示预演与收口**。
> 每阶段「自检校验」全绿才进入下一阶段；阶段汇报格式固定为：**diff 摘要 + 自检结果 + 遗留问题**（AGENTS §8 给 AI 下任务模板）。

### 3.0 写作用域与禁触清单（功能边界，先冻结再开工）

| 域 | 可写文件（仅 dev-D） | 动作 |
| --- | --- | --- |
| console · admin | `frontend/src/console/pages/Admin*.tsx`（新建）、`frontend/src/console/admin-api.ts`（新建，E12/E15/E13 封装） | 新建 |
| console · 装配 | `frontend/src/console/App.tsx`、`frontend/src/console/layout/AppLayout.tsx`、`frontend/src/console/index.css` | 修改（**写锁，D-1 独占，D-3 不得同改**） |
| console · 既有封装 | `frontend/src/console/elders-api.ts` | **只读复用**（`fetchRooms / createDevice / fetchDevices / bindDevice / createElder / assignBed`），不改签名 |
| console · 五 Mock 页 | `frontend/src/console/{workorders,alerts,health,medication,review}-api.ts` 及对应 `pages/*.tsx` | D-3 修改（字段对齐），不改业务逻辑 |
| family | `frontend/src/family/api.ts`、`frontend/src/family/data-source.ts`（新建）、`frontend/src/family/pages/{HomePage,ReportsPage,NotificationsPage}.tsx` | D-2 修改/新建 |
| 文档 | `docs/plans/day05/`（本计划 + DAG + 差异清单 + 开关说明 + 汇报清单）；任务卡 `> 状态：` 行与变更记录 | 修改（**契约正文与基线正文禁改**） |

**禁触清单（违反即返工）**：
1. `app/**`、`alembic/**`、`tests/**`、`mcp-servers/**`、`deploy/**`、`scripts/**` —— 后端与迁移一律不动。
2. `frontend/src/pad/**` —— dev-C 独占；`frontend/src/theme/tokens.css` —— **三端共享写锁，本轮不改**（见 §4.3）。
3. `frontend/vite.config.ts`、`frontend/package.json`、`frontend/*.html` —— 本轮不改（MPA 配置与 dev-C 共享）。
4. `frontend/src/console/api.ts`（auth 域公共模块）—— 默认不改，见决策 K2 方案 B。
5. `docs/接口契约_*.md`、`docs/康伴3D数字人_*.md`、`docs/ER_数据模型设计.md`、`docs/前端页面方案.md` 正文 —— 高改动成本，差异**只登记回传**，由主理人/契约负责人裁定。
6. `docs/team_tasks/day05/PROGRESS.md` —— 自动生成，**禁止手改**；要动进度只改任务卡 `> 状态：` 行（AGENTS §10.5）。
7. 不新增前端组件库、路由库、状态库、测试框架（AGENTS §4 禁区 12；dev-d 卡「不引组件库」）。

### 3.1 W0 · 启动门槛与前置核对（最高优先，D-1 可在此后立即开工）

**工作内容与操作步骤**
1. 读本计划 §4 对接矩阵，向主理人确认三件事并留痕：**①A-4 契约草案落点**（新建 `docs/接口契约_照护台业务数据.md` vs 并入现有文件，主计划 #6 未闭环）；**②《接口契约_家属端数据》冻结评审时点**；**③B-3 校对稿预计交付时点**。
2. 跑一次基线构建并记录结果：`pnpm --dir frontend build`（确认当前三入口可过、`tsc --noEmit` 无错），作为「不回归」对照基线；记录 `frontend/dist/` 中 console/family 产物清单。
3. 冻结写作用域：向 dev-C 通告 `tokens.css` 本轮不改、向 dev-A/dev-B 通告 N3/N4 需要的交付物格式（见 §4.1）。
4. 建立三份登记文件（空表待填）：`docs/plans/day05/d-字段差异清单.md`、`docs/plans/day05/d-家属端切换层说明.md`、`docs/plans/day05/d-汇报清单.md`。
5. 判定开工分支：D-1 **立即开工**；D-2 只启动「不依赖口径的骨架」（§3.3 W2-a）；D-3 只启动「现状字段盘点」（§3.4 W3-a）。

**交付物**：`d-启动门槛核对.md`（确认结论 + 基线构建输出摘要 + 三条通告留痕）；三份空登记表。

**自检校验标准**
- [ ] 三个待确认问题均有书面答复或明确标注「⏳ 未闭环 + 阻塞哪一步 + 谁跟进」，无臆测
- [ ] 基线 `pnpm --dir frontend build` 结果已记录（通过/失败 + 原文输出摘要）
- [ ] 写作用域与禁触清单已向 dev-A/B/C 通告（群内留痕或卡上留痕）
- [ ] 未修改任何代码文件；本轮产出只有 `docs/**` 文档

### 3.2 W1 · D-1 admin 管理页（无前置，M5.4 硬依赖）

**工作内容与操作步骤**
1. **W1-a 端点封装（复用优先）**：新建 `frontend/src/console/admin-api.ts`，只封装后端已就绪而 console 尚无封装的三个端点：`POST /api/staff`(E12)、`POST /api/guardians`(E15)、`POST /api/elders/{elder_id}/guardians`(E13)；请求/响应类型对齐 `docs/接口契约_身份与权限.md §4 E12/E15/E13`（`{name, role, phone, password, elder_ids?}`、`role ∈ admin|care_worker|doctor`、phone 11 位、password 8–128 位；E15 与 E13 两步：先建号拿 `guardian_id` 再绑定）。**E18/E16/E17/E19/E5/E9 一律复用 `elders-api.ts` 现有函数，不另造第二套**（AGENTS §4 禁区 8）。
2. **W1-b 页面新建**：
   - `AdminUsersPage.tsx`：员工建档（E12，含 `elder_ids` 分配以补齐 G2）、家属建号（E15）、家属绑定长者（E13，超 2 位 409 提示）；表单校验与 403/409/422/404 分支文案。
   - `AdminDevicesPage.tsx`：房间床位列表（E18）→ 模拟设备建档（E16）→ 设备列表（E17）→ **一次性 `device_secret` 展示一次**（复用 `elders-api.ts:createDevice` 返回的 `device_secret`，展示后不可再取；不落 localStorage、不写日志）。
   - 两页均使用原生 CSS + `var()`；**只引用 `tokens.css` 中已定义的变量**（`--color-brand` / `--color-line` / `--color-ink` / `--color-danger-soft` / `--color-danger-ink` / `--radius-card` 等），禁止引用 §1.7 列出的未定义变量；危险操作「不铺红底」，用 `text-danger-ink` + 描边 + 二次确认。
3. **W1-c 角色分流接线**：`AppLayout.tsx` 的 `NAV_BY_ROLE.admin` 把「用户与角色」「模拟设备」两项 `enabled` 置 `true` 并给 key（其余「教学数据重置 / 人设配置 / 审计日志」保持置灰——**不属 dev-d 卡四流程范围**，见 §4.2 边界）；`App.tsx` 增加 `AdminView` 分支，admin 登录后渲染对应页面，guardian 仍走占位提示（应走 family）。
4. **W1-d 越权反向验证**：分别用 care_worker / doctor / guardian 凭证直接调用 E12/E15/E13，确认服务端返回 403（身份契约 §6 权限矩阵），前端不因 403 白屏、不回显 `trace_id`。**决策 K2（方案二选一，推荐 B）**：
   - **A**：给 `console/api.ts` 的 `ApiErrorKind` 补 `"forbidden"` 并统一 403 文案 → 改动 auth 域公共模块，需回归登录页全部错误分支；
   - **B（推荐）**：在 `admin-api.ts` 内部解析 403 并抛页面级错误文案，`console/api.ts` 零改动 → 影响面最小、不碰鉴权公共模块。
   选定后在 `d-启动门槛核对.md` 登记，不得两种并存。
5. **W1-e 走查与录屏**：admin 登录逐条走通四流程并录屏；同时录一段非 admin 被拒的反向证据。

**交付物（格式 / 内容范围 / 质量要求）**

| 交付物 | 格式 | 内容范围 | 质量要求 |
| --- | --- | --- | --- |
| `admin-api.ts` + 两个 `Admin*Page.tsx` | TypeScript / TSX | E12/E15/E13 封装 + 四流程表单 + E18/E16/E17 复用接线 | 全量 type hint；不 import `pad/**` / `family/**`；无硬编码 hex；无硬编码身份；`elder_id` 不臆造 |
| `App.tsx` / `AppLayout.tsx` / `index.css` 改动 | diff | 仅 admin 分流与新增样式 | 不动 care_worker / doctor 分支；不改 `tokens.css`；不引组件库 |
| `d-admin页走查录屏` | 视频文件（不入库，路径登记在汇报清单） | 四流程 + 非 admin 403 反向验证 | 每流程可辨识操作与结果；录屏路径写进 `d-汇报清单.md`，**不提交媒体大文件**（AGENTS §4 禁区 11） |
| `d-admin页走查记录.md` | Markdown 表格 | 流程 / 端点 / 请求要点 / 实际状态码 / 结论 | 四流程各一行 + 越权至少 3 行；状态与录屏一致 |

**自检校验标准**
- [ ] admin 登录可见「用户与角色」「模拟设备」两项且可进入；**建员工 / 建家属 / 绑定长者 / 设备建档四流程各走通 1 次**
- [ ] 一次性 `device_secret` 展示且仅展示一次；页面刷新/重进不再出现；未写入 localStorage、日志、录屏外任何持久化位置
- [ ] 非 admin（care_worker / doctor / guardian）访问 E12/E15/E13 **均被 403 拒绝**，前端无白屏、无内部细节回显
- [ ] 新增/改动文件中硬编码十六进制色值 **0 处**（grep 自检）；全部色值走 `var()`；未引用 tokens.css 未定义变量
- [ ] 新增样式 console 表格行高 ≥40px、正文 14–16px、触控 ≥48×48px；浅色侧栏、无深侧栏
- [ ] `pnpm --dir frontend build` 通过（`tsc --noEmit` 零错）；**console 产物中 `@pixiv/three-vrm` / `three-vrm` 零命中**（检查 `frontend/dist/console.html` 及其引用 chunk）
- [ ] `elders-api.ts` 函数签名 **零改动**；未出现第二套 E16/E17/E18 封装
- [ ] 未触碰 `app/**`、`src/pad/**`、`tokens.css`、`vite.config.ts`、`package.json`

### 3.3 W2 · D-2 family Mock→真实切换层（依赖 B-3 / N4 + 冻结评审）

**决策 K1（必须先处置的口径差异）**：dev-d 卡写「环境开关」，《接口契约_家属端数据》§6 **S-11 明写「`api.ts` 内 `USE_MOCK` 常量 + 内置数据，不进环境变量体系」**，且 S-11 属「自裁行」——可改，但**必须走契约 §6 变更流程并留痕 + 冻结评审**。
> **本计划默认按 S-11 执行**：保留 `family/api.ts` 中唯一的 `USE_MOCK` 常量作为开关（现状已是 `true`），不引 `import.meta.env` / `.env`；「数据源抽象」通过**类型与实现分离**完成，而非新增环境变量。若主理人裁定改为环境开关，须先由契约负责人改 S-11 并同提交补修订记录，**dev-D 不得自行改契约正文**。此差异登记进 `d-字段差异清单.md` 并回传 dev-B（N4）。

**工作内容与操作步骤**
1. **W2-a（不依赖 B-3，可与 W1 并行）数据源抽象骨架**：把 `family/api.ts` 中 §229 之后的业务类型与 Mock 数据迁出为独立模块（建议 `family/data-source.ts` 或 `family/mock-data.ts`），`api.ts` 只保留：认证域（`Principal` / `CurrentIdentity` / `login` / `fetchCurrent` / `logout` / `authedFetch`）、**唯一开关 `USE_MOCK`**、业务类型导出、5 个业务函数签名。目标：**页面与类型不变，切换真实后端时只改 fetch 层**（契约 §0.4 第 2 条）。
2. **W2-b 双轨切换层**：5 个函数各自 `if (USE_MOCK) → Mock 实现 / else → 真实 fetch`，真实分支路径严格对齐草案 §4：
   `F0 GET /api/family/elders`、`F1 GET /api/family/elders/{elder_id}/reports`、`F2 GET /api/family/reports/{report_id}`、`N1 GET /api/family/notifications`、`N4 POST /api/family/notifications/{id}/read`。
   响应形状按契约 §4：`{items, total, page, page_size}` 分页外壳；`medication_stats` 四键必全；`audio_url` P0 恒 `null`、前端不渲染播放器；`payload` 未知键忽略不报错；N4 幂等只写 `read_at`、不动 `status`。
3. **W2-c 关系判定**：`HomePage` 长者切换器的数据源只来自 F0；当前身份范围由 `GET /api/sessions/current`（E3 `scope_elder_ids`）核对，**前端不臆造 `elder_id`、不自建绑定关系、不调长者档案接口**（裁决 A-01）。越权 403 用 `SCOPE_FORBIDDEN_TEXT` 文案，不回显内部细节。
4. **W2-d（阻塞，等 N4）口径补定**：B-3 校对稿到手后逐端点比对 5 端点，出 `d-家属端契约差异表`（字段 / 判定 / 自裁三行分类不混淆）；**差异只登记不私改**；契约冻结评审前所有真实分支标注「待冻结」，**不擅自定稿**。
5. **W2-e 开关说明**：`docs/plans/day05/d-家属端切换层说明.md` 写清：开关位置、默认态、Day8 后端交付后的切换步骤、Mock 数据删除条件（S-11：联调通过后删除 mock 分支并同提交登记）、当前「待冻结」清单。

**交付物（格式 / 内容范围 / 质量要求）**

| 交付物 | 格式 | 内容范围 | 质量要求 |
| --- | --- | --- | --- |
| `family/data-source.ts`（或等价模块）+ `family/api.ts` 改动 | TypeScript / diff | 类型与 Mock 数据分离、5 函数双轨 | 页面 import 路径与调用签名不变；`USE_MOCK` 全局唯一；不进环境变量体系（K1）；不 import console/pad |
| `d-家属端契约差异表.md` | Markdown 表格 | 5 端点 × 字段名 / 类型 / 草案出处（§4 或 §2）/ 校对稿结论 / 差异 / 处置 | 每行可回答「出处是契约哪一条」；三行分类不混淆；差异标注「只登记、未私改」 |
| `d-家属端切换层说明.md` | Markdown | 开关位置、默认态、Day8 切换步骤、Mock 删除条件、待冻结清单 | 与代码实际一致；明确写出「冻结评审前不定稿」 |

**自检校验标准**
- [ ] `USE_MOCK` 全仓 family 侧 **仅 1 处**定义；切 `false` 后 5 个函数全部走真实 fetch，切 `true` 后**现有三业务页渲染与切换前完全一致**（Mock 现状不受影响，逐页目视对比：长者切换器 / 日报列表+详情 / 通知列表+已读）
- [ ] 5 端点路径、方法、响应形状与《接口契约_家属端数据》§4 **逐字段一致**；`medication_stats` 四键齐全；`audio_url` 无播放器；`payload` 未知键不抛错
- [ ] 长者切换器数据来源可追溯到 F0 / E3 `scope_elder_ids`；代码中 **0 处**硬编码 `elder_id` 作为关系判定依据
- [ ] 403 走 `SCOPE_FORBIDDEN_TEXT`、401 走统一登录失效处理；不回显 `trace_id`
- [ ] family 375px 无横向滚动、正文 ≥16px、触控 ≥48×48px；`pnpm --dir frontend build` 通过
- [ ] `docs/接口契约_家属端数据.md` **正文零改动**；差异全部落在差异表并已回传 dev-B（N4）
- [ ] 若 K1 被裁定改环境开关：契约修订记录与代码改动**同一次提交**，否则维持常量方案

### 3.4 W3 · D-3 console 五 Mock 页字段对齐（依赖 A-4 / N3 + 草案落点）

**工作内容与操作步骤**
1. **W3-a（不依赖 A-4，可与 W1 并行）现状字段盘点**：逐页导出五个 Mock 层的**全部类型与字段名**及对应 TD §3 表字段猜测出处，形成 `d-字段差异清单.md` 左半表：
   `workorders-api.ts`（`work_orders` / `work_order_transitions`）、`alerts-api.ts`（`notifications`）、`health-api.ts`（`health_records`）、`medication-api.ts`（`medication_plans`）、`review-api.ts`（`health_records` HITL / 版本链）。
   同时登记 §1.6 C4（`source` 四值枚举 vs `staff_report`）与 §1.7 已发现的硬编码演示身份，作为待确认差异，**不自行改**。
2. **W3-b（阻塞，等 N3）草案到手当天对齐**：按 A-4 草案逐字段核对，**每个字段行标注 TD §3 表名 + 字段出处**；产出三列差异结论：`一致 / 前端多字段（登记）/ 前端缺字段（登记）`；口径一律以 TD §3 为准（N3）。
3. **W3-c 双向确认**：差异清单回传 dev-A（N3），**dev-A 与 dev-D 双方确认后**才修 Mock 结构；未确认项保持原样并标注「⏳ 待确认」，**不擅自定稿、不改契约正文**。
4. **W3-d Mock 结构修正**：只改类型定义与 Mock 数据形状，**不改业务判定逻辑**（工单状态机、AC-23 通知状态机、健康阈值分级、药单权限、复核版本链均保持现状）；不新增第二套字段口径、不新增第二种同类能力实现。
5. **W3-e 渲染回归**：五个页面逐页目视核对（列表 / 筛选 / 详情 / 操作弹窗 / 版本链），确认字段改名或结构变化后**页面渲染不回归**；`console/index.css` 若需同步改类名，须与 W1 的写锁协调（同一人合并一次改）。

**交付物（格式 / 内容范围 / 质量要求）**

| 交付物 | 格式 | 内容范围 | 质量要求 |
| --- | --- | --- | --- |
| `d-字段差异清单.md` | Markdown 表格（五页分节） | 页面 / Mock 字段 / 类型 / TD §3 出处 / A-4 草案字段 / 差异 / 双向确认结论 / 处置 | 每行可回答「出处是 TD 哪一行」；**无凭空字段**；未确认项显式标 ⏳ |
| 五个 `*-api.ts` 及必要 `pages/*.tsx` 改动 | TypeScript / diff | 仅类型与 Mock 数据形状 | 业务判定逻辑零改动；不新增第二套口径；不改动后端 |
| 差异回传记录 | 群内/卡上留痕 | 回传时间、接收人（dev-A）、确认结论 | 有双向确认证据，非单方定稿 |

**自检校验标准**
- [ ] 五页**每一字段**都能对上契约草案出处（TD §3 表名 + 字段），覆盖率 100%；无法对上的字段数 = 0（或全部登记为 ⏳ 待确认且未写入代码）
- [ ] `source` 枚举按 TD §3 / ER §5.3 四值处理；如草案与之冲突，**只登记不裁决**（§1.6 C4）
- [ ] 差异清单已回传 dev-A 并有双向确认；**无单方面定稿**
- [ ] 五页渲染逐页无回归（列表/筛选/详情/操作弹窗/版本链各至少 1 次目视核对并记录）
- [ ] 后端 `app/**` 零改动；契约草案正文零改动；未新增第二种同类字段口径
- [ ] `pnpm --dir frontend build` 通过

### 3.5 W4 · 联调与构建门禁（三线汇合）

**工作内容与操作步骤**
1. 汇总 W1/W2/W3 产出，执行 `pnpm --dir frontend build`；核对三入口产物清单。
2. 静态自检：新增/改动文件中硬编码 hex 数、`tokens.css` 未定义变量引用数、`three-vrm` 进入 console/family 产物数，三项均应为 0。
3. 与 dev-A（N3）、dev-B（N4）各做一次「对接即检查单」逐条打勾，未通过项登记为遗留问题并明确责任人与时点。
4. 填写 `d-汇报清单.md`：对照 `docs/plans/day05/day-05-延迟基线与护栏.md` **§S7 自检项**（A2「每个字段行能回答出处是 TD 哪一行」、A3「三页面走通 E12/E18 真实端点、越权被拒」、A1「三行分类未被混淆」）与 dev-d 卡「完成定义」逐条给结论。

**交付物**：`d-汇报清单.md`（逐条结论 + 证据路径 + 遗留问题）；构建输出摘要。

**自检校验标准**
- [ ] `pnpm --dir frontend build` 通过，`tsc --noEmit` 零错误
- [ ] 硬编码 hex = 0；未定义 token 变量引用 = 0；three-vrm 进 console/family 产物 = 0
- [ ] N3 / N4 检查单逐条有结论；未闭环项有责任人与时点
- [ ] 汇报清单覆盖 S7 三项自检 + dev-d 卡全部验收项，无漏项

### 3.6 W5 · M5.4 演示预演与收口

**工作内容与操作步骤**
1. 把 W1-e 的 admin 页走查纳入 **M5.4 Iter2 演示预演**（`dag` §2.2：D-1 是 M5.4 依赖），按主理人 5 分钟脚本节奏提供 admin 段操作人与话术。
2. 走查结果回填 `docs/team_tasks/day05/dev-d-前端console与family.md` 的**变更记录**（dev-d 卡 §联调要求 M5.4），并把 `> 状态：` 从 `未开始` 更新为 `进行中` / `已完成`（**只改任务卡**，不手改 `PROGRESS.md`，由 pre-commit 钩子自动刷新）。
3. 三份计划件状态更新 + `docs/plans/README.md` 索引条目状态更新（同一次提交）。
4. 提交前确认：未产生任何业务代码文件改动（本轮），或（后续执行阶段）所有代码改动均在 §3.0 写作用域白名单内。

**交付物**：任务卡变更记录 + 状态行；计划件与索引状态更新；`d-汇报清单.md` 终版。

**自检校验标准**
- [ ] M5.4 预演中 admin 段一次通过，走查结果已回填任务卡变更记录
- [ ] 任务卡 `> 状态：` 已更新；`PROGRESS.md` **未被手改**
- [ ] 索引条目与本计划/DAG 同一次提交登记（AGENTS §10.3）
- [ ] 完成定义四项全绿：任务表全绿 + `pnpm --dir frontend build` 通过 + N3/N4 对接单全绿 + 汇报清单（对照 S7）齐全

---

## 4. 功能边界与对接矩阵

### 4.1 与 dev-A / dev-B / dev-C 的交接（完成即可对接）

| 对端 | 方向 | 交付物（谁给谁） | 格式要求 | 断了会怎样 | 降级处置 |
| --- | --- | --- | --- | --- | --- |
| **dev-A（N3）** | A-4 → **dev-D** | console 五 Mock 页接口契约草案（标注「待冻结」） | 每个字段行带 TD §3 表名 + 字段出处；端点形状、状态码、分页外壳齐全 | D-3 无法定稿，字段对齐无依据 | 只做 W3-a 现状盘点；**不得凭经验自造字段**；阻塞登记到 W0 门槛 |
| **dev-D（N3）** | dev-D → A-4 | `d-字段差异清单.md`（含 C4 枚举差异、硬编码身份登记） | 五页分节，三列差异结论，未确认项标 ⏳ | dev-A 草案缺少双向确认，Day7/8 建表时口径漂移 | 差异清单先回传并留痕，等确认再改 Mock |
| **dev-B（N4）** | B-3 → **dev-D** | 家属端契约技术校对稿（5 端点逐条结论） | 对照《接口契约_家属端数据》草案；**差异只登记不私改** | D-2 真实分支口径不能定稿 | 只做 W2-a/W2-b 骨架，真实分支标注「待冻结」 |
| **dev-D（N4）** | dev-D → B-3 | K1 口径差异登记（环境开关 vs S-11）、`d-家属端契约差异表.md` | 三行分类（字段/判定/自裁）不混淆 | 冻结评审缺输入，A1 无法闭环 | 差异登记留痕，交主理人组织冻结评审 |
| **dev-C** | 双向边界 | `tokens.css` / `vite.config.ts` / MPA 三入口构建 | 本轮 dev-D **不改这些文件** | 若双方同改共享样式或构建配置 → 白屏或构建冲突 | 需要改 token 时先通告 dev-C，由一人一次性改完并三端回归 |
| **主理人** | 上位 | A1 契约冻结评审、A-4 落点裁定（#6）、M5.4 演示脚本 | 书面结论留痕 | D-2/D-3 停在「待冻结」，Day8 对接埋雷 | 只交付「不定稿版」骨架 + 差异清单，明确标注阻塞 |

### 4.2 与后端的边界

- **D-1**：只消费**已就绪**的 E12/E15/E13/E18/E16/E17/E19/E5/E9；不要求后端新增任何端点、不改 `app/api/**`、不改权限矩阵。E12 的 `PATCH /api/staff/{staff_id}`（改角色/停用）与「教学数据重置 FR-ADM-02 / 人设配置 FR-ADM-07 / 审计日志 FR-ADM-05」**不在 dev-d 卡四流程内**，本轮不接、菜单保持置灰，避免范围膨胀。
- **D-2**：**不接未交付端点**；`/api/family/*` 5 端点在 Day 8 批次前一律走 Mock。切换层的价值是「Day8 后端交付后仅切开关即可对接」，**不是本日联调真实接口**。
- **D-3**：五 Mock 页**保持 Mock**，不把任何页面改成真实 fetch（对应表 Day7/8 未建）；字段对齐只是让 Mock 形状与未来契约一致。
- 权限与范围过滤**只在服务端**；前端只做展示与 403 文案，不实现越权判定、不缓存他人数据。

### 4.3 写锁与并行边界（违反即返工）

| 文件 | 唯一写者 | 约束 |
| --- | --- | --- |
| `frontend/src/console/App.tsx`、`AppLayout.tsx`、`console/index.css` | **D-1（W1）** | D-3 若需改 `index.css` 类名，合并到 W1 的同一次改动，不与 W3 并行 |
| `frontend/src/family/api.ts`、`family/pages/*` | **D-2（W2）** | 与 console 线完全隔离 |
| `frontend/src/console/{workorders,alerts,health,medication,review}-api.ts` 及对应 pages | **D-3（W3）** | 不改 `elders-api.ts`、不改 `console/api.ts` |
| `frontend/src/theme/tokens.css` | **本轮无人** | 三端共享；只引用已有变量，不新增、不修改 |
| `frontend/src/pad/**` | dev-C | dev-D 禁触 |
| `docs/接口契约_*.md`、三份基线文档正文 | 契约负责人 / 主理人 | dev-D 只登记差异，不改正文 |
| `docs/plans/day05/d-*.md` | **dev-D** | 本计划及其派生件 |

### 4.4 禁止事项汇总（dev-d 卡 + AGENTS 红线）

不引组件库；样式不硬编码色值；three-vrm 不进 console 构建产物；非 admin 访问须被拒；不前端臆造 `elder_id`；Mock 现状不受影响；差异双向确认后修正且渲染不回归；**不改动后端**；**不新增第二套字段口径**；**契约冻结评审前不擅自定稿**；不改 `PROGRESS.md`；不提交媒体大文件与密钥；不在前端做健康分级；不做医疗诊断话术；不接真实微信/HIS/支付。

---

## 5. 交付物总表

| # | 交付物 | 格式 | 内容范围 | 质量要求 | 归属 |
| --- | --- | --- | --- | --- | --- |
| 1 | `docs/plans/day05/d-双端前端推进计划.md` | Markdown（本文件） | §0–§8 + 变更记录 | 文件头四行齐全；登记进 `docs/plans/README.md`；事实带出处 | **本轮已交付** |
| 2 | `docs/plans/day05/d-DAG.md` | Markdown（mermaid + ASCII + 表 + JSON） | 节点/边/关键路径/写锁/门禁 | 与本计划同提交；标注「派生物，不承载口径」 | **本轮已交付** |
| 3 | `docs/plans/day05/d-启动门槛核对.md` | Markdown | 三个待确认 + 基线构建 + 通告留痕 | 每个问题有答复或显式 ⏳ | W0 |
| 4 | `d-字段差异清单.md` / `d-家属端切换层说明.md` / `d-汇报清单.md` | Markdown | 见 §3.2–§3.5 | 空表先建，逐阶段填 | W0 建 / W2–W5 填 |
| 5 | admin 页代码（`admin-api.ts` + `Admin*Page.tsx` + 三处装配改动） | TS/TSX/CSS diff | E12/E15/E13 + E18/E16/E17 复用 | 白名单内；零硬编码；无第二套封装 | W1 |
| 6 | `d-admin页走查录屏` + `d-admin页走查记录.md` | 视频（外存）+ Markdown 表 | 四流程 + ≥3 条越权反向验证 | 录屏只登记路径不入库 | W1 / W5 |
| 7 | family 切换层代码 + `d-家属端契约差异表.md` | TS diff + Markdown | 5 端点双轨 + 逐端点差异 | 页面签名不变；USE_MOCK 唯一；待冻结标注 | W2 |
| 8 | 五 Mock 页字段修正 diff | TS diff | 仅类型与 Mock 数据形状 | 业务逻辑零改动；双向确认后落 | W3 |
| 9 | 任务卡变更记录与 `> 状态：` 行 | Markdown 单行 + 表 | M5.4 走查结果 | 不手改 PROGRESS | W5 |

---

## 6. 自检校验总表（每项任务完成后逐条打勾）

| 任务 | 自检项 | 判定方式 | 量化门槛 |
| --- | --- | --- | --- |
| W0 | 三待确认、基线构建、写锁通告、零代码改动 | 文档核对 + 构建输出 | 3/3 有结论或显式 ⏳；代码改动 = 0 |
| D-1 | 四流程走通 | 浏览器走查 + 录屏 | 4/4 |
| D-1 | 非 admin 被拒 | 三种角色反向验证 | 3/3 返回 403，零白屏 |
| D-1 | 一次性密钥 | 展示 + 刷新复查 | 展示 1 次，刷新后 0 次 |
| D-1 | 样式合规 | grep + 目视 | 硬编码 hex = 0；未定义变量引用 = 0；表格行高 ≥40px；触控 ≥48px |
| D-1 | 构建与包隔离 | `pnpm --dir frontend build` + 产物 grep | 构建通过；console 产物 three-vrm 命中 = 0 |
| D-2 | 双轨不回归 | `USE_MOCK` 切 true/false 各走三页 | 三业务页渲染一致；开关定义 = 1 处 |
| D-2 | 端点形状 | 对照契约 §4 逐字段 | 5/5 端点一致；`medication_stats` 四键齐全；`audio_url` 无播放器 |
| D-2 | 关系判定 | 代码审查 | 硬编码 `elder_id` 判定 = 0 处；走 F0 / E3 |
| D-2 | 冻结纪律 | 契约 diff | 契约正文改动 = 0；待冻结标注齐全 |
| D-3 | 字段出处覆盖 | 差异清单逐行 | 100% 可回答 TD §3 出处；凭空字段 = 0 |
| D-3 | 双向确认 | 回传留痕 | dev-A 确认记录 ≥1 份 |
| D-3 | 渲染不回归 | 五页逐页目视 | 5/5 页通过 |
| W4 | 三静态门槛 | grep + 构建 | hex / 未定义变量 / three-vrm 三项均 = 0；build 通过 |
| W5 | 完成定义 | 汇报清单 | 任务表全绿 + build 通过 + N3/N4 检查单全绿 + S7 汇报齐全 |

---

## 7. 风险、未决项与回退

| # | 风险 / 未决 | 影响 | 触发信号 | 处置 / 回退 |
| --- | --- | --- | --- | --- |
| R1 | **A-4 契约草案落点未定**（主计划 #6 ⏳） | D-3 无法启动 W3-b | W0 问询无答复 | 只做 W3-a 盘点；草案到手**当天**完成对齐（N3）；不凭经验自造字段 |
| R2 | **家属端契约未冻结**（草案 v1.0） | D-2 真实分支不能定稿 | 冻结评审未排期 | 真实分支标「待冻结」；差异只登记；K1 差异回传 dev-B；不擅自定稿 |
| R3 | **B-3 校对稿延迟** | N4 联调窗口被压缩 | Day5 内未收到 | 先交 W2-a/W2-b 骨架 + 差异表空表；明确登记「等待 B-3」，不以 Mock 猜测替代 |
| R4 | **K1 开关口径分歧**（环境开关 vs S-11） | 引入环境变量违反契约自裁行 | 主理人要求改 `.env` | 必须先由契约负责人改 S-11 + 修订记录，与代码同提交；否则维持单一常量 |
| R5 | `console/api.ts` 无 `forbidden` kind | 403 被吞成网络文案 | W1-d 反向验证 | 走 K2 方案 B（页面内解析），不改鉴权公共模块；若选 A 须回归登录页全部错误分支 |
| R6 | `tokens.css` 三端共享、已有未定义变量引用 | 新增样式白屏风险（前端页面方案 §5 已记白屏根因） | 构建后页面样式缺失 | 只用已定义变量；本轮不改 tokens.css；确需改先通告 dev-C 并三端回归 |
| R7 | `elders-api.ts:131` `createDevice` 非 2xx 抛数字 `res.status` | admin 设备建档错误分支不稳 | 非 2xx 时页面异常 | **本轮登记不修**（避免越界改既有封装）；在 `AdminDevicesPage` 内做类型守卫兜底；差异回传 |
| R8 | admin 范围膨胀（教学数据重置 / 人设 / 审计日志） | 偏离 dev-d 卡四流程，挤压 M5.4 | 需求方追加 | 明确拒绝并指向「前端页面方案 §3.4 第 4 批 / 后续批次」；菜单保持置灰 |
| R9 | D-1 未按时完成 | **M5.4 演示缺前端环节**（`D1 --> M54`） | W1 走查未过 | 立即上报主理人；演示脚本可临时去掉 admin 段，但须在卡变更记录写明偏差，不得静默跳过 |
| R10 | 前端无测试/lint 框架 | 无法用单测门禁自证 | — | 以构建门禁 + 静态 grep + 浏览器走查 + 录屏为验收证据；**不虚构测试命令** |
| R11 | 契约草案与 Mock 的 `source` 枚举冲突（C4） | 健康代填字段口径漂移 | W3-b 发现 | 只登记、不裁决；按 AGENTS 前置铁律 4 回报主理人 |
| R12 | 媒体文件误入库 | 违反 AGENTS §4 禁区 11 | 提交前 | 录屏只登记外部路径，不提交视频；提交前检查 `git status` |

---

## 8. 完成定义与汇报

**dev-D 完成定义（dev-d 卡 §联调要求原文 + 本计划细化）**：

1. 任务表 **D-1 / D-2 / D-3 全绿**（每项自检 §6 对应行全部打勾）；
2. `pnpm --dir frontend build` 通过，且 three-vrm 不进 console 构建产物；
3. **N3 / N4 对接即检查单逐条有结论**，差异清单已双向确认、已回传；契约冻结评审前不擅自定稿；
4. **M5.4** admin 页走查纳入演示预演，结果回填任务卡变更记录；
5. 汇报清单齐全，逐条对照 `docs/plans/day05/day-05-延迟基线与护栏.md` **§S7 自检项**；
6. 交付物总表 §5 中 1–9 项齐备，计划件与 DAG 同一次提交登记进 `docs/plans/README.md`。

**阶段汇报格式（每个 W 结束）**：`改了哪些文件（diff 摘要） → 自检结果（§6 对应行） → 遗留问题（含责任人/时点）`。每阶段停下经确认再进入下一阶段。

**下游衔接**：本日 D-2 的切换层与 D-3 的字段对齐是 **Day 8（C1/C2 → D8）三端后台对接**的前置；Day 8 后端交付后，family 侧按 S-11「仅切开关 + 联调通过后删 mock 分支」，console 五 Mock 页按已确认契约替换为真实 fetch，页面与类型不变。

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | 首版：通读代码结构与 docs 全量文档后建立项目认知（§1）；梳理 Day 5 全量任务 D5-1–D5-7 与四人 14 项子任务的前置/依赖/步骤/量化验收（§2）；为 dev-D 制定 W0–W5 可落地推进计划，逐项给出操作步骤、交付物格式与质量要求、自检校验标准（§3）；明确与 dev-A/B/C 的功能边界、N3/N4 对接矩阵、写锁与禁触清单（§4）。登记 C1–C6 六项文档冲突及处置、K1/K2 两项待选决策、R1–R12 风险。本轮仅产出文档，未编写或修改任何代码文件 |
