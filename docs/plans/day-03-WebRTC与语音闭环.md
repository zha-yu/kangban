> 状态：草稿　｜　版本：v1.3　｜　日期：2026-11-05
> 关联：docs/team_tasks/README.md（Day 3 任务卡待开）｜ docs/plans/task-06-长者CRUD与设备绑定.md（上游已完成）
> 上游依据：PRD §2.2/§2.3、§4.11、FR-UI-01/09/10/11、FR-TC-10、AC-40/41、§5.3（验收）｜技术设计文档 line 1016–1023「Day 3 · 语音通道」落地清单（实现）｜大纲 line 440–477 Day 3 课时（仅取流程与产出物顺序）
> 变更记录见文末

---

# Day 3 集成推进计划：WebRTC 与语音闭环（含教师 ASR 参考模块接入）

## 0. 一句话目标

按「降级先行、主模式后接」顺序，把教师提供的 `asr` 参考模块中**可复用的抽象与代码模式**抽取进 `app/audio/`，跑通两条链路：①按住说话整段上传 → ASR → 写死回复 → TTS → 大字幕；②WebRTC 全双工回声 + 实时双向可打断。ASR 置信度全程进 trace。

**本日不碰 LLM 路由、不建声纹表、不接 Agent**——Day 3 闭环的"回复"是写死话术（大纲课时 3.4 明确）。

---

## 1. ASR 参考模块（`/Users/min/Downloads/asr`）核心能力梳理结论

### 1.1 架构与入口

三个互不依赖的 FastAPI 入口：浏览器网关 `webapp.py`（`WS /ws/audio` 承载裸 PCM 音频）、信令 `signaling.py`（`WS /ws` 纯 JSON 转发 offer/answer/candidate，内存字典管在线/忙）、aiortc Agent 侧 `agent.py`（websockets 客户端 + RTCPeerConnection）。**信令与音频是两条独立 WS**，且网关只放行 `127.0.0.1:8765` 同源。

### 1.2 Provider 抽象（最有价值的复用件）

`src/asr/providers/__init__.py` 定义四个 ABC + 两个 dataclass，接口形状成熟：

| 抽象 | 关键签名 | 现有实现 |
| --- | --- | --- |
| `ASRProvider` | `start_session() / feed_audio(bytes) / finish() / results()->AsyncIterator[ASRResult] / close()` | `FunASRWSProvider`（FunASR Docker wss，2pass 流式）、`MockASRProvider` |
| `TTSProvider` | `synthesize(text, emotion)->bytes / sample_rate()` | `EdgeTTSProvider`（edge-tts，**非流式**，整段收完再 pydub 转 16k PCM） |
| `VoiceprintProvider` | `verify(audio)->(bool, float, str) / enroll(name, samples) / profile_revision()` | `SherpaVoiceprintProvider`（sherpa-onnx **CAM++** + TEN-VAD 预切段，点积=cosine） |
| `LLMProvider` | `chat(text, context) / reset_context()` | Mock / Echo |

`ASRResult{text, is_final, mode, confidence}` 契约可直接对齐项目需求。

### 1.3 关键实现事实（含缺陷）

1. **ASR 引擎**：FunASR Docker 的 wss server（2pass：online 增量 + offline 句末修正，16k PCM，FSMN-VAD 服务端分句）。**不是第三方流式云 API**；协议层拿不到置信度（`confidence` 恒 0.0 且 orchestrator 转发时不带），置信度埋点需另建。
2. **声纹**：CAM++ embedding（L2 归一化）+ cosine 比对，**默认阈值 0.55**（README 自注同人 0.517–0.568 会误拒），与本项目硬性数值 **0.85** 是两套口径；模型文件 sha256 指纹做 profile revision，SessionTokens 300s 绑定签发/吊销可参考。
3. **前端采集链路**：`audio-worklet.js` 线性插值连续重采样到 16k（60ms/帧 PCM16）；`app.js` 600ms preRoll 句首保护、TEN-VAD WASM 端侧切句、每句独立 WS、最多排队 4 句——这是高质量的复用素材。
4. **WebRTC 侧短板**：音频重采样用 `[::3]` 粗暴抽取、TTS 回放用 `np.repeat` 插值，音质与时基不合格，不可照搬；信令与音频未复用一条连接。
5. **工程健康度**：Python 3.13（项目 3.12）；TTS 依赖 `pydub` 未声明；`test_voice.py` 与实现已脱节；无注册表/配置中心，切换靠改工厂。

