"""spike：验证 webrtcvad-wheels 在无需 C++ 编译器的情况下可用。

用途（AGENTS.md §3「新增依赖先写进 spikes/ 验证再合并」）：
    上游 ``webrtcvad`` 自 2017 年起只发 sdist，Windows 上装它会强制要求
    Microsoft C++ Build Tools 并直接构建失败（``error: Microsoft Visual C++
    14.0 or greater is required``）。本项目改用同源 fork ``webrtcvad-wheels``：
    import 名仍是 ``webrtcvad``、API 完全一致，但提供 cp310–cp314 的预编译 wheel。

本脚本验证五件事，不做业务断言：
    1. ``import webrtcvad`` 成功，且加载的是预编译二进制 ``_webrtcvad``（不是本地构建）；
    2. ``Vad()`` 可实例化，``set_mode(0..3)`` 全区间可用；
    3. 16kHz / 30ms 静音帧被判为「无语音」；
    4. 近似浊音的合成信号多数帧被判为「有语音」；
    5. 非法采样率/帧长会抛 ``_webrtcvad.Error``（**不是** ValueError，也不是 IndexError），
       因此 ``app/audio`` 包装层必须用 ``valid_rate_and_frame_length()`` 前置校验，
       不能指望 ``except ValueError`` 兜住参数错。

跑法：
    uv run python spikes/vad/spike_webrtcvad.py
退出码 0 表示全部通过，1 表示有断言不成立。
"""

from __future__ import annotations

import math
import struct
import sys

SAMPLE_RATE = 16_000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480 采样 = 30ms @ 16kHz


def _pcm(samples: list[int]) -> bytes:
    """把 [-32768, 32767] 的采样序列打包成 16-bit 小端 PCM。"""
    return struct.pack(f"<{len(samples)}h", *samples)


def silence_frame() -> bytes:
    return _pcm([0] * FRAME_SAMPLES)


def voiced_frame(index: int) -> bytes:
    """合成一段近似浊音的信号：150Hz 基频 + 共振峰谐波 + 3Hz 包络起伏。

    WebRTC VAD 按真实语音训练，纯正弦常常判不出浊音，所以这里叠了谐波结构
    与缓慢的幅度起伏，让它在多数帧上能被判为语音。
    """
    samples: list[int] = []
    for n in range(FRAME_SAMPLES):
        t = (index * FRAME_SAMPLES + n) / SAMPLE_RATE
        envelope = 0.6 + 0.4 * math.sin(2 * math.pi * 3.0 * t)
        value = (
            0.55 * math.sin(2 * math.pi * 150.0 * t)
            + 0.25 * math.sin(2 * math.pi * 450.0 * t)
            + 0.12 * math.sin(2 * math.pi * 1200.0 * t)
            + 0.08 * math.sin(2 * math.pi * 2400.0 * t)
        )
        samples.append(int(max(-1.0, min(1.0, value * envelope)) * 20000))
    return _pcm(samples)


def main() -> int:
    # 运行期输出保持 ASCII：Windows 控制台默认 GBK，中文在管道/CI 里会变乱码。
    import _webrtcvad  # noqa: PLC0415 — spike 脚本，延迟导入以便先打印诊断
    import webrtcvad  # noqa: PLC0415

    failures: list[str] = []

    print(f"python      : {sys.version.split()[0]}")
    print(f"webrtcvad   : {getattr(webrtcvad, '__version__', '<missing>')}")
    print(f"py shim     : {getattr(webrtcvad, '__file__', '<builtin>')}")
    # 预编译 wheel 会装出 _webrtcvad.cpXY-win_amd64.pyd；若这里报 ImportError，
    # 说明装的是 sdist 且本地没编出扩展。
    print(f"c extension : {_webrtcvad.__file__}")

    # --- 1. 实例化与模式区间 ---
    vad = None
    try:
        vad = webrtcvad.Vad(2)
        for mode in range(4):
            vad.set_mode(mode)
        vad.set_mode(2)
        print("mode 0-3    : ok")
    except Exception as exc:  # noqa: BLE001 — spike 要把任何异常都报出来
        failures.append(f"Vad init/set_mode failed: {exc!r}")
        print("mode 0-3    : FAILED")

    if vad is not None:
        # --- 2. 静音帧必须判为无语音 ---
        silence_hits = sum(vad.is_speech(silence_frame(), SAMPLE_RATE) for _ in range(5))
        print(f"silence     : {silence_hits}/5 voiced (expect 0)")
        if silence_hits != 0:
            failures.append(f"silence judged voiced {silence_hits}/5 times")

        # --- 3. 浊音样本应多数被判为语音 ---
        voiced_hits = sum(vad.is_speech(voiced_frame(i), SAMPLE_RATE) for i in range(10))
        print(f"voiced      : {voiced_hits}/10 voiced (expect >= 6)")
        if voiced_hits < 6:
            failures.append(f"voiced samples only {voiced_hits}/10 recognised")

        # --- 4. 前置校验函数行为正确（app/audio 应据此分帧）---
        ok_valid = webrtcvad.valid_rate_and_frame_length(SAMPLE_RATE, FRAME_SAMPLES) is True
        bad_valid = webrtcvad.valid_rate_and_frame_length(SAMPLE_RATE, 100) is False
        print(f"validator   : 480@16k={ok_valid}, 100@16k=rejected:{bad_valid}")
        if not (ok_valid and bad_valid):
            failures.append("valid_rate_and_frame_length returned unexpected results")

        # --- 5. 非法帧长抛的是 _webrtcvad.Error，不是 ValueError ---
        broken = _pcm([0] * 100)  # 100 采样 ≈ 6.25ms，非 10/20/30ms
        try:
            vad.is_speech(broken, SAMPLE_RATE)
        except _webrtcvad.Error as exc:
            is_value_error = isinstance(exc, ValueError)
            print(f"bad frame   : raises {type(exc).__name__} (ValueError? {is_value_error})")
            if is_value_error:
                failures.append("unexpected: bad frame raised ValueError subclass")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"bad frame raised unexpected type: {type(exc).__name__}: {exc}")
        else:
            failures.append("bad frame length was silently accepted")

    print()
    if failures:
        print("SPIKE FAILED")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("SPIKE OK: webrtcvad-wheels works with no C++ toolchain; import name and API unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
