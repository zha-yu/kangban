# Day 5 工作推进计划 · 延迟基线与护栏

> 状态：进行中　｜　版本：v1.3　｜　日期：2026-11-07
> 关联：docs/team_tasks/day05/a0-设备密钥轮换溯源.md
> 上游依据：技术设计文档 §8「Day 5 · 延迟与护栏」（L1036–1043）｜PRD FR-3D-01、FR-DLG-10、§5.3（AC-40/42）｜大纲 §Day5 课时 5.1–5.4（仅取流程）
> 变更记录见文末

---

## 0. 已确认口径（2026-11-07 主理人裁决，本计划执行基线）

| # | 事项 | 裁决 |
| --- | --- | --- |
| D1 | 口型同步（大纲 5.1） | **维持现状**：lipsync.ts 能量近似驱动已满足 PRD FR-3D-01 验收（「播放任一 TTS 时可观察到口型开合动画」），不升级音素级 viseme。大纲 5.1 与 PRD 冲突，按效力顺序以 PRD 为准，登记入 README「已知文档冲突」 |
| D2 | 延迟基线落地形态 | **建 metrics 表**：新增第 27 张表存分段耗时明细，P50/P95 用 SQL 复算。属自裁，须完成「文档留痕三件套」（见 S1），不改动已合入迁移，走新迁移文件 0006 |
| D3 | 死循环护栏边界 | **Day5 建 `app/agents/guardrails.py` 骨架 + 假工具单测**（每轮 ≤8 次、同 Tool 1 分钟内第 3 次熔断），Day6 接真 Tool 直接生效 |
| D4 | 可提前启动项 | 四项全批：①家属端契约冻结评审 ②console Mock 接口契约草案 ③admin 管理页提前 ④Day6 Laya spike 预研 |
| D5 | 声纹匹配阈值（2026-11-07 补充裁决） | **0.85 → 0.55（CAM++ 教师口径）**。依据 spikes/voiceprint 实测同人 0.517–0.568、0.85 必误拒。已同批落地：PRD §3.2/FR-VPR-01/02/AC-28（修订记录 v2.8）、TD 四处（v1.7）、AGENTS §6/§7、身份契约 v1.9、`config.py voiceprint_similarity_threshold=0.55`。**闭环真人验收仍未测**，S0 执行 |

---

## 1. 项目认知摘要

**产品边界**（PRD）：教学模拟场景的 CCRC 康养 3D 数字人系统。三端同仓：pad（1920×1080 长者端）/ console（护工·医生·管理员按角色分流）/ family（375px 家属端）。五类职责：情感陪伴、用药提醒、健康采集、戏曲/天气检索、人工告警。**不做医疗诊断、不替代 SOS、除天气外全部外部系统走 Mock**。四条端到端验收链路：用药、健康、告警、日报。

**技术架构**（TD）：六层（前端→接入→智能→能力→数据→外部/Mock）。对话路由 **L0 规则层（≤10 条，≤200ms）→ L1 Laya 分类器（confidence≥0.85 直路由，低置信澄清一次）→ L2 主控 Agent（ReAct，DeepSeek/Qwen）**，安全一票否决最前置；语音 WebRTC 全双工（按住说话降级）；LangGraph 两业务子图 + PG 独立 `langgraph` schema；全链路 trace_id。

**核心目标**：十天迭代内「可演示、可测试、可观测、可降级」，端到端 P95 ≤ 1.5s（简单指令 20 次中 ≥19 次达标）。

**硬约束**：26 表分 7 批迁移（TD §3.6，主迁移合入不改）；`app/services/` 唯一写库层；分层依赖单向；数值阈值写死常量；大纲只供流程、事实一律回 PRD/TD。

---

## 2. Day 5 任务拆解表