### 1.4 可复用能力点清单

| # | 能力点 | 复用方式 |
| --- | --- | --- |
| R1 | 四个 Provider ABC + `ASRResult` 契约 | 抽取为 `app/audio` 协议层的**参考蓝本**（重写，不整目录拷贝） |
| R2 | `FunASRWSProvider` 握手/收流/超时队列结构 | 作为**本地基线 ASR 实现**移植（compose.yaml 的 2pass 参数一并参考） |
| R3 | AudioWorklet 重采样 + preRoll + 句级上传排队 | 移植进 `frontend/pad` 降级采集 |
| R4 | `SherpaVoiceprintProvider` 的 CAM++ 取向量 + cosine 验证 | **Day 3 只做 spike 登记**（模型与 PRD 选型一致：3D-Speaker CAM++），Day 4 正式接 |
| R5 | SessionTokens 签发/吊销模式 | 参考其"会话级授权"思路，但 Day 3 身份统一走 Day 2 设备 JWT |
| R6 | compose.yaml FunASR 容器参数 | 作为本地 ASR 降级基线的部署参考 |

**不可照搬**：webapp.py 整体（同源锁死、8765 独立端口）、edge_tts 作为主 TTS（非流式、pydub 缺依赖）、TEN-VAD 服务端 onnx 版（不搬；端侧 TEN-VAD WASM 经主理人裁决升级为主 VAD，见 §2.3 与变更记录 v1.2）。

---

## 2. 当前项目与待集成能力的匹配点与差异点

### 2.1 已就绪（直接受益）

| 项 | 现状 | 对 Day 3 的意义 |
| --- | --- | --- |
| 语音配置 | `Settings` 已预留 `asr_*`（7 项，含 `asr_emit_confidence`）、`tts_*`（7 项）、`stun/turn/webrtc_*`（8 项）、`voiceprint_similarity_threshold=0.85`、`audio_retention_hours` | Provider 工厂直接读 Settings，无需新增配置面 |
| 设备身份 | Day 2 已交付 `POST /api/device-sessions`（24h 设备会话）、`Identity.elder_id` 仅从库内绑定读取、`current_device` 依赖已测通 | WS 建连鉴权直接复用设备 JWT，**不引入 asr 模块的 SessionTokens** |
| 依赖与 spike | `aiortc>=1.10`、`webrtcvad-wheels>=2.0.14` 已在 pyproject；`spikes/vad/spike_webrtcvad.py` 已验证（注意非法帧长抛 C 扩展 `Error` 非 `ValueError`） | `app/audio/vad.py` 只剩包装层工作 |
| 测试基建 | 真实 DB + 单事务回滚 + 无库 skip + `httpx.ASGITransport` 惯例成熟 | 新测试直接沿用 |
| trace 中间件 | `TraceIdMiddleware` 纯 ASGI、对非 `http` scope 透传 | 中间件本身不用改，但 WS 内 trace 注入需在信令层自行补 |

### 2.2 缺口（Day 3 要补的）

