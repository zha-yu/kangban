"""健康采集与分级子图（SG2 / 计划 H3，B2 骨架契约测试）。

覆盖（与计划 Step 4 必过清单同名）：
1. `normal_values_persist_only`：全正常 → 不 interrupt，persist 后 END；
2. `bp_180_pauses_doctor_interrupt_then_resume`（AC-10/21）：红档 → 双通知 +
   紧急工单 → 停在「等医生确认」interrupt → resume 后归档（含处置时间）；
3. `bp_160_nurse_confirmation`（AC-09）：黄档 → 工单+黄色复核任务 → 护工
   interrupt → 确认后归档；
4. `three_branches_state_demo`：三个 thread 分别跑到绿/黄/红，`get_state`
   能读回决策字段（grade_level/reasons/rule_version/order_ref/notified）；
5. `recovery_from_checkpoint_no_duplicate_order`：红档 interrupt 后模拟进程
   重启（同 checkpointer 新建图实例）→ resume → 工单只建一次；
6. `active_complaint_uses_same_graph_source_voice_active`（AC-34）：主动主诉
   走同一张图，source 入 state；非法 source 拒绝。

**红阶段（Step 2）约定**：业务出口（persist / 建单 / 双通知）注入桩函数，
validate 用图内 Pydantic 结构化（自足、不依赖 A2）；**唯独 grade 走默认真
引擎引用**——在 A2 的 `health_service.grade_health`（X1 分级引擎）
交付前，全部用例应红于同一红因（health_service 未交付 / X1 未接入），
CP1 接线后转绿（Step 4 只补 grade 相关断言细节，用例不变）。

checkpointer 复用独立 schema + 独立池（与用药子图同一条代码路径）；
业务会话用 savepoint 夹具，用例结束回滚。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.graphs.checkpointer import medication_checkpointer as pg_checkpointer
from app.graphs.health_checkup_graph import (
    build_health_checkup_graph,
    resume_health_checkup,
    start_health_checkup,
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
        pytest.skip("PostgreSQL 不可达，跳过健康子图用例")
    async with pg_checkpointer(dsn=_test_psycopg_dsn()) as checkpointer:
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


# ---------------------------------------------------------------- 桩出口


class _FakeExits:
    """记录 persist / 建单 / 双通知的调用次数与内容；grade 不在桩内（走默认真引擎）。"""

    def __init__(self) -> None:
        self.persist_calls: list[dict[str, Any]] = []
        self.yellow_order_calls: list[dict[str, Any]] = []
        self.red_notify_calls: list[dict[str, Any]] = []
        self.red_order_calls: list[dict[str, Any]] = []

    async def persist(self, session: Any, state: dict[str, Any]) -> dict[str, Any]:
        self.persist_calls.append({"state": dict(state)})
        return {"record_id": str(uuid.uuid4()), "version": 1}

    async def yellow_order(self, session: Any, state: dict[str, Any]) -> dict[str, Any]:
        self.yellow_order_calls.append({"state": dict(state)})
        return {"order_id": str(uuid.uuid4()), "order_no": "WO-Y-0001", "type": "health_alert"}

    async def red_notify(self, session: Any, state: dict[str, Any]) -> dict[str, Any]:
        self.red_notify_calls.append({"state": dict(state)})
        return {"staff": "delivered", "guardian": "delivered"}

    async def red_order(self, session: Any, state: dict[str, Any]) -> dict[str, Any]:
        self.red_order_calls.append({"state": dict(state)})
        return {"order_id": str(uuid.uuid4()), "order_no": "WO-R-0001", "type": "emergency"}


def _build(session: Any, saver: AsyncPostgresSaver, exits: _FakeExits) -> Any:
    return build_health_checkup_graph(
        session=session,
        checkpointer=saver,
        persist_fn=exits.persist,
        create_yellow_order_fn=exits.yellow_order,
        notify_red_fn=exits.red_notify,
        create_red_order_fn=exits.red_order,
    )


# 全正常 5 答（图内 Pydantic 结构化接受口语数字串）
_NORMAL: dict[str, str] = {
    "systolic": "120",
    "diastolic": "76",
    "sleep_hours": "7",
    "mood_score": "4",
    "complaint": "没有",
}
_YELLOW: dict[str, str] = {
    "systolic": "160",
    "diastolic": "95",
    "sleep_hours": "6",
    "mood_score": "3",
    "complaint": "有点乏",
}
_RED: dict[str, str] = {
    "systolic": "185",
    "diastolic": "110",
    "sleep_hours": "5",
    "mood_score": "2",
    "complaint": "头晕",
}


async def _answered(
    graph: Any, *, elder_id: uuid.UUID, source: str, answers: dict[str, str]
) -> dict[str, Any]:
    return await start_health_checkup(
        graph, elder_id=elder_id, source=source, initial_answers=answers
    )


# ---------------------------------------------------------------- 1. 绿档


async def test_normal_values_persist_only(session: AsyncSession, saver: AsyncPostgresSaver) -> None:
    exits = _FakeExits()
    graph = _build(session, saver, exits)
    result = await _answered(
        graph, elder_id=uuid.uuid4(), source="voice_scheduled", answers=_NORMAL
    )
    assert "__interrupt__" not in result
    assert result["grade_level"] == "green"
    assert result["record_ref"]["version"] == 1
    assert len(exits.persist_calls) == 1
    assert not exits.yellow_order_calls and not exits.red_order_calls
    assert not exits.red_notify_calls


# ---------------------------------------------------------------- 2. 红档


async def test_bp_180_pauses_doctor_interrupt_then_resume(
    session: AsyncSession, saver: AsyncPostgresSaver
) -> None:
    exits = _FakeExits()
    graph = _build(session, saver, exits)
    elder_id = uuid.uuid4()
    result = await _answered(graph, elder_id=elder_id, source="voice_scheduled", answers=_RED)
    # 红档：双通知 + 紧急工单已完成，停在「等医生确认」
    assert result["grade_level"] == "red"
    assert len(exits.red_notify_calls) == 1
    assert result["notified"] == {"staff": "delivered", "guardian": "delivered"}
    assert result["order_ref"]["type"] == "emergency"
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["kind"] == "red_confirm"

    thread_id = result["graph_thread_id"]
    final = await resume_health_checkup(
        graph, thread_id=thread_id, payload={"decision": "confirmed"}
    )
    assert "__interrupt__" not in final
    assert final["record_ref"]["version"] == 1  # 归档（含实际处置时间，由 persist 出口落实）
    assert len(exits.persist_calls) == 1


# ---------------------------------------------------------------- 3. 黄档


async def test_bp_160_nurse_confirmation(session: AsyncSession, saver: AsyncPostgresSaver) -> None:
    exits = _FakeExits()
    graph = _build(session, saver, exits)
    result = await _answered(
        graph, elder_id=uuid.uuid4(), source="staff_scheduled", answers=_YELLOW
    )
    # 黄档：工单 + 黄色复核任务，停在「等护工确认」
    assert result["grade_level"] == "yellow"
    assert len(exits.yellow_order_calls) == 1
    assert result["order_ref"]["type"] == "health_alert"
    assert not exits.red_notify_calls and not exits.red_order_calls
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["kind"] == "yellow_review"

    thread_id = result["graph_thread_id"]
    final = await resume_health_checkup(
        graph, thread_id=thread_id, payload={"decision": "confirmed"}
    )
    assert "__interrupt__" not in final
    assert final["hitl_result"]["decision"] == "confirmed"
    assert len(exits.persist_calls) == 1


# ---------------------------------------------------------------- 4. 三分支 state 回读


async def test_three_branches_state_demo(session: AsyncSession, saver: AsyncPostgresSaver) -> None:
    graph = _build(session, saver, _FakeExits())
    elder_id = uuid.uuid4()
    # 绿：跑完，get_state 可读回决策字段
    green = await _answered(graph, elder_id=elder_id, source="voice_scheduled", answers=_NORMAL)
    green_state = await graph.aget_state({"configurable": {"thread_id": green["graph_thread_id"]}})
    assert green_state.values["grade_level"] == "green"
    assert green_state.values["grade_reasons"] == [] or isinstance(
        green_state.values["grade_reasons"], list
    )

    # 红：停在医院 interrupt，get_state 可读回 order_ref / notified / 分级依据
    red_exits = _FakeExits()
    red_graph = _build(session, saver, red_exits)
    red = await _answered(red_graph, elder_id=uuid.uuid4(), source="voice_scheduled", answers=_RED)
    red_state = await graph.aget_state({"configurable": {"thread_id": red["graph_thread_id"]}})
    assert red_state.values["grade_level"] == "red"
    assert red_state.values["order_ref"]["type"] == "emergency"
    assert red_state.values["notified"]["staff"] == "delivered"

    # 黄：停在护工 interrupt，get_state 可读回工单号
    yellow_exits = _FakeExits()
    yellow_graph = _build(session, saver, yellow_exits)
    yellow = await _answered(
        yellow_graph, elder_id=uuid.uuid4(), source="staff_scheduled", answers=_YELLOW
    )
    yellow_state = await yellow_graph.aget_state(
        {"configurable": {"thread_id": yellow["graph_thread_id"]}}
    )
    assert yellow_state.values["grade_level"] == "yellow"
    assert yellow_state.values["order_ref"]["order_no"] == "WO-Y-0001"


# ---------------------------------------------------------------- 5. 重启恢复不重复建单


async def test_recovery_from_checkpoint_no_duplicate_order(
    session: AsyncSession, saver: AsyncPostgresSaver
) -> None:
    exits = _FakeExits()  # 重启前后共用同一组桩，统计建单次数
    elder_id = uuid.uuid4()
    first = _build(session, saver, exits)
    result = await _answered(first, elder_id=elder_id, source="voice_scheduled", answers=_RED)
    assert len(exits.red_order_calls) == 1  # interrupt 前已建单一次
    thread_id = result["graph_thread_id"]

    # 模拟进程重启：新建图实例，状态只在 checkpointer 里
    revived = _build(session, saver, exits)
    final = await resume_health_checkup(
        revived, thread_id=thread_id, payload={"decision": "confirmed"}
    )
    assert "__interrupt__" not in final
    assert len(exits.red_order_calls) == 1  # 恢复后不再重复建单
    assert len(exits.persist_calls) == 1


# ---------------------------------------------------------------- 6. 主动主诉同图（AC-34）


async def test_active_complaint_uses_same_graph_source_voice_active(
    session: AsyncSession, saver: AsyncPostgresSaver
) -> None:
    exits = _FakeExits()
    graph = _build(session, saver, exits)
    result = await _answered(graph, elder_id=uuid.uuid4(), source="voice_active", answers=_NORMAL)
    assert result["source"] == "voice_active"  # 双入口同一张图
    assert result["grade_level"] == "green"
    assert len(exits.persist_calls) == 1

    # 非法 source 拒绝（四值枚举之外不入图）
    with pytest.raises(ValueError, match="unknown source"):
        await start_health_checkup(
            graph, elder_id=uuid.uuid4(), source="phone_call", initial_answers=_NORMAL
        )
