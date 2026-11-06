# A4 单元（task-04）· a0 遗留修复 + VAD 主链接入 + metrics 打点 · 工作推进计划

> 状态：已冻结　｜　版本：v1.1　｜　日期：2026-11-06
> 关联：docs/team_tasks/day04/task-04-VAD主链与遗留修复.md（任务卡）｜docs/team_tasks/day04/a0-联调遗留问题修复.md（修复卡）｜docs/team_tasks/day04/README.md（分工总览与 C5/C6 契约）｜docs/plans/day-04-3D与身份闭环.md v2.4（§3.0 图③、M13、R11）
> 上游依据：PRD v2.6（FR-DLG-04/05、FR-UI-11、AC-42、§5.1 端到端 ≤1.5s）｜技术设计文档 v1.5（L983 app/audio VAD 切段、Day 4 落地清单）｜day-03-a5 交付物（vad.py/utterance 端点）
> 变更记录见文末

---

## 0. 一句话定位与现状基线

**定位**：先清 a0 三问题（F-01/F-02 必修，P1-2 已修只需验证补测），再把服务端 webrtcvad 兜底接入 utterance 主链（TEN-VAD 失效自动接管，链路不断），最后建 `app/core/metrics.py` 打点位（AC-42 ≤1.5s 端到端口径的数据底座）。全程禁触 signaling.py、voiceprints 域、frontend。

**现状基线（2026-11-06 开工前 git 实证，head = b075f30）**：

| 项 | 状态 | 证据 |
| --- | --- | --- |
| a0-P1-2（真 ASR/TTS 接入） | ✅ **已修复，只需验证 + 补测试** | 提交 `ab15551`：`app/api/audio.py` 默认依赖已是 `build_asr()/build_tts()`；`_tts_synthesize` 写真实 WAV 到 `static/mock/` 并返回真实相对路径；假 `http://static.example.com/mock.mp3` 已不存在；TTS 不可用 → `audio_url=null` + 话术前缀已实现（`app/api/audio.py:305-311`） |
| a0-F-01（422 统一错误体） | ❌ 未修 | `app/main.py` 已有 `HTTPException`/业务异常 handler，但**无 `RequestValidationError` handler**——FastAPI 默认 422 仍是 Pydantic 原始数组，缺 `code`/`trace_id`（违反 C3 契约 §7 与 a0 卡验收） |
| a0-F-02（垃圾字节 422） | ❌ 未修（且现状是 503 不是 200） | `app/api/audio.py:101-112` `_wav_duration_ms` 对非法头返回 0 → 通过 ≤30s 校验 → 进入 `_frame_iter` 抛 `ASRError` → **503**。a0 卡要求 422（`VALIDATION_ERROR`，音频格式不合法）。注：a0 卡写的「照样 200」与当前代码不符——Day 3 后期 P1-2 修复改变了失败路径，**以「垃圾字节必须 422」为验收口径，现状 503 同样不合格** |
| VAD 主链接入（W4） | ❌ 未接 | `app/audio/webrtc.py` `_dispatch_frames`（L1413-1418）只把 asr 帧推入 ASR 队列，vad 帧**仅计数未消费**；文件头 L15 明言「尚未接 A5 的服务端 VAD 判定回调」。`app/audio/vad.py` `VadSession`（30ms 帧、preRoll 600ms、810ms 静音切段、30s 强切）已交付并带单测 `tests/test_audio_vad.py`，但无生产调用方 |
| metrics 打点位 | ❌ 不存在 | `app/core/metrics.py` 需新建；C5 裁决 5：`identity_ask` 发出 / `identity_confirm` 收到 / `identity_result` 下发三打点位归 A4 |
| 观察项 O-1（FunASR 首包 2219ms） | ⚪ 不混入本卡 | 留痕项，Day 4 评测按「本地 FunASR 基线」单独归档，不在 task-04 交付内 |

---

## 1. 前置依赖与执行边界

### 1.1 依赖

