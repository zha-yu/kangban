# Day04 A2 编码实施计划：信令与对话闭环

> 状态：草稿　｜　版本：v1.8　｜　日期：2026-11-06
> 关联：docs/plans/day04/a2-信令与对话.md；docs/plans/day04/a2-dag-信令与对话.md；docs/plans/day04/a2-prompt-信令与对话.md；docs/team_tasks/day04/task-02-信令与对话闭环.md
> 上游依据：PRD v2.6 §3.2、FR-DLG-05、FR-VPR-01~03、FR-ADM-06/AC-39（验收）；技术设计文档 v1.5 §3.4 `conversation_threads`、§8.3（实现）；Day04 contracts v1.0 C5/C6（协议）
> 变更记录见文末

## 0. 文件定位与使用方式

本文件把 [a2-信令与对话.md](./a2-信令与对话.md) 的小时级 T0–T6 展开为可编码、可测试、可阻塞回报的实施步骤，不取代原计划，也不新增业务口径。

三份执行件各司其职：

| 文件 | 唯一职责 | 使用方 |
| --- | --- | --- |
| `a2-impl-信令与对话.md` | 说明做什么、改哪些文件、怎样验收 | 执行者与评审者 |
| `a2-dag-信令与对话.md` | 说明节点先后、并行关系、外部依赖、门禁与回退 | 调度者与执行者 |
| `a2-prompt-信令与对话.md` | 将目标、约束、DAG 执行规则和报告格式交给编码助手 | AI 编码助手 |

实施细节以本文件为准；节点依赖只以独立 DAG 文件为准；提示词不另造协议。若代码现状与计划描述不同，先记录差异，不得用猜测补齐外部接口。

## 1. 目标、范围与完成定义

### 1.1 目标

在 Day3 C1/C2/C3 不回归的前提下，完成以下 A2 自有能力：

1. 在 `/ws/signal` 追加 C5 身份确认消息、30 秒服务端超时和 `client_diag` 脱敏日志。
2. 提供 Minimax OpenAI 兼容的 httpx 非流式适配器，失败最多重试 2 次并返回明确降级话术。
3. 提供固定前缀上下文与 Redis 最近 5 轮对话，不支持长期记忆、Agent Tool 或三级路由。
4. 在 A1/A3 接口就绪后接入服务端 thread 生命周期、persona greeting 和 ASR FINAL 业务编排。
5. 用自动化测试证明身份隔离、generic 隐私收敛、降级、上下文上限及 C2 兼容性。

### 1.2 非目标

本计划不实现或修改：声纹模型与推理、VAD/ASR/TTS 适配器、WebRTC 建连和媒体轨、pad 前端、KWS、VRM、迁移、长期记忆、LangGraph、MCP、Agent、Laya、规则路由、metrics/P95 管线、配置文件新字段。

### 1.3 完成定义

- **A2 本地实现完成**：N00–N04 及可自验的 N08 用例通过，所有外部未决项均有明确阻塞记录。
- **跨角色接线完成**：N05–N07 所需接口已冻结并由对应 owner 提供，集成测试通过。
- **Day4 闭环完成**：N09 真链或约定测试替身联调通过，N10 全量质量门通过。
- 外部接口未交付时，只允许声明前两级中实际达成的部分，不得把 Mock 结果写成真链验收结果。

## 2. 开工事实与接口门禁

### 2.1 已核实的代码基线

