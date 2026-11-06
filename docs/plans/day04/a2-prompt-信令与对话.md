# Day04 A2 · AI 编码助手执行提示词

> 状态：草稿　｜　版本：v1.0　｜　日期：2026-11-06
> 关联：docs/plans/day04/a2-impl-信令与对话.md；docs/plans/day04/a2-dag-信令与对话.md；docs/team_tasks/day04/task-02-信令与对话闭环.md
> 上游依据：Day04 contracts v1.0 C5/C6；PRD FR-DLG-05、FR-VPR-01~03、FR-ADM-06；技术设计文档服务分层与 `conversation_threads`
> 变更记录见文末

以下正文可整体复制给能够读取和修改当前仓库的 AI 编码助手。执行时以仓库内文件为准，不要把本提示词当作修改冻结契约的授权。

---

## 任务目标

你是康伴（SilverMate）仓库的 A2 后端编码执行者。请严格按 `docs/plans/day04/a2-dag-信令与对话.md` 的拓扑顺序，完成 `docs/plans/day04/a2-impl-信令与对话.md` 定义的 Day04 A2 信令与对话闭环。

最终目标是：在不破坏 Day3 C1/C2/C3 的前提下，增量实现 C5 身份确认信令、Minimax OpenAI 兼容 LLM 适配、固定前缀与 Redis 最近 5 轮、服务端 thread/greeting/ASR FINAL 编排，并用测试证明 C2 兼容、身份隔离、generic 隐私收敛和失败降级。外部接口未就绪时，只完成可独立验证的节点并准确报告阻塞，不得越权改文件或伪造联调结果。

## 一、开工前必读与事实优先级

按顺序完整阅读：

1. `AGENTS.md` 或 `agents.md`，尤其是事实来源、分层、禁区、§5 高风险文件、§8 编码约定。
2. `docs/plans/day04/a2-impl-信令与对话.md`：本任务的实施细节与验收。
3. `docs/plans/day04/a2-dag-信令与对话.md`：唯一节点依赖事实源。
4. `docs/plans/day04/a2-信令与对话.md`：原 T0–T6 小时级计划。
5. `docs/team_tasks/day04/task-02-信令与对话闭环.md` 与 `docs/team_tasks/day04/contracts.md`：A2 写作域及冻结 C5/C6。
6. `docs/康伴3D数字人_PRD.md` 的 §3.2、FR-DLG-05、FR-VPR-01~03、FR-ADM-06/AC-39。
7. `docs/康伴3D数字人_技术设计文档.md` 的服务分层、`conversation_threads`、Redis 最近 5 轮相关段落。
8. 当前实现与测试：`app/audio/signaling.py`、`app/audio/webrtc.py` 的 `_asr_callback`、`app/core/context.py`、`app/core/config.py`、`app/main.py`、`tests/test_audio_ws_auth.py`、`pyproject.toml`。

事实优先级：PRD 定验收，技术设计文档定实现，冻结 C5/C6 定本日协议；大纲不得作为字段、阈值或接口依据。计划与代码现状不同，先记录，不得自行改写基线文档。

## 二、已核实的输入条件

- `signaling.py` 已有 C2 鉴权、心跳、媒体分派、下行队列和清理；只做 C5 增量，不重写骨架。
- `webrtc.py` 当前 FINAL 只经 `deliver_asr_event()` 转发字幕，没有对话业务 seam；该文件属于 A3，禁止你修改。
- `context.py` 当前只有 trace 功能，新增能力必须保留原 API 行为。
- `llm_service.py`、`dialogue_service.py` 尚不存在；`identity_service.py`、`persona_service.py` 和 conversation model 当前不能假定已交付，开工时重新核实。
- `main.py` 已注册 `/ws/signal`，lifespan 尚未提供 Redis；本任务默认不改 `main.py`。
- Settings 已有 LLM 与 Redis 字段，Minimax 通过环境配置注入；不得为了让测试通过而修改供应商默认值。
- `httpx`、`redis` 已在依赖中；不要安装 SDK、修改 `pyproject.toml` 或 `uv.lock`。

## 三、允许与禁止的文件范围

只允许修改或新增：

- `app/audio/signaling.py`
- `app/services/llm_service.py`
- `app/services/dialogue_service.py`
- `app/core/context.py`
- `tests/test_audio_ws_auth.py`（仅必要回归增补）
- `tests/test_audio_identity_ws.py`
- `tests/test_llm_service.py`
- `tests/test_dialogue_context.py`
- `tests/test_dialogue_service.py`
- `tests/test_dialogue_integration.py`（仅真实 seam 可用时）
- `docs/plans/task-a2-信令WS与鉴权.md`（只追加 C5/接口裁决留痕）
- `app/main.py` 仅在外部装配决议明确要求时改约定的一行；当前预期零改动。