| 依赖项 | 来源 | 状态 |
| --- | --- | --- |
| 无跨人依赖（README §2：a0 与四条工作线零耦合） | day04 README | ✅ 可立即开工 |
| `VadSession` + 单测 | day-03-a5 交付 | ✅ 已就绪 |
| C5 `ClientDiagMsg`（stage=vad，兜底激活信号） | day04 contracts.md（已定稿） | ✅ 只消费类型，不改 signaling.py |
| C6 对话链路（utterance 主链下游） | A2 交付 | ⏳ 14:30 联调窗口对接；W4 自验期用 Mock 下游 |
| 设备 JWT 鉴权链 | Day 2 交付 | ✅ 已就绪 |

### 1.2 执行顺序（任务卡既定）

```
S0 基线自检 → S1 F-01 → S2 F-02 → S3 P1-2 验证补测   （上午，a0 清零）
→ S4 VAD 主链接入（W4）→ S5 metrics 打点位           （午后）
→ S6 task-a2 契约变更记录（委托项）+ 门禁收口          （联调窗口前后）
14:30–16:30 联调窗口：真音频验证兜底三场景
```

### 1.3 写作域与禁触

- **写作域**：`app/api/audio.py`、`app/main.py`（仅校验 handler）、`app/audio/vad.py`（+兜底控制器）、`app/core/metrics.py`（新建）、`tests/` 对应用例、a0 卡与 task-04 卡状态行。
- **禁触**：`signaling.py`、voiceprints 域、frontend、已合入迁移、`app/audio/asr.py`/`tts.py`（Day 3 冻结适配层，只 import）。
- **跨界协调点（S4 唯一边界，见 §5 R1）**：vad 帧分派点在 `app/audio/webrtc.py`（Day 3 A3 写作域）。接线改动限定为「在 `_dispatch_frames` 增加一行回调分发」级别的最小 diff，不触碰建连/编解码/playback 路径（R11 口径：只在编排层消费 a5 交付物）；改动前在群内通告 A3，联调窗口当面对接。

---

## 2. 分步执行计划（S0–S6，每步含自检，按步推进待确认）

### S0 · 基线自检（开工第一件事，≈10min）

**内容**：确认本地环境与测试基线，避免在脏基线上开工。

**操作步骤**：
1. `git log --oneline -5` 确认 head 与现状基线一致；工作区干净。
2. `uv run ruff check app tests && uv run ruff format --check app tests` 通过。
3. `uv run pytest -q` 全绿（若 DB 未起：`docker compose up -d postgres redis` 后重跑）。
4. 通读 `app/audio/vad.py`、`app/api/audio.py`、`app/main.py` 现状（本计划 §0 基线已核对，执行时再确认无新提交漂移）。

**自检标准**：
- [ ] pytest 全绿、ruff 零告警，基线记录在案（作为 S6 收口对照）。

---

### S1 · a0-F-01：RequestValidationError 统一错误体（≈40min）

**内容**：`app/main.py` 注册 `RequestValidationError` handler，422 响应体统一为 `{detail, code, trace_id}`（契约 §7）。

**操作步骤**：
1. `app/main.py` 新增 handler（属任务卡允许的「校验 handler」改动类，仅一处）：
   - `code="VALIDATION_ERROR"`；`detail` 由 `exc.errors()` 简化拼接（`loc` + `msg`，去掉 ctx 等不可序列化对象），不回显内部细节；
   - `trace_id` 取 `get_trace_id() or "-"`，复用既有 `ErrorResponse`（`app/api/schemas/auth.py`）。
2. 新增测试（落 `tests/test_audio_utterance.py` 或独立 `tests/test_validation_error.py`）：
   - `POST /api/audio/utterance` 缺 `audio` 字段 → 422 且 body 含 `code="VALIDATION_ERROR"`、`trace_id` 非空；
   - E4 口径复测：非 UUID 路径参数（如 `GET /api/elders/not-a-uuid`）→ 同样统一错误体；
   - 断言不再出现 Pydantic 原始数组形状。

**交付物**：`app/main.py` +1 个 handler；测试 ≥2 条。