- `app/audio/signaling.py` 已有 C2 首帧鉴权、心跳、媒体消息分派、下行队列、连接清理及 `deliver_asr_event()`。
- `app/audio/webrtc.py` 的 `_asr_callback()` 当前只调用 `deliver_asr_event()` 转发字幕；没有调用对话服务的业务 seam。
- `app/core/context.py` 当前只有 trace contextvar；`app/services/` 尚无 `llm_service.py`、`dialogue_service.py`、`identity_service.py`、`persona_service.py`。
- `app/main.py` 已注册 `/ws/signal`，lifespan 尚未创建或关闭 Redis 客户端；本任务预期不改 `main.py`。
- `Settings` 已有 `llm_provider/base_url/api_key/model/timeout/max_retries/temperature` 与 `redis_url/key_prefix`。代码默认值仍是 DeepSeek，Minimax 由部署环境注入，A2 不改默认值。
- `httpx`、`redis` 已在 `pyproject.toml`，无需新增依赖。
- Day3 WS 测试使用进程内 ASGI harness，继续沿用，不改用跨事件循环事务的 `TestClient`。

### 2.2 阻塞项与放行条件

| 编号 | 必须确认的问题 | 责任方 | 可先行工作 | 放行节点 |
| --- | --- | --- | --- | --- |
| B1 | **已裁决（2026-11-06）**：C6 `ThreadInfo.elder_id` 改为 `str | None`，generic 为 `None` | A1/A2 + 主理人 | N01–N04 | N05 generic 持久化 |
| B2 | **已裁决（2026-11-06）**：`conversation_threads` 创建/结束、`session_seq` 与 generic thread_id 由 A1 `identity_service` 独占；A2 只消费 | A1/A2 | C5、LLM、上下文 | N05 |
| B3 | **已解决（2026-11-06）**：`app/main.py` lifespan 创建/关闭 Redis/LLM/Dialogue/Orchestrator，挂 `app.state` | Day04 owner/A3 | 注入式 store 与 Fake Redis 测试 | N06 生产装配、N09 |
| B4 | **已解决（2026-11-06）**：`signaling.dispatch_asr_event` + `set_dialogue_final_handler` 为 FINAL 唯一 awaitable seam；A3 VAD segment → WAV | A2/A3 | A2 服务入口与 Mock 测试 | N07 |
| B5 | 个性化 `personas.greeting` 的只读 provider 与 generic 固定话术 | A3/A2 | greeting 不依赖 LLM 的单元测试 | N05 生产装配 |
| B6 | C6 为内部契约时，伪造/跨长者 thread 的服务异常由哪个现有路由映射为 HTTP 403 | A1/A2/API owner | 服务层拒绝与审计测试 | N09 AC-39 验证 |

B1/B2 已于 2026-11-06 裁决：`ThreadInfo.elder_id` 为 `str | None`；`conversation_threads` 由 A1 `identity_service` 独占创建，A2 只消费 `IdentityThreadResult`（身份 + thread_id），不建 thread、不自创 generic ID。B3/B4 已于 2026-11-06 解决：FINAL 统一走 `signaling.dispatch_asr_event`，不得在同步回调隐藏未跟踪 `create_task` 或绕过统一 seam。

## 3. 文件组织与写作用域

### 3.1 生产代码

| 文件 | 动作 | 只放什么 | 明确不放什么 |
| --- | --- | --- | --- |
| `app/audio/signaling.py` | 增量修改 | 原样追加 C5 TypedDict、连接级确认状态、30s 计时、诊断日志、必要的已冻结下行接口 | ORM 写库、LLM 调用、媒体处理、声纹推理 |
| `app/services/llm_service.py` | 新建 | Minimax httpx 适配、响应解析、重试与固定降级返回值 | SDK、密钥常量、Redis、TTS、WS |
| `app/core/context.py` | 扩展 | 保留 trace API；增加纯参数上下文/消息组装 | DB/Redis/HTTP 查询、客户端身份采信 |
| `app/services/dialogue_service.py` | 新建 | 原样追加 C6 类型、Redis 轮次读写、对话编排、thread/greeting 的已冻结服务接线 | 迁移、媒体编解码、长期记忆、健康/用药写入 |
| `app/main.py` | 默认不改 | 仅在外部装配决议明确要求时增加约定的一行 | 业务逻辑、每请求 Redis 创建 |

### 3.2 测试与留痕