| # | 任务 | 前置依赖 | 任务间依赖 | 执行步骤 | 量化验收标准 |
| --- | --- | --- | --- | --- | --- |
| D5-1 | a0 设备密钥轮换溯源 | 问 A2/E 组同事 11-06 上午设备绑定测试记录 | 独立，可先行 | ① 问人定位操作者 ② 排查绕过 admin 直写 devices 的测试脚本，有则收口 ③ 复核 `_clear_binding` 审计落库（`audit_logs` 表 Day9 建，当前按身份契约 S-08 落结构化日志）④ 给出结论或收口 | 能说清轮换来源（人或脚本）；unbind/bind 操作有可查记录；任务卡状态更新 |
| D5-2 | 口型同步（已达标项） | Day3/4 TTS 下行链路已通 | 无 | 无需开发；仅在 README 登记大纲 5.1 与 PRD FR-3D-01 的冲突及 D1 裁决 | 播放任一 TTS 可观察口型开合（现状即满足，录屏留证） |
| D5-3 | 延迟基线：metrics 表 + 打点收口 | Day3–4 链路打点已具备（core/metrics.py、audio/_observe.py） | 先于 D5-4 基线测量 | ① 文档留痕三件套（S1）② ORM 模型 + 0006 迁移 ③ 打点写入 metrics 表（沿用 core/metrics.py 单点收口，调用方不动）④ SQL 复算 P50/P95 | 打点六段全覆盖（唤醒→ASR→路由→LLM 首 token→TTS 首包→播放）；20 次测量数字可复算；无一条打点含转写全文或隐私字段 |
| D5-4 | P50/P95 基线测量 + 超时熔断三档 | D5-3 完成 | 依赖 D5-3 | ① 跑 20 次「今天天气怎么样」记录分段耗时 ② 出基线表入档（Day6 对照分母）③ 三档降级：ASR>2s→「没听清再说一遍」；LLM 首 token>3s→播「我想一想」占位；TTS 首包>1s→降级文字 | P50/P95 表格可复算；三档超时逐个演示生效、无一次白屏或静默；基线表入档 |
| D5-5 | 死循环护栏骨架 | 无（不依赖 Agent） | 与 D5-3 并行 | ① `app/agents/guardrails.py`：`MAX_TOOL_CALLS_PER_TURN=8`、同 Tool 1 分钟内第 3 次熔断、重复动作检测 ② 假工具单测：第 9 次调用被拒并如实告知；1 分钟内第 3 次同 Tool 触发熔断 | 两类熔断用例全绿；常量取值与 AGENTS §7 一致；Day6 注册 Tool 后自动生效 |
| D5-6 | 修债补测试 | 现有 27 个测试文件 | 与 D5-3/D5-5 并行 | 核心包（auth / elders / devices / 音频信令）覆盖摸底→补缺口→清 TODO | `uv run pytest --cov=app --cov-report=term-missing` 核心包 ≥70% |
| D5-7 | Iter2 验收演示收尾 | D5-2、D5-4 完成 | 收尾依赖项 | ① 5 分钟演示脚本（开场→三功能→一降级→结尾）② 照脚本演练 ③ git tag `iter2-demo` | 演示不翻车；延迟基线表已入档；tag 打成功 |

### 2A · 前期遗漏盘点（Day 1–4 未收口项，v1.2 按主理人确认修订）

> 处置原则：任务卡仍归原负责人，Day5 计划负责**盘点、依赖提示与演示前收口检查**。v1.2 依据主理人 2026-11-07 确认「实际已做完、卡未更新」修订状态。

