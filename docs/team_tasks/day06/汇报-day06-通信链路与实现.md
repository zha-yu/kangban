> 用途：回答老师提问「今天你们组都做了哪些任务，具体是如何实现的？」
> 依据：`docs/team_tasks/day06/`（契约 v1.4 / 七张任务卡 / 讲义状态图）、`docs/plans/day06/`（DAG 与交付存档）、2026-11-09 当日 git 提交、以及当前工作区代码。
> 配套图：`day06-通信全链路.drawio`（draw.io 打开，两页：① 通信全链路 ② 核心逻辑与配置）。
> 全文事实口径：PRD 定验收、技术设计文档定实现、合同冻结件定字段；大纲只取流程不取事实（AGENTS.md 前置铁律）。

---

# 一、先给一句总纲（汇报开场就用这句）

> 「今天 Day 6 我们做的是**把长者的'一句话'变成系统里可追溯、可打断、可恢复的一组事实**。四人分四条线并行：A 线做事件账本与工具事实，B 线做状态中心与上下文，C 线把对话回合收敛到统一出口，D 线在 pad 端做送达确认。通信上我们沿用 Day 3 的 **WS 信令 + WebRTC 音频轨** 主链路，在上面补了 **分段下发 → 送达回执** 闭环。主动用药触达和工具执行仍分别经过各自受控出口；它们共享服务端身份、幂等与审计边界，但不混成一条业务通道。」

**专业表达的关键**：先给「一句话定位」→ 再给「分工矩阵」→ 再给「一条主链路」→ 最后给「一个技术难点 + 一个如实结论」。不要按提交时间流水账讲，老师要听的是「结构」和「为什么这么做」。

---

# 二、今天做了哪些任务（分工 → 产物 → 验收）

## 2.1 四条线 + 一个共担起点

| 线 | 负责人 | 节点 | 今天真正落地的东西 | 关键提交 |
| --- | --- | --- | --- | --- |
| **W0 共担** | 主理人 + dev-A | N00 | 冻结 `day06/contracts.md`（五个标识、12 类事件、`tts_text` v2 / `segment_ack` 四组契约）、同步 PRD/TD/ER | `d6851c0`、`dd4416d` |
| **A 历史事件与工具事实** | Yang Wendi | N01/N04/N10/N12/N26/N23（21.0h） | 0008 三张事件表、`history_service.py` 事件账本、`tool_executor.py`、用药确认子图 + PG checkpointer、工单服务、**E2 主动触达下发**、0012 回执白名单 | `81d24b4`、`5d9b5ea`、`533c3b1`、`5c9d1bc` |
| **B 状态中心与上下文记忆** | 张婧婧 | N02/N05/N06/N08/N09/N19/N22（20.0h） | 0009 `memory_chunks`、0010 用药六表、`state_service.py`（版本 CAS）、`session_coordinator.py`、`core/context.py` 九层装配、`memory_service.py` 召回骨架、E20 改绑后端、讲义状态图 | `a7cc6db`、`3c9c0d9`、`1bd2933` |
| **C Agent 与统一出口** | leimin-dev | N07/N11/N13/N14/N20/N24/N25/N27（21.5h，S1–S5 六阶段） | `turn_service.py` 七步统一出口、`response_service.py` 分段下发 + 四步校验 + 打断五步、ReAct Agent、安全一票否决、天气 MCP server/client、0011 `notifications` | `7bd8abc`、`74c9af2`、`a0ef9a4`、`6abd9eb`、`01e50ca`、`73724d4` |
| **D pad 送达确认与改绑 UI** | zy | N03/N16/N18（10.0h） | `protocol.ts` 契约类型、`playback-queue.ts` 分段队列 + `segment_ack`、`realtime.ts` 打断收口、console 员工列表与改绑弹窗 | `167911c`、`1ba2ee1`、`56ea8eb`、`53713b9`、`10a75ab`/`54d5f23` |

**当日提交量**：非 merge 提交 **44 个**（Yang Wendi 16 / leimin-dev 16 / zy 7 / 张婧婧 5），另有 27 个 merge（PR #42–#55）。

## 2.2 数据底座与验收

- 迁移 `0008`（`conversation_events` / `conversation_turns` / `tool_executions`）、`0009`（`memory_chunks` 23 列 + `vector(1536)`）、`0010`（用药/风险/工单六表）、`0011`（`notifications`）、`0012`（`medication_attempts.receipt` CHECK 白名单）；`0007 → head → downgrade → upgrade` 三次连续成功。
- 全量门禁：`975 passed / 4 skipped / 1 deselected`（`-m "not slow"`），覆盖率 **81.51%**，`ruff` 干净，`--check` / `--check-plans` 零退出码。A 线 G-A2 并发 20 写 `seq` 恰为 1..20；普通语料 50 例安全误报 **0**；E20 改绑三层测试（服务层 8 + API 层 9）全绿。
- 上述是 Day 6 已记录的验收结果；当前工作区补充的用药主动 TTS 接线没有包含在这组历史门禁数字中，本次也未重跑测试。因此汇报时把它称作「本地未提交补充」，不要说成已合入且已由上述门禁验证。
- **必须主动说明的两点诚实结论**：
  1. `PROGRESS.md` 看板显示「2/7 已收口」是**任务卡首部状态行未回填的机械滞后**（看板由 `scripts/update_progress.py` 生成），不是只做了两张卡；执行状态以交付存档 + git 为准。
  2. `a1-延迟基线表.md`：AC-42（简单指令语音响应 ≤1.5s，20 次 ≥19 次）**0/16 不达标**；端到端 P50 **7872ms** / P95 **13537ms**，识别后响应 P50 2920ms / P95 5873ms。主因是三段**非流式兜底形态**：本地 FunASR `asr` P50 4909ms（含说话时长）、LLM 非流式首 token P50 1470ms、`edge-tts` 整段合成首包 P50 1477ms。这是「测了、超标了、原因定位到了」，不是没测。

## 2.3 当天修掉的真实 bug（很能体现深度，建议挑 2 个讲）

