"""Provider 架构单元测试"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

# 添加 src/asr 到路径（providers 在 asr 包内）
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "asr"))

from providers import ASRResult, LLMResponse
from providers.asr.funasr_ws import FunASRWSProvider
from providers.edge_tts import EdgeTTSProvider
from providers.mock_llm import EchoLLMProvider, MockLLMProvider
from providers.orchestrator import VoiceSessionOrchestrator

# ============================== Mock LLM ==============================


class TestMockLLM:
    @pytest.mark.asyncio
    async def test_chat_returns_response(self):
        llm = MockLLMProvider(delay_ms=(1, 10))
        resp = await llm.chat("你好")
        assert isinstance(resp, LLMResponse)
        assert len(resp.text) > 0
        assert resp.emotion in ("neutral", "happy", "sad")

    @pytest.mark.asyncio
    async def test_keyword_matching(self):
        llm = MockLLMProvider(delay_ms=(1, 10))
        resp = await llm.chat("今天天气怎么样")
        assert "天气" in resp.text

    @pytest.mark.asyncio
    async def test_context_accumulation(self):
        llm = MockLLMProvider(delay_ms=(1, 10))
        await llm.chat("第一句")
        await llm.chat("第二句")
        assert len(llm._context) == 4  # 2 user + 2 assistant

    @pytest.mark.asyncio
    async def test_reset_context(self):
        llm = MockLLMProvider(delay_ms=(1, 10))
        await llm.chat("测试")
        await llm.reset_context()
        assert len(llm._context) == 0

    @pytest.mark.asyncio
    async def test_echo_llm(self):
        llm = EchoLLMProvider()
        resp = await llm.chat("hello")
        assert "hello" in resp.text


# ============================== Edge TTS ==============================


class TestEdgeTTS:
    @pytest.mark.asyncio
    async def test_sample_rate(self):
        tts = EdgeTTSProvider()
        assert tts.sample_rate() == 16000

    @pytest.mark.asyncio
    async def test_synthesize_returns_bytes(self):
        tts = EdgeTTSProvider()
        # Mock _mp3_to_pcm16k 避免真实 MP3 解码
        with patch.object(tts, "_mp3_to_pcm16k", return_value=b"\x00" * 320):
            result = await tts.synthesize("测试文本")
            assert isinstance(result, bytes)

    def test_list_voices(self):
        voices = EdgeTTSProvider.list_voices()
        assert len(voices) > 0
        assert any("zh-CN" in v for v in voices)


# ============================== FunASR Provider ==============================


class TestFunASRWSProvider:
    @pytest.mark.asyncio
    async def test_initial_state(self):
        asr = FunASRWSProvider(url="wss://test:10096")
        assert asr.ws is None
        assert asr._closed is False

    @pytest.mark.asyncio
    async def test_mock_asr(self):
        from providers.funasr import MockASRProvider

        asr = MockASRProvider()
        await asr.start_session()
        await asr.feed_audio(b"\x00" * 320)
        await asr.finish()

        results = []
        async for r in asr.results():
            results.append(r)
            if r.is_final:
                break

        assert len(results) > 0
        assert any(r.is_final for r in results)
        await asr.close()


# ============================== Session Tokens ==============================


class TestWebRTCSessionTokens:
    def test_issue_and_validate(self):
        from providers.kangban_agent import WebRTCSessionTokens

        tokens = WebRTCSessionTokens(ttl_seconds=60)
        token, _expires = tokens.issue("user001", "rev1")
        assert isinstance(token, str)
        assert len(token) > 20

        result = tokens.validate(token, "user001", "rev1")
        assert result is not None

    def test_validate_wrong_user(self):
        from providers.kangban_agent import WebRTCSessionTokens

        tokens = WebRTCSessionTokens(ttl_seconds=60)
        token, _ = tokens.issue("user001", "rev1")
        result = tokens.validate(token, "user002", "rev1")
        assert result is None

    def test_validate_expired(self):
        import time

        from providers.kangban_agent import WebRTCSessionTokens

        tokens = WebRTCSessionTokens(ttl_seconds=1)
        token, _ = tokens.issue("user001", "rev1")
        time.sleep(1.1)
        result = tokens.validate(token, "user001", "rev1")
        assert result is None

    def test_revoke(self):
        from providers.kangban_agent import WebRTCSessionTokens

        tokens = WebRTCSessionTokens(ttl_seconds=60)
        token, _ = tokens.issue("user001", "rev1")
        tokens.revoke(token)
        result = tokens.validate(token, "user001", "rev1")
        assert result is None


# ============================== Orchestrator ==============================


class TestVoiceSessionOrchestrator:
    @pytest.mark.asyncio
    async def test_skip_verify_flow(self):
        """skip_verify 模式：直接进 ASR，不做声纹验证"""
        mock_voiceprint = AsyncMock()
        mock_asr = AsyncMock()

        # results() 需要返回 async generator，不是 coroutine
        async def mock_results():
            yield ASRResult(text="测试", is_final=True, mode="2pass-offline")

        mock_asr.results = mock_results

        orch = VoiceSessionOrchestrator(mock_voiceprint, mock_asr)

        events = []

        async def emit(e):
            events.append(e)

        async def audio_blocks():
            yield np.zeros(960, dtype=np.float32)
            yield np.zeros(960, dtype=np.float32)

        await orch.run(
            audio_blocks(),
            emit,
            authorized=False,
            skip_verify=True,
        )

        # 验证 ASR 被启动
        mock_asr.start_session.assert_called_once()
        # 验证收到了 verification accepted 事件
        assert any(
            e.get("type") == "verification" and e.get("accepted") for e in events
        )
        # 验证收到了 transcript 事件
        assert any(e.get("type") == "transcript" for e in events)
        await orch.asr.close()


# ============================== AudioConverter ==============================


class TestAudioConverter:
    def test_webrtc_to_16k(self):
        from providers.kangban_agent import AudioConverter

        pcm_48k = np.array([100, 200, 300, 400, 500, 600], dtype=np.int16).tobytes()
        result = AudioConverter.webrtc_to_16k(pcm_48k)
        assert isinstance(result, np.ndarray)
        assert len(result) == 2  # 6/3

    def test_to_48k(self):
        from providers.kangban_agent import AudioConverter

        pcm_16k = np.array([100, 200], dtype=np.int16).tobytes()
        result = AudioConverter.to_48k(pcm_16k)
        assert isinstance(result, bytes)
        assert len(result) == 6 * 2  # 2*3 samples, 2 bytes each


# ============================== 集成测试 ==============================


class TestIntegration:
    @pytest.mark.asyncio
    async def test_full_pipeline_mock(self):
        """全 Mock 集成测试：声纹→ASR→LLM→TTS"""
        from providers.kangban_agent import KangbanDigitalHuman

        # Mock 所有 Provider
        mock_voiceprint = AsyncMock()
        mock_voiceprint.verify.return_value = (True, 0.95, "test_user")
        mock_voiceprint.profile_revision.return_value = "v1"

        mock_asr = AsyncMock()
        mock_asr.results.return_value = iter(
            [ASRResult(text="你好", is_final=True, mode="2pass-offline")]
        )

        mock_llm = AsyncMock()
        mock_llm.chat.return_value = LLMResponse(text="你好呀", emotion="happy")

        mock_tts = AsyncMock()
        mock_tts.synthesize.return_value = b"\x00" * 32000

        agent = KangbanDigitalHuman(
            voiceprint=mock_voiceprint,
            asr=mock_asr,
            llm=mock_llm,
            tts=mock_tts,
        )

        # 验证 Agent 初始化
        assert agent.voiceprint is mock_voiceprint
        assert agent.asr is mock_asr
        assert agent.llm is mock_llm
        assert agent.tts is mock_tts


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
