# 康伴数字人 Provider 架构

> 声纹、ASR、LLM、TTS 全部抽象为 Provider，可插拔替换
> 当前默认实现：声纹(SpeakerGate) + ASR(FunASR) + LLM(Mock) + TTS(Edge-TTS)

---

## 架构图

```
┌─────────────────────────────────────────────────────────┐
│                    KangbanDigitalHuman                   │
│  （组合所有 Provider，处理 WebRTC 通话链路）               │
├─────────────────────────────────────────────────────────┤
│  VoiceprintProvider  │  ASRProvider  │  LLMProvider    │
│  （声纹验证）         │  （语音识别）  │  （大模型）      │
├─────────────────────────────────────────────────────────┤
│              TTSProvider（语音合成）                      │
└─────────────────────────────────────────────────────────┘
```

---

## Provider 接口定义

### VoiceprintProvider（声纹验证）

```python
class VoiceprintProvider(ABC):
    async def verify(self, audio_16k: bytes) -> tuple[bool, float, str]
    async def enroll(self, name: str, audio_files: list[bytes]) -> None
    def profile_revision(self) -> str
```

### ASRProvider（语音识别）

```python
class ASRProvider(ABC):
    async def start_session(self) -> None
    async def feed_audio(self, audio_16k: bytes) -> None
    async def finish(self) -> None
    def results(self) -> AsyncIterator[ASRResult]
    async def close(self) -> None
```

### LLMProvider（大模型）

```python
class LLMProvider(ABC):
    async def chat(self, text: str, context: list | None) -> LLMResponse
    async def reset_context(self) -> None
```

### TTSProvider（语音合成）

```python
class TTSProvider(ABC):
    async def synthesize(self, text: str, emotion: str) -> bytes  # 16kHz PCM
    def sample_rate(self) -> int
```

---

## 默认实现

| Provider | 实现类 | 说明 |
|---------|--------|------|
| 声纹 | `KangbanVoiceprintProvider` | 复用现有 `SpeakerGate` |
| ASR | `FunASRProvider` | WebSocket 连接 FunASR Docker |
| LLM | `MockLLMProvider` | 关键词匹配固定回复，模拟延迟 |
| TTS | `EdgeTTSProvider` | 微软 Edge-TTS，免费无需 Key |

---

## 替换示例

### 替换 LLM 为 OpenAI

```python
from openai import AsyncOpenAI
from providers import LLMProvider, LLMResponse


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self._context = []

    async def chat(self, text: str, context=None):
        self._context.append({"role": "user", "content": text})
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=self._context,
        )
        reply = resp.choices[0].message.content
        self._context.append({"role": "assistant", "content": reply})
        return LLMResponse(text=reply, emotion="neutral")

    async def reset_context(self):
        self._context.clear()
```

### 替换 TTS 为 Azure

```python
class AzureTTSProvider(TTSProvider):
    def __init__(self, key: str, region: str):
        self.key = key
        self.region = region

    async def synthesize(self, text: str, emotion: str) -> bytes:
        # 调用 Azure Speech SDK
        ...

    def sample_rate(self):
        return 16000
```

---

## 快速开始

```bash
# 1. 安装依赖
cd asr
uv sync

# 2. 启动信令服务
uv run signaling

# 3. 启动数字人 Agent（默认 Provider 组合）
uv run voice-agent

# 4. 浏览器访问 https://127.0.0.1:8765
#    登录 web001，呼叫 agent001（被叫）
#    或在 Agent 终端输入 web001 回车（主叫）
```

### 双向呼叫

| 场景 | 发起方 | 操作 |
|------|--------|------|
| 网页 → Agent | 浏览器 | 输入 `agent001`，点击"发起呼叫" |
| Agent → 网页 | Agent 终端 | 输入 `web001` 回车 |

Agent 主动呼叫时，网页会弹出来电弹窗，用户点击【接听】后建立连接。

---

## 文件结构

```
asr/src/asr/
├── providers/
│   ├── __init__.py              # 抽象接口 + 数据模型
│   ├── orchestrator.py          # 会话编排器（声纹→ASR→LLM→TTS）
│   ├── voiceprint/
│   │   ├── __init__.py
│   │   └── sherpa_gate.py       # Sherpa-ONNX 声纹实现（独立）
│   ├── asr/
│   │   ├── __init__.py
│   │   └── funasr_ws.py         # FunASR WebSocket 实现（独立）
│   ├── voiceprint.py            # 声纹兼容层
│   ├── funasr.py                # ASR 兼容层
│   ├── mock_llm.py              # Mock LLM 实现
│   ├── edge_tts.py              # Edge-TTS 实现
│   └── kangban_agent.py         # 数字人整合 + WebRTC 封装
├── agent.py                     # WebRTC Agent 入口（轻量）
├── voice.py                     # CLI 工具（只保留 microphone + main）
├── webapp.py                    # 浏览器网关（已迁移到 Provider）
└── signaling.py                 # 信令服务
```
