# dev-a 工作推进计划 · 后端观测与数据链路

> 状态：进行中　｜　版本：v1.1　｜　日期：2026-11-07
> 关联：docs/team_tasks/day05/dev-a-后端观测与数据.md（任务卡）
> 上游依据：PRD §5.3（AC-40/42）、FR-DLG-10｜技术设计文档 §8「Day 5 · 延迟与护栏」（L1036–1043）、§3.6｜docs/plans/day05/day-05-延迟基线与护栏.md v1.3 S1–S3、S7-A2｜dag-项目工作流总览与联调节点.md N1/N3/M5.1–M5.2
> 变更记录见文末

---

## 0. 执行基线（本轮主理人裁决 + 代码现状核查，2026-11-07）

### 0.1 四项已确认裁决（本计划编制前经主理人逐项确认）

| # | 事项 | 裁决 |
| --- | --- | --- |
| R1 | 文档留痕三件套（ER / TD / README）实际未落地（dev-a 卡「已由计划侧完成」前提与现状不符，已实测验证） | **纳入 A-1 内执行**：与 ORM/迁移/service 三件同一次提交 |
| R2 | wakeup 段「端侧 wake_ts 优先」与已冻结 C2/C5 契约冲突（`AuthMsg` 无 `wake_ts`；client_diag 仅允许 vrm_load/webrtc/vad/asr 四 stage 且明文「不进 metrics」） | **修订契约**，且上报改动**写入 dev-c 任务卡**（新增 C-5 项，登记裁决来源） |
| R3 | playback（前端播放）段打点只能由 pad 端上报产生 | **纳入 N1 契约，dev-C 上报**，同样**写入 dev-c 任务卡**（新增 C-6 项） |
| R4 | A-4 console 五 Mock 页契约草案落点（day-05 计划待确认 #6） | **新建独立契约文件** `docs/接口契约_照护台业务数据.md`（标注「待冻结」） |

### 0.2 代码现状核查（计划编制时实测，执行以此为基线）

| 现状项 | 实测结论 |
| --- | --- |
| 迁移 | 0001–0005 已合入；**无 0006**；本日新增只加不改 |
| `app/core/metrics.py` | 单点收口（日志级）已存在：`record_wakeup`/`record_connection`/`record_stage`/`record_event`/identity 三点位；**`record_wakeup` 无任何调用方**；trace_id 由 JsonFormatter 注入日志，DB 写入需显式取值 |
| `app/audio/_observe.py` | `record_asr`（asr.py 两处调用）、`record_tts`（tts.py 一处调用）已接线；均只写日志 |
| 六段覆盖 | connection（webrtc.py L2103）、asr、tts 已有日志打点；**wakeup / route / llm_first_token / tts_first_packet（进表）/ playback 未接线** |
| LLM 链路 | `llm_service.chat` 非流式（`stream: False`）；TTS 非流式（首包≈整段可用时刻） |
| 写库层 | `app/services/` 唯一写库层；`app/db.py` 提供 engine/session_factory；无 `metrics_service.py` |
| ORM 约定 | `models/base.py` naming_convention；表名复数 snake_case、`timestamptz`、UUID PK |

---

## 1. 任务 DAG 与执行顺序

```text
A-1 metrics 表三件 + 文档留痕（R1）
  └─→ A-2 打点收口进库（含 R2/R3 契约修订 + dev-c 卡追加）
        └─→ A-3 P50/P95 SQL + 基线表（硬依赖 dev-C C-2 完成，N1）
A-4 console 五 Mock 页契约草案（R4，全程可并行，无依赖）
```

阶段推进（**每阶段完成→汇报→主理人确认→进下一阶段**）：

| 阶段 | 内容 | 确认闸门 |
| --- | --- | --- |
| S1 | A-1 全部（含三件套文档） | 汇报 diff 摘要 + 自检清单全绿 |
| S2 | A-2 全部（含契约修订与 dev-c 卡追加） | 同上 |
| S3 | A-4（可与 S1/S2 并行启动，独立汇报） | 同上 |
| S4 | A-3（等 dev-C C-2 数据齐） | 基线表入档汇报 |

---

## 2. A-1 · metrics 表落地 + 文档留痕（阶段 S1）

### 2.1 工作内容与步骤