禁止修改：冻结 contracts、PRD、技术设计文档、大纲、`app/core/config.py`、`.env.example`、`pyproject.toml`、`uv.lock`、`frontend/**`、`app/audio/webrtc.py`、`asr.py`、`tts.py`、`vad.py`、`app/api/audio.py`、voiceprint/identity/persona 他人模块、迁移、auth/security/metrics 文件。

不要创建 Git commit、不要 push，除非用户在本次任务之外另行明确授权。不要新增 README、第二种 LLM/Redis/身份实现或与任务无关的重构。

## 四、不可违反的输入与安全约束

1. elder_id、thread_id、session_seq、Redis key 只从服务端上下文或服务端事务产生；客户端和模型传入的身份字段一律丢弃。
2. 设备绑定 elder_id 与活动说话人 elder_id 分开保存；generic 不得伪造默认长者，也不得误清设备绑定关系。
3. C5 候选集恒为设备绑定长者 1 位；声纹命中阈值为 cosine ≥0.85，但声纹计算属于 A1，不在本任务实现。
4. `identity_ask` 的 reason 只能为 `low_confidence/vp_unavailable`，`timeout_s=30`；confirm 必须回带 ask_id 和唯一候选 elder_id。
5. 30 秒从服务端实际发送 ask 成功时起算。ask 错配、重复 confirm、候选外 elder_id：warn + 忽略，不发 error、不关连接、不改变已定身份。
6. `identity_result.source` 只能为 `voice/confirm/generic`；每轮判定只发一次最终结果，generic 必须 `elder_id=None`，不得暴露 cosine、embedding 或内部异常。
7. `client_diag` 只写结构化安全字段，不落库、不转发、不进 metrics；默认不要记录客户端 error 原文，以防敏感信息进入日志。
8. generic 不读个人记忆、不读其他 thread 历史、不写健康、不确认用药。Day4 通过不注入和不调用这些端口实现硬隔离，不新增另一套关键词路由。
9. LLM 使用 httpx 调 Minimax OpenAI 兼容 `/chat/completions`，非流式、无 SDK。首次请求后最多重试 2 次，总请求至多 3 次；失败返回固定长者可读模板并标 `degraded=true`，正常为 false。
10. 密钥只从 Settings 读取，不进入代码、日志、测试夹具、报告或提交信息。自动测试只用 MockTransport，不访问真实 Minimax。
11. 最近 5 轮是 5 个 user+assistant 回合，展开最多 10 条历史消息；本轮 user 单独追加。固定人设前缀与动态时间/称呼/历史分离，前缀字节稳定。
12. Redis 客户端必须注入并有明确关闭 owner；不得每轮创建未关闭客户端。Redis 只存对话文本，不存原始音频、embedding、token。
13. C2 `tts_text` 保持兼容，只增加 C6 可选 `degraded`；C3 REST 形状不变。不新增回复消息类型，不直接操作 WebRTC 音轨。
14. 每行新增日志必须带 trace_id；不记录密钥、token、原始音频、embedding、完整敏感个人数据。
15. 所有 I/O async，全量 type hint；services 是唯一写库层，audio/core 不得直接 ORM 写库。

## 五、DAG 执行规则

1. 先解析 DAG 的节点与 deps，按拓扑序执行。一个节点的全部直接依赖为 `done` 才能完整开始；仅允许文档明确标注的本地 `partial`。
2. 每完成一个节点，立即运行该节点聚焦测试并记录命令与结果，再进入后继节点。
3. 任一外部依赖缺失，将受影响节点标为 `blocked` 或 `partial`，沿 DAG 传播；继续执行无依赖受阻的 N01–N04 或测试工作。
4. 不得通过改 DAG、删依赖、skip/xfail、放宽断言、Mock 生产身份或修改他人文件来消除阻塞。
5. 节点状态只能使用 `done / partial / blocked / failed / pending / in_progress`。Mock 通过不等于真链通过，本地质量门通过不等于 N10 全部完成。

按以下节点执行，详细行为以实施计划为准：

### N00：基线与契约门禁

- 运行 Day3 WS 基线测试，保存原始结果。
- 核对 C5/C6，登记实施计划 B1–B6：generic 空值冲突、generic thread 公式、Redis 生命周期、FINAL seam、greeting provider、403 映射边界。
- 在 Day3 A2 计划追加 C5 属 C2 增量的变更记录。
- 若 `ThreadInfo.elder_id: str` 与 generic null 的冲突尚未裁决，不得私改契约或自创 generic ID；只阻塞 N05 的相关部分，不阻塞 N01–N04。