| 文件 | 覆盖重点 |
| --- | --- |
| `tests/test_audio_ws_auth.py` | 只补必要的 C2 回归断言，不重写 harness |
| `tests/test_audio_identity_ws.py` | C5 正常、异常、超时、幂等、诊断与连接清理 |
| `tests/test_llm_service.py` | httpx MockTransport 成功/失败/重试/解析/脱敏 |
| `tests/test_dialogue_context.py` | 固定前缀、最近 5 轮、generic 上下文、trace API 回归 |
| `tests/test_dialogue_service.py` | Redis store、回复/degraded、thread/greeting、FINAL 编排 |
| `tests/test_dialogue_integration.py` | 仅在跨角色 seam 可用后增加；外部不可用时不伪造集成结果 |
| `docs/plans/task-a2-信令WS与鉴权.md` | 代码合入前追加 C5 属 C2 增量及接口裁决的变更记录 |

测试替身只放测试代码或显式测试构造，不在生产路径用 Mock identity 替代 A1、不用内存字典冒充生产 Redis。

### 3.3 禁触文件

不得修改 `app/core/config.py`、`.env.example`、`pyproject.toml`、`uv.lock`、`frontend/**`、`app/audio/{webrtc,asr,tts,vad}.py`、`app/api/audio.py`、voiceprint/identity/persona 的他人文件、任何迁移、`app/core/auth*.py`、`app/core/security.py`、`app/core/metrics.py`。除非用户另行明确授权，不创建 Git 提交。

## 4. 分步实施规格

### N00 · 基线核对与契约门禁（对应 T0）

**依赖**：无。**输入**：C5/C6、Day3 C2、现有 WS 测试。

1. 运行 Day3 WS 基线测试并保存结果，确认后续失败可与原问题区分。
2. 逐字段核对 C5/C6，登记 B1–B6 的决定、owner 和未决状态。
3. 在 Day3 A2 计划变更记录中登记 C5 追加，保持 C2 原文不变。
4. 将外部依赖分为“可注入单测”和“生产装配阻塞”，不等待 A1 spike 才启动 N01–N04。

**产出**：基线结果、契约留痕、阻塞清单。**完成判据**：C2 基线可复现；未决项没有被人写代码私裁。

### N01 · C5 信令与确认状态机（对应 T1）

**依赖**：N00。**文件**：`signaling.py`、`test_audio_identity_ws.py`、必要的 WS 回归文件。

1. 原样追加 `IdentityAskMsg`、`IdentityConfirmMsg`、`IdentityResultMsg`、`ClientDiagMsg`，导出新增类型；`TtsTextMsg` 的 C6 扩展保持向后兼容。
2. 将“设备绑定长者”和“本轮已确认长者”分成不同连接字段，generic 不得清空设备绑定关系或伪造活动长者。
3. 低置信或声纹不可用进入 ASKING：生成服务端 `ask_id`，候选集只取设备绑定长者 1 位，`timeout_s=30`。
4. 30 秒从 `identity_ask` 实际发送成功时起算，而不是客户端收到、入队或前端渲染时起算；断线时取消计时任务。
5. 合法 confirm 必须同时满足：存在待确认 ask、ask_id 匹配、elder_id 等于唯一候选、身份尚未定。成功则取消计时并只发一次 `identity_result(confirm)`。
6. ask_id 错配、重复 confirm、候选外 elder_id 均忽略并写 warn，不发 error、不关连接、不改变身份；候选外请求继续保持未确认，等待合法确认或超时。
7. voice/confirm/generic 的 `identity_result` 每会话每轮判定只发一次；generic 必须 `elder_id=None`，不下发 cosine、embedding 或内部异常。
8. `client_diag` 校验 stage/ok/error 类型。合法消息只记录 stage、ok、trace/device 等安全字段；默认不记录客户端 error 原文，避免“已脱敏”声明被滥用。非法消息走既有坏消息分支，不回诊断回执。
9. 清理逻辑覆盖超时任务、重连踢旧、客户端断连，保持 C2 auth/ping/offer/ice/error 语义不变。