1. **文档留痕三件套**（R1，先于代码，同一次提交）：
   - `docs/ER_数据模型设计.md`：新增 `metrics` 表定义节（字段、类型、可空、索引），并入 §5 索引清单两条：`(trace_id)`、`(stage, created_at)`；
   - `docs/康伴3D数字人_技术设计文档.md` §3.6 批次表：新增一行「Day 5（1 张）｜ metrics ｜ 延迟基线测量」，批次合计口径 26→27 表同步更正（§8 L970「5+5+2+1+9+3+1 = 26」引用处一并核对）；文末修订记录新增条目（改哪条、依据 2026-11-07 主理人裁决 D2、影响范围）；
   - `README.md` §11「已知文档冲突」表登记两行：大纲 5.2「metrics 表」vs TD §3.6 原无此表（已裁决建表）；大纲 5.1 viseme vs PRD FR-3D-01 能量近似（已裁决维持 PRD）。
2. **ORM 模型** `app/models/metrics.py`（在 `app/models/__init__.py` 注册）：字段按 day-05 计划 S1 冻结草案——`id` UUID PK、`trace_id varchar(64) not null`、`elder_id UUID null`、`stage` 枚举七值（wakeup/connection/asr/route/llm_first_token/tts_first_packet/playback，用 CHECK 约束小写下划线）、`duration_ms integer not null`、`source varchar(32)`、`created_at timestamptz not null default now()`；索引 `(trace_id)`、`(stage, created_at)`；全量 type hint。
3. **迁移** `alembic/versions/0006_day5_metrics.py`：先写模型后 `uv run alembic revision --autogenerate`（或手写对齐既有迁移风格），**人工读 diff** 后再 `upgrade head`；downgrade 可逆（DROP TABLE）；0001–0005 一字不改。
4. **写库层** `app/services/metrics_service.py`（唯一写库层）：对外仅暴露 N1 规范签名 `record_stage(trace_id, elder_id | None, stage, duration_ms, source)`；进程内**异步批量缓冲**（如 asyncio 队列 + 定期 flush，flush 失败降级为日志告警不抛出，不阻塞主链路）；stage/source 合法性在 service 校验，非法值丢弃并记日志。
5. **补充测试** `tests/test_metrics_service.py`：合法/非法 stage、批量落库、elder_id 可空、flush 失败不抛出。

### 2.2 交付物

ER 表定义 + TD 批次行与修订记录 + README 冲突两行 + `app/models/metrics.py` + `0006_day5_metrics.py` + `app/services/metrics_service.py` + `tests/test_metrics_service.py`（同一提交，另按规则登记 `docs/plans/README.md`）。

### 2.3 自检校验（全绿才汇报）

- [ ] `uv run alembic upgrade head` 成功，`alembic current` 指向 0006；`downgrade` 后再 `upgrade` 可逆
- [ ] 已合入迁移 0001–0005 一字未改（git diff 验证）
- [ ] 表结构三处一致（ER / TD §3.6 / ORM）逐字段核对
- [ ] TD 修订记录与正文同提交，未删除或回填历史修订行
- [ ] `api/audio/scheduler/graphs` 无任何直接写库（分层单向）
- [ ] `uv run pytest tests/test_metrics_service.py -q` 全绿；全量 `uv run pytest -q` 不回归

---

## 3. A-2 · 打点收口进库 + 契约修订（阶段 S2，依赖 A-1）

### 3.1 工作内容与步骤

1. **契约修订**（R2/R3，先于代码；其中 dev-c 卡追加已按主理人指示**提前完成**，卡已升 v1.1）：
   - `docs/team_tasks/day04/contracts.md`：C2 `AuthMsg` 追加**可选** `wake_ts: float`（端侧 KWS 命中时刻）；C5 client_diag 允许 stage 追加 `wakeup`、`playback`（playback 消息含端侧播放耗时字段）；文末修订记录新增条目（依据 R2/R3 主理人裁决），版本 +1；
   - `docs/team_tasks/day05/dev-c-前端pad语音与3D.md` 任务清单**追加两行**：C-5「端侧 wake_ts 上报（auth 首帧带 wake_ts，KWS 未命中不填走兜底）」、C-6「playback 段打点上报（client_diag，字段名与 record_stage 规范一致）」，验收标准对齐 N1 规范，注明裁决来源（主理人 2026-11-07）。
