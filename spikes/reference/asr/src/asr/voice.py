"""声纹+ASR 命令行工具（CLI only，Provider 实现在 providers/ 目录）"""

import argparse
import asyncio
import json
from pathlib import Path

import numpy as np

from .providers.asr import FunASRWSProvider
from .providers.voiceprint import SherpaVoiceprintProvider

RATE = 16000
MODEL_DIR = (
    Path(__file__).resolve().parents[2] / "funasr-runtime-resources/models/sherpa-onnx"
)


async def microphone():
    import sounddevice as sd

    with sd.InputStream(
        samplerate=RATE, channels=1, dtype="float32", blocksize=960
    ) as stream:
        while True:
            data, overflow = await asyncio.to_thread(stream.read, 960)
            if overflow:
                raise RuntimeError("Microphone overflow")
            yield data[:, 0]


def main():
    parser = argparse.ArgumentParser(description="声纹验证后调用 FunASR 流式识别")
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    enroll = sub.add_parser("enroll")
    enroll.add_argument("--name", required=True)
    enroll.add_argument("--wav", type=Path, nargs="+", required=True)
    enroll.add_argument("--profile", type=Path, default=Path("data/speaker.json"))

    listen = sub.add_parser("listen")
    listen.add_argument("--profile", type=Path, default=Path("data/speaker.json"))
    listen.add_argument("--url", default="wss://127.0.0.1:10096")
    listen.add_argument("--threshold", type=float, default=0.55)
    listen.add_argument("--mode", choices=["online", "2pass"], default="2pass")

    args = parser.parse_args()
    voiceprint = SherpaVoiceprintProvider(
        model_dir=args.model_dir, threshold=args.threshold
    )

    if args.command == "enroll":
        # 读取 WAV 文件为 bytes
        audio_files = []
        for wav_path in args.wav:
            audio_files.append(wav_path.read_bytes())
        asyncio.run(voiceprint.enroll(args.name, audio_files))
        print(f"声纹已保存：{args.profile}")

    elif args.command == "listen":
        asr = FunASRWSProvider(url=args.url, mode=args.mode)

        async def listen_once():
            await asr.start_session()
            print("已连接 FunASR，开始监听...")

            async def feed():
                async for block in microphone():
                    pcm = (np.clip(block, -1, 1) * 32767).astype("<i2").tobytes()
                    await asr.feed_audio(pcm)

            feed_task = asyncio.create_task(feed())

            try:
                async for result in asr.results():
                    print(
                        json.dumps(
                            {
                                "type": "transcript",
                                "text": result.text,
                                "is_final": result.is_final,
                                "mode": result.mode,
                            },
                            ensure_ascii=False,
                        )
                    )
                    if result.is_final and "offline" in result.mode:
                        break
            finally:
                feed_task.cancel()
                await asr.close()

        asyncio.run(listen_once())


if __name__ == "__main__":
    main()
