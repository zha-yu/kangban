"""Provider 抽象层：声纹、ASR、LLM、TTS 全部可插拔替换。"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Optional

# ============================== 数据模型 ==============================


@dataclass
class ASRResult:
    text: str
    is_final: bool
    mode: str = ""  # online / 2pass-offline
    confidence: float = 0.0


@dataclass
class LLMResponse:
    text: str
    emotion: str = "neutral"  # 情感标记，用于 TTS 语调
    is_final: bool = True


# ============================== 声纹验证 ==============================


class VoiceprintProvider(ABC):
    """声纹验证提供者抽象"""

    @abstractmethod
    async def verify(self, audio_16k: bytes) -> tuple[bool, float, str]:
        """
        验证声纹
        :param audio_16k: 16kHz mono int16 PCM 字节流
        :return: (是否通过, 置信度得分, 用户标识/名称)
        """
        ...

    @abstractmethod
    async def enroll(self, name: str, audio_files: list[bytes]) -> None:
        """录入声纹"""
        ...

    @abstractmethod
    def profile_revision(self) -> str:
        """当前声纹档案版本号，用于 token 失效判断"""
        ...


# ============================== ASR ==============================


class ASRProvider(ABC):
    """语音识别提供者抽象"""

    @abstractmethod
    async def start_session(self) -> None:
        """初始化识别会话"""
        ...

    @abstractmethod
    async def feed_audio(self, audio_16k: bytes) -> None:
        """喂入音频块"""
        ...

    @abstractmethod
    async def finish(self) -> None:
        """标记音频结束，等待最终结果"""
        ...

    @abstractmethod
    def results(self) -> AsyncIterator[ASRResult]:
        """异步迭代识别结果"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭会话，释放资源"""
        ...


# ============================== LLM ==============================


class LLMProvider(ABC):
    """大语言模型提供者抽象"""

    @abstractmethod
    async def chat(self, text: str, context: list | None = None) -> LLMResponse:
        """
        发送用户文本，获取回复
        :param text: ASR 识别出的用户文本
        :param context: 对话历史（可选）
        """
        ...

    @abstractmethod
    async def reset_context(self) -> None:
        """清空对话上下文"""
        ...


# ============================== TTS ==============================


class TTSProvider(ABC):
    """语音合成提供者抽象"""

    @abstractmethod
    async def synthesize(self, text: str, emotion: str = "neutral") -> bytes:
        """
        文本转语音
        :param text: 要合成的文本
        :param emotion: 情感标记（影响语调）
        :return: 16kHz mono int16 PCM 字节流
        """
        ...

    @abstractmethod
    def sample_rate(self) -> int:
        """输出音频采样率"""
        ...
