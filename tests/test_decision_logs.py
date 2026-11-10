"""A3 · `decision_logs` 数据层：表结构、脱敏、每轮 1 行、澄清判据、失败不阻断。

分两层：

- **纯结构断言**（不需要 PG）：14 列逐字对齐 TD §3.4、CHECK 取值、索引名、迁移链；
- **落库断言**（需要 PG，`db_conn` 无库时自动 skip）：写一行、脱敏生效、`was_clarified`
  的推断口径、写失败后会话仍可用（SAVEPOINT 的作用）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.models.decision import INTENT_VALUES, ROUTE_LAYERS, DecisionLog
from app.services.decision_log_service import record_decision, redact_input, was_clarified

#: TD §3.4 的 14 列，逐字（多一列少一列都算偏离口径）
EXPECTED_COLUMNS: tuple[str, ...] = (
    "log_id",
    "thread_id",
    "trace_id",
    "elder_id",
    "input_text",
    "intent",
    "confidence",
    "is_sensitive",
    "route_layer",
    "escalated_to_s2",
    "latency_ms",
    "asr_confidence",
    "model_version",
    "created_at",
)


@dataclass
class _FakeDecision:
    route_layer: str = "system_one"
    action: str = "agent"
    intent: str = "chat"
    confidence: float = 0.91
    is_sensitive: bool = False
    escalated_to_s2: bool = True
    latency_ms: int = 168


# ------------------------------------------------------------------ 结构（无需 PG）


def test_table_has_exactly_fourteen_columns() -> None:
    assert tuple(DecisionLog.__table__.columns.keys()) == EXPECTED_COLUMNS


def test_route_layer_check_covers_four_values_plus_legacy_laya() -> None:
    assert ROUTE_LAYERS == ("rule", "system_one", "agent", "fallback", "laya")
    assert "laya" in ROUTE_LAYERS  # 历史值只读兼容，去掉会让历史行违反约束
    assert len(INTENT_VALUES) == 7


def test_indexes_match_migration_names() -> None:
    names = {index.name for index in DecisionLog.__table__.indexes}
    assert names == {
        "ix_decision_logs_thread_id_created_at",
        "ix_decision_logs_route_layer_created_at",
        "ix_decision_logs_trace_id",
    }


def test_migration_is_single_head_after_0013() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()

    assert heads == ["0014_decision_logs"]
    revision = script.get_revision("0014_decision_logs")
    assert revision is not None
    assert revision.down_revision == "0013_touch_played_receipt"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我手机号是13812345678", "我手机号是138****5678"),
        ("身份证110101199003071234", "身份证110101********1234"),
        ("我住302房", "我住***房"),
        ("今天天气不错", "今天天气不错"),
    ],
)
def test_redact_input_masks_pii(text: str, expected: str) -> None:
    assert redact_input(text) == expected


async def test_generic_turn_writes_nothing() -> None:
    """generic 轮次没有 elder_id，而该列 NOT NULL：契约规定不写本表。"""

    class _ExplodingSession:
        def begin_nested(self) -> Any:  # pragma: no cover - 不该被调用
            raise AssertionError("generic 轮次不应触碰会话")

    written = await record_decision(
        _ExplodingSession(),  # type: ignore[arg-type]
        thread_id="th-generic",
        elder_id=None,
        trace_id="trace-1",
        input_text="你好",
        decision=_FakeDecision(route_layer="agent"),
        model_version="laya:multilingual:dev",
    )
    assert written is False


# ------------------------------------------------------------------ 落库（需 PG）


async def _seed_thread(conn: AsyncConnection, thread_id: str) -> None:
    """最小合法链：rooms → devices → conversation_threads（decision_logs 有 FK）。"""
    room_id = (
        await conn.execute(
            sa.text("INSERT INTO rooms (room_no) VALUES (:no) RETURNING room_id"),
            {"no": f"A-{uuid.uuid4().hex[:6]}"},
        )
    ).scalar_one()
    device_id = (
        await conn.execute(
            sa.text(
                "INSERT INTO devices (room_id, token_hash) VALUES (:room, :hash) RETURNING device_id"
            ),
            {"room": room_id, "hash": uuid.uuid4().hex},
        )
    ).scalar_one()
    await conn.execute(
        sa.text(
            "INSERT INTO conversation_threads (thread_id, device_id, identity_source) "
            "VALUES (:tid, :did, 'generic')"
        ),
        {"tid": thread_id, "did": device_id},
    )


async def test_record_decision_writes_one_row_with_redacted_text(
    db_conn: AsyncConnection,
) -> None:
    thread_id = f"th-{uuid.uuid4().hex[:8]}"
    await _seed_thread(db_conn, thread_id)
    elder_id = uuid.uuid4()

    async with AsyncSession(bind=db_conn, expire_on_commit=False) as session:
        written = await record_decision(
            session,
            thread_id=thread_id,
            elder_id=elder_id,
            trace_id="trace-abc123",
            input_text="我手机号13812345678，住302房",
            decision=_FakeDecision(),
            model_version="laya:multilingual:q1",
        )
        assert written is True

    rows = (
        await db_conn.execute(
            sa.text(
                "SELECT intent, confidence, route_layer, escalated_to_s2, latency_ms, "
                "model_version, input_text, trace_id, is_sensitive "
                "FROM decision_logs WHERE thread_id = :tid"
            ),
            {"tid": thread_id},
        )
    ).all()

    assert len(rows) == 1, "每轮恰好 1 行"
    row = rows[0]
    assert row.intent == "chat"
    assert row.route_layer == "system_one"
    assert row.escalated_to_s2 is True
    assert row.latency_ms == 168
    assert row.model_version == "laya:multilingual:q1"
    assert row.trace_id == "trace-abc123"
    assert row.is_sensitive is False
    assert row.confidence == Decimal("0.910")
    # 脱敏必须在落库前完成（grep 数据库不该出现明文手机号）
    assert "13812345678" not in row.input_text
    assert "302" not in row.input_text


async def test_was_clarified_only_looks_at_last_row(db_conn: AsyncConnection) -> None:
    thread_id = f"th-{uuid.uuid4().hex[:8]}"
    await _seed_thread(db_conn, thread_id)
    elder_id = uuid.uuid4()

    async with AsyncSession(bind=db_conn, expire_on_commit=False) as session:
        assert await was_clarified(session, thread_id) is False  # 没有历史

        # 澄清轮：低置信 + 未升级
        await record_decision(
            session,
            thread_id=thread_id,
            elder_id=elder_id,
            trace_id="t-1",
            input_text="那个东西",
            decision=_FakeDecision(
                route_layer="system_one", action="clarify", confidence=0.4, escalated_to_s2=False
            ),
            model_version="laya:multilingual:q1",
        )
        assert await was_clarified(session, thread_id) is True

        # 澄清之后的第二次（升级 Agent）：不再算"已澄清"
        await record_decision(
            session,
            thread_id=thread_id,
            elder_id=elder_id,
            trace_id="t-2",
            input_text="那个东西",
            decision=_FakeDecision(
                route_layer="system_one", action="agent", confidence=0.4, escalated_to_s2=True
            ),
            model_version="laya:multilingual:q1",
        )
        assert await was_clarified(session, thread_id) is False


async def test_record_decision_failure_does_not_break_session(
    db_conn: AsyncConnection,
) -> None:
    """写失败只丢一条统计：SAVEPOINT 让同一个会话还能继续干活。"""
    thread_id = f"th-{uuid.uuid4().hex[:8]}"
    await _seed_thread(db_conn, thread_id)

    async with AsyncSession(bind=db_conn, expire_on_commit=False) as session:
        broken = _FakeDecision(intent="not_an_intent")  # 撞 CHECK 约束
        written = await record_decision(
            session,
            thread_id=thread_id,
            elder_id=uuid.uuid4(),
            trace_id="t-1",
            input_text="随便说点什么",
            decision=broken,
            model_version="laya:multilingual:q1",
        )
        assert written is False

        # 会话仍可用：紧接着写一条合法的
        ok = await record_decision(
            session,
            thread_id=thread_id,
            elder_id=uuid.uuid4(),
            trace_id="t-2",
            input_text="今天天气怎么样",
            decision=_FakeDecision(intent="weather"),
            model_version="laya:multilingual:q1",
        )
        assert ok is True