| 提交 | 问题 | 修法 |
| --- | --- | --- |
| `73724d4` | `ws_superseded` 取消旧连接任务链，会连带取消**正在跑的一轮对话**，导致七步状态/事件半写、分段下发与 TTS 喂轨全断，`CancelledError` 还会炸穿 ASGI | `dispatch_asr_event` 用 `asyncio.shield(task)` 把对话任务与旧连接取消解耦，并挂 `_reap` done_callback 记录异常类型 |
| `3815842` | S4 装配层没注册 `segment_ack` 回调；N14 分支会把文字**整段重发一次**（与第 7 步分段下发双发） | `app/main.py` 重写装配、同一 `ResponseService` 实例注册回调；`reply.response_id is not None` 时跳过整段 `deliver`，只记 `dialogue_reply_already_streamed` |
| `72661ed` | `SegmentAckMsg.at_ms` 被写成可选，与前端 `protocol.ts` 必填不一致（末段 played 计时兜底依赖它） | 收紧为必填，契约与 TS 零差异 |
| `5c9d1bc` | 0010 的 `receipt` CHECK 是占位三值，真实触达回执全被拒；`record_attempt` 还把 CHECK 违反当幂等静默吞掉；迁移名 36 字符超 `VARCHAR(32)` | 0012 扩为六值、改短名 27 字符、`drop_constraint/create_check_constraint` 传短名 `receipt` 绕开 `naming_convention` 二次渲染；补两条回归用例 |
| `5c9d1bc` 收口复查 | 图重放会**重复投递**同一条用药提醒（`record_attempt` 幂等只挡记录不挡下发） | `remind` 节点改「先查后发」+ 新增 `medication_service.get_attempt()` |

---

# 三、通信的整体流程（这是老师最想听的部分）

## 3.1 一句话：三条通道，各司其职

| 通道 | 协议/落点 | 传什么 | 为什么不混用 |
| --- | --- | --- | --- |
| **信令与控制面** | `WS /ws/signal`（文本帧 JSON，一帧一对象） | 鉴权、SDP/ICE、VAD 边界事件、ASR 字幕、TTS 分段文字、送达回执、身份确认、客户端诊断 | 控制消息要**有序、可鉴权、可回放**；音频不走它，避免 JSON 序列化与队头阻塞 |
| **媒体面** | WebRTC（aiortc + `RTCPeerConnection`），上下行音频轨 | 上行麦克风 PCM、下行 TTS PCM | 全双工 + 低延迟 + 可打断，必须走专门的媒体通道；SDP/ICE 借信令面交换 |
| **降级 / 业务面** | REST `POST /api/audio/utterance`；`PUT /api/staff/{id}/elders`；`POST /api/device-sessions` | 整段 WAV 上传、管理端改绑、设备换证 | 无状态、可幂等、可缓存、失败可见；WebRTC 建不起来时保证「不白屏」 |

> 这三条通道在 `app/main.py` 装配：REST 由 `ROUTERS` 注册表挂载（`create_app`，:621-623），`signaling.router` 单独挂 `/ws/signal`（:623，契约裁决 A-03 不加 `/api` 前缀），`install_signaling_handler()` 注册 A3 的 `offer/ice/asr_start/asr_end` 处理器（:624）。

## 3.2 端到端 11 步（主链路，逐帧可对照代码）

