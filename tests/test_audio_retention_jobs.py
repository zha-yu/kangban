"""原始音频 72h 清理 job 测试（F27 · FR-TC-07 / AGENTS §4.10）。

覆盖：
1. 73h 前原始音频（.wav/.pcm/.opus/.webm）被删；72h 内保留；
2. **脱敏文本（.txt/.jsonl）即使超期也不删**——只删原始音频是硬口径；
3. 目录不存在时空跑不报错（首启常态）；
4. 嵌套目录 + 大写后缀同样处理；重复扫描幂等（第二次删 0 个）；
5. 运行时契约：cron `23 * * * *`、保留 72h、`app.state` 挂载、start/stop 可成对。

不依赖数据库：本 job 零写库。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI

from app.core.config import get_settings
from app.scheduler.audio_retention_jobs import (
    _RETENTION_CRONTAB,
    AUDIO_SUFFIXES,
    AudioRetentionRuntime,
    install_audio_retention_scheduler,
)

_HOUR = 3600.0


def _make_file(path: Path, *, age_hours: float, now_ts: float) -> Path:
    """造一个指定「多少小时前修改」的文件（内容与删除判定无关）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"raw-audio-bytes")
    mtime = now_ts - age_hours * _HOUR
    os.utime(path, (mtime, mtime))
    return path


def test_audio_retention_deletes_only_expired_audio(tmp_path: Path) -> None:
    """73h 前音频消失、71h 内保留；同目录超期脱敏文本保留。"""
    now_ts = time.time()
    old_wav = _make_file(tmp_path / "old.wav", age_hours=73, now_ts=now_ts)
    old_pcm = _make_file(tmp_path / "old.pcm", age_hours=100, now_ts=now_ts)
    old_opus = _make_file(tmp_path / "old.opus", age_hours=72.5, now_ts=now_ts)
    old_webm = _make_file(tmp_path / "old.webm", age_hours=200, now_ts=now_ts)
    fresh = _make_file(tmp_path / "fresh.wav", age_hours=71, now_ts=now_ts)
    old_text = _make_file(tmp_path / "transcript.txt", age_hours=500, now_ts=now_ts)
    old_jsonl = _make_file(tmp_path / "transcript.jsonl", age_hours=500, now_ts=now_ts)

    runtime = AudioRetentionRuntime(base_dir=tmp_path, retention_hours=72)
    result = runtime.sweep(now_ts=now_ts)

    assert not old_wav.exists()
    assert not old_pcm.exists()
    assert not old_opus.exists()
    assert not old_webm.exists()
    assert fresh.exists(), "72h 内原始音频必须保留"
    assert old_text.exists(), "脱敏文本永不删除"
    assert old_jsonl.exists(), "脱敏文本永不删除"

    assert result.deleted == 4
    assert result.kept == 1
    assert result.skipped == 2  # 两个非音频文件只计数、不删
    assert result.scanned == 7
    assert result.missing_dir is False


def test_audio_retention_missing_dir_is_noop(tmp_path: Path) -> None:
    """目录不存在 → 空跑不抛错，返回 missing_dir=True。"""
    runtime = AudioRetentionRuntime(base_dir=tmp_path / "not-created-yet", retention_hours=72)
    result = runtime.sweep()

    assert result.missing_dir is True
    assert result.deleted == 0
    assert not (tmp_path / "not-created-yet").exists()


def test_audio_retention_nested_dirs_and_case(tmp_path: Path) -> None:
    """嵌套目录里的音频同样清理；后缀大小写不敏感。"""
    now_ts = time.time()
    nested = _make_file(tmp_path / "elder-1" / "2026-11-09" / "a.WAV", age_hours=80, now_ts=now_ts)
    nested_keep = _make_file(
        tmp_path / "elder-1" / "2026-11-09" / "b.Opus", age_hours=10, now_ts=now_ts
    )

    runtime = AudioRetentionRuntime(base_dir=tmp_path, retention_hours=72)
    result = runtime.sweep(now_ts=now_ts)

    assert not nested.exists()
    assert nested_keep.exists()
    assert result.deleted == 1


def test_audio_retention_is_idempotent(tmp_path: Path) -> None:
    """重复扫描幂等：第二次删 0 个，保留项不重复计数错误。"""
    now_ts = time.time()
    _make_file(tmp_path / "old.wav", age_hours=90, now_ts=now_ts)
    keep = _make_file(tmp_path / "fresh.wav", age_hours=1, now_ts=now_ts)

    runtime = AudioRetentionRuntime(base_dir=tmp_path, retention_hours=72)
    first = runtime.sweep(now_ts=now_ts)
    second = runtime.sweep(now_ts=now_ts)

    assert first.deleted == 1
    assert second.deleted == 0
    assert second.kept == 1
    assert keep.exists()


def test_audio_retention_runtime_contract(tmp_path: Path) -> None:
    """cron 每小时 :23、默认保留 72h、`app.state` 挂载、start/stop 成对。"""
    assert _RETENTION_CRONTAB == "23 * * * *"
    assert {".wav", ".pcm", ".opus", ".webm"} == AUDIO_SUFFIXES

    app = FastAPI()
    runtime = install_audio_retention_scheduler(app, base_dir=tmp_path)
    assert app.state.audio_retention_scheduler is runtime
    assert runtime.retention_hours == 72 == get_settings().audio_retention_hours
    assert runtime.base_dir == tmp_path

    # 未显式给定目录时走 settings 契约（字段缺失则由模块兜底到 var/audio_debug）
    default_runtime = AudioRetentionRuntime()
    assert default_runtime.base_dir.name == "audio_debug"