### N01：C5 信令状态机

- 原样追加 C5 TypedDict，并保留 C2 类型和分派。
- 实现身份未定 → ASKING → IDENTIFIED/GENERIC，以及合法 confirm、异常 confirm、超时、重复结果抑制、断线取消计时。
- `client_diag` 校验 `vrm_load/webrtc/vad/asr`、bool ok、string error；合法消息无回执，非法消息走既有坏消息边界。
- 用可控时钟测试 30 秒，不真实等待；复跑全部 C2 测试。

### N02：Minimax 适配器

- 返回明确的数据结构，至少包含 `text` 和 `degraded`。
- 支持注入 Settings 和 httpx AsyncClient/MockTransport，正确解析 `choices[0].message.content`，拒绝空或畸形响应。
- 覆盖成功、缺 key、超时、429、连接失败、畸形响应、重试次数、退避次数、日志无密钥。
- 客户端有明确 `aclose` 所有权；不要每消息泄漏新客户端。
- 不宣称首 token/P95；≤800ms 只在 N09 做一次真实观察，有数据才报告。

### N03：纯上下文组装

- 保留 trace API；新增只接收参数的 builder，不查 DB/Redis/HTTP，不散读环境变量。
- 首个 system 为稳定固定人设和医疗边界，动态身份/称呼/时间另放上下文。
- 验证固定前缀字节一致、10 轮只保留最近 5 轮、generic 无个人称呼/历史/记忆。

### N04：Redis 五轮与对话回合

- 在 `dialogue_service.py` 原样保留 C6 类型；若 B1 未裁决，不得篡改 ThreadInfo 来“兼容”generic。
- Redis key 使用 `<redis_key_prefix>:dialogue:<thread_id>:turns`，thread_id 只能来自服务端。
- 每轮结构化保存 user/assistant，读取末尾 5 轮；用 Redis 事务管线原子完成追加与裁剪，不擅自增加 TTL。
- 调 LLM 后写回本轮并透传 degraded；history 非法时安全降级为空历史并写脱敏 warn，不臆造。
- Fake Redis 只能用于测试，不得成为生产默认实现。

### N05：thread 与 greeting

- 只有 N01/N04、A1 identity/conversation 接口、B1/B2/B5 裁决均就绪才完整实现。
- 服务端事务创建 thread、递增 session_seq；generic 必须满足冻结后的 null 口径。
- 换人先写旧 thread `ended_at`，再开新 thread，并切换 Redis key，禁止旧历史流入新会话。
- 伪造/跨长者访问在服务层拒绝并写带 trace 的审计；403 由 B6 指定的现有 API 边界映射，不新增越权路由。
- `auth_ok + identity_result` 后读取 persona greeting，直接走既有 TTS/tts_text，不调用 LLM；generic 用已冻结通用 greeting。
- 外部未交付时保持 blocked/partial，不用 Mock 宣告 N05 完成。

### N06：ASR FINAL 编排入口

- 提供单一 awaitable 入口，只接收服务端连接/device 上下文、turn_id、FINAL 文本。
- 顺序必须是身份级联 → thread → 最近 5 轮 → LLM → 写回 → 文本/degraded 返回。
- 同一 turn_id 只编排一次；PARTIAL/ERROR 不触发 LLM，空 FINAL 不触发 LLM。
- 生产装配需要 B3 Redis 生命周期；未就绪时只完成注入式单测并标 partial。

### N07：A2/A3 媒体 seam

- B4 未冻结前停止本节点，不修改 `webrtc.py`，不在同步回调中隐藏未跟踪 `create_task`。
- 冻结后由对应 owner 将 FINAL await 到 N06，保留原 `asr_final` 字幕一次投递。
- 明确 trace/device/turn 注入、异常传播、断线取消；若需 A3 改代码，只输出最小接口和补丁请求。

### N08：安全、失败与回归

- 覆盖 C5 全分支、LLM 故障、generic、换人、Redis 异常、thread 隔离、greeting、FINAL/PARTIAL、日志脱敏。
- 复跑 Day3 WS/audio 测试；不得删除旧测试、放宽旧断言或用 skip 隐藏失败。
- 他域失败与 N00 基线比较，保存证据交还 owner，不直接修改他域文件。

### N09：跨角色联调

- 仅在 N07/N08 和运行环境就绪后执行。
- 复现正常、低置信、声纹不可用三条路径，以及 generic、换人、双连接隔离、greeting 不等待 LLM。
- 通过 B6 入口验证伪造/跨长者 thread 的 403 与审计；没有入口就明确写 HTTP 层未验证。
- 真实 Minimax 只在环境已配置且本次联调明确允许外部调用时执行；不得打印 key。不可用就标 unverified，不能用 Mock 冒充真链。
- 延迟只记录真实单次观察，不编造 ≤800ms、P50/P95 或达标率。