1. **换证**：`POST /api/device-sessions`（`device_id + device_secret`），token 存 `sessionStorage`，`elder_id` 不由客户端传（`frontend/src/pad/api.ts:28-38`）。设备会话**无 refresh 通道**，过期回登录页重走 E4。
2. **唤醒 / 建 WS**：KWS「小伴」命中或点「呼叫小伴」→ `App.tsx` 由 `standby` 切 `inSession`；`SignalSocket.connect()` 在 `onopen` 同步首发 `auth{token, wake_ts?}`，服务端要求 **accept 后 300ms 内**收到（`WS_AUTH_TIMEOUT_MS=300`）。`wake_ts` 是端侧 KWS 命中时刻，用于 `wakeup` 段耗时打点。
3. **鉴权与身份**：服务端 `resolve_principal` 校验 → `auth_ok{elder_id, room_id}`；`identity_service` 每会话首轮强制校验（闸门①），低置信走 `identity_ask`（`timeout_s` 恒 30）→ `identity_confirm` / 30s 超时落 `generic`。今天已移除会话内静默复检（TD v1.9），身份以首轮为准。
4. **建媒体**：`RTCPeerConnection` + `MicCapture`（48k → AudioWorklet → 16k/16bit/mono 20ms 规范帧 + 600ms preRoll 环形缓冲）→ `offer` 上行 → `webrtc._handle_offer()` **先 `close_session(device_id, reason="replaced")` 再 `create_session`** → `answer` 下行 → `ice` 双向（offer 前到达的 candidate 会缓存，answer 后 flush）。本机 loopback 的 STUN/TURN 被 `_is_loopback_ice_url()` 排除，靠 host candidate 建连，不依赖未启动的 coturn。
5. **VAD 切段与打断**：端侧 TEN-VAD（Worker 隔离，threshold 0.5 / 静音 0.8s / minSpeech 0.25s / maxSpeech 30s）产 `speech_start / speech_end`。`speech_start` 若正在播 → 本地立即 `muted=true` + `pause()` + 清字幕 + 发 `asr_start` + `flushPreRollToTrack()` 回放 600ms 句首；`speech_end` → `asr_end`。服务端 `barge_in()` 增 `_playback_generation`、清播放队列，旧 turn 的 PCM 入队直接被拒（`stale turn for TTS playback`）。播放后 `ECHO_COOLDOWN_S=1.5` 抑制 VAD，防 TTS 声音被自己收回。
6. **ASR**：媒体层 `DualFrameSplitter` 把 20ms 帧分叉为 **60ms ASR 帧 / 30ms VAD 帧** → `ASRProvider.stream()` → `ASREvent(PARTIAL/FINAL/ERROR)`。**只有 FINAL 是业务触发点**，`signaling.dispatch_asr_event()` 先投字幕，再 `await` 对话编排（今天加 `asyncio.shield`）。FunASR 本地 `2pass`，`online→PARTIAL`、`offline→FINAL`；`confidence` 拿不到写 `null`，**禁止伪造 0.0**。
7. **对话编排与统一出口**：`_on_dialogue_final`（`app/main.py:300`）→ `DialogueOrchestrator.handle_asr_final` → **L0 安全一票否决**（命中急症直接三表同事务落库，不进 Agent 不调 LLM）→ `TurnService.handle_user_turn` 七步（见 §4.1）。整段还有 LLM 慢响应 watchdog（`degrade_llm_slow_ms=3000`），`try/finally` 强制取消。
8. **工具执行 / MCP**：`CompanionAgent`（ReAct）→ `ToolExecutor.execute()`（白名单、身份只从 `runtime.elder_id`、四类失败、`UNIQUE(idempotency_key)` 幂等、审计）→ 天气走 `MCPClient.call_tool()`（MCP `streamable-http`，默认 `http://localhost:7101/mcp`；容器环境按部署配置服务地址，总超时 5s）。预算复用 `Guardrails(max_tool_calls=8, repeat_window_s=60, repeat_threshold=3)`，不自建第二套。
9. **分段下发**：`ResponseService.start()` 把回复按 `。！？；` 切、单段 ≤40 字、长句按逗号补切；`epoch += 1`；逐段发 `tts_text` v2（带 `response_id/epoch/segment_id/index/total`）。同一轮的完整文本由 `app/main.py::_on_dialogue_final()` 另行交给 `tts_provider.synthesize()`，成功后通过 `enqueue_tts_for_device()` 写入 WebRTC 音轨；所以**文字按段确认、音频整段合成/入轨是两条并行路径**，不是每个 ack 对应一个 PCM 分片。N14 分支不再额外整段重发文本，防止双发。
10. **送达回执闭环**（今天的核心新增）：pad `PlaybackQueue` 收到段 N 的 `tts_text` → 段 N 报 `playing`、段 N-1 结算 `played`；末段由轨 `ended` 触发；打断报 `canceled`（`segment_id` 填最后已播完段、无则 `-1`）。ack「尽力而为 + 500ms 后 1 次重发」，失败只打 `client_diag`，不阻塞播放。服务端 `signaling._handle_segment_ack()` 做形状校验 → `ResponseService.ack()` 做**四步契约校验** → 只有 `played` 推进水位并拼 `delivered_text`，末段 `played` 触发 `response_completed`。
11. **打断收口**：`ResponseService.interrupt()` 五步固定顺序（收到 → 停 TTS 队列 → `epoch += 1` → 逐.cancel() 在途任务 → 追加 `response_interrupted` + `delivery_status=interrupted`）。**已发生的 played 事实只追加不回滚**，这就是 `assistant_delivered_text` 与 `generated_text` 会不一样的原因。

## 3.3 两条支线（讲完主链路再补，显得完整）

- **主动触达支线（E2）**：APScheduler 到点 → `medication_confirmation_graph`（`remind → wait_confirm[interrupt] → record_taken/refused/retry(≤3) → finalize`）→ `_deliver_reminder()`。先由 `app.audio.signaling.deliver()` 下发 `{"type":"tts_text","text":...,"degraded":false}`，**不带** `response_id/epoch/segment_id`（不属于回复流，pad 整段一播、无需分段 ack）；当前工作区补充再调用 `speak_for_device()`，通过 TTS 合成并写到**已经建立的** WebRTC 下行轨。语音失败或设备无媒体会话时保留字幕，不回滚提醒事实、不改变触达回执。`voice_dispatched` 只表示字幕消息入发送队列，**不代表已出声或已听到**。回执三态 `voice_dispatched / device_offline / no_bound_device` 落 `medication_attempts.receipt`（0012 白名单）；之后 `wait_confirm` 用 LangGraph `interrupt()` 挂起，checkpointer 存独立 `langgraph` schema，重启后可恢复，且**先查后发**防图重放重复触达。
- **降级支线**：TEN-VAD 加载失败 → 前端发 `client_diag{stage:"vad", ok:false}` → `webrtc.set_vad_fallback_for_device()` 激活服务端 `VadFallbackController`（webrtcvad，aggressiveness=3，连续 3 帧起说 / 27 帧静音收尾），**不与端侧 VAD 双跑**；WebRTC 整条主链不可用 → `standby` 相渲染 `HoldToTalk`，松开后 `assembleWav()` 组 44 字节头 → `POST /api/audio/utterance`（16k/16bit/mono WAV、≤30s，非法头 422、超时 413、ASR 不可用 503 带降级话术）→ 模板话术 + `static/mock/tts_*.wav` URL 返回，保证不白屏。

---

# 四、技术方案与各自的适用场景（重点：说清楚「为什么用它」）