| 来源卡 | 实际状态 | 遗留内容 | 对 Day5 的影响 | 处置 |
| --- | --- | --- | --- | --- |
| day04/a0 联调遗留修复（v1.3） | 实现已完成（主理人确认），卡仍「进行中」 | ①浏览器真链路勾验 ②F-03 重建的 tasks.ts 待主理人复核 diff ③卡状态/变更记录回填 | S6 演示前必须勾验 | 勾验并入 S6 演示预演；F-03 复核提示主理人（S0）；执行者回填卡 |
| day04/task-01 声纹身份闭环 | **实现已落地、阈值已裁决（D5）**：spike + deploy/voiceprint 容器（7003）+ voiceprint_service + identity_service 首轮校验 + pad 录入页/门卫 + 0005 迁移 + `config.py=0.55`（2026-11-07 五处文档同批修订）；卡仍「未开始」 | ①**真人录音闭环验收未测**（录入→命中→主动确认全链路无实测记录）②卡状态/变更记录回填 | Iter2 演示「身份确认」硬依赖 | **S0 第一优先**：按 0.55 阈值真人录音跑通闭环 → 实测记录回填 task-01 卡 → 回填卡状态 |
| day04/task-02 信令与对话闭环 | 进行中（v1.9） | 对话主链收尾 | S3 延迟测量走对话主链，链路不稳则基线无效 | 向 A2 确认收口时点；S3 开测前冒烟确认 |
| day04/task-04 VAD 主链与遗留修复 | 实现已完成（主理人确认），卡仍「进行中」 | 卡状态/变更记录回填 | 影响 ASR 分段打点口径 | S0 核实回填；S3 开测前冒烟确认 |
| day04/task-05 Day4 前端联调 | 实现已完成（主理人确认），卡仍「未开始」（v2.1） | 卡状态/变更记录回填 | 演示走查依赖 | S0 核实回填；并入 S6 演示预演 |
| day04/a0 O-1 观察项 | 留痕未处置 | FunASR 首包实测 2219ms ＞ 300ms 预算（本地 2pass 固有延迟） | **S3 基线口径**：ASR 段即超 1.5s 总预算，P95 大概率不达标 | S3 基线表按「本地 FunASR 基线」单独归档并注明，云 key 到位后复测；不美化数字 |
| day05/a0 设备密钥轮换溯源 | 未开始 | token_hash 被未知改写，日志在同事终端无法追溯 | 独立 | 即 D5-1（S5 内执行） |

---

## 3. 前端核查表（pad 除外）

### 3.1 页面完整性现状

| 端 | 页面 | 状态 | 数据来源 |
| --- | --- | --- | --- |
| console | LoginPage | 已实现·真实对接 | `/api/sessions` 三件套（E1/E2/E3） |
| console | EldersPage 负责长者 | 已实现·真实对接 | E7/E6 |
| console | WorkOrdersPage 工单看板 | 已实现·**Mock** | 后端无端点（`work_orders` 表 Day7 批次） |
| console | AlertsPage 告警中心 | 已实现·**Mock** | 后端无端点（`notifications` Day8） |
| console | HealthFormPage 健康代填 | 已实现·提交 Mock（长者下拉真实 E7） | `health_records` Day8 |
| console | MedicationPage 药单只读 | 已实现·**Mock** | `medication_plans` Day7 |
| console | ReviewQueuePage 复核队列 | 已实现·**Mock** | `health_records` + HITL，Day8 |
| console | MedicationEditPage 药单维护 | 已实现·**Mock** | 同上 |
| console | admin 角色 | **占位**（侧栏全置灰） | 后端 `/api/staff`、`/api/rooms`、`/api/devices` 已就绪未接 |
| console | guardian 角色 | 占位提示（应走 family） | — |
| family | LoginPage | 已实现·真实对接 | `/api/sessions` |
| family | HomePage 长者切换 / ReportsPage 日报 / NotificationsPage 通知 | 已实现·**全 Mock** | `/api/family/*` 5 端点后端未注册；表 Day8 批次 |

### 3.2 缺失项结构化清单

