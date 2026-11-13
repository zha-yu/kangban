# AI 使用与审查说明（AI_USAGE.md）

> 状态：已交付　｜　版本：v1.0　｜　日期：2026-11-13
> 负责人：dev-C（Day 10 节点 C5）
> 上游依据：`docs/plans/day10/day-10-交付与观测-工作推进计划.md` §4.3 C5 行、`docs/team_tasks/day10/dev-C-前端构建与文档齐套.md` §1 C5
> 本文一切引用路径均经 `ls` 实测存在；找不到实证的审查动作如实标注「未见留痕」，不编造。
> 变更记录见文末

---

## 1. AI 辅助范围

本项目按 day02–day10 计划件与 `docs/team_tasks/` 各日任务卡/PROGRESS 实况归纳：全部业务模块的开发均由 AI 代理（dev-A/B/C/D 四线）在计划件约束下生成，人工负责裁决、审查签署与联调确认。按模块列示 AI 生成的主体范围：

| 日 | 模块 | 主要 AI 生成内容（对应计划件/任务卡） |
| --- | --- | --- |
| Day 2 | 身份与权限域 | `app/core/security.py`、`app/core/auth.py`、`app/core/runtime_context.py`（Identity 骨架）、迁移 `0002_day2_identity.py`、登录凭证接口、长者 CRUD 与设备绑定（`docs/plans/day02/task-04-鉴权内核.md`、`task-03-库与模型基座.md`、`task-05/06`；进度见 `docs/team_tasks/day02/PROGRESS.md`） |
| Day 3 | 音频与媒体层 | ASR/TTS 适配层、信令 WS 与鉴权、WebRTC 媒体层、VAD 兜底、pad 登录页（`docs/plans/day03/task-a1-ASR与TTS适配层.md`、`task-a2-信令WS与鉴权.md`、`day-03-a5-VAD兜底与降级测试.md`） |
| Day 4 | 声纹身份与前端联调 | 声纹身份闭环（`voiceprints` 迁移 `0004/0005`）、3D 展示后端、工单看板/告警中心/健康代填/药单只读/复核队列/药单维护（`docs/plans/day04/task-01-声纹身份.md`、`f2`–`f9` 系列、`docs/team_tasks/day04/PROGRESS.md`） |
| Day 5 | 观测与双端前端 | 后端观测与数据、护栏与降级、本地 VRM 与形象选择页、console/family 双端（`docs/plans/day05/a-后端观测与数据.md`、`dev-b-护栏与降级.md`、`dev-c-pad语音与3D.md`） |
| Day 6 | 长者侧 Agent 闭环 | 历史事件与工具事实、状态中心与上下文记忆、Agent 与统一出口、MCP 客户端雏形、pad 送达确认与改绑（`docs/plans/day06/dev-a-历史与工具执行计划.md`、`dev-b-状态中心与上下文记忆.md`、`dev-c-Agent与统一出口.md`） |
| Day 7 | 三级路由与触达 | 三级路由与决策日志、触达语音接线、用药到点接线、pad 可靠性改绑联调（`docs/plans/day07/dev-a-三级路由与决策日志.md`、`handoff-A-to-C-用药到点接线.md`、`dev-c-day07-执行计划.md`） |
| Day 8 | 健康域与 MCP 双 server | 数据分级规则层、药单管理后端接口、健康采集与分级（绿/黄/红）、MCP 双 server 接线、日报通知与三端后台（`docs/plans/day08/dev-a-数据分级规则层.md`、`dev-a-药单管理后端接口.md`、`day-08-健康采集与三端后台.md`） |
| Day 9 | 安全闸门与生命周期 | 三道隔离闸门（`tests/test_gates.py`）、脱敏集中（`app/core/redact.py`）、生命周期三条流水线、记忆 embedding + 强制 `WHERE elder_id`、注入防线（`eval/fixtures/injection.jsonl` + `tests/test_injection.py`）、72h 音频清理、`docs/SECURITY.md`（`docs/plans/day09/dev-a-安全闸门与生命周期.md`、`dev-b-day09-工作推进计划.md`） |
| Day 10 | 容器化与文档齐套 | 后端/前端 Dockerfile 与 compose 编排（dev-A/C1 并行中）、观测成本线、评测回归、交付文档四件（`docs/plans/day10/day-10-交付与观测-工作推进计划.md` §4.3） |

