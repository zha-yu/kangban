"""AC-42 唤醒实测 · 循环音频生成（录音驱动测试口径，见台账 §10.2）。

用项目 TTS 链（edge 兜底，16k/16bit/mono PCM 输出）合成「小伴」「再见」，
拼成循环单元（小伴 → 1.2s 静音 → 再见 → 3s 静音）×30，输出 wav 供 Chrome
假音频设备（--use-file-for-fake-audio-capture）循环喂给端侧 KWS：
待机期 KWS 命中「小伴」→ 进会话 → ASR 识别循环里的「再见」→ L0 挂断回待机
→ 再次命中……自动循环 20+ 轮，延迟样本由 pad 页面端侧打点收集。

运行：$env:PYTHONPATH='.'; uv run python scripts/gen_wakeup_loop.py
产出：.workbuddy/tmp/xiaoban_loop.wav（不入库；媒体大文件禁区合规）
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

from app.audio.tts import EdgeTTSProvider

OUT = Path(".workbuddy/tmp/xiaoban_loop.wav")
SR = 16000
REPEAT = 30
GAP_AFTER_HIT = 1.2  # 「小伴」→「再见」间隔：给会话建立与 ASR 首包留时间
GAP_AFTER_BYE = 3.0  # 「再见」→ 下一轮：给挂断回待机 + KWS 重挂留时间


async def synth_pcm(text: str) -> bytes:
    tts = EdgeTTSProvider(speed=1.0)
    return await tts.synthesize(text)


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SR * seconds)


async def main() -> None:
    xiaoban = await synth_pcm("小伴")
    bye = await synth_pcm("再见")
    unit = xiaoban + silence(GAP_AFTER_HIT) + bye + silence(GAP_AFTER_BYE)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(unit * REPEAT)
    duration = len(unit) * REPEAT / (SR * 2)
    print(f"OK {OUT} 时长 {duration:.0f}s（单元 {len(unit) / (SR * 2):.1f}s × {REPEAT}）")
    print(f"小伴 PCM {len(xiaoban) / (SR * 2):.2f}s / 再见 PCM {len(bye) / (SR * 2):.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