**自检标准**：
- [ ] 缺字段 / 非 UUID 请求 → 422 响应体含 `code` 与 `trace_id`，无裸数组；
- [ ] 既有 HTTPException 422 路径回归不受影响（pytest 全绿）。

---

### S2 · a0-F-02：非法 WAV 头拒收 422（≈40min）

**内容**：垃圾字节 → 422（`VALIDATION_ERROR`，detail 说明音频格式不合法），正常 WAV 不受影响。

**操作步骤**：
1. 改 `app/api/audio.py`：`_wav_duration_ms` 解析失败时不再静默归 0——在 `utterance()` 的参数校验段（② 段）先做 WAV 头解析，解析失败直接 `HTTPException(422, detail="音频格式不合法")`（走既有 HTTPException handler 自动得 `code=VALIDATION_ERROR` + `trace_id`）。
2. 实现口径：把「头解析 + 时长计算」合成一步（返回 `int | None`，None=非法），`utterance()` 收到 None → 422；`real_duration_ms > 30s` → 413（既有行为不动）。
3. 新增测试：上传垃圾字节（如 `b"\x00"*1024`、文本字节）→ 422；正常 16k/mono/16bit WAV → 200 既有路径不回归；空文件 → 422（既有）。

**交付物**：`app/api/audio.py` 校验段改造；测试 ≥2 条。

**自检标准**：
- [ ] 上传垃圾字节 → 422（不是 503 也不是 200）；
- [ ] 正常 WAV ≤30s → 200 路径不变；>30s → 413 不变。

---

### S3 · a0-P1-2 验证与补测（已修复项收口，≈60min）

**内容**：P1-2 代码已在（提交 ab15551），按 a0 卡验收口径做**验证 + 补齐回归测试**，不做重复实现。

**操作步骤**：
1. 核对/补测试（`tests/test_audio_utterance.py`）：
   - 依赖注入真 `build_asr`/`build_tts` 形状的 provider（或 override 依赖为真适配器协议实现）→ `text` 与所说内容一致（Mock 口径）；
   - `audio_url` 断言：非 `mock.mp3`、为 `/static/mock/tts_*.wav` 真实路径且文件落盘可读；
   - TTS `TTSUnavailable` → 200 + `audio_url=null` + reply.text 含降级话术（若已有则只核对）；
   - ASR `ASRError` → 503 + `asr_unavailable` 话术（既有，回归）。
2. 手工真链路自验（浏览器 + FunASR 真容器，联调准入）：pad 登录 → WS auth_ok → WebRTC 建连 → 说话 → `asr_final` 出真实识别文本；按住说话 → `text` 为真实转写、`audio_url` 为 null 或真实可取地址。
3. a0 卡「验收方式」逐条勾验：真实说话 text 一致；503 场景用 unavailable 注入复测通过。

**交付物**：测试补齐（以实际缺口为准）；a0 卡验收口径勾验记录（写在 a0 卡变更记录）。

**自检标准**：
- [ ] 真实说话 → `text` 与所说一致；`audio_url` 无 mock.mp3；
- [ ] 垃圾字节 → 422（S2 联动复测）；
- [ ] 浏览器真链路两场景（WS 主链 + REST 降级）走通。

---

### S4 · W4：webrtcvad 兜底接入 utterance 主链（≈150min，本日核心）

**内容**：TEN-VAD（端侧主 VAD）失效时，服务端 `VadSession` 自动接管切段，产出 utterance 走既有主链；**不引入第二种 VAD 形态**，不触碰建连/编解码路径。

**设计口径（三场景自动接管）**：

```
场景一（主链路）：端侧 TEN-VAD 正常 → 客户端 asr_start/asr_end 控制切段（现状，不动）
场景二（兜底）：端侧 TEN-VAD 加载失败/不可用
    → pad 经 WS 上报 client_diag{stage:"vad", ok:false}（C5 已定稿，零新协议）
    → 服务端连接级开关切到兜底：webrtc 媒体层分流的 vad 帧喂入 VadSession
    → VadSession 判定 speechend 产出完整 PCM 段 → 走与 asr_end 相同的下游：
      ASR 识别 → asr_final →（Day 4 下游 = A2 对话链路）
场景三（降级二）：webrtcvad 也不可用（如 import 失败）
    → pad 前端自动切「按住说话」REST 整段上传 → POST /api/audio/utterance（S1–S3 已保证真链路）
```