2. **`app/core/metrics.py` 函数体改造**（调用方不动，单点收口纪律）：`record_event`/`record_stage`/`record_wakeup`/`record_connection` 在结构化日志照旧输出之外，组装行数据调用 `metrics_service.record_stage(...)` 异步入队；trace_id 从 `app/core/context.py` 上下文取，取不到则该行不入库（只留日志）并记 warning；**不含转写全文与任何隐私字段**。
3. **`app/audio/_observe.py` 同款改造**：`record_asr`（→ stage=asr，duration 取 total_ms，first_packet 信息保留日志轨）、`record_tts`（→ stage=tts_first_packet，非流式口径 source 注明）。
4. **缺段接线**（只在现有链路对应位置调收口函数，不新建第二套打点）：
   - `wakeup`：signaling auth 首帧处理处调 `record_wakeup(source=...)`——帧带 `wake_ts` 用端侧口径（source=client），未带用 WS accept 时刻兜底（source=backend）；
   - `route` / `llm_first_token`：dialogue_service 链路对应位置调 `record_stage`；
   - `tts_first_packet`：tts.py 现有 first_packet_ms 处补 record_stage 进库；
   - `playback`：由 dev-C 经 client_diag 上报后端代打（依赖步骤 1 契约修订与 dev-C C-6，未到位前该段记缺口不造假数）。
5. **补充测试**：`tests/test_core_metrics.py` 扩展双轨断言（日志字段与入库行一致）、`tests/test_audio_*` 回归。

### 3.2 交付物

两收口文件最小 diff + 缺段接线点最小改动 + 契约修订（contracts.md + dev-c 卡）+ 扩展测试。

### 3.3 自检校验（全绿才汇报）

- [ ] 六段打点齐：wakeup/connection/asr/route/llm_first_token/tts_first_packet/playback 七类 stage 枚举全接入（playback 未联调前登记缺口）
- [ ] 任取一条 metrics 行能按 trace_id 串起一次对话各分段（M5.1 判定）
- [ ] 日志/库内双轨一致：抽 3 个 trace_id 逐段核对
- [ ] 无一行打点含转写全文、手机号等隐私（grep 复查 + 测试断言）
- [ ] `api/audio/scheduler/graphs` 未直接写库
- [ ] contracts.md 修订记录与正文同提交；dev-c 卡追加项注明裁决来源
- [ ] `uv run pytest -q` 全量绿（重点 `test_core_metrics.py`、`test_audio_*`）

---

## 4. A-3 · P50/P95 SQL + 基线表（阶段 S4，依赖 A-2 + dev-C C-2 完成）

### 4.1 工作内容与步骤

1. A-2 收口后**即时通知 dev-C 开测 C-2**（N1 触发，不等待）。
2. 预备 SQL 模板（P50/P95 按 stage 分组、按 trace_id 聚合端到端），等 C-2 的 20 次测量数据入表后执行复算。
3. 出 `docs/plans/day05/day-05-延迟基线表.md`：数字全部由 SQL 对 metrics 表复算、附查询语句可复现；ASR 段按「本地 FunASR 基线」单独归档并注明 O-1（首包 2219ms 实测背景）与云 key 复测条件；20 次样本量、测量方式、环境说明齐；≤1.5s 达标次数如实记录不美化。

### 4.2 交付物

延迟基线表 md（含 SQL 与 FunASR 归档口径）。

### 4.3 自检校验（全绿才汇报）

- [ ] P50/P95 由 SQL 复算得出，报告附查询语句，第三方可复现
- [ ] FunASR 段单独归档 + 云 key 复测条件注明（O-1 口径）
- [ ] 样本量 20、环境说明齐；达标次数如实（不达标也入档）
- [ ] 基线表登记进 `docs/plans/README.md`

---

## 5. A-4 · console 五 Mock 页接口契约草案（阶段 S3，可并行，无依赖）

### 5.1 工作内容与步骤

1. 通读 TD §3 五组表定义：`work_orders`/`work_order_transitions`（Day 7 批次）、`notifications`（Day 8）、`health_records`（Day 8）、`medication_plans`/`medication_events`/`medication_attempts`（Day 7）。
2. 盘点前端 Mock 字段现状：`frontend/src/console/{workorders-api,alerts-api,health-api,medication-api,review-api}.ts` + 对应五页面（WorkOrdersPage/AlertsPage/HealthFormPage/MedicationPage(Read)/ReviewQueuePage/MedicationEditPage）。
3. 起草 `docs/接口契约_照护台业务数据.md`（R4：新建独立文件，标注「待冻结」）：五组端点（工单/告警通知/健康代填/药单只读+维护/复核队列），**每个字段行标注 TD §3 表名+字段出处**，无凭空字段；角色权限边界按 PRD（医生增改停查、护工只读、复核医生）。
4. 附「与前端现状 Mock 字段差异清单」逐项列出（供 dev-D D-3 对齐，N3 交接件）。