| 方案 | 真实实现 / 默认值 | 适用场景 | 不适用 / 边界 |
| --- | --- | --- | --- |
| **WS `/ws/signal`** | `app/audio/signaling.py`；9 类上行 / 11 类下行；300ms 首帧鉴权、20s ping、45s 无帧判死；`4401` 鉴权失败、`4002` 同设备踢旧留新 | 控制面：鉴权、SDP/ICE、字幕、分段文字、ack、身份确认、诊断 | 不传音频；不做服务端主动探测；未知消息回 `error` 但**不关连接** |
| **WebRTC / aiortc** | `app/audio/webrtc.py`；16k/16bit/mono 20ms 规范帧、60ms ASR 帧、30ms VAD 帧、600ms preRoll、下行 48k；`webrtc_ice_timeout_seconds=5`、`webrtc_audio_bitrate_kbps=32` | 全双工语音、低延迟打断 | 一个连接只允许一个 PC；loopback 排除 STUN/TURN；每事件循环独立 registry（`MediaLoopMismatch` 防护） |
| **端侧 TEN-VAD → 服务端 webrtcvad → 按住说话** | `audio/vad.ts` / `app/audio/vad.py` / `HoldToTalk.tsx`；三级自动接管 | 主：有 WASM 的浏览器；兜：WASM 加载失败/30s 超时；降级二：WebRTC 整条不可用 | 兜底只在 `client_diag(vad,false)` 显式激活，避免双 VAD 双切段 |
| **Mock / FunASR / 云 ASR** | `ASR_PROVIDER`（空或未知归一 `mock`）；FunASR `wss://localhost:7002`，`2pass` `chunk_size=[5,10,5]`，握手 `itn=true`，连接 5s / 接收 45s，共享服务 `wss://<IP>:10096` + `FUNASR_INSECURE=1` | Mock：断网可跑 + pytest 全链路；FunASR：本地基线；云：等教师统一 key | FunASR 的 `ping_interval=None`（对 ping 判超时踢连接）、`proxy=None`（websockets ≥17 读 ALL_PROXY）；`confidence` 恒 `None` |
| **Mock / edge-tts / 云 TTS** | `TTS_PROVIDER`；`.env.example` 留空时工厂默认 `mock`；显式设 `edge` 用 `zh-CN-XiaoxiaoNeural` + `TTS_FFMPEG_BIN`（默认 `ffmpeg`，输出 16k/16bit/mono PCM） | Mock 用于无外部依赖的本地/测试；edge 用于未接入教师云 TTS 时验证真实语音，但依赖网络且整段非流式，不能满足 300ms 流式首包目标；云适配器仍待统一 key | 基线测量使用 edge；它的整段合成是 AC-42 延迟归因之一。配置和声道可用不等于流式能力已落地 |
| **REST C3 / E20 / E4** | `POST /api/audio/utterance`、`PUT /api/staff/{staff_id}/elders`（admin only，403 / 409，全量覆盖式改绑）、`POST /api/device-sessions` | 无状态、幂等、管理端；`scope_elder_ids` **逐请求查表**，改绑后旧 token 范围随之变化 | 设备 token 无 refresh，过期重走 E4 |
| **MCP streamable-http** | `app/mcp_client/client.py` + `mcp-servers/weather/`；`endpoint = base + "/mcp"`，每次调用独立建连 → initialize → call → close；`mcp_call_timeout_seconds=5`、`weather_cache_ttl_seconds=600` | 唯一真实外部接口 `get_weather(city)`；Day 8 `mock-operations` 可复用 `MCPClient` 换 `server_url` | 上游失败或无 `weather_api_key` → `ok=False` + 话术「这个我暂时查不到」，**禁止用模型常识生成天气** |
| **事件账本（PG，只追加）** | `history_service.py`；`SELECT last_event_seq … FOR UPDATE` 行锁分配 `seq`（禁止「先查 max 再 +1」）；12 类事件；更正写新事件带 `ref_event_id` | 复盘、幂等、上下文正序；`read_turns` 按 `turn_started` 的 `seq` 排序而非时间戳 | 高频 `response_segment_delivered` 不写历史流水，只改 StateCenter |
| **状态中心（Redis + 版本 CAS）** | `state_service.py`；`session:{thread_id}:state`，TTL 2h；`apply(expected_version, events)` Lua CAS，冲突抛 `StateVersionConflict` **不覆盖**；`SessionCoordinator` 每 thread 一把 `asyncio.Lock`，冲突自动重试 ≤3 次 | 「现在做到哪」的实时一致视图、打断/待答状态 | 关键任务以 PG checkpointer + 业务表为准，**不依赖 TTL**；Redis 挂了降级进程内存（闲聊可用性优先） |
| **长期记忆（pgvector）** | `memory_service.recall_active_chunks`；闸门②强制 `elder_id` + `status=active` + 有效期；`memory_chunks` 有 `vector(1536)` | 上下文九层装配的 L5 | 冲突五关系 / 90 天衰减 / `superseded,disputed` 迁移顺延 Day 7（M-1~M-4）；召回失败本轮不加载记忆，**绝不臆造** |
| **LangGraph + PG Checkpointer** | `app/graphs/checkpointer.py`；独立 schema `langgraph` + 独立连接池 | 用药确认这类跨轮 HITL 任务，`interrupt()` 挂起后重启可恢复 | 业务表与 checkpoint 物理隔离，避免迁移互相污染 |
| **三档慢响应 watchdog** | `degrade_asr_slow_ms=2000` / `llm_slow_ms=3000` / `tts_slow_ms=1000`；先发 `error{code:asr_slow|llm_slow|tts_slow}` + 话术，首包到达取消 | provider 还在跑但超预算时先给长者反馈，不等它返回 | 失败终态已显时迟到的 slow 由前端 `nextDegradeLevel()` 判 `suppressed` |
| **主动触达：WS 字幕 + 现有 WebRTC 下行音轨** | 当前工作区用药 E2：先 `signaling.deliver(tts_text)`，再尽力 `speak_for_device()` 合成并写入已有媒体会话；字幕和语音分别处理 | 长者 pad 已有在线 WS 与 WebRTC 会话时的定时提醒；无媒体会话/合成失败时仍有字幕 | **反向 WebRTC 外呼仍未落地**：服务端不会主动创建 PeerConnection，`task-channel.ts` 也未绑定传输、pad 无自动接听；`voice_dispatched` 仅表示文本入队 |

**端口速查**（AGENTS.md 自定义并已回填 README）：postgres 5432 / redis 6379 / backend 8000 / nginx 80·443 / laya 7001 / FunASR 7002 / 声纹 7003 / weather-mcp 7101 / mock-ops 7102 / coturn 3478·5349 / vite 5173；FunASR 共享容器 10096。

---

# 五、今天的核心逻辑：四个「为什么」（有深度的部分）

## 5.1 为什么七步顺序不能换（`turn_service.handle_user_turn`）

`begin_turn → snapshot/apply → build → agent.run → commit_response → apply → response_stream.start`：

