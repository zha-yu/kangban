"""设备掉线检测调度（A5 · Day 10 S9，FR-ADM-10）。

判定与写库全部在 `device_service`（分层红线：job 只做调用与会话管理）：

- `detect_offline_devices`：心跳超 2 周期 → offline + 通知管理员/负责护工
  ＋ 掉线窗口与用药 T0 重叠 → device_fault 工单；
- `recover_online_devices`：绑定设备恢复新鲜心跳 → online。

**接线归属（补丁需求单 → dev-B，沿 day07 handoff 惯例）**：`app/main.py`
归 dev-B 独占，本包不改。dev-B 在 lifespan 里加两行即可：

```python
from app.scheduler.device_jobs import DeviceJobRuntime

device_jobs_runtime = DeviceJobRuntime(session_factory=session_factory)
# lifespan 启动段（与 medication_scheduler.start() 同层）：
await device_jobs_runtime.start()
# lifespan 关停段：
await device_jobs_runtime.stop()
```

设计为**独立 AsyncIOScheduler 实例**（不挂进 medication_jobs 的私有
`_scheduler`，避免改 B 的文件）：两者互不感知，各管各的 job。
所有 job `max_instances=1` + `coalesce=True`：扫描慢于间隔时不堆叠。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_JOB_ID = "device_offline_check"


async def run_device_offline_check(
    *, session_factory: Callable[[], Any]
) -> dict[str, int]:
    """一次掉线检测 tick：独立会话跑「检测掉线 + 恢复在线」并提交。

    供 APScheduler 周期调用（也供运维手动触发）。异常只记日志不上抛，
    保证调度循环不被单次失败打断（与 health_jobs 的 tick 同口径）。
    """
    # 循环 import 规避：device_service 顶层不依赖 scheduler
    from app.services import device_service

    try:
        async with session_factory() as session:
            outcomes = await device_service.detect_offline_devices(session)
            recovered = await device_service.recover_online_devices(session)
            await session.commit()
        summary = {"offline_detected": len(outcomes), "recovered": len(recovered)}
        if summary["offline_detected"] or summary["recovered"]:
            logger.info("device_offline_tick %s", summary)
        return summary
    except Exception:
        logger.exception("device_offline_tick failed（下个周期重试）")
        return {"offline_detected": 0, "recovered": 0}


class DeviceJobRuntime:
    """设备掉线检测的独立调度运行时（dev-B lifespan 补丁的挂载点）。

    与 medication_jobs.SchedulerRuntime 同形：start/stop 手动管理，
    绑定 uvicorn 的 loop（三次法则，AGENTS.md §9）。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        timezone_name: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        from app.core.config import get_settings

        self._timezone = timezone_name or get_settings().scheduler_timezone
        self._interval = get_settings().device_offline_scan_interval_seconds
        self._scheduler: AsyncIOScheduler | None = None

    async def start(self) -> None:
        if self._scheduler is not None:
            return
        scheduler = AsyncIOScheduler(timezone=self._timezone)
        scheduler.add_job(
            run_device_offline_check,
            IntervalTrigger(seconds=self._interval),
            kwargs={"session_factory": self._session_factory},
            id=_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=self._interval,
        )
        scheduler.start()
        self._scheduler = scheduler
        logger.info(
            "device_jobs started interval=%ss timezone=%s", self._interval, self._timezone
        )

    async def stop(self) -> None:
        if self._scheduler is None:
            return
        self._scheduler.shutdown(wait=False)
        self._scheduler = None
        logger.info("device_jobs stopped")


__all__ = ["DeviceJobRuntime", "run_device_offline_check"]
