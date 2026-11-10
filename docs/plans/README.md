# 计划索引（docs/plans）

> 规则见 `AGENTS.md` §10。**新增计划必须同一次提交里加进下表**，否则等于没写。

## 命名

`<类别>-<编号>-<主题>.md`

| 类别 | 编号 | 例 |
| --- | --- | --- |
| `day` | Day 序号两位 | `day-02-身份与权限.md` |
| `task` | `docs/team_tasks/` 卡序号两位 | `task-03-库与模型基座.md` |
| `adhoc` | `YYYYMMDD` | `adhoc-20261104-契约冻结.md` |

## 索引

| 文件 | 一句话场景 | 状态 | 最后更新 |
| --- | --- | --- | --- |
| [adhoc-20261109-汇报材料收口.md](./adhoc-20261109-汇报材料收口.md) | Day 6 汇报稿、通信流程图与源码注释对齐：澄清回复音频的 WebRTC 写轨路径、用药主动语音尽力出口、文本投递回执语义与当前工作区状态 | 已废弃（文件已删除） | 2026-11-09 |
| [adhoc-20261108-房间床位管理.md](./adhoc-20261108-房间床位管理.md) | 管理端房间/床位管理实施计划（v1.6 已完成）：E18 只读 → E20–E29 十个管理端点 + `org_service` 唯一写库层 + `aggregate_room_status` 聚合 + `audit_logs` 提前落地（0007）+ console 管理页；含落地差异 6 条（FR-ADM-11 编号、`check_roles` 403 口径、端点 10 个、计数前端派生、走查数据、跳过 downgrade） | 已完成 | 2026-11-09 |
| [adhoc-20261109-房间床位交付存档.md](./adhoc-20261109-房间床位交付存档.md) | 房间/床位功能的交付断点存档（原 `task_checkpoint.md`）：项目目标与技术选型、硬性决策 D1–D10、Git 现状与提交边界、接口 E20–E29 全量定义、业务规则 14 条、已知坑 13 条、验证记录与账号环境；供跨会话恢复与复盘 | 已完成（归档） | 2026-11-09 |
| [day08/day-08-健康采集与三端后台.md](./day08/day-08-健康采集与三端后台.md) | Day 8 完整推进计划 v1.1（待主理人确认执行）：H0–H13 任务拆解（前置/依赖/步骤/量化验收）+ DAG + 交付物规格 + 命令级自检；代码现状基线（迁移止于 0016、健康子图/服务未建、mock-operations 已提前交付未注册）；四项裁决登记（D-1 声纹调优改裁决**纳入 Day 8 以 H13/P1 承载** / D-2 医院对接不排期 / D-3 护工看板维持 React / D-4 聚合预警按 P2 弹性）；Day 8 批次 3 表中 notifications 复用 0011，仅 0017 建 health_records + daily_reports | 草稿 | 2026-11-10 |
| [adhoc-20261110-mockoperations提前实现.md](./adhoc-20261110-mockoperations提前实现.md) | mock-operations MCP 提前实现（Day 8 范围、Day 7 空闲产能）：`notify_service` 家属通知唯一写库口 + `mcp-servers/mock-operations` 两工具 server（7102）+ 单测；关键约定 `subject_elder_id` 身份注入参数名；ToolExecutor 注册与 AC-39 证据留 Day 8 | 进行中 | 2026-11-10 |
| [adhoc-20261110-day6全面审查.md](./adhoc-20261110-day6全面审查.md) | Day 6 全面代码审查（只读）：PR #54–57 + 工作区 diff×3 三通道深审合并为 P1×6 + P2×18 + P3×14，每条含文件:行号与证据；D1 根因精确化（finish_turn 零调用）、D3 收敛为双候选（VAD threshold=0.6 / feedWindow 砖死）+ 零遥测盲区；新发现 StateUnavailable 生产不可达、E20 并发无行锁、context.py None+=str 整轮崩、预算双重失真；未提交 diff 结论：质量良好可提交 | 已冻结 | 2026-11-10 |
| [day06/day-06-长者侧Agent闭环.md](./day06/day-06-长者侧Agent闭环.md) | Day 6 完整项目计划 v3.11（执行版）：四处冲突处置（讲义 vs PRD/TD 排期、记忆与用药前置、Laya 不排期且不接第三方商业 API、天气 MCP 保留）+ **25 节点** DAG（6 波次 / 22.5h 关键路径 / 10 把写锁 / 6 道门禁 / 5 条回退）+ **16 项**交付物 + **16 条**自检判据 + 容量实测（S 50.5h / M 68.5h / L 73.0h）+ D-1~D-5 与 Z-1~Z-6 裁决登记 + **§11 执行记录**（N00 契约冻结、0008/0009/0010 三迁移 apply 与可逆性实测、G0/G1 结论）+ §11.4 转达件⑥（scheduler 接线归属：N10 不移交、另立 dev-C2 卡） | 进行中（N00 与三条迁移已执行） | 2026-11-10 |
| [day06/d-推进计划-pad送达与改绑.md](./day06/d-推进计划-pad送达与改绑.md) | Day 6 dev-D 个人实施计划：S1–S8 分步确认制（契约代码同步 → 播放队列 → realtime 接线 → 端到端 20 轮 → 契约增补提案 → 员工列表与改绑弹窗 → 上下文样例 → 收口）；登记三项裁决（GET /api/staff 契约增补通知 dev-B、分段「下一段到达即结算」、接 N22 上下文样例）+ J-Contract/J3/J4/J6 交接清单 + R1–R6 风险 | 已冻结（S1–S4/S6–S8 完成，真人复测延后） | 2026-11-10 |
| [day06/d-DAG.md](./day06/d-DAG.md) | Day 6 独立 DAG 执行路径件（唯一顺序源）：**25 节点 / 36 条边（34 硬 + 2 软）/ 7 波次 / 关键路径 22.5h / 10 把写锁 / 6 道门禁 / 5 条回退**，含节点执行状态（N00 与三条迁移已完成）、W5 合流串行点说明与机器可读 JSON；与主计划 v3.0 对齐 | 进行中 | 2026-11-09 |
| [day06/d-s7-上下文样例底稿.md](./day06/d-s7-上下文样例底稿.md) | S7 上下文样例底稿（真实装配产出，供 dev-B N22 B-7.2 引用）：三场景真实 messages（正常轮 704/4000、generic 对照、budget=120 裁剪演示）、七层↔消息序号映射、四个现状发现（历史 assistant content=null 污染 LLM 请求【D1 影响面升级】/ older_summary 实为原文列表 / 记忆层同源重复 / generic 保留用药任务提示的语义矛盾）、B-7.2 对照（12 轮口径应改 5 轮） | 已完成（供 dev-B 引用） | 2026-11-09 |
| [day06/d-s4-20轮验证记录.md](./day06/d-s4-20轮验证记录.md) | S4/N16 端到端验证记录 v1.0：正式 29 轮 + 补测 6 轮，ack 54/54=100% 达标（≥95%）；打断链路端到端证据（canceled 双口径 -1/末播段、清帧 202）；缺陷 D1–D4 登记（turn 终态全量不落库【dev-C P1】/ ack 不落事件表【dev-C P2】/ 端侧 TEN-VAD 漏检 33s=「没有停止」根因【P1 需真人复测】/ metrics 白名单缺 response_segment【P3】）；真人复测清单按主理人指示延后 | 已完成（自动化部分） | 2026-11-09 |
| [day06/dev-c-Agent与统一出口.md](./day06/dev-c-Agent与统一出口.md) | Day 6 dev-C（Agent 与统一出口）执行计划 v1.1：8 节点全量照任务卡（21.5h，C-0-1）；裁决落档 Q-1 自拟 50 例语料 / Q-2 方案 A 提前建 notifications 表（0011 授权 dev-C 建，C-0-4~C-0-6）；S0 核查 → S1 五步（N20→N24→N25→0011→N27）→ S2 桩期（N07/N11/N13 照冻结契约先桩后并）→ S3 对齐（J3/J4/J7）→ S4 N14 七步合流 → S5 收口；13 项交付物红线、10 条自检命令、12 条风险 | 草稿 | 2026-11-09 |
| [day06/dev-a-历史与工具执行计划.md](./day06/dev-a-历史与工具执行计划.md) | dev-A 个人执行计划：7 节点 / 21.0h（已做 4.0h）逐节点执行步骤、8 项交付物规格、7 条命令级自检、三方对接与写锁、5 条风险；Q1（是否接 N11）与 Q2（天气 key）列为待确认 | 已完成 | 2026-11-09 |
| [day06/handoff-A-to-C-segment-ack.md](./day06/handoff-A-to-C-segment-ack.md) | A→C 接线说明：`segment_ack` 四步校验 →按 `index` 拼 `played` 段→ `commit_response` 一次生效；含事件归属表（`response_completed` 归 commit 独占）、调用时序、截断三规则、generic 分支、6 条自检、2 个待 C 确认项 | 已完成（与 N07 实现已对齐） | 2026-11-09 |
| [day07/d-推进计划-pad可靠性改绑联调.md](./day07/d-推进计划-pad可靠性改绑联调.md) | Day 7 dev-D 个人实施计划：D2/D1/D3 分步确认制（S2.1–S3.4 七确认点），代码现状基线实证（P1-4/5/6、P2-22~26、P3-36~40 逐条行号）、三项主理人裁决登记（VAD threshold=0.38 先设值 / D2 造数允许 / D3 硬推进）、§6 联调独立操作手册（L1–L6 含 SQL 核对与异常判定）、§7 交接清单 | 进行中 | 2026-11-10 |
| [day07/d-联调记录.md](./day07/d-联调记录.md) | Day 7 dev-D 验证证据链：D2 造数验证 8 项全过（150 压测长者 / 分页 101+ 可见 / 差集 added+removed / 409 拦截 / 5 次刷新一致 / createStaff 即现 / 401）；D1/D3 待回填 | 进行中 | 2026-11-10 |
| [day07/dev-c-day07-执行计划.md](./day07/dev-c-day07-执行计划.md) | Day 7 dev-C 完整执行计划 v1.0：C1 用药确认工具绑定（1.0d）+ C2 用药到点调度接线（1.0d）+ C3 Agent/安全边界收口（0.75d），总 2.75 人日；11 项交付物（scheduler 新文件 + companion/tool_executor/safety 改写 + 测试扩展）+ 8 道门禁 + 7 条风险预案；硬约束：不碰 graphs/medication_service/main.py/models/alembic | 草稿 | 2026-11-10 |
| [day07/handoff-A-to-C-用药到点接线.md](./day07/handoff-A-to-C-用药到点接线.md) | A→C 接线契约（Day 7）：`APScheduler` 到点 → `medication_confirmation_graph` 驱动链（A 线公开入口逐个签名、**子图工厂签名现为 `tts_fn`**、驱动链与四时点 T0/+5/+10/+15 × 60 秒窗口、终态四值、played 白名单七值、双入口同图、「scheduler 不拼触达消息」）、双驱动先到先得与重启恢复两条硬规矩、幂等红线八条、开放点 O1–O4、自检清单与 SQL 核对；对应新卡 `docs/team_tasks/day07/dev-c2-用药到点触发接线.md` | 已完成 | 2026-11-10 |
| [day07/dev-a-三级路由与决策日志.md](./day07/dev-a-三级路由与决策日志.md) | dev-A 个人推进计划（Day 7）：项目认知基线 + 12 任务总表与依赖拆解 + A1 配置与契约冻结 / A2 Chinese-Jev sidecar（**S2.0 六步形态门禁决定 A/A′/B 三分支**）/ A3 规则层+路由+`decision_logs` 逐步骤执行、交付物规格、量化验收与停止条件 + J1–J6 交接清单 + 写锁与风险回退 + 8 条待确认（含 `route_layer` 新值、规则 10 条清单、`eval/` 新建授权） | 已完成（A1/A2/A3 已落地，7565b6a） | 2026-11-10 |
| [day07/dev-a-prompt-三级路由.md](./day07/dev-a-prompt-三级路由.md) | 可直接投喂 AI 的 dev-A（Day 7）**剩余 A2/A3 执行提示词** v2.0：A1 与 S2.0 门禁标为已完成事实（laya-multilingual 权重/RLAgent 入口/20 条 85%/1 问 167ms、快路径单问+shadow 裁决/五个已知坑）；A2 backend·server·client 与 6 条测试（sidecar 用 `jev` python 启动、重依赖延迟导入、内部预算 180ms）；A3 规则≤10 条（PRD 来源）、`RouteDecision`、`decision_logs` 迁移（计划 0014，实落 **0016**）、决策日志、turn_service 3.5 步、main.py 按真实签名装配（D1–D4）、50 例报告；含 9 项验收总表、6 条停止条件、汇报模板、联调边界 | 已完成（已按此执行） | 2026-11-10 |
| [day07/handoff-A-to-C-调度测试修红.md](./day07/handoff-A-to-C-调度测试修红.md) | A→C 交接件（可直接投喂 dev-C）：develop `4cefef6` 后 `tests/test_scheduler_medication.py` 8 条 fixture 全 ERROR（`Room(room_number=…, room_type=…)` 与 ORM 的 `room_no` 不符，且该 room 未被使用）+ 6 个 ruff 错误；含两步复现、已核实的根因证据、推荐修法（删未使用构造 / 唯一 room_no）、写作用域与禁触、C2 硬口径不许放宽、`install_scheduler` 签名不许改、五条验收命令与报告模板、A 线接口边界提醒 | 已完成（8 ERROR + 10 lint 随 8c980aa 清零） | 2026-11-10 |
| [day07/a3-路由基线.md](./day07/a3-路由基线.md) | **A3 路由基线报告**：110 条固定集（普通 50 / 敏感 20 / 边界 20 / 模糊 20）的意图准确率 40/50、敏感召回 20/20、四层命中率 rule36/system_one74/agent0/fallback0、路由 p50/p95、逐条 bad case 归因；附记含 195ms 预算实测三档环境、shadow 计入耗时口径、方案 B（分意图澄清）落地前后对比、4 条 `safety.py` 边界缺口与 2 个待裁点 | 已完成（2 项指标如实标红） | 2026-11-10 |
| [day07/a2-延迟复测清单.md](./day07/a2-延迟复测清单.md) | **A2 延迟复测执行单**：一句话结论（需要代码、不需改代码；真正要搬的是**权重 + `rl_agent_api.py` + jev 环境**三样）；三种复测形态（同机静默 / 容器 / 换机器）的成本与局限；三步命令（pure 基线 → `--mode all` → degrade）；判定表（含本机实测对照，并注明 p95 含 shadow 等待）；挂了怎么归因的 7 分支表；配套 `scripts/sidecar_requirements.txt` 可重建 sidecar 环境 | 待执行 | 2026-11-10 |
| [day07/handoff-A-to-D-决策日志查询与route_layer语义.md](./day07/handoff-A-to-D-决策日志查询与route_layer语义.md) | **A→D 交接件**：`decision_logs` 14 列逐列语义与坑（`created_at` 由服务层给值、`input_text` 已脱敏、generic 不写）、4 条可直接照抄的联调 SQL（trace 回溯 / 会话轨迹 / 四层命中率+P95 / 敏感对账，均已在本机库验证）、`route_layer` 五值语义（`laya` 只读、`agent=0` 属正常）、3 条易踩口径（无 `action` 列、`latency_ms` 含 shadow、G1 升级只在 `audit_logs`）、7 条联调检查清单 | 待 dev-D 联调使用 | 2026-11-10 |
| [day07/handoff-A-to-B-终态结算补丁清单.md](./day07/handoff-A-to-B-终态结算补丁清单.md) | **A→B1 交接件（有阻塞性）**：A 已按 J2 删掉第 5 步非 generic 双写，而响应层末段 `played` 只写 `{"total_segments":N}`、`commit_response` 又只补投影不改 payload → `generated_text` 不落库、`assistant_delivered_text` 恒 NULL；含两段精确补丁（played 一次写全 payload + interrupt 结算）、为什么必须注入独立短事务的结算口（`trace a5dc0863` 互锁教训）、不许动的三处、三条验收命令与合并顺序建议 | **已发出 2026-11-10 17:32（IM 转达 B1），待回执**；阻塞推送 | 2026-11-10 |
| [day07/dev-b-day07-工作推进计划.md](./day07/dev-b-day07-工作推进计划.md) | dev-B Day 7 执行计划（3.0 人日）：B1 回合终态与打断闭环（`_settle_turn` 结算收口至响应层，末段 played / interrupt / canceled 三路结算）+ B2 上下文 NULL/预算与状态降级 + 记忆生产接线 + B3 metrics 白名单与 E20 并发口径；含 §4 交接矩阵（J2/J3 节点）与逐步确认制 | 待主理人确认 | 2026-11-10 |
| [day07/handoff-B-to-A-终态结算patch需求.md](./day07/handoff-B-to-A-终态结算patch需求.md) | B1→A3 交接件（J2）：`turn_service` 第 5 步删非 generic `commit_response` 双写 + `TurnContext` 补 `user_text` 的 patch 需求清单；登记「双写被幂等兜住、事件恰好 1 条但 payload `response_status` 停在 playing」的现状基线与 A 侧验收命令 | 已完成 | 2026-11-10 |
| [day07/handoff-B-to-D-打断契约与终态口径.md](./day07/handoff-B-to-D-打断契约与终态口径.md) | B1→D1 交接件（J3）：C→S `interrupt` 契约与终态口径**验收基线**（五步顺序、字段以 day07 README §5.4 冻结口径为准、canceled 水位截断、B1 服务端实现与 1043 回归绿证据），D1 直接按本文联调 | 已完成 | 2026-11-10 |
| [day07/handoff-B-to-D2-E20错误码契约.md](./day07/handoff-B-to-D2-E20错误码契约.md) | B3→D2 交接件：E20 改绑错误码契约与并发口径（401/400/409/422 文案与差集提交、迁移 0014 metrics CHECK / 0015 elders.updated_at 授权留痕），服务端 Step 6 已收口回归绿 | 已完成 | 2026-11-10 |
| [day06/a12-工单建单服务接口设计.md](./day06/a12-工单建单服务接口设计.md) | A12 与 N27 共用的**唯一建单口**设计（只定签名不落实现）：`open_work_order` / `transition`、四元幂等键与查重顺序、`order_no` 规则、`severity` 快照语义、状态机与 `false_alarm` 硬规则、两调用方参数映射、请求 0011 的两条索引、7 条验收判据 | 已实现（走 B；WD-1/WD-2 已登记） | 2026-11-09 |
| [day06/dev-a-DAG.md](./day06/dev-a-DAG.md) | dev-A 独立执行 DAG：18 个执行节点（A00–A17，合计 21.0h）、三条支线（历史 10h / 用药 5h / 工具 5h）+ 收口、16 条内部边 + 3 个外部等待点（B 的 ORM、C 的触达出口、C 的 mcp_client）、4 道门禁、4 条回退、机器可读 JSON | 已完成 | 2026-11-09 |
| [day06/dev-a-prompt-历史与工具.md](./day06/dev-a-prompt-历史与工具.md) | dev-A 交接提示词：目标、6 项必读清单与读后三问、白名单/禁触清单、10 条硬约束、A02–A17 逐节点指令（改哪些文件/做什么/验收命令）、5 条停止条件、节点级与交接级报告模板 | 草稿 | 2026-11-09 |
| [day06/adhoc-20261109-Day06讲义原文存档.md](./day06/adhoc-20261109-Day06讲义原文存档.md) | **外部讲义原文存档**（D-5）：`康伴_Day06_长者侧Agent完整闭环_学生实战讲义.md` 原文复制，仅加 4 行来源说明；是 Day 6 计划的任务来源 | 已完成 | 2026-11-09 |
| [day06/dev-b-状态中心与上下文记忆.md](./day06/dev-b-状态中心与上下文记忆.md) | dev-b（Day 6）执行计划：7 节点 / 20.0 人时 / 7 波次子任务（W1 N19 a0 改绑 → W2 N02 ORM+N09 ORM+N05 StateCenter → W3 N06 ContextBuilder → W4 N08 MemoryService 骨架 → W6 N22 讲义作业）；含现状基线核查、逐节点分步执行、时间分配总表、依赖矩阵、验收标准、风险预案、交付物清单、技术债 M-1~M-4 | 草稿 | 2026-11-09 |
| [day05/a-后端观测与数据.md](./day05/a-后端观测与数据.md) | dev-a（Day 5）执行计划：A-1 metrics 表三件+文档留痕三件套（R1 纳入 A-1）→ A-2 打点收口进库+契约修订与 dev-c 卡追加 C-5/C-6（R2/R3）→ A-3 P50/P95 SQL 基线表（依赖 N1）∥ A-4 console 五 Mock 页契约草案独立文件（R4）；含代码现状核查基线、四项自裁登记（Z1–Z4）与依赖风险登记，S1–S4 分阶段确认制 | 进行中（S1/S2/S3 完成，S4 待 N1） | 2026-11-07 |
| [../接口契约_照护台业务数据.md](../接口契约_照护台业务数据.md) | **N3 交接件**：console 五 Mock 页（工单/通知/健康代填/药单/复核）REST 契约草案 v0.1（待冻结），全字段标注 TD §3 出处 + Mock 差异清单 D-1~D-10 + 开放问题 Q-1/Q-2；产出自 day05/a-后端观测与数据.md S3 | 待冻结 | 2026-11-07 |
| [day05/d-prompt-双端前端.md](./day05/d-prompt-双端前端.md) | 可直接交给 AI 编码助手的 dev-D 执行提示词：任务目标与必读清单、文件白名单与禁触、15 条安全约束（K1 开关口径 / K2 403 处置）、G0+N1–N13 逐节点指令、构建/静态/走查验收命令、8 条停止条件与固定报告模板；X0–X3 外部阻塞传播规则 | 草稿 | 2026-11-07 |
| [day05/d-双端前端推进计划.md](./day05/d-双端前端推进计划.md) | Day 5 dev-D（console/family）工作推进计划：通读代码与 docs 全量文档后的项目认知、Day5 全量任务（D5-1–D5-7 + 四人 14 项子任务）逐项拆解（前置/依赖/步骤/量化验收）、W0–W5 详细步骤与交付物规格与自检标准、功能边界与 N3/N4 对接矩阵、C1–C6 冲突处置与 R1–R12 风险 | 进行中（W0–W4 已执行，W5 待 X4） | 2026-11-08 |
| [day05/d-DAG.md](./day05/d-DAG.md) | 上述计划的独立 DAG 执行路径件：14 执行节点 + 6 外部节点 + 25 条依赖边（mermaid + ASCII）、关键路径 305 min（D-1 线，M5.4 硬依赖）、7 波次并行、L1–L8 写锁、G-A–G-G 门禁、F1–F8 回退、机器可读 JSON | 进行中（G0–G-F 已执行，G-G 待 X4） | 2026-11-08 |
| [day05/d-启动门槛核对.md](./day05/d-启动门槛核对.md) | G0 启动门槛：A-4 落点 / 家属契约冻结 / B-3 交付时点三项待确认（均 ⏳）、基线构建原文、写作用域与禁触通告、K1/K2 处置、环境复测（Docker Desktop 启动后 N5 解除） | 进行中 | 2026-11-08 |
| [day05/d-字段差异清单.md](./day05/d-字段差异清单.md) | D-3 五 Mock 页字段盘点：工单/告警/健康/药单/复核全部类型与字段逐行对 TD §3 表名+行号，三列结论（一致/前端多/前端缺），登记 C4 与硬编码身份等既有差异；含 §8 渲染基线 B1–B5（B2 弹窗样式/B5 H4 已修）；A-4 对照列已按 v0.1 回填（待 N10） | 已完成（N10 文档级双向对齐；N11 零代码改动） | 2026-11-08 |
| [day05/d-家属端契约差异表.md](./day05/d-家属端契约差异表.md) | D-2/N4 家属端契约差异表：已接收 B-3 契约 v1.1（R3 补 id/assigned_at、S-01 改 TD 原文），逐端点完整校对稿与冻结评审仍待；本地自检 L1–L8 按字段/判定/自裁分类，K1 差异回传 dev-B | 进行中（B-3 v1.1 已到，X3 冻结未排期） | 2026-11-08 |
| [day05/d-家属端切换层说明.md](./day05/d-家属端切换层说明.md) | D-2 切换层说明：data-source 拆分、唯一 USE_MOCK、5 函数双轨、Day8 切换步骤、Mock 删除条件、待冻结清单；附 Mock 三页回归与真实 375px 视口验证结果 | 进行中（B-3 v1.1 已到，X3 冻结未排期） | 2026-11-08 |
| [day05/d-admin页走查记录.md](./day05/d-admin页走查记录.md) | D-1/N5 admin 真实走查：四流程 4/4、DB 回查 4/4、越权 3/3（guardian 按 S-24 为 401）、非 admin 无白屏 3/3、一次性密钥关闭态与截图证据；视频录屏尝试失败记录 | 已完成 | 2026-11-07 |
| [day05/d-汇报清单.md](./day05/d-汇报清单.md) | W4/W5 汇报清单：构建门禁、三项静态零、S7 A1/A2/A3、任务卡完成定义、逐任务结论、未执行项与原因、安全范围复核（v1.9，含 merge/清理/弹窗修复与 A-4 回填记录） | 进行中（D-1 走查通过；N8/N10/N11 阻塞） | 2026-11-08 |
| [day05/day-05-延迟基线与护栏.md](./day05/day-05-延迟基线与护栏.md) | Day 5 全量推进计划：metrics 表自裁留痕三件套、P50/P95 基线与三档超时熔断、guardrails 骨架、a0 溯源、Iter2 演示收尾，含前端核查表与四项已批可提前项（S1–S7 分阶段汇报制） | 进行中 | 2026-11-07 |
| [day05/dev-b-护栏与降级.md](./day05/dev-b-护栏与降级.md) | Day 5 dev-B 后端护栏与降级执行计划：B-1 guardrails 骨架（MAX_TOOL_CALLS=8 + 同 Tool 1min 熔断 + 假工具单测 + 接口签名冻结）、B-2 三档超时降级（方案 B：provider 层旁路 watchdog + 扩 ErrorMsg 枚举 asr_slow/llm_slow/tts_slow，复用 WS error 通道，零 dev-A 依赖）、B-3 家属端契约技术校对、B-4 Laya spike 预研；含 N2/N4/N6 联调节点清单与 R1–R12 风险预案 | 已冻结 | 2026-11-08 |
| [task-03-库与模型基座.md](./day02/task-03-库与模型基座.md) | Day 2 身份体系 5 张表 + db 注入 + 0002 迁移 + conftest（含 0003 修正 0001 CHECK 命名） | 已冻结 | 2026-11-04 |
| [task-04-鉴权内核.md](./day02/task-04-鉴权内核.md) | task-4 鉴权内核的目标、拆解、实现步骤、风险与验收标准（T1–T3、T4a 判定层、T4b 接线、T5 测试收口已完成，227 条 0 skip、鉴权三模块覆盖 99.45%；剩 T6 人审） | 进行中 | 2026-11-04 |
| [adhoc-20261104-鉴权内核审查底稿.md](./day02/adhoc-20261104-鉴权内核审查底稿.md) | T6 人工逐行审查用的签署表：按信任半径分段（口令 → 有效期 → 解码兜底 → 判定层 → 接线与闸门③）、五类必测失败对照表、四处待裁决遗留、合并前复跑命令 | 进行中 | 2026-11-04 |
| [task-05-登录与凭证接口.md](./day02/task-05-登录与凭证接口.md) | task-5 登录与凭证接口执行计划：四个端点（POST /api/sessions、POST /api/sessions/refresh、GET /api/sessions/current、POST /api/device-sessions）+ auth_service 唯一写库层 + 11 条集成测试（6 大验收全覆盖） | 草稿 | 2026-11-04 |
| [task-06-长者CRUD与设备绑定.md](./day02/task-06-长者CRUD与设备绑定.md) | Task 6 长者档案 CRUD、设备绑定/解绑、候选长者与范围过滤（E5–E11，T01–T15） | 已冻结 | 2026-11-04 |
| [day-03-WebRTC与语音闭环.md](./day03/day-03-WebRTC与语音闭环.md) | Day 3 语音集成：教师 asr 参考模块能力梳理、匹配/差异裁决、app/audio 插件抽象层边界、T0–T6 实施顺序、V1–V6 验证标准与 P1–P11 风险登记 | 草稿 | 2026-11-05 |
| [adhoc-20261105-音频传输与设备链路技术方案.md](./day03/adhoc-20261105-音频传输与设备链路技术方案.md) | WS 裸流 vs WebRTC 轨八维对比结论；设备编码绑定（device_code 自裁）、信令 WS 长连接、VAD 通道开合、声纹录入（Day 4）四模块实现设计与测试标准 | 已冻结 | 2026-11-05 |
| [task-a2-信令WS与鉴权.md](./day03/task-a2-信令WS与鉴权.md) | Day 3 A2 单元执行计划：C2 契约冻结四裁决、WS 首帧鉴权（300ms/4401/审计）、心跳 20s/45s、信令转发与断线清理、11 条攻击面用例与 refresh 核对结论 | 草稿 | 2026-11-05 |
| [task-a4-pad前端执行计划.md](./day03/task-a4-pad前端执行计划.md) | A4（pad 前端）Day 3 小时级推进计划：T-A–T-H 任务分解与依赖、交付物要求、11 项自检清单（对应 a4-pad-frontend 任务卡） | 进行中 | 2026-11-05 |
| [adhoc-20261105-A3媒体层执行计划.md](./day03/adhoc-20261105-A3媒体层执行计划.md) | A3 Day 3 WebRTC 媒体层执行计划：写作用域、A1/A2/A4/A5 接口边界与交接、逐小时时间表、量化验收与未决裁决项 | 进行中 | 2026-11-05 |
| [adhoc-20261105-A3媒体层DAG.yaml](./day03/adhoc-20261105-A3媒体层DAG.yaml) | A3 媒体层 40 节点 DAG：依赖、关键路径、G0–H3 门禁、交接证据与并行边界 | 进行中 | 2026-11-05 |
| [day-03-a5-VAD兜底与降级测试.md](./day03/day-03-a5-VAD兜底与降级测试.md) | Day 3 A5 机动单元详细计划：C3 契约冻结 → webrtcvad 兜底 vad.py → utterance REST 降级链路 → 攻击面测试（真实 DB，伪造 token+越权+审计）→ 三场景降级话术 → 全量 pytest + README 第 7 节回填；含精确到小时的时间线（09:00–17:30）、机动支援协议、风险登记 | 草稿 | 2026-11-05 |
| [task-a1-ASR与TTS适配层.md](./day03/task-a1-ASR与TTS适配层.md) | Day 3 任务卡 a1 执行计划：7 条 PRD 验收口径 + TD 5 条实现基准 + 五方功能边界（含禁触清单）+ T1.0–T1.8 八步判据 + 3 项自裁（funasr 端口 7002 / 日志承载埋点 / ffmpeg 替代 pydub） | 已完成 | 2026-11-05 |
| [task-a1-DAG.md](./day03/task-a1-DAG.md) | a1 的独立 DAG 执行路径件（唯一事实源）：10 节点 13 边依赖图（mermaid + ASCII）、节点规格与判据、拓扑执行顺序、关键路径 360 min 与浮动时间、G1–G3 三道闸门、5 条失败回退、机器可读 JSON | 已完成 | 2026-11-05 |
| [adhoc-20261106-数字人接入方案对比.md](./adhoc-20261106-数字人接入方案对比.md) | 「当前技术方案（day-04 v2.3，后端）」vs「康伴-数字人模块接入方案（vrm-playground 前端抽库）」八维度结构化对比：4 处本质性差异（VRM 资源归属 deploy+nginx vs frontend/public、avatar_url 接口下发 vs 本地、client_diag 上报、P2 范围越界）+ 8 项细节差异 + B 拍板项对照 + 合并动作清单。**文件已由主理人删除，C7 停用留痕改记 docs/team_tasks/day05/a1-本地VRM与形象选择页.md** | 已废弃（文件已删除） | 2026-11-07 |
| [day04/a4-VAD主链与遗留修复.md](./day04/a4-VAD主链与遗留修复.md) | task-04（A4 单元）推进计划：a0 三问题现状基线（P1-2 已修于 ab15551，F-01/F-02 未修）+ S0–S6 七步拆解（F-01 统一 422 / F-02 垃圾字节 422 / P1-2 验证补测 / W4 webrtcvad 兜底接 utterance 主链 / metrics 打点位 AC-42 / task-a2 契约变更记录 / 门禁收口），含交付物总表、验收对照、R1–R6 风险（webrtc.py 跨界协调、兜底激活语义自裁） | 已冻结 | 2026-11-06 |
| [day04/f1-双端登录页.md](./day04/f1-双端登录页.md) | Day 4 task-05（F1 前端独立卡）执行计划：MPA 双入口 + console/family 两独立登录模块 + token 存储/401 自动刷新 + 375px 响应式；含 E1 实际端点差异登记、S0–S6 步骤自检与 R1–R6 风险；执行中主理人裁决弃用 MUI（改纯 React + 原生 CSS，v1.1 留痕）。**task-05 扩为 Day 4 前端全量后，由 f1-Day4前端联调.md 续接** | 已完成 | 2026-11-06 |
| [day04/f1-Day4前端联调.md](./day04/f1-Day4前端联调.md) | Day 4 task-05（v2.1，Day 4 前端全量）执行计划：W0–W6 七步——pad 五项 P0（3D 展示/唤醒词/声纹录入/身份确认/对话收尾，对接 C4–C7）+ 口型 P1 + 双端登录回归；继承登录线已完成事实与「弃用 MUI」裁决；R1–R7 风险（KWS 体积/VRM0 朝向/资产未就绪 Mock 先行） | 进行中 | 2026-11-06 |
| [day-04-3D与身份闭环.md](./day04/day-04-3D与身份闭环.md) | Day 4 全量后端方案 v2.3：W1 3D 展示（avatar-config 三层降级、nginx 托管 VRM·两步走）、W2 声纹身份闭环（注册三接口、identity_ask/confirm、迁移 0004/0005·CAM++ 192 维、**设备绑定 1v1 候选集**、每会话首轮校验）、W3 上下文与 LLM 对话闭环（FR-DLG-05）、W4 VAD 正式切段、W5 唤醒词端侧 KWS P0（FR-UI-11/AC-42）；M1–M17 改造点、T1–T10 排期、R1–R13 风险、4 项自裁；显式排除 FR-3D-03/viseme/diarization；基线已同步修订（PRD v2.6、技术文档 v1.5） | 已冻结 | 2026-11-07 |
| [day04/task-01-声纹身份.md](./day04/task-01-声纹身份.md) | Day 4 A1 单元声纹身份闭环执行计划：T0 晨会契约冻结 → T1 CAM++ spike（192 维 + cosine 分布，12:30 午站会通报）→ T2 迁移 0004（voiceprints + conversation_threads，含 CHECK 与唯一约束）→ T3 ORM → T4 独立推理容器（deploy/voiceprint/ 端口 7003，主服务不装 torch）→ T5 迁移 0005（embedding vector(N)）→ T6 voiceprint_service（Fernet 加密 + 原始音频即弃）→ T7 REST 三接口（samples/status/delete）→ T8 identity_service（每会话首轮强制 + 静默复检，候选集恒 1 位）→ T9 门禁 → T10 联调收口；10 条风险预案 + 验收标准逐子任务对齐 PRD FR-VPR-01~03 | 草稿 | 2026-11-06 |
| [a2-信令与对话.md](./day04/a2-信令与对话.md) | Day 4 A2 小时级执行计划：C5 信令追加、Minimax 对话与 thread 闭环 | 草稿 | 2026-11-06 |
| [a2-impl-信令与对话.md](./day04/a2-impl-信令与对话.md) | Day 4 A2 编码实施规格：N00–N10 输入/产出、文件边界、B1–B6 接口门禁、状态机与验收追踪 | 草稿 | 2026-11-06 |
| [a2-dag-信令与对话.md](./day04/a2-dag-信令与对话.md) | Day 4 A2 独立 DAG：11 个执行节点、5 个外部节点、18 条依赖边、6 道门禁及机器可读 JSON | 草稿 | 2026-11-06 |
| [a2-prompt-信令与对话.md](./day04/a2-prompt-信令与对话.md) | 可直接交给 AI 编码助手的 A2 提示词：目标、输入约束、DAG 指令、验收命令、停止条件与报告模板 | 草稿 | 2026-11-06 |
| [a2-汇报提纲-day04.md](./day04/a2-汇报提纲-day04.md) | Day 4 A2 组内 5 分钟汇报提纲：时间轴分配、逐段话术、10 条 Q&A 预演、待补信息清单与现场口径纪律 | 草稿 | 2026-11-06 |
| [day04/a3-3D展示后端.md](./day04/a3-3D展示后端.md) | A3（Day 4 · 3D 展示后端）实施计划：现状九条实测、四项口径（version 内容指纹 / rotation 只来自 Settings / nginx 是否进 compose / services 归属）、T1–T7 七步（Settings→取值链服务→avatar-config 接口→seed 与 VRM 资产→nginx 托管→装配与测试→README）、验收命令与五条回退 | 已完成 | 2026-11-06 |
| [day04/a3-DAG.md](./day04/a3-DAG.md) | A3 的独立 DAG 执行路径件：7 节点 9 边依赖图（mermaid + ASCII）、边表（硬/软依赖与传递物）、关键路径 180 min vs 串行 265 min、并行 2 条线与两处写锁（config.py 归 T1、main.py 归 T6） | 已完成 | 2026-11-06 |
| [day04/f2-护工工单看板.md](./day04/f2-护工工单看板.md) | console 第 3 批首项（task-06，F2）执行计划：护工工单看板 Mock 先行 + 浅色侧栏骨架；五项决策（范围/骨架/重开仅管理员/escalated 纯展示/不做边界）、S1–S4 步骤与验收清单 | 已完成 | 2026-11-06 |
| [day04/f3-负责长者列表.md](./day04/f3-负责长者列表.md) | console 第 3 批次项（task-07，F3）执行计划：负责长者列表与详情**真实对接 E7/E6**（首个非 Mock 业务页）；四项决策与验收清单 | 已完成 | 2026-11-06 |
| [day04/f4-告警通知中心.md](./day04/f4-告警通知中心.md) | console 第 3 批次项（task-08，F4）执行计划：护工告警通知中心 Mock 先行（notifications 表 Day 8 未交付）；AC-23 状态机交互与五项决策 | 已完成 | 2026-11-06 |
| [day04/f5-健康代填.md](./day04/f5-健康代填.md) | console 第 3 批次项（task-09，F5）执行计划：健康采集代填表，提交 Mock + 负责长者下拉真实 E7；阈值常量与 source 枚举裁决（f5 D2） | 已完成 | 2026-11-06 |
| [day04/f6-药单只读.md](./day04/f6-药单只读.md) | console 第 3 批收尾项（task-10，F6）执行计划：护工药单**只读**列表，Mock 先行 + 长者下拉真实 E7；零写操作边界与 cron 展示裁决（f6 D3） | 已完成 | 2026-11-06 |
| [day04/f7-复核队列.md](./day04/f7-复核队列.md) | console 医生批次首项（task-11，F7）执行计划：健康记录复核队列，Mock 先行；确认/纠错新版本与版本链、review_note 自裁留痕（f7 D4） | 已完成 | 2026-11-06 |
| [day04/f8-药单维护.md](./day04/f8-药单维护.md) | console 医生批次次项（task-12，F8）执行计划：药单增改停查，共享护工只读页 Mock store；AC-27 version+1 与 cron 录入裁决（f8 D2/D3） | 已完成 | 2026-11-06 |
| [adhoc-20261106-员工与家属建号接口.md](./adhoc-20261106-员工与家属建号接口.md) | A1 建号 CLI + A2 建员工/绑家属：影响范围六层（含两处契约缺口 G1 家属建号、G2 员工-长者归属）、T1–T8 实施计划、五类完工标准（功能/安全/质量门/文档/反向验收）、6 项自裁与裁决 | 草稿 | 2026-11-06 |
| [adhoc-20261106-建号接口-DAG.md](./adhoc-20261106-建号接口-DAG.md) | 上述计划的独立执行 DAG：T0–T8 九节点、11 条边（8 硬 3 软）、5 波次并行编排、4 条写锁、8 个验收门 | 已完成 | 2026-11-06 |
| [day04/f1-vad-worker隔离.md](./day04/f1-vad-worker隔离.md) | 联调缺陷修复：KWS/VAD 两套非 modularize emscripten 产物顶层 `class ExitStatus` 全局冲突，致呼叫后端侧 TEN-VAD 必失效静默降级；主理人拍板方案 A（VAD 移入 Worker 隔离），vad.ts 重写为 Worker 封装、TenVad API 不变、realtime.ts 零改动；S1–S5 步骤 + 四项验收 + R1–R4 风险；v1.1 收口（含录入页居中与 92vw 适配顺带修） | 已完成 | 2026-11-06 |
| [adhoc-20261106-收音与识别优化.md](./adhoc-20261106-收音与识别优化.md) | 联调优化：TEN-VAD threshold 0.5→0.6 + 服务端 webrtcvad aggressiveness 2→3（收窄收音范围）+ FunASR 可配置热词（ASR_HOTWORDS 默认空回落）+ system prompt ASR 纠错条目 + 录入页 3s 环境自检与电平条；明确不做说话人分离与念读校验（边界裁决）；含三场景模拟测试记录表 | 进行中 | 2026-11-06 |
| [day04/f9-长者注册与设备分配.md](./day04/f9-长者注册与设备分配.md) | console 长者注册闭环：契约 v1.8（E5/E9 放开 care_worker + 新增 E16 设备建档/E17 设备列表/E18 房间床位/E19 长者入床，补缺口 G3/G4）+ console 四步向导（建档→选床→建设备展示一次性密钥→引导 pad 录声纹）；pad 零改动 | 已完成 | 2026-11-06 |
| [day05/dev-c-pad语音与3D.md](./day05/dev-c-pad语音与3D.md) | Day 5 dev-C pad 前端执行计划 v1.4：S0 已收口；C-1 三档超时表现已实现并提交（degrade.ts 优先级状态机，7e90613）；C-5/C-6 端侧 wake_ts/playback 打点已实现并提交（02eaea3，A-2 落地免桩直接对齐）；C-5 身份确认前端兜底已提交（ffbf5b6）；剩 C-1 联演录屏、C-4 口型录屏、C-3 声纹实测、C-2 测量、S5 演示 | 进行中 | 2026-11-08 |
| [../team_tasks/day06/a1-延迟基线表.md](../team_tasks/day06/a1-延迟基线表.md) | A-3/C-2 交付（**已移入 docs/team_tasks/day06/，卡号 a1**）：20 轮真实语音会话（21 trace/16 有效单轮）SQL 复算 P50/P95——e2e P50 7872/P95 13537ms、AC-42 达标 0/16 如实入档；分段明细、复算 SQL、FunASR O-1 归档与云 key 复测条件、playback 缺口与 3 异常 trace 登记 | 已完成 | 2026-11-09 |
| [day05/a1-本地VRM与形象选择页.md](./day05/a1-本地VRM与形象选择页.md) | Day 5 A1 方案：数字人改本地资产驱动（推翻 C7 下发，D1 留痕）+ 14 模型全量支持 + 形象选择页 + 程序化待机与 vrma 播放；S0–S6 全部完成（v1.5，含计划外加项：指针跟随、Q2 立绘），真机验收通过 | 已冻结 | 2026-11-07 |

## 状态取值

`草稿` → `进行中` → `已冻结`（计划执行完/定稿）｜`已废弃`（不删，留痕）
