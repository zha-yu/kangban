"""Mock LLM：固定回复，用于开发调试"""

import asyncio
import random

from . import LLMProvider, LLMResponse


class MockLLMProvider(LLMProvider):
    """Mock LLM：根据关键词返回固定回复，模拟真实延迟"""

    def __init__(self, delay_ms: tuple[int, int] = (300, 800)):
        self.delay_ms = delay_ms
        self._context: list = []

    async def chat(self, text: str, context: list | None = None) -> LLMResponse:
        # 模拟网络延迟
        delay = random.randint(*self.delay_ms) / 1000
        await asyncio.sleep(delay)

        self._context.append({"role": "user", "content": text})

        # 关键词匹配回复
        reply = self._match_reply(text)
        self._context.append({"role": "assistant", "content": reply})

        return LLMResponse(text=reply, emotion=self._detect_emotion(reply))

    async def reset_context(self) -> None:
        self._context.clear()

    def _match_reply(self, text: str) -> str:
        text_lower = text.lower()

        if any(w in text_lower for w in ["你好", "您好", "hello", "hi"]):
            return "你好呀！我是康伴数字人，很高兴见到你。今天过得怎么样？"
        if any(w in text_lower for w in ["天气", "气温", "下雨", "晴天"]):
            return "今天天气挺不错的，适合出去走走。你要注意保暖哦。"
        if any(w in text_lower for w in ["名字", "你是谁", "介绍"]):
            return "我是康伴，你的 AI 语音助手。我可以陪你聊天、回答问题、提醒事项。"
        if any(w in text_lower for w in ["再见", "拜拜", "bye"]):
            return "再见啦！记得想我哦，随时叫我。"
        if any(w in text_lower for w in ["谢谢", "感谢"]):
            return "不客气！能帮到你我很开心。"
        if "?" in text or "？" in text:
            return f"你问了一个好问题。关于「{text[:20]}」，让我想想... 我觉得这个问题很有意思，值得深入探讨。"

        return f"我听到你说「{text[:30]}」了。嗯... 让我思考一下怎么回答你。"

    def _detect_emotion(self, text: str) -> str:
        if any(w in text for w in ["开心", "高兴", "哈哈", "太好了"]):
            return "happy"
        if any(w in text for w in ["难过", "伤心", "唉"]):
            return "sad"
        return "neutral"


class EchoLLMProvider(LLMProvider):
    """纯回声：原样返回，用于测试链路"""

    async def chat(self, text: str, context: list | None = None) -> LLMResponse:
        return LLMResponse(text=f"你刚才说：{text}", emotion="neutral")

    async def reset_context(self) -> None:
        pass