**完成判据**：A2-01～A2-06 全绿；计时测试控制时钟，不真实等待 30 秒；C2 原测试全绿。

### N02 · Minimax httpx 适配器（对应 T2）

**依赖**：N00。**文件**：`llm_service.py`、`test_llm_service.py`。

1. 定义普通文本回复结构，至少含 `text: str` 与 `degraded: bool`；对话层不得自行从 HTTP 响应猜降级状态。
2. 使用注入的 `Settings` 与可注入 `httpx.AsyncClient`；请求 `POST {llm_base_url}/chat/completions`，`stream=false`，只从 Settings 取 model、key、temperature、timeout、max_retries。
3. 自动测试用 `MockTransport`，不访问真实服务、不写真实 key。生产客户端有明确 owner 和 `aclose()` 生命周期，不在每条消息中创建后遗留。
4. 成功时校验 OpenAI 兼容响应结构并返回非空文本，`degraded=False`；缺 key、超时、限流、连接失败或响应不可用时返回固定长者可读模板，`degraded=True`。
5. “最多重试 2 次”指首次请求后再尝试至多 2 次，总请求数至多 3 次；仅对约定瞬态错误退避，最后一次失败后不再 sleep。
6. 异常日志只含 trace/错误分类/尝试序号，不含 Authorization、请求头、完整响应体或内部堆栈回显给长者。
7. 非流式实现不宣称首 token 指标；≤800ms 只作为 N09 单次观察预算，不把 20s Settings 超时篡改为 800ms。

**完成判据**：A2-07 与响应解析、重试计数、无密钥日志用例全绿；正常与降级回复均能直接交给现有 TTS 文本入口。

### N03 · 纯上下文组装（对应 T3 前半）

**依赖**：N00。**文件**：`context.py`、`test_dialogue_context.py`。

1. 保留现有 trace 函数及行为，新增纯函数/不可变数据类型，不查库、不读环境变量。
2. 首个 system 消息只放字节稳定的固定人设与医疗边界，避免把时间、称呼、历史塞进固定前缀破坏 cache。
3. 动态身份、称呼和时间放独立上下文消息；时间由调用方传入，存储时间不在本层转换或查询。
4. 历史输入按“轮”截断，最近 5 轮最多展开为 10 条 user/assistant 消息，本轮 user 单独追加，不重复计入上一轮历史。
5. generic 使用通用固定上下文，不带个人称呼、个人记忆、其他 thread 历史；明确需要先确认身份才能进行健康/用药操作。
6. Day4 不增加关键词路由器。generic 的硬安全边界由不注入、不调用任何记忆/健康/用药端口保证，prompt 只负责可读拒绝话术。

**完成判据**：A2-08 的上下文部分通过；相同固定输入字节一致，10 轮后历史仍为 5 轮，trace 原测试无回归。

### N04 · Redis 最近 5 轮与对话回合服务（对应 T3 后半）

**依赖**：N02、N03。**文件**：`dialogue_service.py`、`test_dialogue_service.py`。

1. 原样复制 C6 契约块并标注版本；B1 未解决前不把 generic 强塞进非空 `ThreadInfo`。
2. Redis 客户端通过构造参数注入，由上层生命周期关闭；key 只含服务端 thread_id，并使用 `redis_key_prefix`，不接受客户端传入 key。
3. 每轮以结构化 JSON 保存 user/assistant 文本；读取只取列表末尾 5 轮，追加与裁剪使用同一 Redis 事务管线，保证 `RPUSH + LTRIM` 不分离。
4. 本计划自定义的内部 key 形为 `<redis_key_prefix>:dialogue:<thread_id>:turns`，只存文本，不存原始音频、embedding、token；不擅自增加 TTL 口径。
5. 调 LLM 前依次组装固定前缀、动态上下文、最近 5 轮、本轮文本；返回回复后写回本轮。history 损坏或单条结构非法时按安全空历史处理并记录脱敏 warn，不臆造内容。
6. 将 LLM 返回的 `degraded` 原样传给 WS 映射；正常回复缺省/显式 False，模板回复 True。C3 REST 形状保持不变。
7. 每个 ASR FINAL 在编排入口只调用一次对话服务；PARTIAL 不进入本服务。并发控制若尚无跨角色决议，不自创分布式锁，记录同 thread 并发限制供 N07 确认。

