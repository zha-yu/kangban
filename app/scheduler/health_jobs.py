"""健康采集域调度（B3 · Day 8）：四类 tick job，独立注册不复用 medication tick。

**同一条铁律**（照 `medication_jobs.py` 驱动链模式）：本文件**不做业务写库**，
`session.add / flush` 一行都不允许，ORM 查表也不行——所有业务写经
`health_service` / `notify_service` / `report_service` / `health_checkup_graph`。
图运行结束后对会话做一次 `commit()` 是**会话管理豁免**（L6#6 同款裁决），
不是业务写库。

四类 job（每分钟 cron + 内部时刻窗口守卫，重启/重跑靠状态条件天然幂等）：

1. **19:00 采集触发**：对当日 active 长者逐一起健康图（`source=voice_scheduled`，
   scheduled thread 同日幂等）；checkpointer 里已有当日 checkpoint 的长者跳过——
   触发窗口给 5 分钟是为了进程在整点附近重启也不漏人。
2. **20:00 强制结束 + 漏采派单**（PRD §4.6 / FR-HLT-08）：先按「当日无任何记录」
   判定漏采建 `collection_missed` 黄单（幂等 token 按日），再对仍在挂起的
   scheduled 线程 `force_close`——不再发问，未答标缺失直接进判定落库。
3. **风险扫描（每分钟，无窗口）**：红色事件 2 分钟无人确认 → 原事件标
   `escalated` + 通知管理员，**不建第二个事件/工单**（AC-12）；pending 通知
   30s 重试、3 次失败置 failed（AC-23 前半）。
4. **日报生成触发**：调 C2 `report_service.generate_daily_report`（当日已有
   版本幂等返回，不重建不重发）。触发时刻 PRD/TD 均未定义——**自裁口径**
   （AGENTS 前置铁律 6）：取 20:00 强制结束之后、日切之前的 21:30，调整改
   模块常量 `_DAILY_REPORT_FIRE` 并在提交说明留痕。

采集窗口起止读 Pydantic Settings（`health_checkup_start` / `health_checkup_end`，
待确认项①裁决：允许经环境变量覆盖为可测试时刻）。
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.graphs.health_checkup_graph import (
    build_health_checkup_graph,
    default_thread_id,
    graph_config,
    resume_health_checkup,
    start_health_checkup,
)
from app.services import health_service, notify_service, report_service

logger = logging.getLogger(__name__)

#: 日报触发时刻（上海时间 HH:MM）。出处见模块 docstring「自裁口径」。
_DAILY_REPORT_FIRE: str = "21:30"

#: 触发/收口 tick 的分钟级守卫窗口宽度：进程在整点附近重启也不漏人；
#: 幂等不靠窗口收窄，靠 checkpoint 存在性（触发）与 dedupe token（漏采单）。
_FIRE_WINDOW_MINUTES: int = 5

#: 收口处理窗口（分钟）：20:00 起 1 小时内完成漏采判定与强制收口，
#: 之后当夜不再空扫（漏采单与收口都有幂等，窗口只是省日志噪音）。
_CLOSE_WINDOW_MINUTES: int = 60

#: scheduler 任务 id（replace_existing=True，重启/重新注册安全）
_TRIGGER_JOB_ID = "health_evening_trigger"
_CLOSE_JOB_ID = "health_window_close"
_RISK_SCAN_JOB_ID = "health_risk_scan"
_REPORT_JOB_ID = "health_daily_report"

_UTC = datetime.UTC


def _minutes_of_day(value: str) -> int:
    """解析 "HH:MM" 为当日分钟数（非法配置启动期就该炸，不静默）。"""
    hour_s, minute_s = value.split(":")
    hour, minute = int(hour_s), int(minute_s)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HH:MM value: {value!r}")
    return hour * 60 + minute


# ------------------------------------------------------------------ 顶层安装入口（给 A3 调用）


def install_health_scheduler(
    app: FastAPI,
    *,
    session_factory: Callable[[], Any],
    checkpointer_dsn: str | None = None,
    checkpointer_schema: str | None = None,
    timezone_name: str = "Asia/Shanghai",
    now_fn: Callable[[], datetime.datetime] | None = None,
) -> HealthSchedulerRuntime:
    """构造 HealthSchedulerRuntime 并挂到 `app.state.health_scheduler`，返回实例给调用方。

    ⚠️ 调用方（main.py lifespan，装配归 A3）**必须**自行管理 start() / stop()：
        runtime = install_health_scheduler(app, ...)
        await runtime.start()   # yield 前
        ... yield ...
        await runtime.stop()    # finally 块内

    :param now_fn: 时钟注入（返回 aware UTC datetime）；None = 系统真实时间。
        测试与演练经它覆盖触发时刻，不依赖真实钟点（待确认项①口径）。
    """
    runtime = HealthSchedulerRuntime(
        session_factory=session_factory,
        checkpointer_dsn=checkpointer_dsn,
        checkpointer_schema=checkpointer_schema,
        timezone_name=timezone_name,
        now_fn=now_fn,
    )
    app.state.health_scheduler = runtime
    return runtime


# ------------------------------------------------------------------ HealthSchedulerRuntime


class HealthSchedulerRuntime:
    """健康域调度运行时：长开 checkpointer + 四个 tick + 会话工厂 + 可注入时钟。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        checkpointer_dsn: str | None = None,
        checkpointer_schema: str | None = None,
        timezone_name: str = "Asia/Shanghai",
        now_fn: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._checkpointer_dsn = checkpointer_dsn
        self._checkpointer_schema = checkpointer_schema
        self._timezone_name = timezone_name
        self._tz = ZoneInfo(timezone_name)
        self._now_fn: Callable[[], datetime.datetime] = now_fn or (
            lambda: datetime.datetime.now(_UTC)
        )
        self._scheduler: AsyncIOScheduler | None = None
        self._checkpointer_ctx: Any = None
        self._checkpointer: AsyncPostgresSaver | None = None

    # --- 时钟与窗口守卫 ---

    def _now(self) -> datetime.datetime:
        return self._now_fn()

    def _shanghai(self) -> datetime.datetime:
        return self._now().astimezone(self._tz)

    def _settings(self) -> Any:
        from app.core.config import get_settings

        return get_settings()

    def _in_fire_window(self, *, target: str, width_minutes: int) -> bool:
        """触发窗口守卫：`target`（HH:MM）起 `width_minutes` 分钟内为真。"""
        now_min = _minutes_of_day(self._shanghai().strftime("%H:%M"))
        start_min = _minutes_of_day(target)
        return start_min <= now_min < start_min + width_minutes

    def _in_close_window(self) -> bool:
        """收口窗口守卫：采集窗口结束（health_checkup_end）起 1 小时内为真。"""
        now_min = _minutes_of_day(self._shanghai().strftime("%H:%M"))
        end_min = _minutes_of_day(self._settings().health_checkup_end)
        return end_min <= now_min < end_min + _CLOSE_WINDOW_MINUTES

    # --- start / stop ---

    async def start(self) -> None:
        """启动顺序与 medication_scheduler 一致：先开 checkpointer 再起 scheduler；
        任何环节失败都不阻塞主应用启动（warning 继续，tick 内部各自兜底）。"""
        from app.graphs.checkpointer import medication_checkpointer

        try:
            self._checkpointer_ctx = medication_checkpointer(
                dsn=self._checkpointer_dsn,
                schema=self._checkpointer_schema,
            )
            self._checkpointer = await self._checkpointer_ctx.__aenter__()
            logger.info(
                "health_scheduler_checkpointer_ready schema=%s",
                self._checkpointer_schema or "default",
            )
        except Exception as exc:
            logger.warning(
                "health_scheduler_checkpointer_unavailable",
                extra={"error_type": type(exc).__name__},
            )

        self._scheduler = AsyncIOScheduler(timezone=self._timezone_name)
        for job_id, handler in (
            (_TRIGGER_JOB_ID, self._tick_evening_trigger),
            (_CLOSE_JOB_ID, self._tick_window_close),
            (_RISK_SCAN_JOB_ID, self._tick_risk_scan),
            (_REPORT_JOB_ID, self._tick_report),
        ):
            self._scheduler.add_job(
                handler,
                CronTrigger.from_crontab("* * * * *", timezone=self._timezone_name),
                id=job_id,
                replace_existing=True,
            )
        self._scheduler.start()
        logger.info(
            "health_scheduler_started timezone=%s start=%s end=%s report=%s",
            self._timezone_name,
            self._settings().health_checkup_start,
            self._settings().health_checkup_end,
            _DAILY_REPORT_FIRE,
        )

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("health_scheduler_shutdown")
        if self._checkpointer_ctx is not None:
            try:
                await self._checkpointer_ctx.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning(
                    "health_scheduler_checkpointer_close_error",
                    extra={"error_type": type(exc).__name__},
                )
            self._checkpointer_ctx = None
            self._checkpointer = None

    # --- tick ①：19:00 采集触发 ---

    async def _tick_evening_trigger(self) -> None:
        """采集窗口开始：对 active 长者逐一起图（voice_scheduled）。

        幂等两层：触发窗口守卫 + scheduled thread（同日同长者）在 checkpointer
        里已有 checkpoint 的跳过——重复 tick / 重启恢复都不会二次起图。
        """
        start_time = self._settings().health_checkup_start
        if not self._in_fire_window(target=start_time, width_minutes=_FIRE_WINDOW_MINUTES):
            return
        if self._checkpointer is None:
            logger.warning("health_scheduler_checkpointer_missing skip_trigger")
            return

        day = self._shanghai().date()
        async with self._session_factory() as session:
            elder_ids = await health_service.list_active_elder_ids(session)
        if not elder_ids:
            return

        for elder_id in elder_ids:
            thread_id = default_thread_id(elder_id, "voice_scheduled", on_date=day)
            try:
                async with self._session_factory() as session:
                    graph = build_health_checkup_graph(
                        session=session, checkpointer=self._checkpointer
                    )
                    state = await graph.aget_state(graph_config(thread_id))
                    if state.values:
                        # 当日已起图（含已收口）→ 幂等跳过
                        logger.debug(
                            "health_trigger_skip_existing_thread elder_id=%s thread=%s",
                            elder_id,
                            thread_id,
                        )
                        continue
                    await start_health_checkup(
                        graph,
                        elder_id=elder_id,
                        source="voice_scheduled",
                        thread_id=thread_id,
                    )
                    # 会话管理豁免：图运行中节点写库在 session 上，不提交即回滚
                    await session.commit()
                logger.info(
                    "health_trigger_started elder_id=%s thread=%s day=%s",
                    elder_id,
                    thread_id,
                    day,
                )
            except Exception:
                logger.exception("health_scheduler_trigger_failed elder_id=%s", elder_id)

    # --- tick ②：20:00 强制结束 + 漏采派单 ---

    async def _tick_window_close(self) -> None:
        """窗口结束：先漏采判定建单（按「判定时点」的当日记录），再 force_close 挂起线程。

        顺序刻意先判定后收口：收口本身会落一条全缺失的记录，若先收口再判定，
        「无任何记录」的条件就永远测不出来。漏采单 dedupe token 按日幂等，
        重复 tick / 重复执行都不会多建。
        """
        if not self._in_close_window():
            return
        if self._checkpointer is None:
            logger.warning("health_scheduler_checkpointer_missing skip_close")
            return

        day = self._shanghai().date()

        # ① 漏采判定与建单（active 长者中当日无任何记录者）
        async with self._session_factory() as session:
            elder_ids = await health_service.list_active_elder_ids(session)
            with_records = await health_service.list_elder_ids_with_records_on(session, day=day)
        missed = [elder_id for elder_id in elder_ids if elder_id not in with_records]
        for elder_id in missed:
            try:
                async with self._session_factory() as session:
                    await health_service.create_missed_collection_order(
                        session, elder_id=elder_id, day=day
                    )
                    await session.commit()
            except Exception:
                logger.exception(
                    "health_scheduler_missed_order_failed elder_id=%s day=%s", elder_id, day
                )
        if missed:
            logger.warning("health_window_close_missed day=%s elders=%d", day, len(missed))

        # ② 挂起的 scheduled 线程强制收口（不再发问，未答标缺失直接进判定）。
        # 只对「采集路径」挂起的线程收口；hitl_yellow / hitl_red 的 HITL 挂起
        # 是合法等待（黄档已被图内 force_close 代确认，红档等医生），不碰。
        _COLLECTING_NODES = {
            "collect",
            "validate",
            "grade",
            "create_yellow_order",
            "notify_red",
            "create_red_order",
        }
        for elder_id in elder_ids:
            thread_id = default_thread_id(elder_id, "voice_scheduled", on_date=day)
            try:
                async with self._session_factory() as session:
                    graph = build_health_checkup_graph(
                        session=session, checkpointer=self._checkpointer
                    )
                    state = await graph.aget_state(graph_config(thread_id))
                    if not state.values:
                        continue  # 当日没起过图：漏采单已覆盖，无需收口
                    if not (set(state.next) & _COLLECTING_NODES):
                        continue  # 已跑完或停在 HITL 等确认：不是采集挂起，跳过
                    await resume_health_checkup(graph, thread_id=thread_id, force_close=True)
                    await session.commit()
                logger.info("health_window_force_closed elder_id=%s thread=%s", elder_id, thread_id)
            except Exception:
                logger.exception(
                    "health_scheduler_force_close_failed elder_id=%s thread=%s",
                    elder_id,
                    thread_id,
                )

    # --- tick ③：红色升级 + 通知重试扫描（每分钟） ---

    async def _tick_risk_scan(self) -> None:
        """AC-12：红事件 2 分钟无人确认 → 原事件标 escalated（不建第二个事件/工单）；
        AC-23 前半：pending 通知 30s 重试、3 次失败置 failed。

        两个底层（scan_escalations / retry_pending_notifications）都是状态条件
        扫描，重复执行天然幂等。
        """
        now = self._now()
        async with self._session_factory() as session:
            event_ids = await notify_service.scan_escalations(session, now=now)
            for event_id in event_ids:
                try:
                    await notify_service.escalate_red_event(session, event_id=event_id)
                except Exception:
                    logger.exception("health_scheduler_escalation_failed event_id=%s", event_id)
            await notify_service.retry_pending_notifications(session, now=now)
            # 会话管理豁免：escalate / retry 的状态推进都在 session 上，
            # 不提交即随会话回滚（无升级事件时通知重试同样需要落库）
            await session.commit()

    # --- tick ④：日报生成触发 ---

    async def _tick_report(self) -> None:
        """21:30（自裁口径，见模块 docstring）对 active 长者逐人生成当日日报。

        `generate_daily_report` 对「当日已有版本」幂等返回（不重建不重发），
        重复 tick 无副作用；长者不存在由服务层抛 ValueError，逐人 try 包住不阻断他人。
        """
        if not self._in_fire_window(target=_DAILY_REPORT_FIRE, width_minutes=_FIRE_WINDOW_MINUTES):
            return

        day = self._shanghai().date()
        async with self._session_factory() as session:
            elder_ids = await health_service.list_active_elder_ids(session)
        if not elder_ids:
            return

        for elder_id in elder_ids:
            try:
                async with self._session_factory() as session:
                    ref = await report_service.generate_daily_report(
                        session, elder_id=elder_id, report_date=day
                    )
                    await session.commit()
                logger.info(
                    "health_daily_report elder_id=%s day=%s report_id=%s created=%s",
                    elder_id,
                    day,
                    ref.report_id,
                    ref.created,
                )
            except Exception:
                logger.exception("health_scheduler_report_failed elder_id=%s day=%s", elder_id, day)


# ------------------------------------------------------------------ 类型导出


AsyncSessionFactory = Callable[[], AsyncIterator[Any]]


__all__ = [
    "AsyncSessionFactory",
    "HealthSchedulerRuntime",
    "install_health_scheduler",
]