- **事实先落**：先把 `turn_started` / `asr_final_committed` 写进事件账本，模型看不到「还没发生过的事」。
- **第 5 步 `delivered_text=None`**：此时只是「生成了」，不是「长者听到了」；投影只存已送达文本，打断后上下文里才不会出现老人根本没听过的句子。
- **第 5 步与第 7 步共用同一枚 `response_id`**：事实与下发可对账（`app/main.py:279-291` 让 `TurnService` 与 ack 回调持有**同一个** `ResponseService` 实例，否则 ack 找不到会话 epoch）。
- **状态写在下发前、事务在下发后 commit**：失败时 `except BaseException → rollback`，不会留下「已下发但没事实」的孤儿回复。
- generic（无 `elder_id`）不建 `ConversationTurn` 投影，改直写 `response_completed` 事件，避免 `HistoryError`。

## 5.2 为什么迟到回执要「四步校验」（`ResponseService.ack`）

| 步 | 判什么 | 防住什么 |
| --- | --- | --- |
| ① `response_id` 存在且 `device_id` 相同 | 未知/串设备的回执 | `unknown_response` |
| ② `epoch == 当前会话 epoch` | **跨代淘汰**：上一代回复的迟到 ack | `stale_epoch` |
| ③ `response.status == playing` | **同代内打断后的追认拦截**：只校验 epoch 时，被打断的回复仍可能被后续 ack 追认 | `wrong_status` |
| ④ `0 ≤ segment_id < total` 且 `> last_acked_segment`（严格单调） | 保证 `[0..水位]` 每段都已 played，截断公式才成立 | `not_monotonic` |

- `epoch` 管跨代、`status` 管同代，两条都要，缺一不可。
- **只有 `played` 推进水位**；`playing` 只走 ①②③ 打 `response_segment` 指标。若让 `playing` 推进，同段随后的 `played` 会被 ④ 判迟到丢弃，`delivered_text` 恒为 `None`；改成 `>=` 又会把「播到一半的段」算成听过。
- `canceled` 是陈述性上报，**跳过 ④**、不推进水位、不改状态；`segment_id` 回显水位，一段没播完填 `-1` 哨兵，与服务端 `last_acked_segment` 初值同构。
- `delivered_text = "".join(segments[:last_acked_segment+1])`，按 `index` 升序而非到达顺序；`delivery_status` 只取 `delivered/interrupted/unknown`，**永不写 `understood`**（系统不知道长者是否听懂）。

## 5.3 为什么事件账本要「行锁 + 只追加」

`conversation_threads.last_event_seq` 用 `SELECT … FOR UPDATE` 在同一事务里 +1 写回再插事件，避免并发两轮拿到同一个 `seq`（今天 A 线 G-A2 并发 20 写，seq 恰为 1..20）。更正/替代一律写新事件带 `ref_event_id` 指向旧行，**禁止 UPDATE 旧行**——这是「事实不可回滚」在存储层的落点，与打断不回滚已 played 段是同一条原则。

## 5.4 为什么状态中心适配器不走 `SessionCoordinator.submit`

`CoordinatorStateCenterAdapter.apply()` 显式调 `StateCenter.apply(expected_version, events)` 做 CAS，而不是 `submit`：`submit` 会重取最新版本并在冲突时自动重试，那会**把第 2 步与第 6 步之间的并发写入静默吞掉**，七步契约要求的显式版本语义就失效了。版本冲突时 `StateVersionConflict` 冒泡不翻译，由 `handle_user_turn` 的 `except BaseException` 回滚事务（`turn_service.py:265-326`）。

---

# 六、关键代码逐行注释解读（汇报时投屏 / 讲代码用）

> 说明：下面既有源码节选，也说明了直接写在关键逻辑旁的注释。`response_service.py` 标出为何只有 `played` 才进入 `delivered_text`；用药子图标出字幕先投递、语音尽力补发及回执边界。工作区有未提交修改，汇报时请把「今天已提交实现」与「当前分支本地补充」分开说明。

### A. 统一出口七步 —— `app/services/turn_service.py:653-759`（节选）

```python
async with self._session_factory() as session:          # ① 一个事务罩住整个回合；失败整体 rollback
    try:
        turn_ref = await self._history.begin_turn(...)   # 第 1 步：事实先落（含 turn_started 事件）
        if turn_ref.duplicate:                          # 进程重启后 ASR 重发：不重跑 LLM，显性失败
            raise ValueError(...)
        await self._history.append_event(
            ..., event_type="asr_final_committed", ...)  # 补 ASR 定稿事实（12 值枚举内）

        snapshot = await self._state.snapshot(...)      # 第 2 步：读一致状态（带 version）
        state_version = await self._state.apply(
            runtime.thread_id, snapshot.version,         # 显式 CAS：版本不符直接抛，不静默重试
            ("turn_started", "asr_final_committed"))

        context = await self._builder.build(...)        # 第 3 步：九层上下文 + 记忆召回（闸门②）
        token = _pending_agent_events.set(buffer)       # 第 4 步：工具事件先缓冲，轮次结束统一回灌
        try:
            result = await self._agent.run(...)         #       ReAct + Guardrails(8 次 / 60s / 3 次熔断)
        finally:
            _pending_agent_events.reset(token)
        for pending in buffer:
            await self._history.append_event(...)       #       tool_started/succeeded/failed 落账本

        response_id = uuid.uuid4().hex                  # 第 5 步：铸造一枚 response_id，事实与下发共用
        await self._history.commit_response(
            ..., generated_text=result.text,
            delivered_text=None,                        # ★ 此时无送达事实；投影只存「长者真正听到的」
            status="playing")
        await self._state.apply(..., state_version, ("response_completed",))   # 第 6 步：再改状态
        handle = await self._response_stream.start(
            context, result, response_id=response_id)   # 第 7 步：最后下发（同一枚 response_id）
        await session.commit()                          # 全链成功才提交
    except BaseException:
        await session.rollback()                        # 含 CancelledError 也回滚（shield 之后尤其重要）
        raise
```

**汇报话术**：「我们把'生成'和'送达'拆成两个事实，中间的差值就是打断造成的截断。这样下一轮上下文里不会出现老人没听到的话。」