**完成判据**：10 轮测试始终只读最近 5 轮；Redis 调用参数无客户端身份；degraded 与写回行为准确；Fake Redis 只用于测试。

### N05 · thread 闸门与 greeting（对应 T4）

**依赖**：N01、N04，以及 B1/B2/B5 裁决、A1 conversation model/迁移和只读身份接口。**文件**：仅 A2 允许域，外部文件由 owner 提供。

1. voice/confirm/generic 身份由 A1 `identity_service` 在服务端事务内解析；`conversation_threads` 创建/结束、`session_seq` 递增与 generic thread_id 均由 A1 独占完成。A2 只消费 A1 `IdentityDecision` 的终态 `thread_id`（经 `identity_decision_to_result` 投影），客户端/model 提供的 elder_id/thread_id 一律忽略；A1 未回传 `session_seq` 时 A2 留空，不伪造。
2. generic 必须满足 `identity_source=generic AND elder_id IS NULL`；`ThreadInfo.elder_id` 已裁决为可空，A2 不填“默认长者”占位。
3. 说话人变化由 A1 `identity_service` 结束旧 thread（`ended_at`）并开新 thread；A2 随新 `thread_id` 切换 Redis key，禁止读取旧 thread 历史。
4. 对伪造/跨长者访问执行服务层拒绝并写带 trace 的审计日志；HTTP 403 只由 B6 确定的现有 API 边界映射，不新增越权路由。
5. `auth_ok + identity_result` 后通过 B5 只读 provider 获取 greeting，直接送既有 TTS/`tts_text`，不调用 LLM；generic 使用冻结的通用 greeting。
6. A1 identity/thread 返回、A2 greeting、身份结果之间的顺序用集成测试固定，防止未确定身份先读个人上下文。

**完成判据**：A2-09/A2-10 全绿；换人后旧 thread 已结束、新 thread 无旧历史；伪造身份不改变服务端上下文。外部依赖未齐时本节点保持阻塞，不用 Mock 宣告完成。

### N06 · ASR FINAL 业务编排入口（对应 T5 A2 侧）

**依赖**：N01、N04、N05；生产装配还需 B3。**文件**：`dialogue_service.py`，必要时含已确认的 `signaling.py` 接口。

1. 定义单一 awaitable 入口，输入只含服务端 device/连接上下文、turn_id 和 FINAL 文本，不接受客户端 elder_id/thread_id。
2. 顺序固定为：A1 `identity_service`（身份级联 + thread）→ 读取该 thread 最近 5 轮 → LLM → 写回 → 返回文本和 degraded；A2 不建 thread，只消费终态 `thread_id`；`needs_confirm` 由接线层转 `IdentityConfirmationRequired` 并发 C5 `identity_ask`。
3. 同一 turn_id 只编排一次；PARTIAL、ERROR 不触发 LLM。空 FINAL 不调用 LLM，按已确认边界返回或忽略。
4. 回复通过 C2 `tts_text` 下发并可带 `degraded`；音频仍由既有 TTS/WebRTC track 负责，本服务不直接操作音轨。
5. ASR、LLM、TTS 错误分别沿用 C1/C2 降级边界，不新增消息类型，不把内部异常写进字幕。

**完成判据**：FINAL 一次调用、PARTIAL 零调用、身份先于历史读取、generic 隔离、degraded 映射都有自动化证据。

### N07 · A2/A3 媒体 seam 接线（对应 T5 跨角色侧）

