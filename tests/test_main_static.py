"""TTS 落盘目录 /static 挂载测试（联调遗留 gap：audio_url 须真实可取）。

audio.py（C3）把 TTS WAV 存 `<cwd>/static/mock/` 并返回 `/static/mock/*.wav`；
nginx（Day 10）上线前由 main.py 挂载 StaticFiles 保证 URL 可取。
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def _static_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 cwd 切到 tmp 目录，audio.py 与 main.py 挂载点共用同一 cwd 口径。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _wav_byte() -> bytes:
    """最小合法 WAV（44 头 + 1 个样本）。"""
    pcm = struct.pack("<h", 0)
    header = b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + len(pcm)),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, 1, 1, 16_000, 32_000, 2, 16),
            b"data",
            struct.pack("<I", len(pcm)),
        ]
    )
    return header + pcm


def test_static_mount_serves_tts_file(_static_root: Path) -> None:
    """已落盘的 TTS WAV 可经 /static/mock/ HTTP 取回（audio_url 闭环）。"""
    mock_dir = _static_root / "static" / "mock"
    mock_dir.mkdir(parents=True)
    (mock_dir / "tts_smoke.wav").write_bytes(_wav_byte())

    client = TestClient(create_app(Settings(app_env="test")))
    resp = client.get("/static/mock/tts_smoke.wav")

    assert resp.status_code == 200
    assert resp.content[:4] == b"RIFF"


def test_static_mount_missing_dir_no_boot_error() -> None:
    """check_dir=False：static/ 不存在时应用照常启动（目录由 utterance 惰性创建）。"""
    client = TestClient(create_app(Settings(app_env="test")))
    resp = client.get("/static/mock/not_exist.wav")

    assert resp.status_code == 404


def test_static_mount_rejects_path_traversal(_static_root: Path) -> None:
    """不落盘不脱敏红线：挂载不得成为目录穿越出口。"""
    client = TestClient(create_app(Settings(app_env="test")))
    resp = client.get("/static/../app/main.py")

    assert resp.status_code in (404, 400)
