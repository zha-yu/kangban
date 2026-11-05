#!/usr/bin/env python3
"""FunASR runtime 真链路探针（spike 用，不进 app/）。

用途：验证 `docker-compose.yml` 里的 funasr 服务真的能出字，并量首包耗时。
它只做「喂 PCM → 打印 partial/final → 报首包 ms」这一件事，不承担适配器职责
（`app/audio/asr.py` 归 A1 asr-core，本文件不 import 它，免得把探针结论绑死在实现上）。

用法：

    docker compose up -d funasr
    uv run --with websockets python spikes/funasr/ws_probe.py
    uv run --with websockets python spikes/funasr/ws_probe.py --wav my_16k.wav --text "今天天气怎么样"
    uv run --with websockets python spikes/funasr/ws_probe.py --url wss://127.0.0.1:7002 --insecure

口径（与 app/audio 的帧契约一致，不可协商）：

- 入口音频必须是 **16 kHz / 16 bit / 单声道 PCM**；
- **不做重采样**——格式不对直接报错退出。重采样是 A3/A5 的活，探针里偷偷补上会把
  「重采样写错了」误记成「FunASR 不认音频」，这条 spike 就白做了。

协议（FunASR runtime 2pass 流式，参考 spikes/reference/asr）：

1. 先发一条 JSON 配置（mode/chunk_size/audio_fs/wav_format/is_speaking=True）；
2. 再按帧发二进制 PCM；
3. 说话结束发 `{"is_speaking": false, "is_end": true}`；
4. 服务端回 JSON：`2pass-online`（partial，is_final=false）、`2pass-offline`（final，is_final=true）。

退出码：0 = 拿到非空 final；2 = 超时没等到 final；3 = final 文本为空。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
import time
import wave
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parents[2]
# 仓库内自带的 16 k 中文示例音频（来自 spikes/reference/asr，避免探针自带二进制大文件）。
DEFAULT_WAV = REPO_ROOT / "spikes/reference/asr/examples/asr_example_zh.wav"

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16 bit
CHANNELS = 1


def read_pcm(path: Path) -> bytes:
    """读 WAV 并返回裸 PCM；格式不合契约就报错，不猜、不转。"""
    if not path.is_file():
        raise SystemExit(f"[x] 找不到音频文件：{path}")
    with wave.open(str(path), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        if (channels, width, rate) != (CHANNELS, SAMPLE_WIDTH, SAMPLE_RATE):
            raise SystemExit(
                f"[x] {path.name} 是 {rate} Hz / {width * 8} bit / {channels} ch，"
                f"需要 {SAMPLE_RATE} Hz / {SAMPLE_WIDTH * 8} bit / {CHANNELS} ch。"
                " 请先用 ffmpeg 转：ffmpeg -i in.wav -ar 16000 -ac 1 -c:a pcm_s16le out.wav"
            )
        return wav.readframes(wav.getnframes())


def normalize(text: str) -> str:
    """比对用：去掉空白与中英文标点，只比字面。"""
    drop = set(" \t\r\n，。、！？；：,.!?;:\"'“”‘’（）()【】[]")
    return "".join(ch for ch in text if ch not in drop)


def make_ssl_context(url: str, insecure: bool) -> ssl.SSLContext | None:
    if not url.startswith("wss://"):
        return None
    ctx = ssl.create_default_context()
    if insecure:
        # 镜像内是自签证书，本地联调才关校验；生产必须换成可信证书。
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def run(args: argparse.Namespace) -> int:
    wav_path = Path(args.wav)
    pcm = read_pcm(wav_path)
    audio_ms = len(pcm) / SAMPLE_WIDTH / SAMPLE_RATE * 1000
    chunk_bytes = int(SAMPLE_RATE * SAMPLE_WIDTH * args.chunk_ms / 1000)
    frames = [pcm[i : i + chunk_bytes] for i in range(0, len(pcm), chunk_bytes)]

    print(f"[i] url       : {args.url}")
    print(f"[i] wav       : {wav_path.name}  ({audio_ms / 1000:.2f} s, {len(frames)} 帧 × {args.chunk_ms} ms)")
    if args.text:
        print(f"[i] 期望文本  : {args.text}")

    first_partial_ms: float | None = None
    final_ms: float | None = None
    final_text = ""
    partial_count = 0
    t0 = 0.0

    async def receiver() -> None:
        nonlocal first_partial_ms, final_ms, final_text, partial_count
        async for raw in ws:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                print(f"[?] 非 JSON 帧：{raw!r}")
                continue
            if data.get("error"):
                print(f"[x] 服务端报错：{data['error']}")
                return
            text = (data.get("text") or "").strip()
            mode = data.get("mode") or ""
            is_final = bool(data.get("is_final"))
            if text and first_partial_ms is None:
                first_partial_ms = elapsed_ms
            if not is_final:
                partial_count += 1
                print(f"[partial +{elapsed_ms:6.0f} ms] {mode:14s} {text}")
                continue
            final_ms = elapsed_ms
            final_text = text
            print(f"[final   +{elapsed_ms:6.0f} ms] {mode:14s} {text}")
            if "offline" in mode:
                return

    ssl_ctx = make_ssl_context(args.url, args.insecure)
    async with websockets.connect(
        args.url,
        ssl=ssl_ctx,
        subprotocols=["binary"],
        ping_interval=None,
        max_size=2**20,
        open_timeout=args.timeout,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "mode": "2pass",
                    "chunk_size": [5, 10, 5],
                    "chunk_interval": 10,
                    "encoder_chunk_look_back": 4,
                    "decoder_chunk_look_back": 1,
                    "audio_fs": SAMPLE_RATE,
                    "wav_format": "pcm",
                    "wav_name": "ws_probe",
                    "is_speaking": True,
                    "itn": True,
                }
            )
        )
        recv_task = asyncio.create_task(receiver())
        t0 = time.perf_counter()
        for frame in frames:
            await ws.send(frame)
            if args.realtime:
                # 实时节奏推流，partial 才是「真的流式」而不是一口气灌完再看结果。
                await asyncio.sleep(args.chunk_ms / 1000)
        await ws.send(json.dumps({"is_speaking": False, "is_end": True}))

        try:
            await asyncio.wait_for(recv_task, timeout=args.timeout)
        except TimeoutError:
            recv_task.cancel()
            print(f"[x] {args.timeout}s 内没等到 final（拿到 {partial_count} 个 partial）")
            return 2

    print("---- 汇总 ----")
    print(f"音频        : {audio_ms / 1000:.2f} s")
    print(f"partial 数  : {partial_count}")
    print(f"首包(partial): {'—' if first_partial_ms is None else f'{first_partial_ms:.0f} ms'}")
    print(f"final       : {'—' if final_ms is None else f'{final_ms:.0f} ms'}")
    print(f"final 文本  : {final_text or '（空）'}")
    if args.text:
        same = normalize(final_text) == normalize(args.text)
        print(f"与期望一致  : {'是' if same else '否'}")
    if not final_text:
        print("[x] final 文本为空 —— 探针不认这种情况为「跑通」")
        return 3
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="FunASR runtime 真链路探针（16k PCM → partial/final + 首包 ms）")
    p.add_argument("--url", default="ws://127.0.0.1:7002", help="FunASR runtime 地址（默认 ws://127.0.0.1:7002）")
    p.add_argument("--wav", default=str(DEFAULT_WAV), help="16 kHz / 16 bit / 单声道 WAV")
    p.add_argument("--text", default="", help="期望文本，仅用于人工比对，不参与识别")
    p.add_argument("--chunk-ms", type=int, default=60, help="推流帧长，默认 60 ms（=1920 字节，与 app/audio 一致）")
    p.add_argument("--realtime", dest="realtime", action="store_true", default=True, help="按实时节奏推流（默认开）")
    p.add_argument("--fast", dest="realtime", action="store_false", help="尽可能快推完，只看总耗时")
    p.add_argument("--insecure", action="store_true", help="wss 时跳过证书校验（自签证书联调用）")
    p.add_argument("--timeout", type=float, default=30.0, help="等 final 的超时秒数，默认 30")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except OSError as exc:
        print(f"[x] 连不上 {args.url}：{exc}")
        print("    先确认容器已起且 healthy：docker compose up -d funasr && docker compose ps funasr")
        return 1


if __name__ == "__main__":
    sys.exit(main())