**依赖**：N06、B4（已解决）、A3 owner 配合。**写作边界**：`webrtc.py` 仅允许 `_asr_callback`/`_vad_callback`/segment 缓存的最小改动。

1. 已实现：`webrtc._asr_callback` 将 FINAL await 到 `signaling.dispatch_asr_event`，`asr_final` 字幕仍只投递一次。
2. 已注入 `device_id` / `turn_id` / `trace_id` / `audio_bytes`；未在同步回调隐藏未跟踪任务。
3. FINAL handler 异常由 `dispatch_asr_event` 捕获并只写 `error_type`，不反抛、不重复触发；断线时清理 VAD segment 缓存。
4. seam 的 A3 改动已限于 `_asr_callback` / `_vad_callback` / `_latest_segments`，不改媒体主链。

**完成判据**：`tests/test_dialogue_integration.py` 已覆盖进程内 `FINAL → handler` 一次、字幕一次、PARTIAL 零次、双设备隔离、WAV 交接；真实 ASGI/浏览器链路待 N09。

### N08 · 安全、失败与回归矩阵（对应 T6 自验）

**依赖**：N01–N06 中已实现部分；最终完成需全部节点。

1. 汇总运行 C5、LLM、context、Redis、thread、greeting、FINAL 分支测试，不用 skip/xfail 隐藏本任务失败。
2. 故障演练覆盖 LLM 超时/429/断连、声纹不可用、30s 超时、generic、重复/错配 confirm、换人、Redis 异常、TTS 失败边界。
3. 安全断言覆盖候选外身份、伪造 thread、跨 thread 历史、日志敏感字段、原始音频不入 Redis、无密钥入夹具。
4. 复跑 Day3 WS 与 audio 回归；失败若位于他人文件，保留证据并交还 owner，不直接修他人模块。

**完成判据**：A2-01～A2-11 中可在当前环境执行的项目全部有结果；未执行项逐条标为 blocked/unverified，而非 passed。

### N09 · 跨角色联调与真链观察（对应 T5/T6 联调）

**依赖**：N07、N08，以及 A1/A3/A4 运行环境和 B3/B6 放行。

1. 按“说话 → ASR FINAL → 身份 → thread → Minimax → tts_text/TTS”验证正常、低置信、声纹不可用三条路径。
2. 验证 generic 无个人历史/健康/用药写入，换人产生新 thread，双连接不串消息，greeting 不等待 LLM。
3. 通过 B6 指定入口验证伪造/跨长者 thread 返回 403 并留审计；没有入口时如实保留 AC-39 HTTP 层未验证。
4. Minimax 真实调用只使用本机 `.env`，自动测试不依赖它；记录一次响应耗时观察，不伪造 ≤800ms 或 P95 结论。
5. 原始音频不落盘，日志不记录密钥、声纹向量或完整敏感文本。

**完成判据**：每条路径有复现步骤、实际结果和 trace；真实服务不可用时明确记为未验证，不以 Mock 替代真链结论。

### N10 · 全量质量门与交付（对应 T6 收口）

**依赖**：N09；若 N09 阻塞，可运行本地质量门但节点不得标为全部完成。

1. 执行聚焦测试、Day3 回归、`uv run pytest -q`、`uv run ruff check app tests`、`uv run ruff format --check app tests` 和 `git diff --check`。
2. 审查 diff 只含允许文件；检查没有密钥、真实个人数据、音频、模型资产或依赖变更。
3. 核对 C5/C6 与冻结契约一致，所有自裁仅限内部实现并留痕，外部协议没有静默变化。
4. 输出节点状态、文件 diff、测试原文摘要、阻塞项、未验证真链、回退建议。除非另行明确授权，不提交 commit。

**完成判据**：质量门全绿且 diff 无越界；任何红项、跳过或外部未验证均醒目标注，不宣称“全部完成”。

## 5. 核心状态与数据不变量

### 5.1 身份状态机