| 缺失项 | 对应任务 | 影响范围 | 优先级 | 前置条件与依赖 |
| --- | --- | --- | --- | --- |
| 工单看板后端接口 | TD Day7 批次（`work_orders`/`work_order_transitions`） | 护工看板、漏服工单链路 | 高 | 等建表；契约草案本次提前 |
| 告警通知后端接口 | Day8 `notifications` | 告警中心、家属通知 | 高 | 等建表；`risk_events` 在 Day7 |
| 健康代填提交接口 | Day8 `health_records` + 服务端分级规则 | 健康→复核→日报链路 | 高 | 等建表 + 规则层（Day6） |
| 药单只读/维护接口 | Day7 `medication_plans`/`medication_events`/`medication_attempts` | 医生药单、pad 用药卡片 | 高 | 等建表；CSV 导入属 Day7 |
| 复核队列接口 | Day8 `health_records` HITL | 医生复核、版本链 | 高 | 等建表 |
| family 日报/通知/授权长者 5 端点 | Day8 批次 + 《接口契约_家属端数据》（**草案 v1.0 未冻结**） | family 三业务页 | 高 | 契约冻结评审（本次已批）；`elder_guardians` 表已建 |
| admin 管理页前端 | 前端页面方案 §3.4 第 4 批，**本次裁决提前** | 管理员演示场景 | 中 | 后端端点已就绪，无阻塞 |
| 医生 14 天趋势图（FR-HLT-09）、药品疑问转告（FR-DLG-09） | 第 3/4 批余项 | 医生角色完整性 | 中 | `health_records`（Day8） |
| pad 戏曲/天气结果卡、记忆查看、首次引导 | Day6/7 功能后接 | 演示完整度 | 中 | Day6 三级路由、Day7 记忆/娱乐 |

**后端已有→前端未接**：`/api/staff`(E12)、`/api/guardians`(G1/E13)、`/api/rooms`(E18)、`/api/devices`、`/api/voiceprints`、`/api/audio`。
**DB 现状**：ORM 已建 12 表（A/B 组 + voiceprints + conversation_threads，迁移 0001–0005）；C/D 组（medication_*、health_records、work_orders*、notifications、daily_reports、decision_logs、audit_logs、memory_chunks、media_*）未建——与前端 Mock 项一一对应，属排期内缺失而非遗漏。

---

## 4. 可提前处理内容清单（主理人已批，全部纳入本计划）

| # | 可提前内容 | 交付物 | 依赖条件 |
| --- | --- | --- | --- |
| A1 | 《接口契约_家属端数据》草案 v1.0 → 冻结评审 | 冻结版契约（状态行改「已冻结」+ 修订记录） | 主理人评审签字 |
| A2 | console 五个 Mock 页接口契约草案 | `docs/接口契约_照护台业务数据.md`（或并入现有契约文件，评审时定） | 仅文档，对照 TD §3 表定义起草，字段行不得自创 |
| A3 | admin 管理页提前 | console admin 角色页面（用户/角色/长者绑定/模拟设备，对齐 FR-ADM-01/06 与 E12/E18 端点） | 后端端点已就绪；排 Day5 机动位或 Day6 前完成 |
| A4 | Day6 Laya spike 预研 | `spikes/laya_hello/` 独立脚本：能装、能跑、记 CPU 单句延迟 | 确认本地权重获取方式；**不合并主线** |

---

## 5. 工作推进计划（分阶段，每阶段完成后停下汇报，经确认再进入下一阶段）

> 执行顺序主线：**S0 → S1 → S2 → S3 ∥ S4 → S5 → S6**；A1–A4 穿插在机动位。每阶段的「自检校验」全绿才汇报，汇报格式：diff 摘要 + 自检结果 + 遗留问题。

### S0 · 前期遗漏收口前置检查（v1.2 修订，半天内）

**工作内容与步骤**：
1. ~~声纹阈值裁决~~ **已完成（D5，2026-11-07）**：阈值 0.55 已同批落地 PRD v2.8 / TD v1.7 / AGENTS §6+§7 / 身份契约 v1.9 / `config.py` 默认值。
2. **声纹闭环真人验收**（task-01，第一优先）：3 段真人录音录入 → 同人第二次登录命中（voice 终态 + cosine 值记录）→ 非同人走主动确认 → 任何一步失败按 C4 降级放行。实测记录回填 task-01 卡，卡状态更新。
3. **卡状态回填**：task-04、task-05、day04/a0 三张卡按主理人确认的「实际已完成」回填 `> 状态：` 行与变更记录（原执行者名下，PROGRESS 钩子自动刷新）。
4. **task-02 收口确认**：向 A2 确认信令与对话收口时点；S3 开测前对话主链冒烟（pad 登录→WS auth_ok→WebRTC 建连→说话→asr_final 真实文本），链路不稳不进基线测量。
5. 提示主理人复核 F-03（day04/task-05 执行者重建补交的 tasks.ts diff）。

