"""原始音频 72h 清理（F27 · FR-TC-07 / AGENTS §4.10「原始音频最长保留 72h」）。

**只删原始音频，不删脱敏文本**：白名单只看后缀 `.wav` / `.pcm` / `.opus` / `.webm`，
同目录下的转写/脱敏产物（`.txt` / `.jsonl` 等）一律不碰——它们是复盘与评测的输入，
删了就把「出过什么事」的证据一起删掉。

**目录契约**（主理人 2026-11-12 裁决 D4）：扫描 `settings.audio_debug_dir`
（默认 `var/audio_debug`，字段已补进 `app/core/config.py` 并登记 `.env.example`）。
`getattr(settings, "audio_debug_dir", ...)` 兜底保留，兼容未升级的测试桩配置。
落盘生产方（谁往这个目录写）登记 Day 10，本轮只证明 job 行为。

**独立 runtime**：照 `memory_jobs.py` / `health_jobs.py` 先例自建文件，不并入用药 /
健康调度（两个调度各自 start/stop，互不影响）。cron `23 * * * *`（每小时 :23）：
避开整点的用药 tick 与健康 tick，清理是低优先级维护任务。

**幂等**：删除即状态收敛——第二次扫描同一目录删 0 个。目录不存在时空跑并留日志，
不抛错（首启时目录往往还没建）。

**调度器铁律**：本文件不 import `app.models`、不 `session.add`——它根本不碰数据库。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI

from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: 音视频原始音频后缀白名单（只有这些会被删；其余（含脱敏文本）一律保留）
AUDIO_SUFFIXES: frozenset[str] = frozenset({".wav", ".pcm", ".opus", ".webm"})

#: 音频调试目录默认值（`settings.audio_debug_dir` 缺失时的兜底，见模块 docstring）
_DEFAULT_AUDIO_DEBUG_DIR = "var/audio_debug"

#: 每小时 :23 跑一次（避开整点的用药/健康 tick）
_RETENTION_CRONTAB = "23 * * * *"
_RETENTION_JOB_ID = "audio_retention_sweep_hourly"


@dataclass(frozen=True, slots=True)
class AudioSweepResult:
    """一次扫描的结果（测试与日志共用；不写库，只是内存结构）。"""

    scanned: int = 0
    deleted: int = 0
    kept: int = 0
    skipped: int = 0
    missing_dir: bool = False


def install_audio_retention_scheduler(
    app: FastAPI,
    *,
    base_dir: str | os.PathLike[str] | None = None,
    retention_hours: int | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> AudioRetentionRuntime:
    """构造 AudioRetentionRuntime 并挂到 `app.state.audio_retention_scheduler`。

    ⚠️ 调用方（main.py lifespan，装配归装配窗口）必须自行 start() / stop()：
        runtime = install_audio_retention_scheduler(app)
        await runtime.start()   # yield 前
        ... yield ...
        await runtime.stop()    # finally 块内

    :param base_dir: 覆盖扫描目录（缺省读 `settings.audio_debug_dir`，再缺省到 `var/audio_debug`）。
    :param retention_hours: 覆盖保留时长（缺省读 `settings.audio_retention_hours`，即 72）。
    """
    runtime = AudioRetentionRuntime(
        base_dir=base_dir,
        retention_hours=retention_hours,
        timezone_name=timezone_name,
    )
    app.state.audio_retention_scheduler = runtime
    return runtime


class AudioRetentionRuntime:
    """72h 清理运行时：一个小时级 job，无 checkpointer、无会话工厂（零数据库依赖）。"""

    def __init__(
        self,
        *,
        base_dir: str | os.PathLike[str] | None = None,
        retention_hours: int | None = None,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        settings = get_settings()
        resolved_dir = base_dir or getattr(settings, "audio_debug_dir", _DEFAULT_AUDIO_DEBUG_DIR)
        self._base_dir = Path(resolved_dir)
        self._retention_hours = (
            retention_hours if retention_hours is not None else settings.audio_retention_hours
        )
        self._timezone_name = timezone_name
        self._scheduler: AsyncIOScheduler | None = None

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def retention_hours(self) -> int:
        return self._retention_hours

    async def start(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=self._timezone_name)
        self._scheduler.add_job(
            self._tick_sweep,
            CronTrigger.from_crontab(_RETENTION_CRONTAB, timezone=self._timezone_name),
            id=_RETENTION_JOB_ID,
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info(
            "audio_retention_scheduler_started timezone=%s crontab=%s base_dir=%s retention_hours=%d",
            self._timezone_name,
            _RETENTION_CRONTAB,
            self._base_dir,
            self._retention_hours,
        )

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("audio_retention_scheduler_shutdown")

    async def _tick_sweep(self) -> None:
        """cron 入口：把同步文件扫描丢到线程池，避免阻塞事件循环。"""
        try:
            result = await asyncio.to_thread(self.sweep)
        except Exception:
            logger.exception("audio_retention_sweep_failed base_dir=%s", self._base_dir)
            return
        if result.missing_dir:
            logger.info("audio_retention_dir_missing base_dir=%s", self._base_dir)
        else:
            logger.info(
                "audio_retention_swept base_dir=%s scanned=%d deleted=%d kept=%d skipped=%d",
                self._base_dir,
                result.scanned,
                result.deleted,
                result.kept,
                result.skipped,
            )

    def sweep(self, *, now_ts: float | None = None) -> AudioSweepResult:
        """扫描目录并删除超期原始音频（同步、幂等；`now_ts` 仅供测试注入时间）。"""
        if not self._base_dir.is_dir():
            return AudioSweepResult(missing_dir=True)

        cutoff = (time.time() if now_ts is None else now_ts) - self._retention_hours * 3600
        scanned = deleted = kept = skipped = 0

        # os.walk(followlinks=False)：不跟随目录符号链接，避免顺着链接删到目录外
        for root, _dirs, files in os.walk(self._base_dir, followlinks=False):
            for name in files:
                path = Path(root) / name
                scanned += 1
                if path.suffix.lower() not in AUDIO_SUFFIXES:
                    skipped += 1  # 脱敏文本/其它产物：永不删除
                    continue
                if path.is_symlink():
                    skipped += 1  # 链接指向不受本 job 管辖
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    skipped += 1  # 扫描与删除之间被生产方改名/删除：不算失败
                    continue
                if mtime >= cutoff:
                    kept += 1
                    continue
                try:
                    path.unlink()
                except FileNotFoundError:
                    skipped += 1
                except OSError:
                    logger.warning("audio_retention_unlink_failed path=%s", path, exc_info=True)
                    skipped += 1
                else:
                    deleted += 1

        return AudioSweepResult(
            scanned=scanned,
            deleted=deleted,
            kept=kept,
            skipped=skipped,
            missing_dir=False,
        )


__all__ = [
    "AUDIO_SUFFIXES",
    "AudioRetentionRuntime",
    "AudioSweepResult",
    "install_audio_retention_scheduler",
]
