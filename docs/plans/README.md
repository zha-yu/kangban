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
| [task-03-库与模型基座.md](./task-03-库与模型基座.md) | Day 2 身份体系 5 张表 + db 注入 + 0002 迁移 + conftest（含 0003 修正 0001 CHECK 命名） | 已冻结 | 2026-11-04 |
| [task-04-鉴权内核.md](./task-04-鉴权内核.md) | task-4 鉴权内核的目标、拆解、实现步骤、风险与验收标准（T1–T3、T4a 判定层、T4b 接线、T5 测试收口已完成，227 条 0 skip、鉴权三模块覆盖 99.45%；剩 T6 人审） | 进行中 | 2026-11-04 |
| [adhoc-20261104-鉴权内核审查底稿.md](./adhoc-20261104-鉴权内核审查底稿.md) | T6 人工逐行审查用的签署表：按信任半径分段（口令 → 有效期 → 解码兜底 → 判定层 → 接线与闸门③）、五类必测失败对照表、四处待裁决遗留、合并前复跑命令 | 进行中 | 2026-11-04 |
| [task-05-登录与凭证接口.md](./task-05-登录与凭证接口.md) | task-5 登录与凭证接口执行计划：四个端点（POST /api/sessions、POST /api/sessions/refresh、GET /api/sessions/current、POST /api/device-sessions）+ auth_service 唯一写库层 + 11 条集成测试（6 大验收全覆盖） | 已冻结 | 2026-11-04 |
| [task-06-长者CRUD与设备绑定.md](./task-06-长者CRUD与设备绑定.md) | Task 6 长者档案 CRUD、设备绑定/解绑、候选长者与范围过滤（E5–E11，T01–T15） | 已冻结 | 2026-11-04 |
| [day-03-WebRTC与语音闭环.md](./day-03-WebRTC与语音闭环.md) | Day 3 语音集成：教师 asr 参考模块能力梳理、匹配/差异裁决、app/audio 插件抽象层边界、T0–T6 实施顺序、V1–V6 验证标准与 P1–P11 风险登记 | 进行中 | 2026-11-05 |
| [adhoc-20261105-音频传输与设备链路技术方案.md](./adhoc-20261105-音频传输与设备链路技术方案.md) | WS 裸流 vs WebRTC 轨八维对比结论；设备编码绑定（device_code 自裁）、信令 WS 长连接、VAD 通道开合、声纹录入（Day 4）四模块实现设计与测试标准 | 已冻结 | 2026-11-05 |
| [task-a2-信令WS与鉴权.md](./task-a2-信令WS与鉴权.md) | Day 3 A2 单元执行计划：C2 契约冻结四裁决、WS 首帧鉴权（300ms/4401/审计）、心跳 20s/45s、信令转发与断线清理、11 条攻击面用例与 refresh 核对结论 | 草稿 | 2026-11-05 |
| [task-a4-pad前端执行计划.md](./task-a4-pad前端执行计划.md) | A4（pad 前端）Day 3 小时级推进计划：T-A–T-H 任务分解与依赖、交付物要求、11 项自检清单（对应 a4-pad-frontend 任务卡） | 进行中 | 2026-11-05 |
| [adhoc-20261105-A3媒体层执行计划.md](./adhoc-20261105-A3媒体层执行计划.md) | A3 Day 3 WebRTC 媒体层执行计划：写作用域、A1/A2/A4/A5 接口边界与交接、逐小时时间表、量化验收与未决裁决项 | 进行中 | 2026-11-05 |
| [adhoc-20261105-A3媒体层DAG.yaml](./adhoc-20261105-A3媒体层DAG.yaml) | A3 媒体层 40 节点 DAG：依赖、关键路径、G0–H3 门禁、交接证据与并行边界 | 进行中 | 2026-11-05 |
| [day-03-a5-VAD兜底与降级测试.md](./day-03-a5-VAD兜底与降级测试.md) | Day 3 A5 机动单元详细计划：C3 契约冻结 → webrtcvad 兜底 vad.py → utterance REST 降级链路 → 攻击面测试（真实 DB，伪造 token+越权+审计）→ 三场景降级话术 → 全量 pytest + README 第 7 节回填；含精确到小时的时间线（09:00–17:30）、机动支援协议、风险登记 | 草稿 | 2026-11-05 |
| [task-a1-ASR与TTS适配层.md](./task-a1-ASR与TTS适配层.md) | Day 3 任务卡 a1 执行计划：7 条 PRD 验收口径 + TD 5 条实现基准 + 五方功能边界（含禁触清单）+ T1.0–T1.8 八步判据 + 3 项自裁（funasr 端口 7002 / 日志承载埋点 / ffmpeg 替代 pydub） | 进行中 | 2026-11-05 |
| [task-a1-DAG.md](./task-a1-DAG.md) | a1 的独立 DAG 执行路径件（唯一事实源）：10 节点 13 边依赖图（mermaid + ASCII）、节点规格与判据、拓扑执行顺序、关键路径 360 min 与浮动时间、G1–G3 三道闸门、5 条失败回退、机器可读 JSON | 进行中 | 2026-11-05 |
| [adhoc-20261106-数字人接入方案对比.md](./adhoc-20261106-数字人接入方案对比.md) | 「当前技术方案（day-04 v2.3，后端）」vs「康伴-数字人模块接入方案（vrm-playground 前端抽库）」八维度结构化对比：4 处本质性差异（VRM 资源归属 deploy+nginx vs frontend/public、avatar_url 接口下发 vs 本地、client_diag 上报、P2 范围越界）+ 8 项细节差异 + B 拍板项对照 + 合并动作清单 | 已冻结 | 2026-11-06 |
| [day04/a4-VAD主链与遗留修复.md](./day04/a4-VAD主链与遗留修复.md) | task-04（A4 单元）推进计划：a0 三问题现状基线（P1-2 已修于 ab15551，F-01/F-02 未修）+ S0–S6 七步拆解（F-01 统一 422 / F-02 垃圾字节 422 / P1-2 验证补测 / W4 webrtcvad 兜底接 utterance 主链 / metrics 打点位 AC-42 / task-a2 契约变更记录 / 门禁收口），含交付物总表、验收对照、R1–R6 风险（webrtc.py 跨界协调、兜底激活语义自裁） | 草稿 | 2026-11-06 |
| [day04/f1-双端登录页.md](./day04/f1-双端登录页.md) | Day 4 task-05（F1 前端独立卡）执行计划：MPA 双入口 + console/family 两独立登录模块 + token 存储/401 自动刷新 + 375px 响应式；含 E1 实际端点差异登记、S0–S6 步骤自检与 R1–R6 风险；执行中主理人裁决弃用 MUI（改纯 React + 原生 CSS，v1.1 留痕）。**task-05 扩为 Day 4 前端全量后，由 f1-Day4前端联调.md 续接** | 已完成 | 2026-11-06 |
| [day04/f1-Day4前端联调.md](./day04/f1-Day4前端联调.md) | Day 4 task-05（v2.1，Day 4 前端全量）执行计划：W0–W6 七步——pad 五项 P0（3D 展示/唤醒词/声纹录入/身份确认/对话收尾，对接 C4–C7）+ 口型 P1 + 双端登录回归；继承登录线已完成事实与「弃用 MUI」裁决；R1–R7 风险（KWS 体积/VRM0 朝向/资产未就绪 Mock 先行） | 进行中 | 2026-11-06 |
| [day-04-3D与身份闭环.md](./day-04-3D与身份闭环.md) | Day 4 全量后端方案 v2.3：W1 3D 展示（avatar-config 三层降级、nginx 托管 VRM·两步走）、W2 声纹身份闭环（注册三接口、identity_ask/confirm、迁移 0004/0005·CAM++ 192 维、**设备绑定 1v1 候选集**、每会话首轮校验）、W3 上下文与 LLM 对话闭环（FR-DLG-05）、W4 VAD 正式切段、W5 唤醒词端侧 KWS P0（FR-UI-11/AC-42）；M1–M17 改造点、T1–T10 排期、R1–R13 风险、4 项自裁；显式排除 FR-3D-03/viseme/diarization；基线已同步修订（PRD v2.6、技术文档 v1.5） | 草稿 | 2026-11-06 |
| [day04/task-01-声纹身份.md](./day04/task-01-声纹身份.md) | Day 4 A1 单元声纹身份闭环执行计划：T0 晨会契约冻结 → T1 CAM++ spike（192 维 + cosine 分布，12:30 午站会通报）→ T2 迁移 0004（voiceprints + conversation_threads，含 CHECK 与唯一约束）→ T3 ORM → T4 独立推理容器（deploy/voiceprint/ 端口 7003，主服务不装 torch）→ T5 迁移 0005（embedding vector(N)）→ T6 voiceprint_service（Fernet 加密 + 原始音频即弃）→ T7 REST 三接口（samples/status/delete）→ T8 identity_service（每会话首轮强制 + 静默复检，候选集恒 1 位）→ T9 门禁 → T10 联调收口；10 条风险预案 + 验收标准逐子任务对齐 PRD FR-VPR-01~03 | 草稿 | 2026-11-06 |