**操作步骤**：
1. `app/audio/vad.py` 新增 `VadFallbackController`（写作域内）：
   - 持有连接级 `VadSession`；`feed(frame: bytes) -> bytes | None`（复用 `VadSession.feed_frame`）；
   - `set_active(bool)`：由 `client_diag{stage=vad}` 驱动开/关；关闭时 `flush()` 丢弃残留；
   - 激活期产出的语音段打 trace 日志（`vad_fallback_segment`，含段字节数/耗时/trace_id），不落盘（AGENTS §4.10）。
2. 接线点（**最小 diff**，webrtc.py 属 A3 写作域，见 §5 R1）：
   - `_dispatch_frames` 处把 vad 帧分发回调一行：`self._on_vad_frames(...)` → `VadFallbackController.feed`；
   - 会话关闭路径挂 `flush()`（防 preRoll 残留，对齐 vad.py flush 契约）；
   - 不改 offer/answer/ICE/播放轨任何逻辑；改动前群内通告 A3，联调窗口当面过 diff。
3. 激活语义（自裁项，随代码同提交留痕）：以 `client_diag stage=vad ok=false` 为**显式激活信号**；`ok=true` 或连接断开即停用；未收到 diag 时兜底不启用（不与端侧 VAD 双跑，避免双切段冲突）。
4. 测试（`tests/test_audio_vad.py` 扩展或新增 `tests/test_audio_vad_fallback.py`）：
   - TEN-VAD 不可用注入（激活 controller）→ 喂合成语音帧 → 产出完整 utterance 段，字节数符合「preRoll + 语音段」；
   - 未激活 → feed 不产出段（主链路不回归）；
   - 非法帧长 → warning 跳过不断链（复用 VadSession 既有行为）；
   - 关闭时 flush 无残留、无异常。
5. 与 A2 下游对接（联调窗口）：兜底段产出后走 A2 的 utterance 主链消费点（C6）；自验期用桩下游断言「切段仍出 utterance，链路不断」。

**交付物**：`VadFallbackController` + webrtc.py 最小接线 diff + 测试 ≥4 条。

**自检标准**：
- [ ] TEN-VAD 不可用注入 → 服务端兜底切段仍出 utterance，链路不断；
- [ ] 未激活时不产出段，Day 3 回归用例全绿（R11 门禁）；
- [ ] webrtc.py diff ≤ 5 行级改动，A3 知情留痕。

---

### S5 · metrics 打点位：`app/core/metrics.py`（≈60min）

**内容**：新建 `app/core/metrics.py`，承载「唤醒→建连→ASR→路由→LLM→TTS」与 C5 identity 三打点位，作为 AC-42（≤1.5s，20 次中 ≥18 次）的数据底座。

**口径（Day 4 = 日志级 + trace 字段，不建 metrics 表、不接 OTel——day-04 计划 §0 明确 metrics 表在 Day 5、OTel 在 Day 10）**：

| 打点位 | 触发处 | 记录字段 |
| --- | --- | --- |
| `wakeup` | 端侧 KWS 命中随上行带入（auth 首帧或 client_diag 附 `wake_ts`，**晨会对齐口径**；后端兜底口径 = WS accept 时刻） | wake_ts、连接建立耗时 |
| `connection` | WebRTC offer 收到 / 建连完成 | 建连耗时（offer→answer） |
| `stage_ms(stage)` | ASR（复用 `_observe.record_asr` 已有首包/总耗时）、路由、LLM、TTS（复用 `record_tts`） | stage、elapsed_ms、trace_id |
| `identity_ask_sent` / `identity_confirm_received` / `identity_result_sent` | C5 裁决 5 三处（由 A2 的 signaling 调用 A4 提供的函数，**A4 只提供 API 不改 signaling.py**） | ask_id、reason、elapsed、trace_id |

