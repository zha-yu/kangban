"""用药确认子图与用药事实服务（A08–A11 / 计划 V8）。

覆盖：
1. 正常路径：T0 触达 → `interrupt` 停住 → 回「吃了」→ 终态 `taken_reported`；
2. **重启恢复**：用同一 checkpointer 新建图实例，`get_state` 能读回等待位置，
   继续跑不重发已完成触达（`medication_attempts` 仍只有 1 条）；
3. 无回应：重试到第 4 次触达后进 `missed_unconfirmed`，`retry_count=3`、
   `needs_escalation=True`（A12 的交接位）；
4. 事件幂等：同 `(plan_id, scheduled_at)` 同内容返回同一 `reminder_id`，异内容抛冲突；
5. 终态不覆盖：已定终态再写另一个 → `TerminalStateConflict`；
6. **A12 建单**：`missed_unconfirmed` 落一张 `missed_medication` / yellow 工单，
   且 `needs_escalation` 与「有没有建成单」互不替代。

checkpointer 用**独立 schema + 独立池**（与生产同一条代码路径）；
业务行用 savepoint 夹具，用例结束回滚。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.graphs.checkpointer import medication_checkpointer
from app.graphs.medication_confirmation_graph import (
    MAX_ATTEMPTS,
    build_medication_graph,
    graph_config,
    resume_medication_task,
    start_medication_task,
)
from app.models.organization import Elder
from app.models.staff import StaffAccount
from app.services import medication_service
from app.services.medication_service import (
    MedicationConflict,
    TerminalStateConflict,
)


def _test_psycopg_dsn() -> str:
    """conftest 的测试库 URL（psycopg 同步风格），checkpointer 用它建独立池。"""
    settings = get_settings()
    raw = settings.database_url_test
    if not raw:
        url = make_url(settings.database_url)
        url = url.set(database=f"{url.database}_test")
        raw = url.render_as_string(hide_password=False)
    return str(raw).replace("postgresql+asyncpg://", "postgresql://")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def saver(_test_db_bootstrap: bool) -> AsyncIterator[AsyncPostgresSaver]:
    if not _test_db_bootstrap:
        pytest.skip("PostgreSQL 不可达，跳过用药子图用例")
    async with medication_checkpointer(dsn=_test_psycopg_dsn()) as checkpointer:
        yield checkpointer


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    try:
        conn = await db_engine.connect()
    except Exception as exc:
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")

    trans = await conn.begin()
    sf = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    async with sf() as sess:
        try:
            yield sess
        finally:
            await sess.rollback()
    await trans.rollback()


@pytest.fixture
async def seeded(session: AsyncSession) -> dict[str, Any]:
    """一位长者 + 一位医生 + 一条用药计划（建事件需要这两个 FK）。"""
    elder = Elder(name="张桂芬", status="active")
    doctor = StaffAccount(name="王医生", role="doctor", password_hash="x" * 32)
    session.add_all([elder, doctor])
    await session.flush()

    from sqlalchemy import text

    plan_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO medication_plans "
            "(plan_id, elder_id, drug_name, dose, schedule_cron, start_date, created_by) "
            "VALUES (:plan_id, :elder_id, :drug, :dose, :cron, :start, :created_by)"
        ),
        {
            "plan_id": plan_id,
            "elder_id": elder.elder_id,
            "drug": "氨氯地平",
            "dose": "5mg 一片",
            "cron": "0 8 * * *",
            "start": datetime.date(2026, 11, 1),
            "created_by": doctor.staff_id,
        },
    )
    return {"elder_id": elder.elder_id, "plan_id": plan_id}


async def _new_event(session: AsyncSession, seeded: dict[str, Any]) -> uuid.UUID:
    return await medication_service.create_event(
        session,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        scheduled_at=datetime.datetime(2026, 11, 9, 8, 0, tzinfo=datetime.UTC),
        drug_snapshot={"drug_name": "氨氯地平", "dose": "5mg 一片"},
        triggered_by="scheduler",
    )


# ------------------------------------------------------------------ 1. 正常路径


async def test_taken_path_sets_terminal(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    reminder_id = await _new_event(session, seeded)
    graph = build_medication_graph(session=session, checkpointer=saver)

    first = await start_medication_task(
        graph,
        reminder_id=reminder_id,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline="2026-11-09T08:15:00+00:00",
    )
    assert "__interrupt__" in first, "首轮必须停在 wait_confirm"

    event = await medication_service.get_event(session, reminder_id)
    assert event is not None and event["status"] == "waiting_confirm"

    await resume_medication_task(graph, reminder_id=reminder_id, answer="吃了")

    event = await medication_service.get_event(session, reminder_id)
    assert event is not None
    assert event["terminal_state"] == "taken_reported"
    attempts = await medication_service.list_attempts(session, reminder_id)
    assert [a["attempt_no"] for a in attempts] == [1]
    assert attempts[0]["response"] == "吃了"


# ------------------------------------------------------------------ 2. 重启恢复


async def test_restart_recovers_without_resending(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    reminder_id = await _new_event(session, seeded)
    graph = build_medication_graph(session=session, checkpointer=saver)
    await start_medication_task(
        graph,
        reminder_id=reminder_id,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline="2026-11-09T08:15:00+00:00",
    )

    # 模拟进程重启：新建图实例，状态只在 checkpointer 里
    revived = build_medication_graph(session=session, checkpointer=saver)
    snapshot = await revived.aget_state(graph_config(reminder_id))
    assert snapshot.values["reminder_id"] == str(reminder_id)
    assert snapshot.values["attempt_no"] == 1

    await resume_medication_task(revived, reminder_id=reminder_id, answer="吃过了")

    attempts = await medication_service.list_attempts(session, reminder_id)
    assert len(attempts) == 1, "恢复后不得把已发过的触达再发一次"
    event = await medication_service.get_event(session, reminder_id)
    assert event is not None and event["terminal_state"] == "taken_reported"


# ------------------------------------------------------------------ 3. 重试到终态


async def test_no_answer_retries_then_missed(
    session: AsyncSession, seeded: dict[str, Any], saver: AsyncPostgresSaver
) -> None:
    reminder_id = await _new_event(session, seeded)
    graph = build_medication_graph(session=session, checkpointer=saver)
    await start_medication_task(
        graph,
        reminder_id=reminder_id,
        plan_id=seeded["plan_id"],
        elder_id=seeded["elder_id"],
        deadline="2026-11-09T08:15:00+00:00",
    )

    final: dict[str, Any] = {}
    for _ in range(MAX_ATTEMPTS):
        final = await resume_medication_task(graph, reminder_id=reminder_id, answer="no_response")

    event = await medication_service.get_event(session, reminder_id)
    assert event is not None
    assert event["terminal_state"] == "missed_unconfirmed"
    assert event["retry_count"] == MAX_ATTEMPTS - 1, "四次触达 = 三次重试"

    attempts = await medication_service.list_attempts(session, reminder_id)
    assert [a["attempt_no"] for a in attempts] == [1, 2, 3, 4]

    snapshot = await graph.aget_state(graph_config(reminder_id))
    assert snapshot.values["needs_escalation"] is True, "missed 必须交给 A12 建单"
    assert final.get("terminal") in (None, "missed_unconfirmed")

    # A12：finalize 写完结态后建漏服单（表里没有 source 列，靠确定性单号幂等）
    from sqlalchemy import select

    from app.models.medication import WorkOrder

    orders = list(
        (
            await session.execute(select(WorkOrder).where(WorkOrder.elder_id == seeded["elder_id"]))
        ).scalars()
    )
    assert len(orders) == 1, "一次漏服只建一张单"
    assert orders[0].type == "missed_medication"
    assert orders[0].severity == "yellow", "漏服是黄档；判红会稀释急症"
    assert orders[0].status == "new"
    assert orders[0].assignee_id is None, "该长者未配责任护工 → 未分派是真实状态"
    assert orders[0].order_no.startswith("WO-")


# ------------------------------------------------------------------ 4. 事件幂等


async def test_create_event_is_idempotent(session: AsyncSession, seeded: dict[str, Any]) -> None:
    first = await _new_event(session, seeded)
    second = await _new_event(session, seeded)
    assert first == second, "同键同内容必须返回同一条事件"

    with pytest.raises(MedicationConflict):
        await medication_service.create_event(
            session,
            plan_id=seeded["plan_id"],
            elder_id=seeded["elder_id"],
            scheduled_at=datetime.datetime(2026, 11, 9, 8, 0, tzinfo=datetime.UTC),
            drug_snapshot={"drug_name": "氨氯地平", "dose": "10mg 一片"},
            triggered_by="scheduler",
        )


# ------------------------------------------------------------------ 5. 终态不覆盖


async def test_terminal_state_is_write_once(session: AsyncSession, seeded: dict[str, Any]) -> None:
    reminder_id = await _new_event(session, seeded)
    await medication_service.set_terminal(
        session, event_id=reminder_id, terminal_state="taken_reported"
    )
    # 同值重写 = 幂等
    await medication_service.set_terminal(
        session, event_id=reminder_id, terminal_state="taken_reported"
    )
    with pytest.raises(TerminalStateConflict):
        await medication_service.set_terminal(
            session, event_id=reminder_id, terminal_state="missed_unconfirmed"
        )