### B. 分段下发 + 四步校验 —— `app/services/response_service.py:295-340, 416-458`

```python
self._epochs[turn.thread_id] = self.session_epoch(turn.thread_id) + 1   # ① 开新代：旧回复 ack 全部 stale
segments = split_segments(text)        # 。！？；优先，单段 ≤40 字，长句按逗号补切；拼接恒等于原文
for index, segment in enumerate(segments):
    message = {"type": "tts_text", "text": segment, "degraded": state.degraded,
               "response_id": response_id, "epoch": epoch,
               "segment_id": index, "index": index, "total": total}       # 新增 5 字段全部可选
    delivered = self._deliver(turn.device_id, message)                     # 经 signaling.deliver 下行
    record_stage("response_segment", ...)                                  # 每段一条分段耗时指标

# ---- ack 四步 ----
if state is None or state.device_id != device_id:  return reject("unknown_response")   # ①
if epoch != self.session_epoch(state.thread_id):  return reject("stale_epoch")        # ② 跨代淘汰
if state.status != PHASE_PLAYING:                 return reject("wrong_status")       # ③ 同代拦截
if status == ACK_CANCELED: return True          # ★ 跳过 ④，陈述性上报，不推进水位
if not 0 <= segment_id < state.total or segment_id <= state.last_acked_segment:
    return reject("not_monotonic")              # ④ 严格单调：截断公式的前提
if status == ACK_PLAYED:
    state.last_acked_segment = segment_id       # ★ 只有 played 推进水位
    # 只把完整播放的连续前缀写进上下文；playing 只是进度信号，不能算作听到
    state.delivered_text = "".join(state.segments[: segment_id + 1])
    if segment_id == state.total - 1:           # 末段 played 才触发 response_completed
        ...
```

**汇报话术**：「epoch 与 status 是两道不同的闸：一个防跨代，一个防同代内打断后被追认。`playing` 绝不推进水位，否则会把播了一半的段算成'听过'。」

### C. 前端送达确认 —— `frontend/src/pad/audio/playback-queue.ts:71-104, 140-153`

```ts
handleSegment(msg: TtsTextMsg): boolean {
  if (!isSegmentedTts(msg)) return false;             // 缺 response_id/segment_id → 整段一播（向后兼容）
  if (this.maxEpoch !== null && epoch < this.maxEpoch) {
    this.deps.sendDiag(false, `stale_segment_dropped ...`); return false;  // 前端最后一道「不串音」闸
  }
  this.maxEpoch = epoch;                              // epoch 原样回传，客户端不得自增
  if (this.active && this.active.responseId !== msg.response_id) this.flushCanceled(); // 新回复替代旧回复
  if (!this.active) { this.active = {...}; }
  else {
    this.settlePlayed(this.active.playingSegment);    // ★ 收到段 N ⇒ 段 N-1 必已播完 ⇒ 报 played
    this.active.playingSegment = segmentId;
  }
  this.sendAckFor(segmentId, "playing");              // 段 N 报 playing（只用于首段开播延迟指标）
}

private flushCanceled(): void {                      // 打断收口
  this.deliverAck({ ..., status: "canceled",
    segment_id: lastConfirmedPlayed ?? -1,           // ★ 一段都没播完 → -1 哨兵
    at_ms: Math.round(performance.now()) });
}
```

**汇报话术**：「WebRTC 是单轨连续流，前端拿不到单段 `onended`，所以我们用『下一段的 tts_text 到达』作为上一段播完的信号，末段交给轨结束事件。」

### D. 旧连接取消不炸对话回合 —— `app/audio/signaling.py:462-486`

```python
task = asyncio.create_task(handler(device_id, turn_id, trace_id, text, audio_bytes))
try:
    await asyncio.shield(task)          # ★ 脱离旧连接任务链的取消传播
except asyncio.CancelledError:
    # 旧连接死了，但对话任务继续跑完七步；新连接由 deliver 按 device_id 自动接管文字/音频
    def _reap(t: asyncio.Task[None]) -> None:
        exc = t.exception() if not t.cancelled() else None
        ...                             # 后台任务异常必须被消费，否则 "Task exception was never retrieved"
    task.add_done_callback(_reap)
    raise                              # 取消语义照常向上传播，不吞
```

**汇报话术**：「这是今天 S4 试跑抓到的真实缺陷：同设备踢旧留新时，旧连接的取消会把正在跑的一轮对话一起取消，造成事件写一半、音频喂一半。用 shield 把'连接生命周期'和'对话回合生命周期'解耦，再加 done_callback 兜住后台异常。」

### E. 装配层：为什么必须是同一个 `ResponseService` —— `app/main.py:277-291, 494-513`

```python
response_service = ResponseService(deliver=signaling.deliver, events=HistoryEventSink(session_factory))
turn_service = TurnService(..., response_stream=response_service,      # 第 7 步分段下发用这个实例
    state_center=CoordinatorStateCenterAdapter(coordinator, state_center),
    context_builder=ContextBuilder())                                  # N05/N06 真身注入
...
async def _on_segment_ack(conn, message):
    await response_service.handle_segment_ack(device_id=conn.device_id, message=message)  # 同一实例
signaling.set_segment_ack_handler(_on_segment_ack)   # 形状校验在 signaling，四步契约校验在 service
```

**汇报话术**：「如果 ack 落到另一个实例，`session_epoch` 对不上，所有回执都会被判 `stale_epoch`。装配层把七步、分段下发、ack 校验绑到同一实例，是今天 S4 接线修复的关键。」

### F. 主动触达与防重放 —— `app/graphs/medication_confirmation_graph.py:146-246`

