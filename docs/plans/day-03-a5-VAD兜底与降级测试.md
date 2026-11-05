> 状态：草稿　｜　版本：v1.0　｜　日期：2026-11-05
> 关联：docs/team_tasks_day03/a5-flex-vad-fallback-tests.md（任务卡）｜ docs/plans/day-03-WebRTC与语音闭环.md v1.5（执行基线）
> 上游依据：技术设计文档 Day 3 落地清单 line 1016–1023（实现）｜ PRD §2.2（降级不白屏）｜ PRD §2.3（失败分类）｜ PRD FR-TC-06（覆盖率）｜ PRD §1.2（不白屏）｜ adhoc-20261105-音频传输与设备链路技术方案 v1.3 M3/VAD 兜底口径
> 变更记录见文末

---

# task-07 · A5 机动单元：VAD 兜底 + 降级链路 + 测试收口 详细推进计划

## 0. 一句话定位

**为 Day 3 语音闭环建造"安全网"**：当下游 ASR/TTS/WS 任何一环失效、或 TEN-VAD WASM 加载失败时，系统必须自动降级、不白屏、给出可读话术，且攻击面 100% 真实 DB 测试覆盖。机动剩余工时随时阻塞支援 A1–A4。

## 1. 前置条件与依赖全景

### 1.1 外部依赖

| 依赖项 | 来源 | 状态 | 备注 |
| --- | --- | --- | --- |
| webrtcvad-wheels | pyproject.toml dependencies | ✅ 已就绪 | spike 已验证：valid_rate_and_frame_length 前置校验必须调用；非法帧长抛 `_webrtcvad.Error`（不是 ValueError） |
| FastAPI + python-multipart | pyproject.toml | ✅ 已就绪 | REST 降级链路 multipart 解析 |
| Day 2 真实 DB + 设备身份 | pytest conftest + app/core/auth.py | ✅ 已就绪 | 攻击面测试必须用真实 app + 真实 DB，禁 mock（项目约定） |
| device-sessions 端点 | Day 2 已交付 | ✅ 已就绪 | 降级 utterance 走设备 JWT 鉴权 |
| aiortc loop 绑定防护经验 | Day 3 主理人风险 P1 | ✅ 已就绪 | vad.py 本身不涉及 WebRTC，但攻击面测试会打 WS → 需知 A2/A3 loop 策略 |

### 1.2 无前置（A5 独有的并行优势）

A5 与 A1/A2 **完全并行**，可 09:00 立即开工。与 A3/A4 的唯一对接点：
- C3 契约 → A4 降级模式消费（09:30 契约会定稿）
- vad.py → A3/A4 TEN-VAD 失败时回落（14:30 联调期对接）

### 1.3 不依赖的（明确排除）