| # | 缺口 | 说明 |
| --- | --- | --- |
| G1 | `app/audio/` 零代码 | `signaling.py / webrtc.py / vad.py / asr.py / tts.py` 五个文件全待建（技术文档 line 1019 清单） |
| G2 | 无 WS 路由与 WS 鉴权 | `get_current_principal` 依赖 `Header()`，WS 场景不可复用，需 query 参数或首帧令牌方案 |
| G3 | `.env` 密钥全空 | `ASR_PROVIDER/ASR_BASE_URL/ASR_APP_ID/ASR_API_KEY`、TTS 五项、`TURN_CREDENTIAL` 均为占位/空，云 API 联调等教师发 key |
| G4 | 无 frontend/ | pad 端 getUserMedia、字幕、状态条全无；MPA Vite 工程未初始化 |
| G5 | 无 coturn | compose 只有 postgres+redis；局域网先走 STUN/host candidate，跨网段等教师 coturn 拓扑 |
| G6 | 无 WS trace 注入 | TraceIdMiddleware 不覆盖 websocket scope |

### 2.3 与 PRD/技术文档口径的差异裁决

| 差异 | asr 模块口径 | 项目口径（必须执行） | 裁决 |
| --- | --- | --- | --- |
| ASR 来源 | FunASR Docker 自建 | 第三方**流式** ASR API（教师统一 key），首包 ≤300ms | 云 API 为主实现；FunASR 移植为**本地基线/降级实现**（同一抽象两个实现，不违反禁区 8）；云 key 未到前用 FunASR/Mock 先行开发 |
| ASR 置信度 | 协议拿不到，恒 0.0 | 置信度埋点进 trace 是硬验收（FR-TC-04/08、大纲课时 3.3） | 适配器层负责：云 API 有则透传；FunASR 无则打 `confidence=null` + `source=provider` 标记，不得伪造数值。**主理人确认（2026-11-05）**：字段现在就加（迟早要加），但"假值"只能是 `null` 不许是 `0.0`；字段不参与业务逻辑，后续不用无影响 |
| TTS | edge-tts 非流式 | 流式 TTS API，首包 ≤300ms | edge-tts 仅作断网/降级兜底候选；主实现等教师 key；`pydub` 等新依赖先过 `spikes/` |
| VAD | TEN-VAD（WASM+onnx） | `webrtcvad`（技术文档 line 1019 原口径、`WEBRTC_VAD_AGGRESSIVENESS` 已配） | **主理人裁决（2026-11-05）**：端侧 TEN-VAD WASM 为主，服务端 webrtcvad 兜底；PRD v2.5 / 技术文档 v1.4 已补修订记录，AGENTS §1 同步 |
| 声纹阈值 | 0.55 | 0.85（PRD §3.2 硬性数值） | 声纹整链路属 Day 4，Day 3 不引入 |
| Python | 3.13 | 3.12（四处对齐，禁改） | 只抽取代码模式，不整仓搬移、不加 `requires-python` 冲突 |
| 身份 | 模块自管 SessionTokens | 设备 JWT + `runtime_context`，客户端传值一律丢弃（禁区 9） | WS 鉴权走设备 JWT |

---

## 3. 插件抽象层的设计边界

### 3.1 边界总原则

1. **协议落 `app/audio/`，实现可插拔，工厂读 Settings**：`asr.py` / `tts.py` 各定义一个协议（Protocol 或 ABC，沿用 asr 模块的签名形状但按需裁剪），配 `create_asr_provider(settings)` / `create_tts_provider(settings)` 工厂，按 `settings.asr_provider` / `settings.tts_provider` 分派；未配置（默认空）时回落 `mock` 实现，保证**未配置时保持原有行为、可跑通测试**（项目内存约定：新配置默认提供回落语义）。
2. **`app/audio` 不写库**：转写文本、会话记录的落库一律不在此层做（`app/services/` 是唯一写库层）；Day 3 产物只进 trace/日志，`conversation_threads`/`voiceprints` 落库等 Day 4 迁移。
3. **不做第二形态**：ASR 的"流式音频输入→转写+置信度输出"只有一个抽象；云 API 与 FunASR 是它的两个实现类。不另造"批量转写""文件转写"第二套入口。
4. **协议最小面**：只保留 Day 3 需要的方法（start_session / feed_audio / finish / results / close；synthesize_stream→首包耗时打点），不预支 Day 4+ 的 emotion、声纹 embedding 等参数——YAGNI，需要时再扩协议。
5. **依赖方向**：`app/audio → app/core`（config/runtime_context/trace/logging）单向；`app/api` 只做参数校验与调 `app/audio` 适配器；禁止 `app/audio` 反向 import `api/graphs/services`。