**交付物**：声纹阈值裁决记录（回填 spike README「遗留决策」节）；task-01 闭环实测记录；三张卡状态回填。

**自检校验**：
- [ ] 阈值裁决有明确结论且三处文档（如选方案 A）同提交；未裁决则闭环实测不启动
- [ ] task-01 实测：同人命中、非同人主动确认两条路径均有记录
- [ ] task-01/04/05、day04/a0 卡状态已回填（PROGRESS 钩子刷新）
- [ ] task-02 收口时点有明确答复；冒烟通过（或明确的不通过结论 + 阻塞登记）

### S1 · metrics 表文档留痕三件套（自裁合规先行）

**工作内容与步骤**：
1. 技术设计文档 §3.6 批次表补「Day 5（1 张）｜ metrics ｜ 延迟基线测量」一行；文末修订记录追加 vNext 条目（写明：改哪条、依据 2026-11-07 主理人裁决 D2、影响 26→27 表口径）。
2. README「已知文档冲突」表登记两行：大纲 5.2「metrics 表」vs TD §3.6 无此表（已裁决建表）；大纲 5.1 viseme vs PRD FR-3D-01 能量近似（已裁决维持 PRD）。
3. `docs/ER_数据模型设计.md` 同步补 metrics 表定义（同一次提交）。

**metrics 表字段草案**（实施时按 PRD §5.3 分段口径定稿，本计划先冻结骨架）：

```text
metrics
- id            UUID PK
- trace_id      varchar(64) not null   -- 关联全链路
- elder_id      UUID null              -- 可空：唤醒前打点无身份
- stage         枚举：wakeup / connection / asr / route / llm_first_token / tts_first_packet / playback
- duration_ms   integer not null
- source        varchar(32)            -- 端侧 wake_ts / 服务端兜底等口径区分
- created_at    timestamptz not null default now()
索引：(trace_id)、(stage, created_at)
```

**交付物**：TD 修订记录条目、README 冲突表两行、ER 表定义、迁移文件 `0006_day5_metrics.py`（先模型后 autogenerate，人工读 diff）。

**自检校验**：
- [ ] 修订记录与正文改动同一次提交；未删除或回填历史修订行
- [ ] 已合入迁移 0001–0005 一字未改
- [ ] `uv run alembic upgrade head` 成功、`alembic current` 指向 0006
- [ ] 表结构三处（ER/TD/ORM）一致

### S2 · 打点收口进 metrics 表

**工作内容与步骤**：
1. 只改 `app/core/metrics.py` 与 `app/audio/_observe.py` 两个单点收口的函数体：结构化日志照旧输出，同时异步批量写入 metrics 表（写库经 `app/services/` 新增 `metrics_service.py`，遵守「唯一写库层」）。
2. 核对打点位六段齐全：wakeup（端侧 wake_ts 优先，WS accept 兜底）→ connection → asr → route → llm_first_token → tts_first_packet → playback。
3. 路由/LLM 阶段当前无独立模块的，在现有 dialogue 链路对应位置调 `record_stage`，不新建第二套打点。

**交付物**：`app/services/metrics_service.py` + 两个收口文件的最小改动 + 补充测试。

**自检校验**：
- [ ] `api/audio/scheduler/graphs` 未直接写库（分层单向）
- [ ] 任取一条 metrics 行，能按 trace_id 串起一次完整对话各分段
- [ ] 无一行打点包含转写全文、手机号等隐私（复跑 `test_audio_*`、`test_core_metrics.py` 全绿）
- [ ] 日志与库内数据双轨一致（同一 trace_id 抽查 3 条）

### S3 · P50/P95 基线测量 + 三档超时熔断（依赖 S2）