### N10：质量门与交付

- 执行下节所有可运行质量命令，检查实际 diff、密钥、禁触文件、协议漂移。
- N09 未完整完成时，可以运行本地质量门，但 N10 只能标 partial。
- 不提交 commit；按“七、输出格式”给出最终报告。

## 六、验收标准与命令

### 6.1 必须有的自动化证据

- C2：原 auth、心跳、offer/ice、断线清理、双连接隔离无回归。
- C5：两种 ask reason、合法 confirm、ask 错配、重复 confirm、候选外 elder、30s generic、结果一次、诊断合法/非法、断线取消。
- LLM：成功、超时、429、断连、畸形响应、缺 key、至多 3 次总请求、degraded、无敏感日志。
- Context/Redis：10 轮后仅最近 5 轮、固定前缀稳定、generic 隔离、写回与裁剪、坏历史安全处理。
- Thread/greeting：服务端 ID、伪造拒绝、换人 ended_at、新 thread 无旧历史、greeting 零 LLM。
- 编排：FINAL 一次、PARTIAL/ERROR/空文本零次、身份先于历史、degraded 映射、双设备不串消息。

### 6.2 聚焦与全量命令

按节点运行相应聚焦命令，最终执行：

```bash
uv run pytest tests/test_audio_ws_auth.py tests/test_audio_identity_ws.py -q
uv run pytest tests/test_llm_service.py tests/test_dialogue_context.py tests/test_dialogue_service.py -q
uv run pytest tests/test_audio_asr.py tests/test_audio_tts.py tests/test_audio_webrtc.py tests/test_audio_utterance.py tests/test_audio_vad.py -q
uv run pytest -q
uv run ruff check app tests
uv run ruff format --check app tests
git diff --check
git status --short
```

若某文件因外部依赖未创建，不得把对应命令写成 passed；标明未执行及原因。若全量测试存在 N00 已记录的基线失败，分别报告“原已失败”和“本次新增失败”，不得笼统写全绿。

## 七、停止条件

出现以下任一情况，停止受影响节点并报告，但仍可继续 DAG 中不受阻的工作：

- 需要修改冻结 C5/C6、PRD、技术设计文档或禁触文件才能继续。
- B1/B2 未裁决却被要求生成 generic thread_id 或填充非空 elder_id。
- 需要伪造客户端/model 身份、绕过设备 JWT、加入生产 Mock identity 才能通过测试。
- 需要修改 `webrtc.py` 才能接 FINAL，但 B4 尚未冻结。
- 需要硬编码密钥、记录真实敏感数据、保存原始音频或访问生产系统。
- 需要删除/放宽测试、隐藏失败、伪报真实服务或性能结果才能完成。

停止时写清：节点、缺失输入、阻塞依赖、需要谁作何决定、已经完成的无关工作、建议的最小下一步。

## 八、最终输出格式

只在实际执行和验证后，按以下 Markdown 结构报告，不要粘贴整份代码：

```markdown
## 执行结论
<完成 / 部分完成 / 阻塞；一句话说明实际达到的范围>

## 节点状态
| 节点 | 状态 | 实际产出 | 测试证据或阻塞原因 |
| --- | --- | --- | --- |
| N00…N10 | … | … | … |

## 文件变更
| 文件 | 变更摘要 |
| --- | --- |
<只列实际变更；另附 git status 中的越界检查结果>

## 验证结果
| 命令 | 结果 | 说明 |
| --- | --- | --- |
<列真实退出状态、passed/failed 数量；未执行必须说明>

## 契约与外部依赖
| 编号 | 状态 | owner/决定 | 剩余阻塞 |
| --- | --- | --- | --- |
| B1…B6 | … | … | … |

## 安全与范围检查
<身份服务端注入、generic 隔离、日志脱敏、原始音频、禁触文件、依赖与协议是否变化>

## 真链与限制
<分别说明 Mock、集成、真实 Minimax/ASR/TTS 的证据；未验证项和性能观察>

## 建议的下一步
<按 DAG 列出解锁所需的最小动作；没有则写“无”>
```

不要声称已 commit、已联调、已达标或全量通过，除非对应操作和证据确实发生。

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 新增可直接交给 AI 编码助手的执行提示词：包含任务目标、必读输入、文件权限、安全约束、N00–N10 DAG 执行指令、测试命令、停止条件与固定报告模板；明确不越权、不伪造真链、不擅自提交。 |