### 3.2 各文件职责边界（技术文档 line 1019）

| 文件 | 职责 | 明确不做 |
| --- | --- | --- |
| `app/audio/asr.py` | ASRProvider 协议 + 工厂 + 云 API 流式适配器骨架 + FunASR 基线适配器 + Mock；`ASRResult(text, is_final, confidence)` | 不落库、不做 VAD、不碰路由 |
| `app/audio/tts.py` | TTSProvider 协议 + 工厂 + 流式合成适配器（首包耗时埋点）+ Mock | 不做音频播放（播放在前端/aiortc 轨） |
| `app/audio/vad.py` | webrtcvad **服务端兜底**包装：`valid_rate_and_frame_length` 前置校验、16k/10ms 帧常量 | 服务端不引入第二种 VAD 实现；端侧主 VAD 为 TEN-VAD WASM（前端资产，非服务端第二形态） |
| `app/audio/signaling.py` | WS 路由：承载 SDP/ICE offer/answer/candidate；WS 级 trace_id 注入；设备 JWT 鉴权 | 不承载音频裸流（音频走 WebRTC 轨） |
| `app/audio/webrtc.py` | aiortc PeerConnection 管理、回声测试、音频轨收发、断线检测 | 不写业务回复逻辑 |

---

## 4. 分步骤集成实施顺序

> 顺序依据：技术文档 Day 3「降级先行，主模式后接」；大纲课时 3.1→3.4 只取产出物顺序。每步一个提交，Conventional Commits。

### T0 · 前置准备（半步）

- 初始化 `frontend/` MPA Vite 工程（pad 入口骨架：大字幕、状态条占位、`getUserMedia` 权限申请；three-vrm 依赖**不装**，pad 专属依赖不得进其他入口——分层硬约束 4）。
- **提前做 pad 登录页**（主理人裁决 2026-11-05）：大字登录表单接入 Day 2 已就绪的会话接口（`POST /api/device-sessions` 设备换证，入参 `device_id + device_secret` 双因子）——零契约变更、零迁移、不建 device_code（最终裁决详见 adhoc-20261105-音频传输与设备链路技术方案 v1.3）；不扩 PRD 验收清单，属范围提前。
- **HTTPS 策略（主理人裁决 2026-11-05）**：Day 3 全程本机 `localhost:5173` 开发，零证书成本；跨机联调需求出现时再上 mkcert 本地受控证书（登记待办）。
- **FunASR 纳入 compose（主理人裁决 2026-11-05）**：`docker-compose.yml` 追加 `funasr` 服务（教师模块 compose.yaml 的 2pass 参数作参考），README 第 6 节登记端口与环境变量；云 key 未到期间它是唯一可用的真 ASR 基线。
- `.env` 增补教师发放的 ASR/TTS key（若有）；未发放则本期全部以 mock/FunASR 基线开发，key 到位只换配置不改代码。
- 验证第 0 步（吸收自康伴方案 §7.1）：**不改我们任何代码**，先原样跑通教师模块——起 FunASR 容器（首次拉镜像数 GB，healthcheck `start_period` 180s）+ 教师模块自己的页面说一句话出字，证明依赖链整体可用；顺手实测一次出字耗时（PRD 首包 ≤300ms 待验证，见 P12）。
- **验证**：`pnpm --dir frontend dev` 起页面；登录页可换发设备会话；`uv run pytest -q` 仍绿（存量 270 条不回归）。

### T1 · 降级链路：整段上传 → ASR → 写死回复 → TTS → 大字幕