### 5.2 交付物

契约草案 md（待冻结）+ 差异清单（同一文件内一节）。

### 5.3 自检校验（全绿才汇报）

- [ ] 五组端点齐；每个字段行可回答「出处是 TD 哪一表哪一字段」
- [ ] 无凭空字段；PRD 权限口径（药单医生改/护工只读）未走样
- [ ] 差异清单覆盖五页面 Mock 现状，无漏页
- [ ] 文件标注「待冻结」并登记 `docs/plans/README.md`；当天交 dev-D 对齐（N3）

---

## 6. 自裁登记项（随代码同提交留痕）

| # | 事项 | 自裁口径 | 依据 |
| --- | --- | --- | --- |
| Z1 | llm_first_token 打点口径 | LLM 当前非流式（`stream: False`），首 token ≈ 整段 chat 返回时刻；`source` 注明 `non_stream`，Day6+ 流式化后口径自动变准 | TD Day5 未定义非流式口径 |
| Z2 | tts_first_packet 口径 | TTS 非流式，首包 = 整段可用时刻（`_observe.record_tts` 既有口径沿用） | tts.py 既有实现 |
| Z3 | metrics 批量缓冲形态 | 进程内 asyncio 队列 + 定期 flush；flush 失败降级日志不抛出（观测不阻塞主链路） | AGENTS §2 唯一写库层 + 观测旁路性质 |
| Z4 | trace_id 取不到的行不入库 | 只留日志轨 + warning；不为对齐而伪造 trace_id | AGENTS §4 红线 9 同源纪律 |

## 7. 风险与依赖登记

| # | 风险/依赖 | 影响 | 处置 |
| --- | --- | --- | --- |
| Dp1 | A-3 硬依赖 dev-C C-2 的 20 次测量完成（N1） | 基线表当天出不来 | A-2 完成即时通知；S4 弹性排到联调窗 |
| Dp2 | playback 段依赖 dev-C C-6 上报（R3） | 该段数据可能晚于 A-2 | 收口函数先支持，缺口登记不造假数 |
| Dp3 | day04/task-02（信令对话收尾）未收口则链路不稳、基线无效 | A-3 数据无效 | 沿 day-05 计划 S0 冒烟前置，不通过不测量 |
| Rk1 | autogenerate 可能漂移命名/索引 | 迁移 diff 不干净 | 先写模型再 autogenerate + 人工读 diff（AGENTS §3） |
| Rk2 | 27 张表口径扩散（其他文档仍写 26） | 口径不一致 | 本次只改 TD §3.6 与 ER；其余文档出现 26 表处逐处核对登记，不顺手改无依据处 |

## 8. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | 首版：基于 dev-a 任务卡、dag N1/N3、day-05 计划 S1–S3/S7-A2 与代码现状核查编制；四项主理人裁决（R1–R4）入 0.1 节；三件套纳入 A-1、契约修订与 dev-c 卡追加（C-5/C-6）纳入 A-2、A-4 定独立契约文件 |
| v1.1 | 2026-11-07 | 执行进展：S1（A-1）完成——三件套（ER v0.4/B6、TD v1.8、README 冲突两行）+ ORM/0006 迁移/metrics_service/8 用例；存量修复 guardians 409（主理人批准顺手修）。S2（A-2）完成——C2/C5 契约修订 v1.5（wakeup 统一走 C2 auth 首帧，偏差 1 已在 C5 修订记录登记）、双轨改造与缺段接线（route 段登记缺口待 Day 6 路由接线，偏差 2）、playback 代打、9+1 用例。S3（A-4）完成——docs/接口契约_照护台业务数据.md v0.1（待冻结，差异清单 D-1~D-10、开放问题 Q-1/Q-2）。S4（A-3）待 dev-C C-2 数据 |
| v1.2 | 2026-11-07 | 主理人决定：**Q-1/Q-2 悬置至 S4 数据齐后一并裁决**，契约保持 v0.1「待冻结」不阻塞 dev-D 阅读对齐；S4 触发条件 = dev-C 完成 C-2 的 20 次测量（N1），届时先 TRUNCATE metrics 清测试残留再复算 |