| 当前状态 | 事件 | 动作 | 下一状态 |
| --- | --- | --- | --- |
| 已鉴权、身份未定 | 声纹命中绑定长者 | 发一次 voice 结果，绑定活动身份 | IDENTIFIED |
| 已鉴权、身份未定 | 低置信/声纹不可用 | 发 ask，实际发送后启动 30s 计时 | ASKING |
| ASKING | 合法候选内 confirm | 取消计时，发一次 confirm 结果 | IDENTIFIED |
| ASKING | ask 错配/重复/候选外 | warn + 忽略，不发 error、不关连接 | 原状态 |
| ASKING | 服务端 30s 到期 | 发一次 generic(null) 结果 | GENERIC |
| ASKING/IDENTIFIED | 重复 confirm | warn + 忽略 | 原状态 |
| IDENTIFIED | 静默复检发现换人 | 结束旧 thread，重新身份级联 | 身份未定/ASKING |
| 任意状态 | 断线/被新连接替代 | 取消计时与在途任务，清理本连接 | CLOSED |

### 5.2 必须始终成立的约束

- 设备绑定 elder_id 与活动说话人 elder_id 不混用；客户端/model 身份字段不作为事实来源。
- 每个身份判定轮次至多一个最终 `identity_result`；generic 永远 `elder_id=None`。
- thread_id、Redis key、session_seq 均由服务端确定；不同 thread 的历史不可互读。
- 最近 5 轮是 5 个 user+assistant 回合，不是 5 条消息；固定前缀与动态上下文分离。
- LLM 首次请求之后最多再重试 2 次；模板回复 `degraded=true`，正常回复 `degraded=false`。
- generic 没有记忆、健康、用药写入依赖；Day4 不通过新增路由规则模拟另一套安全层。
- 日志每行带 trace_id，不含 token、密钥、embedding、原始音频和客户端未可信脱敏文本。

## 6. 验收追踪矩阵

| 原验收 | 节点 | 自动化/联调证据 |
| --- | --- | --- |
| A2-01 C2 回归 | N00/N01/N10 | 原 WS/audio 测试与全量 pytest |
| A2-02 identity_ask | N01 | 两种 reason、唯一候选、ask_id、30s 字段 |
| A2-03 identity_confirm | N01 | 正常、错配、重复、候选外、连接保持 |
| A2-04 generic | N01/N03/N06 | 控制时钟超时、null、无个人上下文/动作端口 |
| A2-05 identity_result | N01 | 三 source、单次下发、无比对细节 |
| A2-06 client_diag | N01 | 合法/非法 stage、敏感 error 原文不落日志 |
| A2-07 LLM | N02/N09 | MockTransport 分支、真实单次观察（可选且如实） |
| A2-08 context | N03/N04 | 10 轮、5 轮上限、固定前缀、generic |
| A2-09 thread | N05/N06/N09 | 服务端 ID、403 边界、换人、历史隔离 |
| A2-10 greeting/TTS | N05/N06 | 身份后 greeting、零 LLM、复用 C2/C6 |
| A2-11 全链路 | N07/N09 | 正常/低置信/服务不可用三条复现记录 |

质量门不额外换算 A2 单文件覆盖率；本任务新增分支必须各有测试，全项目接口覆盖目标仍按 PRD FR-TC-06。

## 7. 调度、阻塞与回退

唯一节点依赖以 [a2-dag-信令与对话.md](./a2-dag-信令与对话.md) 为准。可优先并行的三条线为：

- N01：C5 信令与状态机；
- N02 → N04：LLM 与对话服务；
- N03 → N04：纯上下文与对话服务。

N01–N04 不等待 A1 声纹真实模型。N05 之后遇外部阻塞时，继续完善已允许域的测试和文档，不越权修改依赖方文件。真实服务不可用只影响 N09 的真链结论，不自动撤销已通过的单元测试，也不能把终端节点标为全量完成。

## 8. 交付报告格式

执行者最终必须按以下顺序报告：