- 新建 `app/audio/asr.py`、`app/audio/tts.py`：协议 + 工厂 + Mock 实现 + FunASR 基线适配器（移植 R2）+ 云 API 适配器骨架。
- FunASR 适配器固化协议细节（吸收自康伴方案 §6.3）：握手 JSON（`mode=2pass`、`chunk_size=[5,10,5]`、`audio_fs=16000`、`wav_format=pcm`、`itn=true`）、`subprotocols=["binary"]`、`ping_interval=None`（FunASR 不支持 ping 帧，开了会被判超时踢掉）、`asyncio.timeout(15)` 包住整个接收循环（参考实现"等 offline final 才结束"，对端不回会挂住）、`is_speaking=false + is_end=true` 收尾。
- 适配层错误二分类（吸收自康伴方案 §6.6）：`ASRUnavailable`（建连失败/超时/对端断开 → 临时错误，可退避重试 ≤2 次）与 `ASRProtocolError`（返回无法理解 → 不可重试，直接降级话术）；上层只决策"重试还是降级"，不解析厂商错误码，对齐 AGENTS §8 失败分类精神。
- 新增 REST 端点（`app/api/`，只做校验+调适配器）：上传整段音频 → ASR 转写（含置信度）→ 返回文本+写死回复话术；TTS 合成回传音频。
- 置信度与各阶段耗时写进结构化日志/trace（`asr_confidence`、`asr_first_result_ms`、`tts_first_packet_ms`）。
- pad 页面：`MediaRecorder` 按住说话整段上传（移植 R3 的 preRoll 思路可后置到 T4）、大字幕展示转写与回复、播放 TTS。
- **验证标准（阶段 V1）**：
  - 说一句"今天天气怎么样"，控制台在说完后 **1s 内**打印文字+置信度字段；trace 里能查到 `asr_confidence`（大纲课时 3.3 验收）。
  - mock 实现：无任何外部依赖时端到端可跑（回落语义生效）。
  - pytest：适配器协议与工厂单测、mock 全链路集成测试 ≥70% 新增代码覆盖。

### T2 · WS 信令 + WS 鉴权 + WS trace

- 新建 `app/audio/signaling.py`：`WS /ws/signal`（路由路径以实现时 README 登记为准）；消息模型 offer/answer/candidate/hangup；每连接建立即注入 trace_id 并贯穿日志。
- 设备 JWT 鉴权：连接时 `query token` 或首帧鉴权消息二选一（推荐首帧鉴权消息，避免 token 进 URL 日志）；复用 `core/security` 解码 + `devices` 回查，`elder_id` 仅从库内绑定读（禁区 9）。
- **验证标准（阶段 V2）**：伪造 token WS 握手即 4401 关闭并留审计日志；合法设备 token 完成一次 offer/answer 往返；日志按 trace_id 可查全过程。

### T3 · aiortc 回声测试

- 新建 `app/audio/webrtc.py`：RTCPeerConnection 建立、收音频轨原样推回（回声）、ICE 状态与超时（`webrtc_ice_timeout_seconds`）处理。
- pad 页面接 WebRTC 建连（`VITE_ENABLE_WEBRTC` 开关）。
- **验证标准（阶段 V3）**：耳机里能听到自己说话、无报错日志（大纲课时 3.2 验收）；同一局域网两台机器互通（跨网段 TURN 验证等教师 coturn 拓扑，先行登记为待办）。
- 重点排障：aiortc loop 与 uvicorn loop 绑定（本日最典型故障，同坑第 3 次必须写进 AGENTS §9）。

### T4 · 全双工语音闭环

- `app/audio/vad.py`：webrtcvad 服务端兜底包装（前置校验 + 16k/帧常量 + 单测覆盖非法帧长路径）。
- pad 端接 TEN-VAD WASM 主 VAD（移植教师模块 `BrowserTenVad` 切句 + preRoll），TEN-VAD 加载失败回落服务端 webrtcvad 分帧。
- webrtc.py 内：收轨 → VAD 分帧 → feed ASR 流式适配器 → results 迭代；回复文本 → TTS 流式 → PCM 写回音频轨。
- 打断：说话时立即停止 TTS 写轨（数字人立刻停）；"再说一遍"重放上一句 TTS。
- 前端状态条三态：正在听/正在想/正在说。
- **验证标准（阶段 V4）**：全双工实时双向、说话可打断（技术验收 line 1022）；2pass-online 中间结果与 offline 修正都能在字幕上体现。