**操作步骤**：
1. 新建 `app/core/metrics.py`：仿 `_observe.py` 的「单点收口」模式——`record_event(name, **fields)` + `record_stage(stage, elapsed_ms)`，结构化日志承载（每行带 trace_id），Day 5 换 `/metrics`、Day 10 换 OTel 时只改本文件。
2. A4 自己可打的点位直接接：建连（webrtc offer 到达处一行）、ASR/TTS（`_observe` 已有，metrics 侧聚合）。
3. identity 三点位：函数签名定稿后群内通告 A2 消费（A2 在自己的 signaling 改动里调用，符合 C5 裁决 5 分工）。
4. 测试：record 函数输出结构化字段断言（caplog）；不写转写全文、不写隐私（脱敏红线）。

**交付物**：`app/core/metrics.py` + 测试 ≥2 条 + A2 消费通告。

**自检标准**：
- [ ] 打点函数全带 trace_id，无隐私字段；
- [ ] AC-42 口径可从打点日志还原「唤醒→建连→首响应」分段耗时；
- [ ] A2 三打点位 API 就绪并被知晓。

---

### S6 · 委托项 + 门禁收口（≈40min）

**内容**：task-a2 契约变更记录（A2 委托）+ 全量门禁 + 卡状态推进。

**操作步骤**：
1. 在 `docs/plans/day03/task-a2-信令WS与鉴权.md` 文末变更记录追加一条：登记 C5 追加（`client_diag`/`identity_*` 消息）与 `tts_text.degraded` 扩展对 C2 契约的影响——**只补记录，不改契约内容**（任务卡委托范围）。
2. 门禁：`uv run ruff check app tests && uv run ruff format --check app tests && uv run pytest -q` 全绿；对照 S0 基线确认无回归。
3. 联调窗口（14:30–16:30）真音频验证兜底三场景（S4 自检第 1 条 + S3 真链路），结果记入 task-04 卡。
4. 更新 `docs/team_tasks/day04/task-04-VAD主链与遗留修复.md` 的 `> 状态：` 行与变更记录（PROGRESS 看板自动汇总，不手改进度文件）。

**自检标准**：
- [ ] task-a2 变更记录已补、契约内容零改动；
- [ ] pytest 全绿 + ruff 通过（任务卡验收第 4 行）；
- [ ] task-04 卡状态行与变更记录已更新。

---

## 3. 交付物总表与质量要求

| # | 交付物 | 位置 | 格式/质量要求 |
| --- | --- | --- | --- |
| 1 | F-01 统一 422 错误体 | `app/main.py` | 1 个 handler；detail 简化无内部细节；复用 ErrorResponse |
| 2 | F-02 非法 WAV 422 | `app/api/audio.py` | 垃圾字节 422；正常/超长路径零回归 |
| 3 | P1-2 验证补测 | `tests/test_audio_utterance.py` | text 一致、无 mock.mp3、TTS 降级 audio_url=null |
| 4 | VAD 兜底主链 | `app/audio/vad.py` + `app/audio/webrtc.py`（最小 diff） | 三场景自动接管；不引第二 VAD 形态；A3 知情 |
| 5 | metrics 打点位 | `app/core/metrics.py`（新建） | 单点收口模式；trace_id 全覆盖；AC-42 分段可还原 |
| 6 | task-a2 变更记录 | `docs/plans/day03/task-a2-信令WS与鉴权.md` | 只补记录不改内容 |
| 7 | 测试增量 | `tests/` | 全部真实 DB/真实 app（禁 mock 攻击面约定）；新增 ≥10 条 |

---

## 4. 验收对照（任务卡 4 行验收 → 本计划落点）

| 任务卡验收标准 | 覆盖步 |
| --- | --- |
| 真实说话 → text 与所说一致；audio_url 为 null 或真实可取地址（无 mock.mp3） | S3 |
| 上传垃圾字节 → 422；缺字段/非 UUID → 错误体含 code/trace_id | S1、S2 |
| TEN-VAD 不可用注入 → 服务端兜底切段仍出 utterance，链路不断 | S4 |
| `uv run pytest -q` 全绿；`ruff check app` 通过 | S0 基线 + S6 收口 |
| （上游）AC-42 ≤1.5s 数据底座 + C5 裁决 5 三打点位 | S5 |