**工作内容与步骤**：
1. **开测前冒烟**（依赖 S0 结论）：对话主链（task-02/04）收口确认后，走一遍 pad 登录→建连→说话→asr_final，链路不稳不进基线测量。
2. 跑 20 次「今天天气怎么样」（固定种子设备 + 王奶奶人设），逐次记录分段耗时入 metrics 表。
3. SQL 复算 P50/P95，出基线表文档 `docs/plans/day05/day-05-延迟基线表.md`（含样本量、测量方式、环境说明；全走大模型口径，作 Day6 对照分母）。**O-1 口径**：ASR 段按「本地 FunASR 基线」单独归档并注明首包 2219ms 实测背景，云 key 到位后复测；P95 大概率超 1.5s，如实入档不美化，Day6 分层后对比。
4. 三档超时降级实现与演示：ASR>2s→「没听清再说一遍」；LLM 首 token>3s→播「我想一想」占位；TTS 首包>1s→降级文字。降级话术进常量不进 prompt。

**交付物**：延迟基线表 md（含 FunASR 单独归档口径）；三档超时降级代码 + 录屏证据。

**自检校验**：
- [ ] S0 冒烟已通过后再测量
- [ ] P50/P95 由 SQL 对 metrics 表复算得出，报告附查询语句，数字可复算
- [ ] 基线表明确标注「本地 FunASR 基线」与云 key 复测条件
- [ ] 三档超时逐个人为制造（弄坏 ASR/LLM/TTS），页面提示各自不同且可读，无一次白屏或静默
- [ ] PRD §5.3 口径：简单指令 ≤1.5s 达标次数如实记录（达标与否都如实入档，不美化）

### S4 · 死循环护栏骨架（与 S2/S3 并行）

**工作内容与步骤**：
1. 新建 `app/agents/guardrails.py`：`MAX_TOOL_CALLS_PER_TURN = 8`；同 Tool 1 分钟滑动窗口内第 3 次调用即熔断；重复动作检测接口（Day6 接 Tool 注册表）。
2. 单测：假工具场景下第 9 次调用被拒并返回如实告知文案；1 分钟内同 Tool 第 3 次触发熔断；窗口过期后计数复位。

**交付物**：`app/agents/guardrails.py` + `tests/test_guardrails.py`。

**自检校验**：
- [ ] 常量取值与 AGENTS §7 / PRD FR-DLG-10 一致，无私改
- [ ] `uv run pytest tests/test_guardrails.py -q` 全绿
- [ ] 模块无对外部服务依赖，Day6 注册 Tool 后无需改 guardrails 本体

### S5 · 修债补测试 + a0 溯源收口（机动穿插）

**工作内容与步骤**：
1. `uv run pytest --cov=app --cov-report=term-missing` 摸底，核心包（core/auth、services/auth_service、elders/devices API、audio 信令）低于 70% 的补测。
2. 清 Day1–4 攒的 TODO（只清已裁决项，新增决策先登记）。
3. a0 卡（day05）：问人 → 查脚本 → 核对 `_clear_binding` 审计落库 → 结论回填任务卡并更新状态行；种子 secret 再被轮换时按卡内待办 4 的 sha256 方法重置。
4. day04/a0 遗留跟进：真链路勾验待办移交 S6 演示预演执行；F-03 复核提示主理人（S0 第 3 步）。

**交付物**：覆盖率报告；TODO 清理清单；day05/a0 任务卡状态更新。

**自检校验**：
- [ ] 核心包覆盖率 ≥70%，报告入档
- [ ] `uv run pytest -q` 全量绿
- [ ] day05/a0 有明确结论（人或脚本），任务卡 `> 状态：` 更新（PROGRESS 由钩子自动刷新，不手改）

### S6 · Iter2 演示收尾（依赖 S3；含 Day4 真链路勾验）

**工作内容与步骤**：
1. 5 分钟演示脚本：开场 → 三个功能（语音对话/**声纹身份确认**/3D 口型）→ 一个降级（TTS 超时降文字或断线重连）→ 结尾；入档 `docs/plans/day05/day-05-演示脚本.md`。
2. **演示预演兼 Day4 遗留勾验**：同一遍走查覆盖 day04/a0「浏览器真链路勾验」（pad 登录→WS auth_ok→WebRTC 建连→说话→asr_final 真实文本；按住说话→真实转写）与 day04/task-05 联调主线走查，结果回填对应任务卡。
3. 照脚本完整演练一遍，翻车点即修。
4. `git tag iter2-demo`。