边界说明：AI 不自行决定超出计划件写域的变更——写域锁（各日任务书 §0/§6）与分步确认制（每步完成向主理人确认再进下一步）是全部计划件的统一执行机制（例：`docs/plans/day09/dev-a-安全闸门与生命周期.md` §4、`docs/plans/day10/dev-c-day10-工作推进计划.md` §4）。

## 2. 人工审查记录（高风险五类：auth / 迁移 / Tool 权限 / MCP / Dockerfile）

按 AGENTS §5「先出方案再落代码＋人工逐行读 diff」要求，逐类核对留痕：

| 类 | 审查载体（实测存在的计划件） | 记载的审查动作 | 留痕状态 |
| --- | --- | --- | --- |
| **auth** | `docs/plans/day02/adhoc-20261104-鉴权内核审查底稿.md` | 签署表式逐段审查：按信任半径排序（口令→有效期与白名单→解码→判定层→接线与闸门③）、按提交序列（`16513da`/`bf0dad2`/`2107292`/`92ea5ae`）核对写作用域、五类必测失败对照表、四处遗留待裁决（其 §8）、合并前复跑命令与 §10 签署栏 | 底稿已产出；首部状态为「进行中（待主理人逐段签署）」，**签署完成未见留痕**。另 `docs/plans/day03/adhoc-20261105-音频传输与设备链路技术方案.md` 变更记录 v1.1 载明 pad 登录键变更「auth 高风险区，实现前人审」，v1.3 最终裁决选择绕开（零契约变更、不触碰 auth 高风险区） |
| **迁移** | `docs/plans/day02/task-03-库与模型基座.md`（`0002`）；`docs/plans/day09/dev-a-安全闸门与生命周期.md` §3.2/S1（`0020_day9_medication_enums.py`） | `0002`：枚举一律 CHECK 禁 PG ENUM 等基座规则随计划冻结；`0020`：列为本包高风险文件，S1 步「方案要点」小节登记 upgrade/downgrade 可逆与 downgrade 防数据丢失方案，随确认点❷（ORM CHECK 是否同步改）经主理人裁决后落代码 | 计划件留痕完整；迁移文件实测存在（`alembic/versions/`）。**逐行读 diff 的签署完成未见留痕**（沿 day09 分步确认制，确认在对话轮完成） |
| **Tool 权限** | `docs/plans/day09/dev-b-day09-工作推进计划.md` §1.5、B2 步骤 1；`docs/plans/day06/dev-c-Agent与统一出口.md` | day09 §1.5 将 `app/services/tool_executor.py`、`app/agents/tool_registry.py` 等六处列为高风险「先出方案经确认后才落代码」；B2 步骤 1 载明先出「幂等查重→running 行→调用→回写」共用段方案；day06 计划件记载 Agent 统一出口与工具事实层设计。行为侧兜底：`tests/test_gates.py` 闸门③ 用例（模型自报 `elder_id` 丢弃＋审计＋runtime 注入） | 方案先行与测试兜底留痕完整；**人工读 diff 的签署完成未见留痕** |
| **MCP** | `docs/plans/day09/dev-b-day09-工作推进计划.md` §1.5（`app/mcp_client/**` 列入高风险）、B1 节 | `app/mcp_client/client.py` 头注自述「身份只能从服务端 runtime_context 注入，模型自报的 elder_id 一律丢弃（AC-29）；审计走 services 唯一写库入口」；day09 B1 将 MCP 双 server 接线、幂等与 `tool_executions` 记账、工具授权闸门加固列入同一确认流程。注入防线实况：`eval/fixtures/injection.jsonl`（16 条）＋ `tests/test_injection.py`（AC-36） | 计划件与代码注释留痕完整；**人工读 diff 的签署完成未见留痕** |
| **Dockerfile** | `docs/team_tasks/day10/dev-A-容器化与部署链.md` §0.3；`docs/plans/day10/day-10-交付与观测-工作推进计划.md` A1/C1 行；`docs/plans/day10/dev-c-day10-工作推进计划.md` QC-2 | day10 dev-A 任务书 §0.3 将 `Dockerfile`、`docker-compose.yml`、`deploy/nginx.conf` 列为高风险「先出方案（写 §5 执行记录）经确认后才落代码」；C1 前端 `frontend/frontend.Dockerfile` 经主理人 2026-11-13 裁决授权（QC-2）后新建并执行构建验证 | **截至本文件交付时（2026-11-13），后端 `Dockerfile` 与 `frontend/frontend.Dockerfile` 均未落地**（A1/C1 并行进行中，day10 PROGRESS 0/4 收口）——属「计划件登记在案、实际 diff 审查待交付确认点」，如实登记，不预写审查结论 |

