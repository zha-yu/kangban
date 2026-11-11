"""记忆治理调度（B5 收尾 · Day 8 Step 7，FR-MEM-07）：衰减 + 归并两类周维护 job。

裁决③（计划 §5）：独立新建本文件（照 health_jobs 先例），不并入既有注册文件。
裁决④（计划 §5）：`memory_service` 暴露 `decay_stale` / `consolidate_similar`
两个语义化接口，本文件两个 job 分别调用——**零写库铁律**照 `medication_jobs.py`
/ `health_jobs.py` 驱动链模式：不 import `app.models`、无 `session.add`，
事务由 service 层自管（service 自开会话则自行提交）。

触发时刻自裁口径（AGENTS 前置铁律 6）：PRD FR-MEM-07 / TD §3.4 只定频次
（「定期衰减（90 天）」「重复记忆归并（每周）」），未定具体时刻——取每周一
04:10（衰减）/ 04:20（归并）业务低峰执行；调整改模块常量并在提交说明留痕。

幂等：两接口均为状态条件扫描——衰减收敛于 importance 下限（到 0.10 不再变化），
归并后旧条目退出 active，重复执行不产生重复变更/重复归并记录。
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI

from app.services import memory_service

logger = logging.getLogger(__name__)

#: 90 天衰减 job：每周一 04:10（自裁口径，见模块 docstring）
_DECAY_CRONTAB = "10 4 * * 1"

#: 每周归并 job：每周一 04:20（衰减之后执行，新腾出的低分条目一并参与配对扫描）
_CONSOLIDATE_CRONTAB = "20 4 * * 1"

_DECAY_JOB_ID = "memory_decay_weekly"
_CONSOLIDATE_JOB_ID = "memory_consolidation_weekly"


def install_memory_scheduler(
    app: FastAPI, *, timezone_name: str = "Asia/Shanghai"
) -> MemorySchedulerRuntime:
    """构造 MemorySchedulerRuntime 并挂到 `app.state.memory_scheduler`，返回实例给调用方。

    ⚠️ 调用方（main.py lifespan，装配归 A3）必须自行管理 start() / stop()（与
    health_scheduler 同形）：start 在 yield 前，stop 在 finally 块内。
    """
    runtime = MemorySchedulerRuntime(timezone_name=timezone_name)
    app.state.memory_scheduler = runtime
    return runtime


class MemorySchedulerRuntime:
    """记忆治理调度运行时：两个周维护 tick，无 checkpointer、无会话工厂依赖
    （事务完全由 memory_service 接口自管）。"""

    def __init__(self, *, timezone_name: str = "Asia/Shanghai") -> None:
        self._timezone_name = timezone_name
        self._scheduler: AsyncIOScheduler | None = None

    async def start(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=self._timezone_name)
        self._scheduler.add_job(
            self._tick_decay,
            CronTrigger.from_crontab(_DECAY_CRONTAB, timezone=self._timezone_name),
            id=_DECAY_JOB_ID,
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._tick_consolidate,
            CronTrigger.from_crontab(_CONSOLIDATE_CRONTAB, timezone=self._timezone_name),
            id=_CONSOLIDATE_JOB_ID,
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info(
            "memory_scheduler_started timezone=%s decay=%s consolidate=%s",
            self._timezone_name,
            _DECAY_CRONTAB,
            _CONSOLIDATE_CRONTAB,
        )

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("memory_scheduler_shutdown")

    # --- tick ①：90 天衰减（每周一 04:10） ---

    async def _tick_decay(self) -> None:
        """90 天衰减：只调 `memory_service.decay_stale`（裁决④ 接口①），
        事务由 service 自管；失败只记日志不抛——周维护尽力而为，不影响主链。"""
        try:
            records = await memory_service.decay_stale()
        except Exception:
            logger.exception("memory_decay_job_failed")
            return
        logger.info("memory_decay_job_done decayed=%d", len(records))

    # --- tick ②：每周归并（每周一 04:20） ---

    async def _tick_consolidate(self) -> None:
        """每周归并：只调 `memory_service.consolidate_similar`（裁决④ 接口②），
        输出归并记录条数（FR-MEM-07 验证口径）；失败纪律同上。"""
        try:
            records = await memory_service.consolidate_similar()
        except Exception:
            logger.exception("memory_consolidation_job_failed")
            return
        logger.info("memory_consolidation_job_done merged_pairs=%d", len(records))


__all__ = ["MemorySchedulerRuntime", "install_memory_scheduler"]
