"""spike：验证 edge-tts → ffmpeg 子进程 → 16k/16bit/单声道 PCM 的兜底链路。

为什么要有这个 spike（AGENTS.md §3「新依赖先写进 spikes/ 验证再合并」）：
    T1.4 的结论是**零新增 Python 依赖** —— mp3 解码不引 pydub，直接用 ffmpeg 子进程
    ``["ffmpeg","-i","pipe:0","-f","s16le","-ar","16000","-ac","1","pipe:1"]``。
    所以本次要验的不是「新库能不能装」，而是「ffmpeg 这条本地链路在目标机器上到底通不通」，
    以及 ffmpeg 缺失时 ``TTSUnavailable("ffmpeg not found")`` 是否是可预期的那条降级路。

本机实测（2026-11-05，写进报告、不回填 README，README 由 Lead 统一改）：
    - **WSL 里没有 ffmpeg**（``which ffmpeg`` 空）；
    - **Windows 侧有**：``C:\\ProgramData\\chocolatey\\bin\\ffmpeg.EXE``（ffmpeg 8.1.2 essentials），
      而 ``uv run`` 起的 pytest / 脚本是 Windows 进程，``shutil.which("ffmpeg")`` 能找到它。
    - 因此 16k PCM 这条链路在本机可用；容器内是否可用取决于镜像里是否装 ffmpeg（本 spike 不覆盖）

三种模式：
    ``--mode sine``（默认兜底）：用 ffmpeg 的 lavfi 正弦源现造一段 mp3，再走**与生产完全相同**的
        `app.audio.tts.mp3_to_pcm()` 转码 → 只验 ffmpeg 链路，不联网、不需要 edge-tts。
    ``--mode edge``：走 `EdgeTTSProvider.synthesize()` 的公开路径，真联网合成（需要装了 edge-tts）。
    ``--mode auto``：装了 edge-tts 就真合成，没装就**大声告警**后落到 sine —— 告警里写清楚
        「这次没验到 edge-tts 那一段」，避免把 sine 的结果当成云端链路已验证。

跑法（仓库根目录）：
    uv run python spikes/edge_tts/spike_synthesize.py                # auto
    uv run python spikes/edge_tts/spike_synthesize.py --mode sine --seconds 1.5
    uv run python spikes/edge_tts/spike_synthesize.py --mode edge --text "你好，我是康伴"

退出码：0 = 产出了非空 PCM 且 wav 回读时长 > 0；1 = 前置不满足或链路失败。
产物 wav 落在 `spikes/edge_tts/out/`（.gitignore §9 已忽略 `spikes/**/out/`：媒体文件不进提交），
路径打印在最后一行。仓库外写不动的原因见 `_default_out_dir()` 的 docstring。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

# 脚本直接跑时 sys.path[0] 是 spikes/edge_tts/，把仓库根补进来才能 import app.*
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.audio.tts import (  # noqa: E402 - 必须在 sys.path 补丁之后导入
    EDGE_DEFAULT_VOICE,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    EdgeTTSProvider,
    TTSUnavailable,
    mp3_to_pcm,
)

DEFAULT_TEXT = "你好，我是康伴。今天天气不错，要不要出去走走？"
DEFAULT_SINE_SECONDS = 1.0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="edge-tts / ffmpeg → 16k PCM 链路验证")
    parser.add_argument("--mode", choices=("auto", "edge", "sine"), default="auto")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--voice", default=EDGE_DEFAULT_VOICE)
    parser.add_argument("--speed", type=float, default=1.0, help="语速倍率（1.0=原速）")
    parser.add_argument(
        "--seconds", type=float, default=DEFAULT_SINE_SECONDS, help="sine 模式音频时长"
    )
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件名或绝对路径")
    parser.add_argument(
        "--out",
        default=None,
        help="wav 输出路径（默认 spikes/edge_tts/out/out.wav，已被 .gitignore 忽略）",
    )
    return parser.parse_args(argv)


def _default_out_dir() -> Path | None:
    """spike 产物目录：`spikes/edge_tts/out/`（`.gitignore` §9 已忽略 `spikes/**/out/`）。

    为什么不写系统临时目录：本机从 WSL 拉起 Windows Python 时，**仓库外一律写不动**——
    `os.mkdir(%LOCALAPPDATA%\\Temp\\x)` 直接 `PermissionError: [WinError 5]`，
    `~/.cache/tts-spike/` 同样被拒；而 `tempfile.gettempdir()` 又因为 `TEMP` 是 8.3 短名
    回退到当前工作目录（仓库根），于是第一版把 `tts-spike-xxxx/out.wav` 写进了仓库根。
    所以默认落在**仓库内但已被 .gitignore 忽略**的 `spikes/**/out/`：既不进提交，也能写。
    需要写到别处就传 `--out`。
    """
    out_dir = _REPO_ROOT / "spikes" / "edge_tts" / "out"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - 环境问题，如实报错
        print(f"[WARN] 无法创建 {out_dir}：{exc}")
        return None
    return out_dir


def _make_sine_mp3(ffmpeg_bin: str, seconds: float, out_path: Path) -> bytes:
    """用 ffmpeg 的 lavfi 正弦源造一段真 mp3，充当「edge-tts 的输出」。"""
    subprocess.run(  # noqa: S603 - argv 全为常量，输入只有本地生成的测试音频
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(out_path),
        ],
        capture_output=True,
        check=True,
    )
    return out_path.read_bytes()


async def _synth_via_edge(text: str, voice: str, speed: float) -> bytes:
    """走生产公开路径（含观测埋点），返回 16k PCM。"""
    return await EdgeTTSProvider(voice=voice, speed=speed).synthesize(text)


def _write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(PCM_CHANNELS)
        wav_file.setsampwidth(PCM_SAMPLE_WIDTH)
        wav_file.setframerate(PCM_SAMPLE_RATE)
        wav_file.writeframes(pcm)


def _wav_duration(path: Path) -> float:
    """回读 wav 自证：不只看内存里的字节数，磁盘上的文件也得能被标准库读出时长。"""
    with wave.open(str(path), "rb") as wav_file:
        return wav_file.getnframes() / wav_file.getframerate()


def _pcm_duration(pcm: bytes) -> float:
    return len(pcm) / (PCM_SAMPLE_RATE * PCM_SAMPLE_WIDTH * PCM_CHANNELS)


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认按 ANSI 代码页（cp936）编码 stdout，中文在 UTF-8 终端里会变乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = _parse_args(argv)
    ffmpeg_path = args.ffmpeg if Path(args.ffmpeg).is_file() else shutil.which(args.ffmpeg)
    print(f"[info] ffmpeg       = {ffmpeg_path or '（未找到）'}")

    if ffmpeg_path is None:
        print("[FAIL] ffmpeg 不在 PATH 上：容器/机器需先装 ffmpeg，否则 TTS 只能走 Mock。")
        print("       预期行为见 app/audio/tts.py：TTSUnavailable('ffmpeg not found')。")
        return 1

    has_edge = importlib.util.find_spec("edge_tts") is not None
    mode = args.mode
    if mode == "auto":
        mode = "edge" if has_edge else "sine"
        if not has_edge:
            print(
                "[WARN] 未安装 edge-tts（本仓零新增依赖，不装也应该能跑），本次只验 ffmpeg 链路；"
            )
            print(
                "       **这不会验证 edge-tts → mp3 那一段**，需要时手动装 edge-tts 后再跑 --mode edge。"
            )

    out_dir = _default_out_dir()
    if args.out is not None:
        out_path = Path(args.out).resolve()
        work_dir = out_path.parent
        work_dir.mkdir(parents=True, exist_ok=True)
    elif out_dir is not None:
        work_dir = out_dir
        out_path = work_dir / "out.wav"
    else:
        print("[FAIL] 拿不到产物目录：请用 --out 指定 wav 路径")
        return 1
    print(f"[info] 产物目录     = {work_dir}")

    started = time.perf_counter()

    try:
        if mode == "edge":
            print(f"[info] mode=edge   text={args.text!r} voice={args.voice}")
            pcm = asyncio.run(_synth_via_edge(args.text, args.voice, args.speed))
            mp3_bytes = -1  # 公开路径不暴露中间 mp3，这里如实标注「未取」
            convert_ms = (time.perf_counter() - started) * 1000.0
        else:
            print(f"[info] mode=sine   seconds={args.seconds}（lavfi 440Hz → mp3 → ffmpeg 转码）")
            mp3 = _make_sine_mp3(ffmpeg_path, args.seconds, work_dir / "sine.mp3")
            convert_started = time.perf_counter()
            pcm = mp3_to_pcm(mp3, ffmpeg_bin=ffmpeg_path)
            convert_ms = (time.perf_counter() - convert_started) * 1000.0
            mp3_bytes = len(mp3)
    except TTSUnavailable as exc:
        print(f"[FAIL] TTSUnavailable: {exc}")
        return 1
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"")[-300:].decode("utf-8", "replace").strip()
        print(f"[FAIL] ffmpeg 退出码 {exc.returncode}：{tail}")
        return 1

    total_ms = (time.perf_counter() - started) * 1000.0
    if not pcm:
        print("[FAIL] 拿到的 PCM 为空")
        return 1

    _write_wav(out_path, pcm)
    wav_seconds = _wav_duration(out_path)
    pcm_seconds = _pcm_duration(pcm)

    print(f"[info] mp3          = {mp3_bytes if mp3_bytes >= 0 else '未取（edge 公开路径）'} B")
    print(
        f"[info] pcm          = {len(pcm)} B = {pcm_seconds:.3f} s "
        f"@{PCM_SAMPLE_RATE}Hz / {PCM_SAMPLE_WIDTH * 8}bit / {PCM_CHANNELS}ch"
    )
    print(f"[info] 转码耗时      = {convert_ms:.1f} ms（总 {total_ms:.1f} ms）")
    print(f"[info] wav 回读      = {wav_seconds:.3f} s → {out_path}")

    if wav_seconds <= 0:
        print("[FAIL] wav 回读时长为 0")
        return 1
    print(f"[ok] 链路通过：mode={mode}，wav 时长 {wav_seconds:.3f} s > 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
