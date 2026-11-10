"""APScheduler 任务注册：用药到点、19:00 健康采集、日报、漏采派单、原始音频 72h 清理（Day 7-9）。

所有后台任务必须绑定到 uvicorn 的 loop（三次法则，AGENTS.md §9）。

**C2 Day 7 交付**：`install_scheduler` 是唯一公开入口——A3 在 lifespan 上调用它即可。
"""

from __future__ import annotations

from app.scheduler.medication_jobs import SchedulerRuntime, install_scheduler

__all__ = ["SchedulerRuntime", "install_scheduler"]
