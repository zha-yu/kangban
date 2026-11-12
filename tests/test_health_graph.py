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
from sqlalchemy import make_url, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.graphs.checkpointer import medication_checkpointer as pg_checkpointer
from app.graphs.health_checkup_graph import (
    RED_ALERT_TTS_TEXT,
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
    # 红档：先落库（create_red_order）→ 后通知（notify_red）→ 停在「等医生确认」
    assert result["grade_level"] == "red"
    assert len(exits.red_order_calls) == 1
    assert len(exits.red_notify_calls) == 1
    assert result["notified"]["staff"] == "delivered"
    assert result["notified"]["guardian"] == "delivered"
    # F08：红档话术是代码常量，随 notify 返回给语音/面板消费
    assert result["notified"]["tts_text"] == RED_ALERT_TTS_TEXT
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


# ---------------------------------------------------------------- 7. F06 跨字段校验

_CONFLICT: dict[str, str] = {
    "systolic": "120",
    "diastolic": "130",  # 收缩压 <= 舒张压 → 两项 invalid
    "sleep_hours": "7",
    "mood_score": "4",
    "complaint": "没有",
}


async def test_cross_field_conflict_reasks_once_then_marks_missing(
    session: AsyncSession, saver: AsyncPostgresSaver
) -> None:
    """F06（PRD line 361）：120/130 → 两项 invalid → 重问一次 → 仍失败标缺失判黄。"""
    exits = _FakeExits()
    graph = _build(session, saver, exits)
    result = await _answered(graph, elder_id=uuid.uuid4(), source="voice_active", answers=_CONFLICT)
    # 第一次重问：回到 collect 问收缩压
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["question"] == "systolic"

    thread_id = result["graph_thread_id"]
    second = await resume_health_checkup(graph, thread_id=thread_id, payload="120")
    assert second["__interrupt__"][0].value["question"] == "diastolic"

    third = await resume_health_checkup(graph, thread_id=thread_id, payload="130")
    # 仍冲突 → 标缺失 → 黄档复核（不伪造正常值、不判红）
    assert third["grade_level"] == "yellow"
    assert third["record"]["systolic"] is None
    assert third["record"]["diastolic"] is None
    assert "__interrupt__" in third
    assert third["__interrupt__"][0].value["kind"] == "yellow_review"


async def test_cross_field_check_applies_to_injected_validate_fn(
    session: AsyncSession, saver: AsyncPostgresSaver
) -> None:
    """注入 validate_fn 也必须过跨字段检查（F06 不可被注入绕过）。"""

    def _naive_validate(answers: dict[str, Any]) -> dict[str, Any]:
        # 故意不做跨字段校验的注入实现
        return {
            "record": {
                "systolic": 120,
                "diastolic": 130,
                "sleep_hours": 7,
                "mood_score": 4,
                "complaint": None,
            },
            "invalid": [],
        }

    exits = _FakeExits()
    graph = build_health_checkup_graph(
        session=session,
        checkpointer=saver,
        validate_fn=_naive_validate,
        persist_fn=exits.persist,
        create_yellow_order_fn=exits.yellow_order,
        notify_red_fn=exits.red_notify,
        create_red_order_fn=exits.red_order,
    )
    result = await start_health_checkup(
        graph,
        elder_id=uuid.uuid4(),
        source="voice_active",
        initial_answers=_CONFLICT,
    )
    # 注入实现返回的 120/130 被图内跨字段检查拦下 → 回 collect 重问
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["question"] == "systolic"


# ---------------------------------------------------------------- 8. X7 reviewed_externally


async def test_yellow_reviewed_externally_ends_without_persist(
    session: AsyncSession, saver: AsyncPostgresSaver
) -> None:
    """console 已复核 → 图直接 END，不二次 persist（防二次确认/二次版本）。"""
    exits = _FakeExits()
    graph = _build(session, saver, exits)
    result = await _answered(graph, elder_id=uuid.uuid4(), source="voice_active", answers=_YELLOW)
    assert result["__interrupt__"][0].value["kind"] == "yellow_review"

    final = await resume_health_checkup(
        graph,
        thread_id=result["graph_thread_id"],
        payload={"decision": "reviewed_externally", "review_status": "confirmed"},
    )
    assert "__interrupt__" not in final
    assert final["hitl_result"]["decision"] == "reviewed_externally"
    assert exits.persist_calls == []  # 不再 persist


async def test_red_reviewed_externally_ends_without_persist(
    session: AsyncSession, saver: AsyncPostgresSaver
) -> None:
    exits = _FakeExits()
    graph = _build(session, saver, exits)
    result = await _answered(graph, elder_id=uuid.uuid4(), source="voice_active", answers=_RED)
    assert result["__interrupt__"][0].value["kind"] == "red_confirm"

    final = await resume_health_checkup(
        graph,
        thread_id=result["graph_thread_id"],
        payload={"decision": "reviewed_externally", "review_status": "corrected"},
    )
    assert "__interrupt__" not in final
    assert exits.persist_calls == []


# ---------------------------------------------------------------- 9. 通知失败不回滚落库


class _NotifyRaisesExits(_FakeExits):
    async def red_order(self, session: Any, state: dict[str, Any]) -> dict[str, Any]:
        self.red_order_calls.append({"state": dict(state)})
        return {
            "record_id": str(uuid.uuid4()),
            "risk_event_id": str(uuid.uuid4()),
            "work_order_id": str(uuid.uuid4()),
        }

    async def red_notify(self, session: Any, state: dict[str, Any]) -> dict[str, Any]:
        self.red_notify_calls.append({"state": dict(state)})
        raise RuntimeError("staff notify down")


async def test_notify_failure_keeps_persisted_order_and_continues(
    session: AsyncSession, saver: AsyncPostgresSaver
) -> None:
    """X3 v0.6 条件 3：通知异常不回滚落库，图继续 HITL → persist，标记 degraded。"""
    exits = _NotifyRaisesExits()
    graph = _build(session, saver, exits)
    result = await _answered(graph, elder_id=uuid.uuid4(), source="voice_active", answers=_RED)

    assert result["order_ref"]["record_id"]
    assert result["order_ref"]["work_order_id"]
    notified = result["notified"]
    assert notified["degraded"] is True
    assert notified["notification_error"] == "RuntimeError"
    assert notified["tts_text"] == RED_ALERT_TTS_TEXT
    assert result["__interrupt__"][0].value["kind"] == "red_confirm"

    final = await resume_health_checkup(
        graph, thread_id=result["graph_thread_id"], payload={"decision": "confirmed"}
    )
    assert "__interrupt__" not in final
    assert len(exits.persist_calls) == 1  # 图没有因通知失败中断


# ---------------------------------------------------------------- 10. F14 健康语音执行器


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        assert ex == 15 * 60
        self.data[key] = str(value).encode()

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


async def test_health_executor_redis_thread_mapping_end_to_end(
    session: AsyncSession, saver: AsyncPostgresSaver
) -> None:
    """F14：无活跃线程 → 起图；有线程 + answer → resume；终态删 key。"""
    from contextlib import asynccontextmanager

    from app.core.config import get_settings
    from app.core.runtime_context import Identity
    from app.services.tool_impls import make_health_checkup_executor

    @asynccontextmanager
    async def _sf():
        yield session

    redis = _FakeRedis()
    executor = make_health_checkup_executor(
        session_factory=_sf, checkpointer=saver, redis_client=redis
    )
    from app.models.organization import Elder

    elder = Elder(name="执行器测试长者", status="active")
    session.add(elder)
    await session.flush()
    elder_id = elder.elder_id
    runtime = Identity(kind="device", device_id=str(uuid.uuid4()), elder_id=str(elder_id))
    key = f"{get_settings().redis_key_prefix}:health:active:{elder_id}"

    first = await executor({}, runtime)
    assert first["status"] == "awaiting_answer"
    assert first["question"] == "systolic"
    assert key in redis.data  # thread_id 只写服务端 Redis

    answers = [("120", "diastolic"), ("76", "sleep_hours"), ("7", "mood_score"), ("4", "complaint")]
    for value, next_question in answers:
        step = await executor({"answer": value}, runtime)
        assert step["status"] == "awaiting_answer"
        assert step["question"] == next_question

    done = await executor({"answer": "没有"}, runtime)
    assert done["status"] == "completed"
    assert done["severity"] == "green"
    assert done["record_id"]
    assert key not in redis.data  # 终态删 key


# ---------------------------------------------------------------- 11. AC-09/10/21/22 trace 证据


async def test_ac_09_10_21_22_each_has_trace_id(
    session: AsyncSession, saver: AsyncPostgresSaver, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-09/10/21/22 各跑一次并钉住 trace_id（CP-B3 证据）。

    - AC-09：165/85 → 黄（复核单，无红事件）；
    - AC-10：182/85 → 红；
    - AC-21：182/null → 单分量立即红；
    - AC-22：黄档在库 → 补录 185 → 原事件升级 red、不建第二事件。
    """
    import logging

    from app.core.context import reset_trace_id, set_trace_id
    from app.models.health import HealthRecord
    from app.models.medication import RiskEvent, WorkOrder
    from app.models.organization import Elder
    from app.services import health_service

    caplog.set_level(logging.INFO)

    scenarios: list[tuple[str, dict[str, str | None], str]] = [
        (
            "ac09-yellow-165-85",
            {
                "systolic": "165",
                "diastolic": "85",
                "sleep_hours": "7",
                "mood_score": "4",
                "complaint": "没有",
            },
            "yellow",
        ),
        (
            "ac10-red-182-85",
            {
                "systolic": "182",
                "diastolic": "85",
                "sleep_hours": "7",
                "mood_score": "4",
                "complaint": "没有",
            },
            "red",
        ),
        (
            "ac21-single-182-null",
            {
                "systolic": "182",
                "diastolic": None,
                "sleep_hours": "7",
                "mood_score": "4",
                "complaint": "没有",
            },
            "red",
        ),
    ]

    for trace_id, answers, expected in scenarios:
        token = set_trace_id(trace_id)
        try:
            elder_i = Elder(name=f"AC 长者-{trace_id}", status="active")
            session.add(elder_i)
            await session.flush()

            async def _real_order(
                session: Any, state: dict[str, Any], *, _elder_id: uuid.UUID = elder_i.elder_id
            ) -> dict[str, Any]:
                """测试侧真实落库适配器：调 C 的 create_health_record（record+event+工单）。"""
                record = dict(state.get("record") or {})
                ref = await health_service.create_health_record(
                    session,
                    elder_id=_elder_id,
                    source="voice_active",
                    operator_id=None,
                    graph_thread_id=state.get("graph_thread_id"),
                    systolic=record.get("systolic"),
                    diastolic=record.get("diastolic"),
                    sleep_hours=record.get("sleep_hours"),
                    mood_score=record.get("mood_score"),
                    mood_note=record.get("mood_note"),
                    complaint=record.get("complaint"),
                )
                return {
                    "record_id": str(ref.record_id),
                    "risk_event_id": str(ref.risk_event_id) if ref.risk_event_id else None,
                    "work_order_id": str(ref.work_order_id) if ref.work_order_id else None,
                }

            exits = _FakeExits()
            graph = build_health_checkup_graph(
                session=session,
                checkpointer=saver,
                create_yellow_order_fn=_real_order,
                notify_red_fn=exits.red_notify,
                create_red_order_fn=_real_order,
            )
            result = await _answered(
                graph, elder_id=elder_i.elder_id, source="voice_active", answers=answers
            )
            assert result["grade_level"] == expected, trace_id
            assert any(
                "health_graph_graded" in rec.getMessage() and trace_id in rec.getMessage()
                for rec in caplog.records
            ), trace_id

            # 真实落库：一条 record + 一个 risk_event + 一张对应 severity 工单
            records = (
                (
                    await session.execute(
                        select(HealthRecord).where(HealthRecord.elder_id == elder_i.elder_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(records) == 1 and records[0].severity == expected, trace_id
            events = (
                (
                    await session.execute(
                        select(RiskEvent).where(RiskEvent.elder_id == elder_i.elder_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(events) == 1 and events[0].severity == expected, trace_id
            orders = (
                (
                    await session.execute(
                        select(WorkOrder).where(WorkOrder.elder_id == elder_i.elder_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(orders) == 1 and orders[0].severity == expected, trace_id
        finally:
            reset_trace_id(token)

    # AC-22：黄档未关闭 → 补录 185 → 原事件升级，不建第二事件
    elder = Elder(name="AC22 长者", status="active")
    session.add(elder)
    await session.flush()
    trace_22 = "ac22-yellow-upgrade-185"
    token = set_trace_id(trace_22)
    try:
        first = await health_service.create_health_record(
            session,
            elder_id=elder.elder_id,
            source="voice_active",
            systolic=165,
            diastolic=85,
            sleep_hours=7.0,
            mood_score=4,
            mood_note=None,
            complaint=None,
            operator_id=None,
            graph_thread_id="health:ac22:yellow",
        )
        second = await health_service.create_health_record(
            session,
            elder_id=elder.elder_id,
            source="voice_active",
            systolic=185,
            diastolic=90,
            sleep_hours=7.0,
            mood_score=4,
            mood_note=None,
            complaint=None,
            operator_id=None,
            graph_thread_id="health:ac22:red",
        )
        await session.flush()

        assert second.severity == "red"
        assert second.risk_event_id == first.risk_event_id  # 不新建第二事件
        event = (
            await session.execute(
                select(RiskEvent).where(RiskEvent.event_id == first.risk_event_id)
            )
        ).scalar_one()
        assert event.severity == "red" and event.status == "open"
        orders = (
            (await session.execute(select(WorkOrder).where(WorkOrder.elder_id == elder.elder_id)))
            .scalars()
            .all()
        )
        # 工单 severity 是建单快照：黄单仍 yellow，红单另开
        assert sorted(order.severity for order in orders) == ["red", "yellow"]
        records = (
            (
                await session.execute(
                    select(HealthRecord).where(HealthRecord.elder_id == elder.elder_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(records) == 2
        logging.getLogger(__name__).info(
            "ac22_yellow_to_red trace_id=%s event_id=%s severity=%s status=%s "
            "work_order_severities=%s",
            trace_22,
            event.event_id,
            event.severity,
            event.status,
            sorted(order.severity for order in orders),
        )
    finally:
        reset_trace_id(token)


# ---------------------------------------------------------------- 12. F22 黄档复核落库（真工单）


async def test_boundary_review_creates_follow_up_order_once(
    session: AsyncSession, saver: AsyncPostgresSaver
) -> None:
    """历史/他人/模糊 → `follow_up` + `yellow` 工单；同 turn 只建一单、不建 risk_events。"""
    from contextlib import asynccontextmanager

    from sqlalchemy import func as sa_func

    from app.models.medication import RiskEvent, WorkOrder
    from app.models.organization import Elder
    from app.router.decision import Router
    from app.services.tool_impls import make_boundary_review_executor
    from app.system_one.schemas import SystemOneResult

    elder = Elder(name="边界复核长者", status="active")
    session.add(elder)
    await session.flush()
    elder_id = elder.elder_id

    @asynccontextmanager
    async def _sf():
        yield session

    boundary_review = make_boundary_review_executor(session_factory=_sf)

    class _SystemOne:
        async def decide_fast(self, text: str, *, trace_id: Any = None, state: Any = None):
            return SystemOneResult(intent="chat", confidence=0.95)

        async def decide_shadow(self, text: str, *, trace_id: Any = None, state: Any = None):
            return SystemOneResult(intent="other", confidence=0.0)

    class _Runtime:
        elder_id = str(elder.elder_id)
        turn_id = "turn-boundary-1"
        current_task = None
        media_status = None
        identity = None

    router = Router(system_one=_SystemOne(), boundary_review=boundary_review)
    await router.decide("昨天胸口疼，现在好了", thread_id="th-boundary", runtime=_Runtime())
    await router.decide("昨天胸口疼，现在好了", thread_id="th-boundary", runtime=_Runtime())

    orders = (
        (await session.execute(select(WorkOrder).where(WorkOrder.elder_id == elder_id)))
        .scalars()
        .all()
    )
    assert len(orders) == 1
    assert orders[0].type == "follow_up"
    assert orders[0].severity == "yellow"
    events = await session.execute(
        select(sa_func.count()).select_from(RiskEvent).where(RiskEvent.elder_id == elder_id)
    )
    assert int(events.scalar_one()) == 0  # 不建 risk_events


# ---------------------------------------------------------------- 14. P10 红档落库包装（create_red_landing）


async def _seed_red_targets(session: AsyncSession) -> dict[str, uuid.UUID]:
    """长者 + primary_care 护工 + active 监护人：双通知两侧都有收件人。"""
    from app.models.guardian import GuardianAccount
    from app.models.organization import Elder
    from app.models.relation import ElderGuardian, ElderStaffAssignment
    from app.models.staff import StaffAccount

    elder = Elder(name="红档长者", status="active")
    worker = StaffAccount(name="值班护工", role="care_worker", password_hash="x" * 32)
    guardian = GuardianAccount(name="家属甲", password_hash="x" * 32)
    session.add_all([elder, worker, guardian])
    await session.flush()
    session.add_all(
        [
            ElderStaffAssignment(
                elder_id=elder.elder_id, staff_id=worker.staff_id, relation="primary_care"
            ),
            ElderGuardian(
                elder_id=elder.elder_id,
                guardian_id=guardian.guardian_id,
                relation="女",
                status="active",
            ),
        ]
    )
    await session.flush()
    return {"elder_id": elder.elder_id, "staff_id": worker.staff_id}


async def test_red_landing_persists_without_notification(session: AsyncSession) -> None:
    """P10：`create_red_landing` 只落库——record/risk_event/工单齐备，`notifications` 行数 = 0。"""
    from sqlalchemy import func as sa_func

    from app.models.health import HealthRecord
    from app.models.medication import RiskEvent, WorkOrder
    from app.models.notifications import Notification
    from app.services import health_service

    seeded = await _seed_red_targets(session)
    ref = await health_service.create_red_landing(
        session,
        elder_id=seeded["elder_id"],
        source="voice_active",
        systolic=185,
        diastolic=90,
        sleep_hours=7.0,
        mood_score=4,
        mood_note=None,
        complaint=None,
        operator_id=None,
        graph_thread_id="health:red-landing:1",
    )
    await session.flush()

    assert ref.severity == "red"
    assert ref.version == 1
    assert ref.risk_event_id is not None and ref.work_order_id is not None

    # 落库三件套都在（同一事务，未 commit 也可查）
    assert (
        await session.execute(select(HealthRecord).where(HealthRecord.record_id == ref.record_id))
    ).scalar_one_or_none() is not None
    assert (
        await session.execute(select(RiskEvent).where(RiskEvent.event_id == ref.risk_event_id))
    ).scalar_one_or_none() is not None
    order = (
        await session.execute(select(WorkOrder).where(WorkOrder.order_id == ref.work_order_id))
    ).scalar_one()
    assert order.severity == "red" and order.type == "health_alert"

    # 关键判据：落库阶段零通知（通知归图内 notify_red 节点）
    notif_count = await session.execute(
        select(sa_func.count())
        .select_from(Notification)
        .where(Notification.dedupe_key.like(f"risk_event:{ref.risk_event_id}:%"))
    )
    assert int(notif_count.scalar_one()) == 0

    # 后再调 notify_red_dual → staff / guardian 各 1 条（不是各 2 条）
    notify_ref = await health_service.notify_red_dual(
        session,
        elder_id=seeded["elder_id"],
        record_id=ref.record_id,
        risk_event_id=ref.risk_event_id,
        severity="red",
    )
    await session.flush()
    assert len(notify_ref.notification_ids) == 2
    staff = await session.execute(
        select(sa_func.count())
        .select_from(Notification)
        .where(Notification.dedupe_key.like(f"risk_event:{ref.risk_event_id}:staff:%"))
    )
    guardian = await session.execute(
        select(sa_func.count())
        .select_from(Notification)
        .where(Notification.dedupe_key.like(f"risk_event:{ref.risk_event_id}:guardian:%"))
    )
    assert int(staff.scalar_one()) == 1
    assert int(guardian.scalar_one()) == 1

    # 重复调用幂等：行数不增
    await health_service.notify_red_dual(
        session,
        elder_id=seeded["elder_id"],
        record_id=ref.record_id,
        risk_event_id=ref.risk_event_id,
        severity="red",
    )
    total = await session.execute(
        select(sa_func.count())
        .select_from(Notification)
        .where(Notification.dedupe_key.like(f"risk_event:{ref.risk_event_id}:%"))
    )
    assert int(total.scalar_one()) == 2


async def test_red_landing_rows_survive_notify_failure(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X3 v0.6 条件 3：通知抛错不回滚落库——record / risk_event / 工单仍在。"""
    from sqlalchemy import func as sa_func

    from app.models.health import HealthRecord
    from app.models.medication import RiskEvent, WorkOrder
    from app.models.notifications import Notification
    from app.services import health_service, notify_service

    seeded = await _seed_red_targets(session)
    ref = await health_service.create_red_landing(
        session,
        elder_id=seeded["elder_id"],
        source="voice_active",
        systolic=190,
        diastolic=95,
        sleep_hours=6.0,
        mood_score=3,
        mood_note=None,
        complaint=None,
        operator_id=None,
    )
    await session.flush()

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("notify channel down")

    async def _no_target(*args: Any, **kwargs: Any) -> Any:
        raise notify_service.NotificationTargetNotFound("guardian unbound")

    monkeypatch.setattr(health_service, "send_staff_notifications", _boom)
    monkeypatch.setattr(health_service, "send_guardian_notifications", _no_target)

    notify_ref = await health_service.notify_red_dual(
        session,
        elder_id=seeded["elder_id"],
        record_id=ref.record_id,
        risk_event_id=ref.risk_event_id,
        severity="red",
    )
    assert notify_ref.notification_ids == []  # 两侧都失败，不伪造通知

    assert (
        await session.execute(select(HealthRecord).where(HealthRecord.record_id == ref.record_id))
    ).scalar_one_or_none() is not None
    assert (
        await session.execute(select(RiskEvent).where(RiskEvent.event_id == ref.risk_event_id))
    ).scalar_one_or_none() is not None
    assert (
        await session.execute(select(WorkOrder).where(WorkOrder.order_id == ref.work_order_id))
    ).scalar_one_or_none() is not None
    rows = await session.execute(
        select(sa_func.count())
        .select_from(Notification)
        .where(Notification.dedupe_key.like(f"risk_event:{ref.risk_event_id}:%"))
    )
    assert int(rows.scalar_one()) == 0


async def test_red_graph_lands_then_notifies_exactly_once(
    session: AsyncSession, saver: AsyncPostgresSaver
) -> None:
    """装配真实适配器：红档图 → 落库三条 + 双通知各 1 条（不多不少）。"""
    from sqlalchemy import func as sa_func

    from app.graphs.health_checkup_graph import create_red_landing_adapter, notify_red_dual_adapter
    from app.models.health import HealthRecord
    from app.models.medication import RiskEvent, WorkOrder
    from app.models.notifications import Notification

    seeded = await _seed_red_targets(session)
    graph = build_health_checkup_graph(
        session=session,
        checkpointer=saver,
        create_red_order_fn=create_red_landing_adapter,
        notify_red_fn=notify_red_dual_adapter,
    )
    result = await _answered(
        graph, elder_id=seeded["elder_id"], source="voice_active", answers=_RED
    )
    await session.flush()

    assert result["grade_level"] == "red"
    order_ref = result["order_ref"]
    assert order_ref["record_id"] and order_ref["risk_event_id"] and order_ref["work_order_id"]
    assert result["notified"]["notification_ids"]
    assert result["notified"]["degraded"] is False
    assert result["notified"]["tts_text"] == RED_ALERT_TTS_TEXT
    assert result["__interrupt__"][0].value["kind"] == "red_confirm"

    record = (
        await session.execute(
            select(HealthRecord).where(HealthRecord.record_id == uuid.UUID(order_ref["record_id"]))
        )
    ).scalar_one()
    assert record.severity == "red"
    assert (
        await session.execute(
            select(RiskEvent).where(RiskEvent.event_id == uuid.UUID(order_ref["risk_event_id"]))
        )
    ).scalar_one() is not None
    assert (
        await session.execute(
            select(WorkOrder).where(WorkOrder.order_id == uuid.UUID(order_ref["work_order_id"]))
        )
    ).scalar_one() is not None

    event_id = order_ref["risk_event_id"]
    staff = await session.execute(
        select(sa_func.count())
        .select_from(Notification)
        .where(Notification.dedupe_key.like(f"risk_event:{event_id}:staff:%"))
    )
    guardian = await session.execute(
        select(sa_func.count())
        .select_from(Notification)
        .where(Notification.dedupe_key.like(f"risk_event:{event_id}:guardian:%"))
    )
    assert int(staff.scalar_one()) == 1
    assert int(guardian.scalar_one()) == 1


async def test_red_branch_without_wired_landing_raises(
    session: AsyncSession, saver: AsyncPostgresSaver
) -> None:
    """未注入 `create_red_order_fn` → 红档节点抛 RuntimeError，不静默降级。"""
    graph = build_health_checkup_graph(session=session, checkpointer=saver)
    with pytest.raises(RuntimeError, match="create_red_order_fn not wired"):
        await _answered(graph, elder_id=uuid.uuid4(), source="voice_active", answers=_RED)


def test_work_orders_never_constructed_in_graph_or_tool_layer() -> None:
    """X3 v0.6 条件 1：B 的图与工具层不构造 `WorkOrder(`，只经 C 的落库包装。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    guarded = [*sorted((root / "app" / "graphs").glob("*.py")), root / "app/services/tool_impls.py"]
    offenders = [
        str(path.relative_to(root))
        for path in guarded
        if "WorkOrder(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"B 侧出现了 WorkOrder 构造：{offenders}"


def test_pending_create_red_order_removed_from_production() -> None:
    """P10 收口：适配层桩必须从生产代码里消失（测试桩可保留）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    hits = [
        str(path.relative_to(root))
        for path in (root / "app").rglob("*.py")
        if "pending_create_red_order" in path.read_text(encoding="utf-8")
    ]
    assert hits == []