1. **节点状态表**：N00–N10 分别为完成/部分完成/阻塞/未执行，并给证据。
2. **文件变更摘要**：逐文件说明新增行为，列出实际 diff 文件。
3. **测试结果**：逐条列命令、通过/失败数量和未执行原因，不省略红项。
4. **契约与外部依赖**：B1–B6 当前决定、owner、仍缺什么。
5. **安全与范围检查**：身份注入、generic、日志、原始音频、禁触文件。
6. **真链与限制**：Mock 与真实服务结果分开陈述；无证据的性能数字不写。

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 基于 Day04 A2 小时级计划、C5/C6 与当前代码基线，新增 N00–N10 编码实施规格；明确文件边界、B1–B6 接口门禁、身份状态机、Redis/LLM 内部实现口径、A2-01～A2-11 验收追踪及交付报告格式。 |
| v1.1 | 2026-11-06 | B1/B2 裁决落地：`ThreadInfo.elder_id` 改为 `str | None`；`conversation_threads` 创建/结束、`session_seq` 与 generic thread_id 由 A1 `identity_service` 独占；N05/N06 改为 A2 只消费 `IdentityThreadResult`（身份 + thread），不建 thread、不双写。 |
| v1.2 | 2026-11-06 | A1 `identity_service` 实际交付 `IdentityDecision`（thread_id/confidence/reason，未回传 ThreadInfo/session_seq）；A2 增加 `identity_decision_to_result` 适配，N05/N06 只消费终态 `thread_id`，`needs_confirm` 转 `IdentityConfirmationRequired`；`session_seq` 不伪造。 |
| v1.3 | 2026-11-06 | B3/B4 落地：`app/main.py` lifespan 装配 Redis/LLM/Dialogue/Orchestrator；`signaling.dispatch_asr_event` 为 FINAL 唯一 awaitable seam，`webrtc._asr_callback` 从 VAD segment 生成 WAV 并 await；新增 `tests/test_dialogue_integration.py`。 |
| v1.4 | 2026-11-06 | C5 接线落地：voice/generic 终态发布 `identity_result`；confirm/timeout handler 调 A1 开 thread 并写入 `(device_id, trace_id)` 缓存；A1 开新 thread 前关闭该设备旧 thread；新增 `apply_generic_identity`、`begin_identity_recheck` 与相关测试。 |
| v1.5 | 2026-11-06 | A1 换人检测修正：已识别会话低于阈值 → 结束旧 thread + `confirm/low_confidence`，generic 命中 → voice 新 thread；A2 复检分支在 `thread_id=None` 时清缓存并发 `identity_ask`；补 5 类 recheck 测试。 |
| v1.6 | 2026-11-06 | `session_seq` 与 TTS 音频落地：A1 `IdentityDecision.session_seq` 回传，A2 投影到 `IdentityThreadResult`；回复先发 `tts_text`，再 TTS 合成 PCM 经 `webrtc.enqueue_tts_for_device` 写播放轨；TTS 失败保留字幕。 |
| v1.7 | 2026-11-06 | 合并 `origin/dev`（293881d，merge cb177db）后的 A2 兼容：`client_diag{stage:vad, ok:false}` 激活远端 W4 服务端 VAD 兜底；identity 三打点位接入；`webrtc.py` 双方 VAD 实现共存合并；新增 C5 VAD 兜底与 metrics 用例；聚焦回归 111 passed / 1 skipped。 |
| v1.8 | 2026-11-06 | W4 兜底段下游接线：无 `on_vad_segment` 时 `_handle_vad_segment` 默认调 `_transcribe_vad_segment`，整段切 60ms ASR 帧 → FINAL 经 `on_asr_event` → `_asr_callback`（字幕 + A2 handler + WAV）；兜底 turn 写入 `_playback_turn_id`；客户端 turn 进行中/非 OPEN 跳过；兜底激活时停用本地 `VadSession`；新增 4 条测试，聚焦回归 126 passed / 1 skipped。 |