- **不依赖 FunASR 真链路**：降级链路跑 Mock ASR 即可完成闭环
- **不依赖 frontend/**：REST 端点用 httpx 测；攻击面测试走 Python ASGI transport
- **不新增任何迁移**（AGENTS §5 P10 已禁）

---

## 2. 时间线（精确到小时）

```
09:00 – 09:30  契约会：C3 定稿（与 A1/A2 同步）
09:30 – 10:30  Block 1：app/audio/vad.py（webrtcvad 兜底）
10:30 – 12:00  Block 2：降级链路核心（utterance 路由 + Mock ASR/TTS）
12:00 – 12:30  午餐
12:30 – 12:40  午站会：进度/阻塞通报
12:40 – 14:00  Block 3：攻击面测试（伪造 token + 越权 elder_id + 审计落库）
14:00 – 15:00  Block 4：降级话术（三场景文案 + 触发逻辑）
15:00 – 16:30  Block 5：降级链路全场景验证 + 支援 A1–A4 阻塞
16:30 – 17:00  Block 6：全量 pytest + 覆盖率核对 + README 第 7 节回填
17:00 – 17:30  收口会：提交 + 汇总各卡变更记录
```

---

## 3. Block 1 · C3 契约冻结（09:00–09:30）

### 工作内容

与 A1/A2 共同确认 [contracts.md](../team_tasks_day03/contracts.md) C3 章节内容。若会议无异议，直接把 C3 原样拷贝进 `app/api/audio.py` 顶部。

### C3 契约核心内容（冻结前可微调）

```python
# 端点：POST /api/audio/utterance
# Content-Type: multipart/form-data
# Authorization: Bearer <device access_token>

# 表单字段：
#   audio: file    # 16kHz/16bit/单声道 WAV，按住说话整段，含 preRoll
#   duration_ms: int  # 客户端计时，服务端仅入 trace 不信任

# 200 响应：
# {"text": str, "reply": {"text": str, "audio_url": str | None}}
#   text   = ASR 终稿
#   reply.text   = 回复话术（降级链路为模板话术）
#   reply.audio_url = 服务端合成音频可取地址；无 TTS 时为 null

# 错误码：
#   401  token 失效/伪造
#   413  超长音频拒收（>30s）
#   503  ASR 不可用，body 携带降级话术
```

### 操作步骤

1. 09:00 前预读 contracts.md C3 章节 + day-03 计划 T1/T5 相关段落
2. 契约会：确认 `duration_ms` 是否必传（建议选填，服务端从文件头计算）
3. 确认 `audio_url` vs 直返 PCM：**本次冻结为 audio_url**（等 TTS 决议，若 A1 边缘-tts 走不通再改回）
4. 09:25 会议结束，立即写 C3 到 `app/api/audio.py` 顶部并提 PR 初稿

### 交付物

| 物 | 位置 | 格式 |
| --- | --- | --- |
| C3 契约 dataclass/TypedDict | `app/api/audio.py` 顶部 | Python 代码 |
| 契约会确认记录 | `docs/team_tasks_day03/contracts.md` 末尾「契约会确认记录」行 | 填充状态列 + 备注 |

### 自检标准

- [ ] C3 代码在 `app/api/audio.py` 顶部，**不超过 20 行**（只含 TypedDict + 端点签名注释）
- [ ] contracts.md 确认记录行「C3」状态列从「待确认」改为「已冻结」，备注写明 audio_url 定稿
- [ ] ruff check 不报错（纯类型定义）

---

## 4. Block 2 · app/audio/vad.py（16k/30ms 帧校验 + speechstart/speechend 判定）

### 前置

无（契约会结束后立即开始，不依赖任何单元）

### 工作目标

在 `app/audio/vad.py` 中实现 webrtcvad **服务端兜底包装层**。这是端侧 TEN-VAD WASM 加载失败时的唯一回落路径。

### 操作步骤

**Step 1 · 帧常量与前置校验**（09:30–10:00）

```python
# 必须定义的常量：
SAMPLE_RATE = 16_000          # 项目硬性：所有语音链路 16k
FRAME_MS = 30                 # webrtcvad 固定帧长选项只有 10/20/30（服务端兜底专用）
FRAME_SAMPLES = 480           # 16000 * 30 / 1000
PRE_ROLL_MS = 600             # 毫秒绝对值，与端侧 TEN-VAD 体系共享同一量纲
PRE_ROLL_FRAMES = PRE_ROLL_MS // FRAME_MS  # 20 帧（注意：端侧帧长 60ms 时是 10 帧，各自按帧长换算）
PRE_ROLL_BYTES = PRE_ROLL_FRAMES * FRAME_SAMPLES * 2  # 19200 bytes（16bit）
WEBRTC_VAD_MODE = 2           # 从 settings.webrtc_vad_aggressiveness 读取，默认 2
```

**核心约束**：webrtcvad 不抛 ValueError，非法帧长抛 `_webrtcvad.Error`（见 spike 验证结论）。包装层必须：
- **每个帧调用前先 `valid_rate_and_frame_length(SAMPLE_RATE, len(frame_bytes)//2)`**
- 校验失败时记录 `warning` 日志（含 trace_id），**跳过但不中断会话**

**Step 2 · VAD 会话类**（10:00–10:30）

```python
class VadSession:
    """单轮语音的 VAD 切段器。"""
    
    def __init__(self, aggressiveness: int = 2): ...
    
    async def feed_frame(self, frame: bytes) -> None:
        """喂入 30ms PCM16 帧，内部维护 preRoll + 当前 speech 缓冲。"""
        # 非法帧长 → 打日志跳过，不抛异常
        # 连续 N 帧 speech → speechstart（preRoll 先刷出）
        # 连续 M 帧静音 → speechend（返回完整语音段）
    
    @property
    def is_speech(self) -> bool: ...    # 当前是否判定在说话
    def flush(self) -> bytes | None: ... # 强制结束，返回残留语音段
    def reset(self) -> None: ...
```

**Step 3 · 单测**（嵌入 Block 5 收口阶段一起写，但接口设计必须在此 Block 确定）

### 交付物

| 物 | 位置 | 格式 |
| --- | --- | --- |
| `app/audio/vad.py` | 新建 | Python async 模块（≈120 行） |
| `tests/test_audio_vad.py` | 新建 | pytest-asyncio 文件 |

### 自检标准

- [ ] `uv run ruff check app/audio/vad.py` 零告警
- [ ] `uv run ruff format --check app/audio/vad.py` 通过
- [ ] spike_webrtcvad.py 验证的 5 条结论全部复现（静音不判浊音/浊音多数判语音/前置校验正确/非法帧长被前置拦截/不抛 _webrtcvad.Error）
- [ ] 类签名 `VadSession.feed_frame(frame: bytes) -> None` 和 `VadSession.flush() -> bytes | None` 供 A3 调，**不暴露 webrtcvad 原生 API**
- [ ] `settings.webrtc_vad_aggressiveness` 已在 Settings 预留（只读，不改 config.py）

### 明确不做

- 不引入第二种 VAD 实现（端侧 TEN-VAD WASM 是 A4 写作域，非服务端第二形态）
- 不做帧重采样（上游 A3 或 A4 必须保证喂入 16k；这里只校验不转换）
- 不写日志脱敏之外的任何业务逻辑

---

## 5. Block 3 · 降级链路核心 — `app/api/audio.py` utterance 端点（10:30–12:00）

### 前置

C3 契约已冻结并落 `app/api/audio.py` 顶部（Block 1 结束时完成）

### 工作目标

让"按住说话整段上传"这条降级链路**独立跑通**——不依赖任何下游服务即可完成闭环。这是 Day 3 的 T1 验收项（V1 阶段）。

### 架构设计

```
POST /api/audio/utterance
  ↓
① 鉴权：verify device JWT（复用 core/auth.py）→ 解出 device_id + elder_id（从库内绑定读，禁客户端传）
② 参数校验：multipart audio file >0 且 ≤30s（413 拒收）
③ ASR：工厂分派（默认 mock → 返回硬编码 "今天天气怎么样"）
④ 组装回复：TTS 工厂合成 → 保存到 static 目录 → 返回 audio_url
⑤ 统一错误包装：{detail, code, trace_id}（项目约定）
```

### 操作步骤

**Step 1 · 端点骨架 + 鉴权**（10:30–11:00）

```python
# app/api/audio.py
from fastapi import APIRouter, File, UploadFile, Header
from app.core.auth import verify_device_token
from app.core.runtime_context import RuntimeContext

router = APIRouter(prefix="/api/audio", tags=["audio"])

@router.post("/utterance")
async def utterance(
    audio: UploadFile = File(...),
    authorization: str = Header(...),
) -> dict:
    # 1. verify_device_token → 解 identity
    # 2. elder_id 从库内绑定读（禁 elder_id 入参）
    # 3. 构建 RuntimeContext
```

**Step 2 · ASR + TTS 工厂调用**（11:00–11:40）

```python
# 降级链路关键：
# - ASR 失败 → 打 trace + 返回 "ASR 暂不可用，请稍后再试" + 提示人工
# - TTS 失败 → audio_url=null + 大字幕仍返回 reply.text（不白屏）
# - 降级话术模板定义在常量（不配 prompt）：
DEGRADE_TEMPLATES = {
    "asr_unavailable": "抱歉，语音识别暂时不可用，请稍后再试或联系工作人员。",
    "tts_unavailable": "抱歉，语音播报暂时不可用，但我会用文字回复您：",
    "tool_breaker": "抱歉，当前请求处理超时，我会尽快通知工作人员。",
}
```

**Step 3 · 错误包装与 trace**（11:40–12:00）

- 所有异常走全局 handler → `{detail, code, trace_id}`
- trace_id 从 X-Trace-Id header 读（前端传递），若无则生成
- **置信度进 trace**：`asr_confidence=None`（Mock/FunASR 口径），但字段必须存在

### 交付物

| 物 | 位置 | 格式 |
| --- | --- | --- |
| `app/api/audio.py` | 新建 | FastAPI Router 文件（≈150 行） |
| `app/main.py` | 修改 1 行 | 追加 `audio.router` 到 `app.include_router()` 列表 |

### 自检标准

- [ ] `uv run uvicorn app.main:app --reload --port 8000` 启动无报错
- [ ] `curl -X POST http://localhost:8000/api/audio/utterance -F "audio=@test.wav" -H "Authorization: Bearer <伪造token>"` → 401 + 错误包装
- [ ] `curl -X POST http://localhost:8000/api/audio/utterance -F "audio=@test.wav" -H "Authorization: Bearer <合法token>"` → 200 + `{"text": ..., "reply": ...}` + trace_id 在响应头
- [ ] ASR 容器关掉时，合法请求返回 503 + 降级话术文本（不是 crash/白屏）
- [ ] ruff check + format 全绿
- [ ] **写死回复话术**："您说的是『{asr_text}』，我记下了。"（Day 3 不接真 LLM，大纲课时 3.4 明确）

### 明确不做

- 不写业务回复逻辑（Day 3 = 写死话术）
- 不接 Agent/三级路由（Day 6）
- TTS audio_url 临时写 static/，不接 nginx 托管（Day 10 再补部署）

---

## 6. Block 4 · 攻击面测试（12:40–14:00）

### 前置

Block 3 端点跑通 + A2 的 WS `/ws/signal` 端点就绪（14:00 左右 A2 会完成）

### 工作目标

**全部用真实 app + 真实 DB + 禁 mock**——这是项目「攻击面测试约定」的硬规则（AGENTS §8）。伪造 token、越权参数必须真实命中拦截逻辑并断言审计落库。

### 测试用例清单

| # | 场景 | 目标接口 | 预期 |
| --- | --- | --- | --- |
| **A1** | 伪造 JWT（alg: none） | WS `/ws/signal` + 首帧 auth | 4401 关闭 + `audit_logs` 插 1 条（action=ws_auth_fail） |
| **A2** | 过期设备会话 token | WS `/ws/signal` | 4401 + 审计 |
| **A3** | 被停用设备的 token | WS `/ws/signal` | 4401 + 审计 |
| **A4** | 首帧 300ms 超时 | WS `/ws/signal` | 4401 + 审计 |
| **B1** | 伪造 JWT 打 utterance | REST `/api/audio/utterance` | 401 + 错误包装 |
| **B2** | 过期设备 token 打 utterance | REST `/api/audio/utterance` | 401 |
| **B3** | utterance 传 elder_id 参数（服务端应忽略） | REST `/api/audio/utterance` | 服务端响应的 elder_id **不是**客户端传入值（从绑定关系取） |
| **C1** | 超长音频 31s 打 utterance | REST `/api/audio/utterance` | 413 |
| **C2** | multipart 缺 audio 字段 | REST `/api/audio/utterance` | 422 Pydantic 校验 |
| **C3** | WAV 头损坏的伪文件 | REST `/api/audio/utterance` | 422 或正常处理（**不 crash**） |

### 操作步骤

**Step 1 · pytest fixtures 扩展**（12:40–13:00）

```python
# tests/conftest.py 追加（不动已有 db_conn fixture）：
# - `app` fixture：create_app() 实例（httpx ASGITransport）
# - `asgi_client(app)`：httpx.AsyncClient(transport=ASGITransport(app))
# - `real_device` fixture：真实 DB 创建 device + elder + device_session → 返回 device_token
# - `forged_token` fixture：pyjwt 手造 alg:none 或 RS256 自签名 token
```

**Step 2 · REST 攻击面用例**（13:00–13:30）

文件：`tests/test_audio_security.py`
- 真实 DB + 事务回滚（复用 db_conn fixture）
- 每条用例断言：状态码 + 错误包装 + trace_id 存在

**Step 3 · WS 攻击面用例**（13:30–14:00，等 A2 端点就绪后追加）

文件：`tests/test_audio_ws_security.py`
- httpx.AsyncClient.ws_connect() 发起连接
- 每条断言：关闭码 + 审计落库（`audit_logs` 表查询 count 增量）

### 交付物

| 物 | 位置 | 格式 |
| --- | --- | --- |
| 攻击面测试文件 | `tests/test_audio_security.py` + `tests/test_audio_ws_security.py` | pytest-asyncio |
| conftest 扩展 | `tests/conftest.py` 末尾追加 | pytest fixtures |

### 自检标准

- [ ] `uv run pytest tests/test_audio_security.py -v` 10/10 PASS，**无 mock**（每个用例内无 anyio mocking / unittest.mock.patch）
- [ ] WS 攻击面 4 条：每条都断言 `audit_logs` 表 count 增加 1（查真实 DB）
- [ ] 覆盖率：`uv run pytest --cov=app --cov-report=term-missing tests/test_audio_security.py` → audio 模块 security 分支 ≥95%

### 明确不做

- 不做提示注入测试（FR-TC-07 属 Day 9）
- 不做越权 Tool 调用测试（属 Agent 层 Day 6+）
- 不做完整审计日志格式断言（只断言 count > 0，格式由 auth 层保证）

---

## 7. Block 5 · 降级话术 + 全场景验证（14:00–15:00）

### 前置

Block 3 utterance 端点跑通 + Block 4 攻击面测试完成

### 工作目标

让三个降级场景**各有不同的、长者可读的话术**，且全程不白屏。

### 三场景降级话术定稿

| 场景 | 触发条件 | 话术（长者可读） | 代码路径 |
| --- | --- | --- | --- |
| **ASR 不可用** | FunASR 容器关闭 / 网络不通 | "抱歉，语音识别暂时不可用。您可以直接输入文字，或稍后再试。" | ASRProvider.stream() 抛 `ASRUnavailable` → catch → 返回 503 + body.text=话术 |
| **TTS 不可用** | 边缘-TTS 服务异常 | "抱歉，语音播报暂时不可用，但我会用文字回复您。" | TTSProvider.synthesize() 抛 → catch → audio_url=null，reply.text 仍返回 |
| **云 LLM 超时**（占位，Day 6 接 LLM 时启用） | HTTP 2s 超时 | "抱歉，我正在思考，您可以稍后再试或联系工作人员。" | 当前 Day 3 为写死回复，本场景登记为占位，话术模板先定义 |

### 操作步骤

**Step 1 · 话术常量集中管理**（14:00–14:20）

```python
# app/audio/degrade.py 或集中在 app/api/audio.py 顶部
DEGRADE_TEMPLATES: dict[str, str] = {
    "asr_unavailable": (
        "抱歉，语音识别暂时不可用。您可以直接输入文字，或稍后再试。"
    ),
    "tts_unavailable": (
        "抱歉，语音播报暂时不可用，但我会用文字回复您：{reply}"
    ),
    "llm_timeout": (
        "抱歉，我正在思考，您可以稍后再试或联系工作人员。"
    ),
    "tool_breaker": (
        "抱歉，当前请求处理超时，我会尽快通知工作人员。"
    ),
}
```

**Step 2 · 降级触发测试**（14:20–15:00）

```bash
# 测试 1：关 FunASR，请求 utterance → 503 + ASR 话术
docker stop funasr_container

# 测试 2：Mock ASR 返回正常，Mock TTS 抛异常 → 200 + tts_unavailable 话术 + audio_url=null
# （pytest mock 一条 TTS 实现抛异常）

# 验证标准：
# - 每个场景返回状态码 200（TTS 降级）或 503（ASR 降级）
# - 响应 body 不 crash、有可读话术
# - 前端渲染不白屏（等 A4 联调，此处先用 httpx 验证服务端）
```

### 交付物

| 物 | 位置 | 格式 |
| --- | --- | --- |
| `DEGRADE_TEMPLATES` 常量 | `app/api/audio.py` 或 `app/audio/__init__.py` | Python dict |
| 降级触发集成测试 | `tests/test_audio_degrade.py` | pytest |

### 自检标准

- [ ] `uv run pytest tests/test_audio_degrade.py -v` 3 条全 PASS
- [ ] FunASR 容器关闭时，utterance 返回的 JSON 里 `reply.text` 等于 "抱歉，语音识别暂时不可用..."
- [ ] **不 crash**：无论 mock ASR 抛什么异常，HTTP 响应状态码 ∈ {200, 400, 401, 503}，**不是 500**
- [ ] 降级话术长度：≤40 字（长者端大字幕不超屏）

---

## 8. Block 6 · 测试收口 + README 回填（16:30–17:00）

### 前置

所有 Block 1–5 完成

### 工作目标

全量门禁绿灯 + 覆盖率达标 + README 有 Day 3 验收记录。这是当天最后一个交付物，也是收口会的核心输入。

### 操作步骤

**Step 1 · 跑全量门禁**（16:30–16:40）

```bash
# 依次执行
uv run ruff check app tests
uv run ruff format --check app tests
uv run pytest -q                    # 全量（含 Day 1/2 存量）
uv run pytest --cov=app --cov-report=term-missing  # 覆盖率
```

**预期结果**（若失败需立即修复，不等收口会）：
- ruff check 零告警
- ruff format 全绿
- pytest -q 全绿（Day 1/2 存量不回归）
- 覆盖率 ≥70%（coverage fail_under=70 已钉死）

**Step 2 · README 回填第 7 节**（16:40–16:55）

```markdown
## 7. Day 3 验收记录（A5 维护）

> 回填格式模板：按当日 V1–V6 阶段，每阶段一行「是否通过」+「验证方式」+「备注偏差」。

### 7.1 日期：2026-11-05

| 阶段 | 验证标准 | 结果 | 备注 |
| --- | --- | --- | --- |
| V1 降级链路 | utterance 1s 内返回 ASR 文本+置信度，无配置可跑 Mock | ✅ / ❌ | |
| V2 WS 鉴权 | 伪造 token 4401 + 审计落库 | ✅ / ❌ | |
| V3 回声 | 耳机听到回声，无报错日志 | ✅ / ❌ | A3 责任，此处登记交叉验证结论 |
| V4 全双工可打断 | 实时双向、说话即打断 | ✅ / ❌ | A3+A4 责任，同上 |
| V5 降级话术 | ASR/TTS/麦克风三场景各自可读、不白屏 | ✅ / ❌ | |
| V6 覆盖率 | pytest 全绿 + ≥70% + 核心权限 100% | ✅ / ❌ | |
```

**Step 3 · 汇总各卡变更记录**（16:55–17:00）

遍历 docs/team_tasks_day03/ 下所有卡，确认每张卡变更记录已填写；若 A1–A4 有未登记的契约变动，A5 提醒补录。

### 交付物

| 物 | 位置 | 格式 |
| --- | --- | --- |
| README 第 7 节 | README.md 追加 | Markdown 表格 + 回填内容 |
| 门禁执行截图/输出 | 收口会口头汇报 | 无需持久化 |

### 自检标准

- [ ] `uv run pytest -q` 全绿，**无 skip 超过 2 条**（skip 需解释原因）
- [ ] README 第 7.1 节 V1/V2/V5/V6 四个 A5 负责的阶段结果列填 "✅" 或 "❌" + 备注说明
- [ ] A5 不负责的阶段（V3/V4）也登记交叉验证结论（"A3 责任，已确认 ✅"）

---

## 9. 机动支援协议（全程 30% 工时预留）

### 9.1 触发条件

任何 A1–A4 成员在群里说「阻塞超 30 分钟 @A5」，A5 **立即中断当前 Block**、接手对方写作域内具体任务。

### 9.2 典型支援场景

| 场景 | 支援内容 | 预计耗时 |
| --- | --- | --- |
| A3 卡 aiortc loop 绑定（风险 P1） | 协助定位 loop 冲突 + 写防护单测 | 60–90min |
| A1 卡 FunASR 容器起不来 | 排 Docker compose + 拉镜像 + healthcheck | 30–60min |
| A2 卡 WS trace 注入 | 补 ASGI middleware 覆盖 websocket scope | 45min |
| A4 卡 TEN-VAD WASM 加载失败 | 立即切服务端 webrtcvad（Block 1 已就绪） | 10min |

### 9.3 支援纪律

- **所有权不转移**：支援任务的写作域归属不变，A5 在对方文件里写的代码以对方名义提交（或协商 split commit）
- **支援留痕**：在对方任务卡变更记录里追加「A5 支援于 XX:XX–XX:XX，处理了什么问题」
- **A5 写作域文件不被他人改**：反过来 A5 的 vad.py / audio.py / tests 只 A5 有权写

---

## 10. 风险登记（与 day-03 计划联动，A5 专属视角）

| # | 风险 | 概率 | 影响 | 缓解 |
| --- | --- | --- | --- | --- |
| R1 | webrtcvad-wheels 虽然 spike 过，但在真实容器里行为不同 | 低 | 低 | Block 1 写包装层后立即 pytest，非法帧长全路径断言 |
| R2 | A3 卡 WS 端点 15:00 前完不成 → WS 攻击面用例阻塞 | 中 | 中 | Block 4 前半先写 REST 攻击面（不依赖 A2），WS 后半等 A2；同时支援 A2 加速 |
| R3 | 降级话术被 PRD 否决需要改文案 | 低 | 低 | 话术在 Block 5 集中常量管理，改 dict 即可，不波及代码逻辑 |
| R4 | FunASR 真容器起不来 → utterance 一直走 mock | 中 | 低 | 目标是"闭环跑通"不是"真 ASR"；Mock 覆盖所有 ASR 不可用路径测试 |
| R5 | 17:00 收口前 pytest 不绿 | 中 | 高 | 每 Block 结束跑一次门禁增量验证，不等到收口 |

---

## 11. 提交规范（AGENTS §8 强制）

| 提交时机 | 范围 | Conventional Commit |
| --- | --- | --- |
| Block 1 结束（09:30） | C3 契约 | `feat(audio): 冻结 C3 降级 REST 契约` |
| Block 2 结束（10:30） | vad.py | `feat(audio): webrtcvad 服务端兜底包装` |
| Block 3 结束（12:00） | utterance + audio.py | `feat(api): POST /api/audio/utterance 降级链路` |
| Block 4 结束（14:00） | 攻击面测试 | `test(audio): 攻击面覆盖 伪造token+越权+审计` |
| Block 5 结束（15:00） | 降级话术 | `feat(audio): 三场景降级话术常量` |
| **收口（17:00）** | README 第 7 节 + 全量 lint/pytest 通过 | `docs: Day 3 A5 收口 README 验收记录 + pytest 全绿` |

**纪律**：push 前 `git pull --rebase`；只提交自己写作域文件；支援别人写的代码随对方的提交合入。

---

## 12. 不做清单（明确排除，避免范围蔓延）

| 项 | 归属 | 原因 |
| --- | --- | --- |
| TEN-VAD WASM 主 VAD | A4 写作域 | 端侧实现，非服务端第二形态 |
| ASR 真云 API 接入 | A1 主实现 | A5 只做兜底，不测真链路 |
| LLM 路由与回复 | Day 6 | Day 3 回复是写死话术 |
| 声纹 CAM++ 实装 | Day 4 | 批次归属，A5 可做 spike 验证 |
| 提示注入测试（FR-TC-07） | Day 9 | 安全测试迭代分批 |
| 反向 WebRTC 外呼 | Day 7 | 功能批次 |
| voiceprints / conversation_threads 表 | Day 4 迁移 | 裁决 B2：Day 1–3 不建 embedding 列 |

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-05 | 初稿：12 节完整推进计划，含精确时间线、Block 分解、操作步骤、交付物表格、自检标准、风险登记、提交规范、不做清单 |