---

## 5. 风险与协调点

| # | 风险/协调点 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | **webrtc.py 接线跨界**：vad 帧分派点在 Day 3 A3 写作域，任务卡写作域未含 webrtc.py | 归属争议 / 阻塞 S4 | 最小 diff（回调分发一行 + 关闭挂 flush 一行）；改前群内通告 A3、联调当面过 diff；不动建连/编解码/playback（R11 口径）；若 A3 异议 → 提主理人裁决，S4 先用桩完成 controller 单测 |
| R2 | 兜底激活信号依赖 `client_diag stage=vad`（C5 消息在 A2 的 signaling 域） | 自验期无法端到端触发 | 自验用直接调 `VadFallbackController` 的单测 + WS 测试桩注入 diag；联调窗口与前端/A2 对真触发 |
| R3 | 「唤醒」时刻口径未定（端侧 KWS 在前端，后端收不到唤醒事件形状） | AC-42 分段口径漂移 | 晨会对齐：后端兜底口径 = WS accept 时刻起算，端侧 wake_ts 由 auth/diag 携带，字段名随 C5 记录留痕 |
| R4 | a0 卡「垃圾字节照样 200」与现状 503 不符 | 验收口径混淆 | 以「垃圾字节必须 422」为唯一口径（a0 卡验收方式列原文）；现状 503 同判不合格 |
| R5 | 双 VAD 双切段冲突（端侧 + 服务端同时开） | 重复 utterance / 串段 | 显式激活语义（S4 步骤 3）：diag ok=false 才启用，ok=true 即停用 |
| R6 | 联调窗口前 A2 下游未就绪 | S4 端到端自验受限 | 自验期用桩下游（断言切段产出即算通过）；真链路留 14:30 窗口 |

---

## 6. 提交规范（Conventional Commits，一步一提交）

| 时机 | 范围 | 提交信息 |
| --- | --- | --- |
| S1 完成 | main.py + 测试 | `fix(api): 422 参数校验统一错误体（a0-F-01）` |
| S2 完成 | audio.py + 测试 | `fix(api): 非法 WAV 头拒收 422（a0-F-02）` |
| S3 完成 | 测试补齐 + a0 卡留痕 | `test(audio): utterance 真 ASR/TTS 链路回归补测（a0-P1-2）` |
| S4 完成 | vad.py + webrtc.py 最小 diff + 测试 | `feat(audio): webrtcvad 兜底接入 utterance 主链（W4）` |
| S5 完成 | metrics.py + 测试 | `feat(core): 唤醒至 TTS 全链路打点位（AC-42 数据底座）` |
| S6 完成 | task-a2 记录 + 卡状态 | `docs(day04): task-04 收口——契约变更记录与验收登记` |

> 自裁项（S4 激活语义、R3 唤醒口径）在对应 commit 正文留痕（AGENTS 铁律 6）。

---

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：现状基线 git 实证（P1-2 已修于 ab15551，F-01/F-02 未修，VAD 主链未接，metrics 不存在）；S0–S6 七步拆解（每步含操作步骤/交付物/自检标准）；交付物总表、任务卡验收逐条对照、R1–R6 风险登记（含 webrtc.py 跨界协调与兜底激活语义自裁项）、提交规范 |
| v1.1 | 2026-11-06 | 执行收口冻结：S0–S6 全部完成（提交 1530b92/94f0baf/41e078c/ba937c5/a224579/eccf8f8/fcb5e41/0c88237/52d0415）；实际落点与计划的偏差仅两处——①S4 下游采用 `VadSegmentCallback` 回调接缝（计划写「回调分发」，一致）+ 连接级 controller 常驻 MediaSession（自裁，见 fcb5e41 正文）；②metrics 建连接线落在 webrtc `_handle_offer`（+4 行，计划口径「offer 到达处一行」）。联调窗口真音频验证留待 14:30–16:30，结果记 task-04 卡 |