```python
message = {"type": "tts_text", "text": text, "degraded": False}   # ★ 不带 response_id/epoch/segment
device_ids = await medication_service.list_bound_device_ids(session, elder_id=elder_id)
if not device_ids: return "no_bound_device"                       # 三态回执，如实记录
for device_id in device_ids:
    if deliver_fn(str(device_id), message):
        dispatched = True
        try:
            spoken = await speak_fn(str(device_id), text)          # 文本先入队，语音尽力写 WebRTC
        except Exception:
            spoken = False                                         # 语音失败保留字幕，不改回执
return "voice_dispatched" if dispatched else "device_offline"

async def remind(state):
    already = await medication_service.get_attempt(session, event_id=..., attempt_no=...)
    if already is not None: ...                                   # ★ 先查后发：图重放不再重复播报
    else:
        receipt = await _deliver_reminder(...)
        await medication_service.record_attempt(..., receipt=receipt)   # 下发失败也记，是事实
    await medication_service.set_waiting_confirm(...)
```

实际实现中，文本入队成功后才调用 `speak_fn(device_id, text)`；`speak_for_device()` 再完成 TTS 合成和 WebRTC 写轨。语音支路失败只记日志并保留字幕，回执按文本投递结果记录。图恢复会重放节点，幂等记录本身挡不住外部投递，所以 `remind` 先查 attempt 再决定是否触达。

**汇报话术**：「主动触达不属于任何回复流，所以复用 `tts_text` 但不带分段字段；当前工作区在字幕先到后补一条尽力语音，写入既有 WebRTC 下行轨。回执只代表文本已入队。图恢复会重放节点，因此触达前先查 attempt，避免同一次提醒重复投递。」

### G. MCP 身份闸门③ —— `app/mcp_client/client.py:361-409`

```python
cleaned_args, claimed_elder_id = _resolve_args_identity(args)   # args 里带 elder_id → 丢弃并审计
if claimed_elder_id is not None:
    result = MCPResult(ok=False, data=None, error="unauthorized")   # 不建连，越权零成本拒绝
else:
    result = await self._invoke(tool_name, cleaned_args)           # asyncio.timeout(5s) 罩住全程
await self._audit_sink.write(AuditEntry(action=..., latency_ms=..., outcome=...))  # 每次调用必写审计
```

**汇报话术**：「身份只从服务端 `runtime_context` 注入，模型或客户端传的 `elder_id` 一律丢弃并留 `tool_identity_override_attempt` 审计；天气查不到就明说查不到，不让模型编。」

---

# 七、必要配置说明（老师问「怎么跑起来」时照着念）

| 类别 | 配置项（= 环境变量全大写） | 默认 | 落点 |
| --- | --- | --- | --- |
| WS 硬常量 | `WS_AUTH_TIMEOUT_MS` / `WS_PING_INTERVAL_S` / `WS_DEAD_AFTER_S` / `IDENTITY_ASK_TIMEOUT_S` | 300ms / 20s / 45s / 30s | `signaling.py:67-78` |
| WS 关闭码 | `WS_CLOSE_AUTH_FAILED` / `WS_CLOSE_SUPERSEDED` | 4401 / 4002 | `signaling.py:83-85` |
| 音频规范 | 采样率 / 规范帧 / ASR 帧 / VAD 帧 / preRoll / 下行 | 16k·16bit·mono / 20ms / 60ms / 30ms / 600ms / 48k | `webrtc.py:68-97` |
| ICE | `stun_url` / `turn_url` / `turn_username` / `turn_credential_ttl_seconds` / `webrtc_ice_timeout_seconds` / `webrtc_audio_bitrate_kbps` / `webrtc_vad_aggressiveness` | `stun:localhost:3478` / `turn:localhost:3478` / `silvermate` / 3600 / 5s / 32 / 3 | `config.py:122-134` |
| ASR | `asr_provider` / `asr_timeout_ms` / `asr_emit_confidence` / `asr_insecure` | `""`(→mock) / 300 / True / False | `config.py:98-111` |
| TTS | `tts_provider` / `tts_timeout_ms` / `tts_speed` / `tts_ffmpeg_bin` | `""`(→mock) / 300 / 1.0 / `ffmpeg` | `config.py:113-121`；edge 模式才用 ffmpeg |
| LLM | `llm_provider` / `llm_base_url` / `llm_model` / `llm_timeout_seconds` / `llm_max_retries` | `deepseek` / `https://api.deepseek.com/v1` / `deepseek-chat` / 20s / 2 | `config.py:81-88`（注：Day 4 任务卡与 `llm_service.py` 记为 Minimax OpenAI 兼容端点，按教师 key 切换） |
| 慢响应 | `degrade_asr_slow_ms` / `degrade_llm_slow_ms` / `degrade_tts_slow_ms` | 2000 / 3000 / 1000 | `config.py:184-191` |
| 预算 | `max_tool_calls_per_turn` / `tool_repeat_window_seconds` / `tool_repeat_break_threshold` | 8 / 60 / 3 | `config.py:174-182` |
| MCP | `mcp_call_timeout_seconds` / `weather_cache_ttl_seconds` / `mcp_weather_server_url` | 5s / 600s / `http://localhost:7101` | `mcp_client/client.py:317-320` |
| 状态中心 | Redis `session:{thread_id}:state` TTL | 7200s（2h） | `state_service.py:213` |
| 其他 | `voiceprint_similarity_threshold` / `audio_retention_hours` / `elder_session_expire_hours` / `rate_limit_per_minute` | 0.55 / 72 / 24 / 60 | `config.py` |

**消息契约速记**（`day06/contracts.md` v1.4）：
- 五个标识全部服务端生成、客户端不可指定：`thread_id`（会话）/ `call_id`（建连）/ `turn_id`（一轮，幂等键）/ `response_id`（一次回复）/ `task_id`（跨轮任务）。
- `tts_text` v2 新增 5 字段**全部可选**，`epoch` 客户端原样回传不得自增；`segment_ack` 的 `status ∈ playing|played|canceled`，`at_ms` 必填。
- 12 类事件名与 `rejected_reason` 四枚举（`unknown_response / stale_epoch / wrong_status / not_monotonic`）都是冻结值，改契约要先升版本号再改代码。

---

# 八、怎么讲得更专业、更有条理（表达层）

## 8.1 推荐结构（3 分钟版）