| [day04/a3-3D展示后端.md](./day04/a3-3D展示后端.md) | A3（Day 4 · 3D 展示后端）实施计划：现状九条实测、四项口径（version 内容指纹 / rotation 只来自 Settings / nginx 是否进 compose / services 归属）、T1–T7 七步（Settings→取值链服务→avatar-config 接口→seed 与 VRM 资产→nginx 托管→装配与测试→README）、验收命令与五条回退 | 草稿 | 2026-11-06 |
| [day04/a3-DAG.md](./day04/a3-DAG.md) | A3 的独立 DAG 执行路径件：7 节点 9 边依赖图（mermaid + ASCII）、边表（硬/软依赖与传递物）、关键路径 180 min vs 串行 265 min、并行 2 条线与两处写锁（config.py 归 T1、main.py 归 T6） | 进行中 | 2026-11-06 |
| [day04/f2-护工工单看板.md](./day04/f2-护工工单看板.md) | console 第 3 批首项（task-06，F2）执行计划：护工工单看板 Mock 先行 + 浅色侧栏骨架；五项决策（范围/骨架/重开仅管理员/escalated 纯展示/不做边界）、S1–S4 步骤与验收清单 | 已完成 | 2026-11-06 |
| [day04/f3-负责长者列表.md](./day04/f3-负责长者列表.md) | console 第 3 批次项（task-07，F3）执行计划：负责长者列表与详情**真实对接 E7/E6**（首个非 Mock 业务页）；四项决策与验收清单 | 已完成 | 2026-11-06 |
| [day04/f4-告警通知中心.md](./day04/f4-告警通知中心.md) | console 第 3 批次项（task-08，F4）执行计划：护工告警通知中心 Mock 先行（notifications 表 Day 8 未交付）；AC-23 状态机交互与五项决策 | 已完成 | 2026-11-06 |
| [day04/f5-健康代填.md](./day04/f5-健康代填.md) | console 第 3 批次项（task-09，F5）执行计划：健康采集代填表，提交 Mock + 负责长者下拉真实 E7；阈值常量与 source 枚举裁决（f5 D2） | 已完成 | 2026-11-06 |
| [day04/f6-药单只读.md](./day04/f6-药单只读.md) | console 第 3 批收尾项（task-10，F6）执行计划：护工药单**只读**列表，Mock 先行 + 长者下拉真实 E7；零写操作边界与 cron 展示裁决（f6 D3） | 已完成 | 2026-11-06 |
| [day04/f7-复核队列.md](./day04/f7-复核队列.md) | console 医生批次首项（task-11，F7）执行计划：健康记录复核队列，Mock 先行；确认/纠错新版本与版本链、review_note 自裁留痕（f7 D4） | 已完成 | 2026-11-06 |
| [day04/f8-药单维护.md](./day04/f8-药单维护.md) | console 医生批次次项（task-12，F8）执行计划：药单增改停查，共享护工只读页 Mock store；AC-27 version+1 与 cron 录入裁决（f8 D2/D3） | 已完成 | 2026-11-06 |
| [adhoc-20261106-员工与家属建号接口.md](./adhoc-20261106-员工与家属建号接口.md) | A1 建号 CLI + A2 建员工/绑家属：影响范围六层（含两处契约缺口 G1 家属建号、G2 员工-长者归属）、T1–T8 实施计划、五类完工标准（功能/安全/质量门/文档/反向验收）、6 项自裁与裁决 | 草稿 | 2026-11-06 |
| [adhoc-20261106-建号接口-DAG.md](./adhoc-20261106-建号接口-DAG.md) | 上述计划的独立执行 DAG：T0–T8 九节点、11 条边（8 硬 3 软）、5 波次并行编排、4 条写锁、8 个验收门 | 进行中 | 2026-11-06 |

## 状态取值

`草稿` → `进行中` → `已冻结`（计划执行完/定稿）｜`已废弃`（不删，留痕）