**自检校验**：
- [ ] 演示一次通过不翻车（录屏留证）
- [ ] day04/a0 真链路勾验结论已回填该卡变更记录
- [ ] tag 已打且指向演示版 commit

### S7 · 可提前项机动位（A1–A4，穿插执行）

| 项 | 步骤 | 交付物 | 自检 |
| --- | --- | --- | --- |
| A1 家属端契约冻结 | 按草案 §6 变更流程走冻结评审，状态行改「已冻结」+ 修订记录 | 冻结版契约 | 修订记录与正文同提交；三行分类（字段/判定/自裁）未被混淆 |
| A2 console 契约草案 | 对照 TD §3（work_orders*/medication_*/health_records/notifications）起草端点契约，Mock 字段与 TD 字段逐一对齐，差异列表登记 | 契约草案（标注「待冻结」） | 每个字段行能回答「出处是 TD 哪一行」；无凭空字段 |
| A3 admin 管理页 | console admin 角色接 `/api/staff` `/api/rooms` `/api/devices`（用户/角色/长者绑定/模拟设备，FR-ADM-01/06） | admin 可用页面 | 三页面走通 E12/E18 真实端点；越权场景（非 admin 访问）被拒 |
| A4 Laya spike | `spikes/laya_hello/`：安装→加载 checkpoint→官方 demo→记录 CPU 单句延迟 | spike 报告（能装/能跑/延迟数字） | 不动主线代码；延迟数字实测非估算 |

---

## 6. 需确认问题清单（本轮已闭环）

| # | 问题 | 状态 |
| --- | --- | --- |
| 1 | 口型口径：PRD 能量近似 vs 大纲 viseme | ✅ 已裁决：维持现状（D1） |
| 2 | 延迟基线落点：报告入档 vs 建 metrics 表 | ✅ 已裁决：建 metrics 表（D2，自裁留痕见 S1） |
| 3 | guardrails Day5 边界 | ✅ 已裁决：骨架 + 单测，Day6 生效（D3） |
| 4 | 可提前项范围 | ✅ 已裁决：A1–A4 全批（D4） |
| 5 | a0 溯源需 A2/E 组同事提供 11-06 上午测试记录 | ⏳ **待人工跟进**（S5 内执行，需主理人协调同事） |
| 6 | A2 契约草案落点：新建独立契约文件 vs 并入现有文件 | ⏳ 待主理人定（S7 执行前确认） |
| 7 | ~~声纹阈值两套口径裁决~~ | ✅ 已裁决并落地（D5，2026-11-07）：0.55（CAM++ 教师口径），PRD v2.8 / TD v1.7 / AGENTS / 契约 v1.9 / config 同批修订 |
| 8 | day04/task-02（信令对话）收口时点，S3 依赖其冒烟通过 | ⏳ 待 A2 确认（v1.1 补） |

---

## 7. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | 首版：基于 PRD/TD/大纲核查与四项主理人裁决（D1–D4）制定 Day5 全量推进计划 |
| v1.1 | 2026-11-07 | 补前期遗漏盘点（§2A）：Day1–4 七项未收口遗留入表；新增 S0 前置检查；S3 补冒烟前置与 O-1 FunASR 基线归档口径；S5/S6 并入 day04/a0 真链路勾验与 task-05 走查 |
| v1.2 | 2026-11-07 | 按主理人确认「task-01/04/05、day04/a0 实际已完成、卡未更新」修订 §2A 状态；task-01 实现已核实落地（voiceprint_service/identity_service/deploy 容器/pad 录入页/0005 迁移）；新增声纹阈值两套口径裁决（0.55 vs 0.85）为 S0 第一优先与需确认 #7 |
| v1.3 | 2026-11-07 | 主理人裁决 D5：声纹阈值 0.85→0.55 已同批落地（PRD v2.8、TD v1.7、AGENTS §6/§7、身份契约 v1.9、config.py 默认值）；§2A task-01 与 S0 改为「阈值已落地、真人闭环验收未测」，S0 第一优先改为执行实测 |