1. **0–20s 定位**：一句话总纲（§一），先说「今天解决的是什么问题」。
2. **20–50s 分工**：四条线矩阵（§2.1），每人一句「做了什么 → 落到哪个文件」，不展开。
3. **50s–2min 主链路**：按「三条通道 → 11 步」讲（§3），**只画一条线**，把 ack 闭环讲透。
4. **2–2:40 深度**：挑两个「为什么」（推荐 §5.2 四步校验 + §5.1 生成/送达分离），这是区分「做过」和「想明白」的地方。
5. **2:40–3min 验收与留白**：975 passed / 81.51%、AC-42 0/16 及归因、看板滞后、遗留项——**主动说不达标比被问出来更专业**。

## 8.2 措辞替换表

| 别这么说 | 这么说 |
| --- | --- |
| 「我们做了个 WebSocket 传语音」 | 「控制面走 WS 文本帧，媒体面走 WebRTC 音频轨，SDP/ICE 借信令面交换，两者不互相阻塞」 |
| 「加了个回执」 | 「在回复流上引入 `response_id + epoch` 两代标识，服务端四步校验淘汰迟到回执，用 `played` 水位计算 `delivered_text`」 |
| 「打断就是把播放停掉」 | 「打断是五步收口：停队列 → epoch+1 → 取消在途任务 → 记事件，且**已发生的 played 事实只追加不回滚**」 |
| 「应该没问题」 | 「门禁 975 passed / 覆盖率 81.51%；AC-42 实测 0/16 不达标，原因已定位到三段非流式实现」 |
| 「AI 会记住老人说的话」 | 「事件账本只追加 + 记忆召回受闸门②（elder_id/status/有效期）过滤，召回失败本轮不加载，不臆造」 |
| 「都做完了」 | 「A 线 21.0/21.0 收官，B/C/D 代码与测试已合入；看板状态行未回填所以显示 2/7」 |

## 8.3 三条叙事纪律

- **契约 → 实现 → 验收**三段式：先说字段/口径怎么定的（`contracts.md` 冻结），再说代码落在哪，最后给测试结果。老师问「怎么保证前后端一致」时，答案就是「先改契约再改代码 + 版本号 + 变更记录」。
- **每个技术选型都带「适用 / 不适用」**：例如 edge-tts 只说「我们用了 edge-tts」是方向性概述；说「当前用 edge-tts 在缺少教师云 TTS key 时验证语音链路，但它依赖网络且整段非流式、首包 >1s，不能满足 AC-42；接上流式云 provider 后仍需重测」才是实现方案。
- **数字 + 文件 + 提交号**：「修了 bug」→「`73724d4` 用 `asyncio.shield` 把对话回合与旧连接取消解耦」。可核查是专业感的最大来源。

## 8.4 预判提问（Q&A 预案）

| 老师可能问 | 回答要点 |
| --- | --- |
| 「为什么不用 HTTP 传语音？」 | 全双工 + 打断要毫秒级停播，HTTP 请求/响应做不到；REST 只作为 WebRTC 不可用时的整段上传降级（C3），且不白屏。 |
| 「怎么保证不重复播 / 不重复扣药？」 | 三层：`turn_id` 幂等（重复 FINAL 不重跑 LLM）、`UNIQUE(idempotency_key)`（工具副作用只一次）、LangGraph checkpointer + `remind` 先查后发（图重放不重复触达）。 |
| 「老人打断后系统记什么？」 | 记 `generated_text` 与 `delivered_text` 两个事实：后者只拼 `played` 段按 index 升序，`response_interrupted` 带 `last_played_segment`；不写 `understood`。 |
| 「身份会不会被模型伪造？」 | 五个标识全服务端生成；`elder_id` 只从 `runtime_context` 注入；工具 args 里的 `elder_id` 丢弃并写 `tool_identity_override_attempt` 审计；MCP 层越权零成本拒绝。 |
| 「性能达标了吗？」 | 不达标，0/16，P50 7872ms / P95 13537ms；已定位为 FunASR 4.9s（含说话）+ LLM 1.5s + edge-tts 1.5s 三段非流式形态；云 key 到位 / route 接线 / OTel 后复测。 |
| 「看板怎么只完成 2 个？」 | 看板由脚本机械镜像任务卡首部状态行，四条线的状态行还没回填；实际执行状态看 `docs/plans/day06/` 交付存档与当天 44 个非 merge 提交。 |
| 「还有什么没做？」 | 如实列：`ResponseService` 内部 `NullTtsQueue` 仍是 no-op，打断取消不由它管理；回复音频目前由 `main.py` 另行整段合成并写 WebRTC 音轨，当前工作区的用药 E2 语音也只是尽力写入已有会话；PRD 目标里的反向 WebRTC 外呼 / 自动接听仍未落地；E2 尚无 played 回执闭环；WD-1/WD-2 建单口与幂等列、M-1~M-4 记忆冲突治理顺延 Day 7。 |

---

# 九、文件索引（现场可点开佐证）

- 契约：`docs/team_tasks/day06/contracts.md`（v1.4）、`docs/team_tasks/day03/contracts.md`
- 执行与验收：`docs/plans/day06/dev-a-历史与工具执行计划.md`、`dev-a-DAG.md`、`handoff-A-to-C-segment-ack.md`、`docs/team_tasks/day06/a1-延迟基线表.md`
- 通信：`app/audio/signaling.py`、`app/audio/webrtc.py`、`app/audio/vad.py`、`app/audio/asr.py`、`app/audio/tts.py`、`app/main.py`
- 核心逻辑：`app/services/turn_service.py`、`response_service.py`、`history_service.py`、`state_service.py`、`tool_executor.py`、`app/mcp_client/client.py`、`app/graphs/medication_confirmation_graph.py`
- 前端：`frontend/src/pad/protocol.ts`、`audio/playback-queue.ts`、`realtime.ts`、`signal-socket.ts`、`components/HoldToTalk.tsx`
- 流程图：`docs/team_tasks/day06/day06-通信全链路.drawio`
