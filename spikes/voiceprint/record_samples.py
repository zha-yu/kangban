"""声纹互测现场录音脚本（O3 声纹调优 · Day 8 Step 9 / H13）。

交互式采集规范样本：16kHz 单声道 WAV，文件名 <人名>_<序号>.wav，
落盘 spikes/voiceprint/samples/（.gitignore，原始录音不入库可随时删除）。

运行（--with 临时安装 sounddevice/numpy，不动 pyproject）：
    uv run --with sounddevice --with numpy python spikes/voiceprint/record_samples.py

流程：输入人名 → 输入段数（默认 3）→ 每段倒计时 3 秒 → 录音（带进度条与
电平提示）→ 自动保存；录完一人可继续录下一位，Ctrl+C 或回车空行结束。
备选：如果你用 Windows 录音机录了 m4a，用本脚本转换：
    uv run python spikes/voiceprint/record_samples.py convert <m4a路径> <人名>
    （需系统装有 ffmpeg；转换后自动重采样到 16kHz 单声道）
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
TARGET_RATE = 16000
DEFAULT_TAKES = 3
TAKE_SECONDS = 6
COUNTDOWN = 3


def _resample_to_16k_mono(data: "object", rate: int) -> tuple[bytes, int]:
    """任意声道/采样率 → 16kHz 单声道 PCM16（numpy 线性插值重采样）。"""
    import numpy as np

    audio = np.asarray(data, dtype=np.float32)
    if audio.ndim == 2:  # 多声道取第一声道
        audio = audio[:, 0]
    if rate != TARGET_RATE:
        n_in = len(audio)
        n_out = int(round(n_in * TARGET_RATE / rate))
        x_in = np.linspace(0.0, 1.0, n_in, endpoint=False)
        x_out = np.linspace(0.0, 1.0, n_out, endpoint=False)
        audio = np.interp(x_out, x_in, audio)
    peak = float(np.max(np.abs(audio))) or 1.0
    if peak < 0.98:  # 归一化到合适电平，保留余量
        audio = audio * (0.85 / peak)
    return (audio * 32767).astype("<i2").tobytes(), TARGET_RATE


def _save_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def record_person() -> None:
    import sounddevice as sd

    name = input("\n人名（拼音/字母，如 zhangsan）：").strip()
    if not name or "_" in name:
        print("[错误] 人名必填，且不能含下划线（文件名按 <人名>_<序号>.wav 拆分）")
        return
    takes_raw = input(f"段数（回车默认 {DEFAULT_TAKES}）：").strip()
    takes = int(takes_raw) if takes_raw.isdigit() and int(takes_raw) > 0 else DEFAULT_TAKES

    print(
        f"\n开始录制 {name}：{takes} 段 × {TAKE_SECONDS} 秒。\n"
        "提示：每段之间请停顿重说（不要连读）；正常音量、正常语速即可。"
    )
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    # 采样率探测：优先 16k 直采，不支持则设备默认采样率录后重采样
    dev_rate = int(sd.query_devices(None, kind="input")["default_samplerate"])
    use_rate = TARGET_RATE
    try:
        sd.check_input_settings(samplerate=TARGET_RATE)
    except Exception:
        use_rate = dev_rate
        print(f"[提示] 设备不支持 {TARGET_RATE}Hz 直采，按 {dev_rate}Hz 录后自动重采样")

    for i in range(1, takes + 1):
        import time

        for s in range(COUNTDOWN, 0, -1):
            print(f"第 {i}/{takes} 段，{s} 秒后开始...", flush=True)
            time.sleep(1)

        data = sd.rec(
            int(TAKE_SECONDS * use_rate), samplerate=use_rate, channels=1, dtype="float32"
        )
        print("录音中", end="", flush=True)
        elapsed = 0
        while sd.get_stream().active:
            time.sleep(0.5)
            elapsed += 0.5
            rms = float((data[: int(elapsed * use_rate)] ** 2).mean() ** 0.5) if elapsed > 0.5 else 0.0
            bar = "#" * min(int(rms * 60), 30)
            print(f"\r录音中 [{bar if bar else '（检测不到声音，检查麦克风）'}] {elapsed:.0f}s/{TAKE_SECONDS}s", end="", flush=True)
        sd.wait()
        print()

        pcm, rate = _resample_to_16k_mono(data, use_rate)
        path = SAMPLES_DIR / f"{name}_{i}.wav"
        _save_wav(path, pcm, rate)
        print(f"  已保存 {path}")


def convert_m4a(src: str, name: str) -> None:
    """m4a/mp3 等常见格式 → 16k 单声道 WAV（依赖系统 ffmpeg）。"""
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        print("[错误] 未安装 ffmpeg。请先安装（winget install ffmpeg）或改用本脚本直接录音")
        sys.exit(1)
    source = Path(src)
    if not source.is_file():
        print(f"[错误] 文件不存在：{source}")
        sys.exit(1)
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    existing = len(list(SAMPLES_DIR.glob(f"{name}_*.wav")))
    target = SAMPLES_DIR / f"{name}_{existing + 1}.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(source), "-ar", str(TARGET_RATE), "-ac", "1", str(target)],
        check=True,
        capture_output=True,
    )
    print(f"已转换保存 {target}")


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "convert":
        if len(sys.argv) < 4:
            print("用法：record_samples.py convert <m4a路径> <人名>")
            sys.exit(1)
        convert_m4a(sys.argv[2], sys.argv[3])
        return

    try:
        import sounddevice as sd  # noqa: F401

        devices = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        if not devices:
            print("[错误] 没有检测到麦克风输入设备")
            sys.exit(1)
        default = sd.query_devices(None, kind="input")
        print(f"输入设备：{default['name']}（默认 {int(default['default_samplerate'])}Hz）")
        while True:
            record_person()
            if input("\n继续录下一位？（回车继续，输入 q 退出）：").strip().lower() == "q":
                break
        print(f"\n完成。样本在 {SAMPLES_DIR}，确认后运行阈值扫描：\n"
              "  uv run python spikes/voiceprint/threshold_sweep.py")
    except KeyboardInterrupt:
        print("\n已中断（已保存的文件不受影响）")


if __name__ == "__main__":
    main()