## 3. 关键决策（day02–day10 计划件裁决记录）

以下 13 条为影响架构或口径的裁决记录，全部引自实测存在的计划件：

| # | 决策 | 记载位置 |
| --- | --- | --- |
| 1 | 鉴权载荷以冻结契约为准：`typ` 用 `access/refresh/device/staff_card`（弃 `human_*` 别名）、版本载荷名 `tv`（弃 `ver`）、签名算法显式限定白名单不从 JWT header 动态选择——不在代码中兼容两套载荷 | `docs/plans/day02/task-04-鉴权内核.md` §2.2 |
| 2 | 数据基座规则：枚举一律 CHECK 禁用 PG ENUM（裁决 C6）；「每位长者 active 监护人 ≤2」为服务层校验不落 DDL（裁决 C4） | `docs/plans/day02/task-03-库与模型基座.md` |
| 3 | pad 登录键三转定稿：v1.1 裁撤 `device_code` 改 elder_id 单凭证 → v1.2 主理人自撤（泄露面风险）→ v1.3 终裁 `device_id + device_secret` 双因子直连既有 E4 契约，零契约变更、不触碰 auth 高风险区 | `docs/plans/day03/adhoc-20261105-音频传输与设备链路技术方案.md` 变更记录 v1.1–v1.3 |
| 4 | pad 登录错误统一 401 口径：设备不存在/密钥不匹配/已锁定同码，防存在性探测（v1.4）；设备会话不设 refresh 通道，过期重新登录（v1.5） | 同上，变更记录 v1.4–v1.5 |
| 5 | VAD 口径：端侧 TEN-VAD WASM 为主、服务端 `webrtcvad` 兜底（推翻技术文档原「服务端 onnx」口径，PRD/技术文档同步补修订记录） | `docs/plans/day03/day-03-WebRTC与语音闭环.md` §2.3、变更记录 v1.2 |
| 6 | 音频通道生命周期冲突裁决（D2）：WS 断开由 A2 最终关闭时调 A3 立即关闭 PeerConnection，不实现第二套保活语义 | `docs/plans/day03/adhoc-20261105-A3媒体层执行计划.md` 生命周期冲突裁决 D2 |
| 7 | 声纹阈值沿革 0.85→0.55（2026-11-07 裁决下调）→ **0.62 定稿**（PRD v3.1 实测定稿，依据 `eval/reports/声纹调优报告.md`）；三档级联：声纹失败→主动确认→通用模式（不读记忆/不写健康/不确认用药） | `docs/plans/adhoc-20261109-房间床位交付存档.md`（0.55 沿革）、`eval/reports/声纹调优报告.md`、PRD §3.2 |
| 8 | MCP 工具身份铁律：身份只从服务端 `runtime_context` 注入，模型自报 `elder_id` 一律丢弃＋`tool_identity_override_attempt` 审计（AC-29）；审计走 services 唯一写库入口 | `app/mcp_client/client.py` 头注；`docs/plans/day06/dev-c-Agent与统一出口.md`；行为测试 `tests/test_gates.py` 闸门③ |
| 9 | `route_degraded` 契约冻结：降级路径响应契约定案（dev-B 评审＋主理人裁决，三项待确认全按建议定案） | `docs/plans/day08/adhoc-20261111-route_degraded契约冻结.md` |
| 10 | 媒体素材格式扩展名放开（wav/mp3 值域、契约 §7 v1.3 留痕，dev-B 评审通过＋主理人裁决同一次提交落地） | `docs/plans/adhoc-20261111-媒体素材格式扩展名.md` §5 |
| 11 | Day 8 审查三疑似的既裁不修：`notifications.status` 照契约 5 值冻结（`is_mock` 布尔承担 Mock 区分，不新增 `simulated`）；日报管理员重发走 pending 扫描不建独立端点；工牌「只建表」为合规——并确立 44h 超载先裁序（Day 8 完成定义项不可顺延） | `docs/plans/day08/adhoc-20261112-审查缺口修复.md` §0.2/§0.3 |
| 12 | N0 契约四方签认＋X10 作废（2026-11-12 电子回执）；day09 六处高风险文件（tool_registry/mcp_client/safety.py/graphs/medication_service/tool_executor）先出方案再落代码；MCP 幂等与记账统一入 `tool_executions` | `docs/plans/day09/adhoc-20261112-X10作废与N0签认单.md`；`docs/plans/day09/dev-b-day09-工作推进计划.md` §1.5/§2.2 |
| 13 | Day 10 契约 Y1/Y3：前端镜像产物路径 `/usr/share/nginx/html/{pad,console,family}`、构建命令 `npm ci && npm run build`、红线「three-vrm 不进 console/family 产物」；nginx 静态段与 `/api`、`/ws` 反代由 dev-A 独占合入 | `docs/plans/day10/day-10-交付与观测-工作推进计划.md` §3 Y1/Y3 |