### T5 · 降级与故障话术

- WebRTC 建连失败自动降级按住说话（T1 链路）；ASR 不可用/TTS 不可用各自给不同可读提示，页面不白屏不静默（大纲课时 3.4：降级分支让 AI 先写、人审）。
- 麦克风授权被拒：可读原因 + 重试入口（功能验收 line 1021）。
- **验证标准（阶段 V5）**：关掉 ASR、关掉 TTS、拒绝麦克风授权三个场景逐一演示，各得不同提示且无一次白屏。

### T6 · 测试收口与留痕

- pytest：WS 鉴权攻击用例（沿用真实 app + 真实 DB、禁 mock 的项目约定）、VAD 边界、工厂分派、mock 全链路；`uv run pytest --cov` 仍 ≥70%。
- README 回填：新增 WS 端点路径、前端构建期变量实际取用情况（顺手修复 `VITE_WS_BASE_URL` 与 `VITE_PAD_WS_URL` 的文档漂移）。
- 本日未完成项登记 `docs/team_tasks/README.md` 偏差表；跨网段 TURN、云 API 真 key 联调列 Day 4 前置。

---

## 5. 各阶段验证标准汇总

| 阶段 | 硬验收 | 出处 |
| --- | --- | --- |
| V1 | 说完 1s 内出文字+置信度；`asr_confidence` 进 trace；无配置可跑 mock | 大纲课时 3.3、PRD §4.11 P2 注 |
| V2 | 伪造 token 握手拒绝；合法设备 token 完成 SDP 往返；trace 贯穿 WS | 技术文档 Day 3 依赖项、禁区 9 |
| V3 | 回声可闻、跨机（STUN/host）互通、无报错日志 | 大纲课时 3.2 |
| V4 | 全双工实时双向可打断；中间+修正转写可见；ASR 置信度进 trace | 技术验收 line 1022 |
| V5 | ASR/TTS/麦克风三场景各自可读降级、不白屏 | 大纲课时 3.4、技术验收 line 1022 |
| V6 | pytest 全绿 + 覆盖 ≥70%；README/偏差表回填 | AGENTS §3、PRD FR-TC-06 |

**Day 3 整体功能验收（技术文档 line 1021）在 T4+T5 后整体复跑一遍**：按住说话"我有点闷"→松手 3 秒内大字幕+语音回答；"再说一遍"重听；"打断"立刻停。

**明确不在 Day 3 验收范围**：P95 ≤1.5s（Day 5 打点复算）、声纹身份闭环（Day 4）、三级路由与真实回复（Day 6）、反向 WebRTC 外呼（Day 7）。

---

## 6. 风险点与注意事项

