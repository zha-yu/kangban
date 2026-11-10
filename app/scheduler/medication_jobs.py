r"""用药到点的调度驱动链（C2 · Day 7）。

**一条铁律**：本文件**绝对不写库**。`session.add / commit / flush` 一行都不允许；
ORM 查表也不行（service 层是唯一 import models 的地方）。所有写库由
`medication_confirmation_graph`（子图内部调 `medication_service`）完成。

两条子任务：

1. **新到点 tick**：`medication_due_service.list_due_plans()` 拿触发列表 → 对每条
   `medication_service.create_event`（幂等键 `(plan_id, scheduled_at)`）→
   `build_medication_graph` → `start_medication_task`（跑到 `wait_confirm` 的 interrupt
   就停住，让出执行）。
2. **等待中的子图 resume tick**：扫所有 waiting_confirm 的事件 → 从 checkpointer
   读子图状态 → 算下次触达时点 → 到点就 resume 一个空 answer。

硬约束：

- `grep "session.add\|session.commit" app/scheduler/` 必须零命中
- `grep "from app.models" app/scheduler/` 必须零命中
- scheduler 持长开 checkpointer，不在 tick 内临时开关
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession

from app.graphs.medication_confirmation_graph import (
    MAX_ATTEMPTS,
    build_medication_graph,
    graph_config,
    resume_medication_task,
    start_medication_task,
)
from app.services import medication_service
from app.services.medication_due_service import (
    DuePlan,
    get_event_scheduled_at,
    list_due_plans,
    list_waiting_confirm_events,
)

logger = logging.getLogger(__name__)

#: 四次触达间隔（分钟）：T0 / +5 / +10 / +15（PRD FR-MED-05）
_ATTEMPT_INTERVAL_MINUTES: int = 5

#: resume 等待中子图时用的空 answer（触发子图 retry 路径：_NO_ANSWER_WORDS 里有 ""）
_EMPTY_ANSWER_FOR_RETRY: str = ""

#: scheduler 任务 id（replace_existing=True，重启/重新注册安全）
_TICK_JOB_ID: str = "medication_due_tick"
_RESUME_JOB_ID: str = "medication_resume_tick"


# ------------------------------------------------------------------ 顶层安装入口（给 A3 调用）


def install_scheduler(
    app: FastAPI,
    *,
    session_factory: Callable[[], Any],
    checkpointer_dsn: str | None = None,
    checkpointer_schema: str | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> SchedulerRuntime:
    """构造 SchedulerRuntime 并挂到 `app.state.medication_scheduler`，返回实例给调用方。

    ⚠️ 调用方（main.py lifespan）**必须**自行管理 start() / stop() 生命周期：
        runtime = install_scheduler(app, ...)
        await runtime.start()   # yield 前
        ... yield ...
        await runtime.stop()    # finally 块内

    不用 `@app.on_event("startup")` / `@app.on_event("shutdown")` 是因为 FastAPI 0.110+
    里 lifespan 与旧式事件互斥，后者会被静默忽略。

    :param session_factory: 装配层的数据库会话工厂（`app.db.session_factory`）。
    :param checkpointer_dsn / checkpointer_schema: LangGraph checkpointer 连接参数。
        None = 走 Pydantic Settings（`langgraph_checkpoint_database_url` /
        `langgraph_checkpoint_schema`），与子图 N10 的口径一致。
    :param timezone_name: cron 解释时区（默认 Asia/Shanghai）。
    :return: 已构造但**未启动**的 SchedulerRuntime 实例。
    """
    runtime = SchedulerRuntime(
        session_factory=session_factory,
        checkpointer_dsn=checkpointer_dsn,
        checkpointer_schema=checkpointer_schema,
        timezone_name=timezone_name,
    )
    app.state.medication_scheduler = runtime
    return runtime


# ------------------------------------------------------------------ SchedulerRuntime


class SchedulerRuntime:
    """scheduler 运行时容器——长开 checkpointer + 两个定时任务 + 会话工厂。

    把这些东西封成一个类，start / stop 成对，不在模块顶层残留任何全局状态。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        checkpointer_dsn: str | None = None,
        checkpointer_schema: str | None = None,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self._session_factory = session_factory
        self._checkpointer_dsn = checkpointer_dsn
        self._checkpointer_schema = checkpointer_schema
        self._timezone = timezone_name
        self._scheduler: AsyncIOScheduler | None = None
        self._checkpointer_ctx: Any = None
        self._checkpointer: AsyncPostgresSaver | None = None

    # --- start / stop ---

    async def start(self) -> None:
        """启动顺序：开 checkpointer → 起 scheduler → 注册两个 tick。

        任何环节失败都**不阻塞主应用启动**：打 warning 继续 yield，避免 PG/Redis 临时
        不可用时整个 FastAPI 起不来。scheduler 自身会停在半初始化状态，下一次 restart
        会重新尝试（APScheduler 的 replace_existing=True 保证幂等）。
        """
        from app.graphs.checkpointer import medication_checkpointer

        # 开 checkpointer（长开，整个进程生命周期）
        try:
            self._checkpointer_ctx = medication_checkpointer(
                dsn=self._checkpointer_dsn,
                schema=self._checkpointer_schema,
            )
            self._checkpointer = await self._checkpointer_ctx.__aenter__()
            logger.info(
                "medication_scheduler_checkpointer_ready schema=%s",
                self._checkpointer_schema or "default",
            )
        except Exception as exc:
            logger.warning(
                "medication_scheduler_checkpointer_unavailable",
                extra={"error_type": type(exc).__name__},
            )
            # checkpointer 没起来时 scheduler 也没法正常工作（resume tick 会
            # 因 self._checkpointer is None 直接跳过），但主应用不受影响

        # 起 scheduler（绑定 uvicorn 事件循环）
        self._scheduler = AsyncIOScheduler(timezone=self._timezone)
        self._scheduler.add_job(
            self._tick_due_plans,
            CronTrigger.from_crontab("* * * * *", timezone=self._timezone),
            id=_TICK_JOB_ID,
            replace_existing=True,
        )
        # resume tick 也每分钟跑
        self._scheduler.add_job(
            self._tick_resume_waiting,
            CronTrigger.from_crontab("* * * * *", timezone=self._timezone),
            id=_RESUME_JOB_ID,
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info(
            "medication_scheduler_started timezone=%s jobs=%s",
            self._timezone,
            [_TICK_JOB_ID, _RESUME_JOB_ID],
        )

    async def stop(self) -> None:
        """关闭顺序：先停 scheduler（不阻塞进行中的任务）→ 关 checkpointer。"""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("medication_scheduler_shutdown")
        if self._checkpointer_ctx is not None:
            try:
                await self._checkpointer_ctx.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning(
                    "medication_scheduler_checkpointer_close_error",
                    extra={"error_type": type(exc).__name__},
                )
            self._checkpointer_ctx = None
            self._checkpointer = None

    # --- 两个 tick ---

    async def _tick_due_plans(self) -> None:
        """每分钟 tick：查 due plans → create_event → build_graph → start。

        scheduler 自己零写库——create_event 是 medication_service 的写口（被允许），
        子图 start_medication_task 内部的 remind 节点会调 medication_service 写 attempts，
        也是子图层的事。
        """
        async with self._session_factory() as session:
            try:
                due = await list_due_plans(session)
            except Exception:
                logger.exception("medication_scheduler_due_scan_failed")
                return

        if not due:
            return

        for plan in due:
            try:
                await self._trigger_plan(plan)
            except Exception:
                logger.exception(
                    "medication_scheduler_trigger_failed plan_id=%s", plan.plan_id
                )

    async def _tick_resume_waiting(self) -> None:
        """每分钟 tick：扫 waiting_confirm 的子图，到点了就 resume 一个空 answer。"""
        async with self._session_factory() as session:
            waiting = await list_waiting_confirm_events(session)

        if not waiting:
            return

        now = datetime.datetime.now(datetime.UTC)
        for event in waiting:
            try:
                await self._maybe_resume(event.reminder_id, now)
            except Exception:
                logger.exception(
                    "medication_scheduler_resume_failed reminder_id=%s", event.reminder_id
                )

    # --- 核心驱动链 ---

    async def _trigger_plan(self, plan: DuePlan) -> None:
        """单条计划驱动链：create_event → build_graph → start。"""
        # ① 幂等建事件（同 (plan_id, scheduled_at) 已存在 → 返回原 reminder_id）
        async with self._session_factory() as session:
            try:
                reminder_id = await medication_service.create_event(
                    session,
                    plan_id=plan.plan_id,
                    elder_id=plan.elder_id,
                    scheduled_at=plan.scheduled_at,
                    drug_snapshot={
                        "drug_name": plan.drug_name,
                        "dose": plan.dose,
                    },
                    triggered_by="scheduler",
                )
                await session.commit()
            except medication_service.MedicationConflict as exc:
                # 同键异内容——药单改版了，这是业务问题不是 scheduler bug
                logger.warning(
                    "medication_scheduler_conflict plan_id=%s scheduled_at=%s error=%s",
                    plan.plan_id,
                    plan.scheduled_at,
                    exc,
                )
                return

        if self._checkpointer is None:
            logger.warning("medication_scheduler_checkpointer_missing skip_trigger")
            return

        # ② build_graph → start_medication_task（停在 wait_confirm interrupt）
        async with self._session_factory() as graph_session:
            graph = build_medication_graph(
                session=graph_session,
                checkpointer=self._checkpointer,
            )
            await start_medication_task(
                graph,
                reminder_id=reminder_id,
                plan_id=plan.plan_id,
                elder_id=plan.elder_id,
                deadline=plan.deadline,
                triggered_by="scheduler",
            )

        logger.info(
            "medication_scheduler_triggered plan_id=%s reminder_id=%s elder_id=%s",
            plan.plan_id,
            reminder_id,
            plan.elder_id,
        )

    async def _maybe_resume(self, reminder_id: uuid.UUID, now_utc: datetime.datetime) -> None:
        """检查一个 waiting_confirm 的子图是否到点，到点了就 resume。

        步骤：
        1. graph.aget_state() 取当前状态 → attempt_no + terminal
        2. 已有终态 → 跳过（双驱动守卫：对话侧可能已经 resume 过了）
        3. attempt_no >= MAX_ATTEMPTS → 跳过（不应出现在 waiting_confirm，但双保险）
        4. 算下次触达时点 = T0 + attempt_no * 5min
        5. 到点 → resume(graph, reminder_id, answer="")
        """
        if self._checkpointer is None:
            return

        graph_id = graph_config(reminder_id)

        # ① 读子图状态
        async with self._session_factory() as session:
            graph = build_medication_graph(
                session=session,
                checkpointer=self._checkpointer,
            )
            state = await graph.aget_state(graph_id)

        if state is None:
            logger.warning(
                "medication_scheduler_state_missing reminder_id=%s", reminder_id
            )
            return

        values = state.values or {}
        terminal = values.get("terminal")
        attempt_no = int(values.get("attempt_no") or 1)

        # ② 双驱动守卫：已有终态或 attempt 已到位 → 跳过
        if terminal is not None:
            logger.info(
                "medication_scheduler_resume_skip_terminal reminder_id=%s terminal=%s",
                reminder_id,
                terminal,
            )
            return
        if attempt_no >= MAX_ATTEMPTS:
            logger.info(
                "medication_scheduler_resume_skip_exhausted reminder_id=%s attempt_no=%d",
                reminder_id,
                attempt_no,
            )
            return

        # ③ 从 medication_events.scheduled_at 算 T0（service 层只读查询）
        async with self._session_factory() as session:
            t0 = await get_event_scheduled_at(session, reminder_id)
        if t0 is None:
            logger.warning(
                "medication_scheduler_event_missing reminder_id=%s", reminder_id
            )
            return

        next_fire = t0 + datetime.timedelta(minutes=attempt_no * _ATTEMPT_INTERVAL_MINUTES)

        # ④ 还没到点 → 跳过
        if now_utc < next_fire:
            return

        # ⑤ 到点 → resume 空 answer（子图会走 retry：attempt_no+1 → remind → wait_confirm）
        async with self._session_factory() as resume_session:
            graph = build_medication_graph(
                session=resume_session,
                checkpointer=self._checkpointer,
            )
            await resume_medication_task(
                graph, reminder_id=reminder_id, answer=_EMPTY_ANSWER_FOR_RETRY
            )

        logger.info(
            "medication_scheduler_resumed reminder_id=%s attempt_no=%d",
            reminder_id,
            attempt_no,
        )


# ------------------------------------------------------------------ 类型导出


AsyncSessionFactory = Callable[[], AsyncIterator[AsyncSession]]


__all__ = ["AsyncSessionFactory", "SchedulerRuntime", "install_scheduler"]