## 4. 引用路径核对清单

本文件 §1–§3 引用的文件路径与存在性核对结果（2026-11-13 `ls` 实测）：

<details>
<summary>核对明细（全部通过）</summary>

- `docs/plans/day02/adhoc-20261104-鉴权内核审查底稿.md` ✅
- `docs/plans/day02/task-03-库与模型基座.md` ✅、`docs/plans/day02/task-04-鉴权内核.md` ✅
- `docs/plans/day03/adhoc-20261105-音频传输与设备链路技术方案.md` ✅、`docs/plans/day03/day-03-WebRTC与语音闭环.md` ✅、`docs/plans/day03/adhoc-20261105-A3媒体层执行计划.md` ✅
- `docs/plans/adhoc-20261109-房间床位交付存档.md` ✅、`eval/reports/声纹调优报告.md` ✅
- `docs/plans/day06/dev-c-Agent与统一出口.md` ✅
- `docs/plans/day08/adhoc-20261111-route_degraded契约冻结.md` ✅、`docs/plans/day08/adhoc-20261112-审查缺口修复.md` ✅
- `docs/plans/adhoc-20261111-媒体素材格式扩展名.md` ✅
- `docs/plans/day09/adhoc-20261112-X10作废与N0签认单.md` ✅、`docs/plans/day09/dev-a-安全闸门与生命周期.md` ✅、`docs/plans/day09/dev-b-day09-工作推进计划.md` ✅
- `docs/plans/day10/day-10-交付与观测-工作推进计划.md` ✅、`docs/plans/day10/dev-c-day10-工作推进计划.md` ✅、`docs/team_tasks/day10/dev-A-容器化与部署链.md` ✅、`docs/team_tasks/day10/dev-C-前端构建与文档齐套.md` ✅
- `docs/team_tasks/day02/PROGRESS.md` ✅、`docs/team_tasks/day04/PROGRESS.md` ✅、`docs/team_tasks/day09/PROGRESS.md` ✅、`docs/team_tasks/day10/PROGRESS.md` ✅
- `app/mcp_client/client.py` ✅、`app/core/redact.py` ✅、`app/services/tool_executor.py` ✅、`alembic/versions/0020_day9_medication_enums.py` ✅
- `tests/test_gates.py` ✅、`tests/test_injection.py` ✅、`eval/fixtures/injection.jsonl` ✅

</details>

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-13 | 首版（Day 10 C5 交付）：AI 辅助范围（day02–day10 按模块归纳）、高风险五类人工审查留痕核对（auth 签署未见完成留痕、Dockerfile 未落地均如实登记）、关键决策 13 条、引用路径核对清单。 |