| # | 风险 | 缓解 |
| --- | --- | --- |
| P1 | aiortc event loop 与 uvicorn loop 绑定冲突（本日最典型故障） | 一切后台任务绑定 uvicorn loop；同坑第 3 次写进 AGENTS §9（三次法则） |
| P2 | getUserMedia 需 HTTPS/localhost | T0 敲定策略；跨机联调先内网 IP+端口转发方案，必要时本地受控证书 |
| P3 | 云 ASR/TTS key 未到 | 全程以 mock + FunASR 基线开发；key 到位只改 `.env`，适配器协议不变；联调排 Day 4 前置 |
| P4 | FunASR 无置信度字段 | 适配器层打 `confidence=null + source` 标记，**禁止伪造数值**；置信度验收以"字段进 trace"为准 |
| P5 | 新依赖（pydub、edge-tts、funasr 相关）引入 | 一律先写 `spikes/` 最小验证再 `uv add`（AGENTS §3）；不整仓搬 asr 模块的 3.13 依赖树 |
| P6 | WS token 泄露（query 传 token 进访问日志） | 采用首帧鉴权消息方案；日志脱敏沿用 `app/core` 既有工具 |
| P7 | 搬运 asr 模块代码带入缺陷（`[::3]` 重采样、`np.repeat` 插值、pydub 缺声明） | 只移植结构与算法思路，媒体处理重写；WebRTC 侧音频重采样按 aiortc 帧率规范实现 |
| P8 | TTS edge-tts 非流式，首包远超 300ms 预算 | 定位为断网兜底候选而非主实现；主 TTS 等教师 key；Day 5 打点时不把 edge-tts 计入基线 |
| P9 | 声纹资产诱惑（模型已在手边） | 严守批次：`voiceprints`/`conversation_threads` 属 Day 4（ER §6），embedding 列 Day 4 定维度后才 ADD COLUMN（裁决 B2）；Day 3 只允许把 CAM++ spike 结论写进 spikes/ 留痕 |
| P10 | 迁移红线 | Day 3 不新增任何表迁移（head 保持 `0003`）；如需修 P-01（phone NOT NULL+唯一索引）另开 0004，与语音工作分开提交 |
| P11 | 大字幕/状态条口径属 pad 端 UX | 按 PRD FR-UI 系列实现，不自创交互；本计划不裁决 UI 细节，实现时回 PRD 取 |
| P12 | FunASR 容器实际推理延迟与并发表现未核实（其 compose 默认 `decoder-thread-num=4`；PRD 要求首包 ≤300ms 待实测） | T0 原样跑通后即实测一句出字耗时；不达标不影响开发但登记偏差，"云 API 为主实现"口径不变（吸收自康伴方案附录） |

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-05 | 初稿：ASR 参考模块能力梳理、匹配/差异裁决、插件抽象层边界、T0–T6 实施顺序、V1–V6 验证标准、P1–P11 风险登记 |
| v1.1 | 2026-11-05 | 吸收康伴-Day3 方案四个独有点：T0 增加"教师模块原样跑通"验证第 0 步；T1 固化 FunASR 协议细节（binary 子协议/禁 ping/15s 终稿超时/收尾标志）与 ASRUnavailable/ASRProtocolError 错误二分类；风险表新增 P12（FunASR 延迟与并发未实测） |
| v1.2 | 2026-11-05 | 主理人四项裁决落盘：①confidence 字段现在就加，占位只用 `null` 不造假值；②VAD 口径改为端侧 TEN-VAD WASM 为主、webrtcvad 服务端兜底（PRD v2.5 / 技术文档 v1.4 已补修订记录，AGENTS §1 同步）；③文件结构维持技术文档五文件平铺方案，不建 `ws.py`；④音频链路维持 WebRTC 音频轨 + 信令独立 WS，WS 裸流仅限按住说话降级走 REST |
| v1.3 | 2026-11-05 | 主理人三项新裁决：T0 增加 pad 登录页提前（接入设备换证接口）、HTTPS 策略定为全程本机 localhost（跨机证书留待办）、FunASR 容器纳入 docker-compose 作本地基线（README 登记端口/变量）；康伴-Day3-语音通道与ASR接入方案.md 的四个独有点已并入本计划，文件经主理人指示删除（可从废纸篓找回） |
| v1.4 | 2026-11-05 | 主理人最终裁决：音频主链路确认 WebRTC 音频轨 + 信令 WS；pad 登录撤回 elder_id 直连、恢复 device_code 方案（0004 迁移），过渡期粘贴 device_id + device_secret 走既有 E4 契约（详见 adhoc-20261105-音频传输与设备链路技术方案 v1.2） |
| v1.5 | 2026-11-05 | 主理人最终裁决：device_code 出局（零迁移），pad 登录定稿为 device_id + device_secret 直连既有 E4 契约，T0 登录页口径同步（详见 adhoc 方案 v1.3） |
