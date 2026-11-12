"""Day 9 B2 · X5 记忆管理三函数 + X4/X12 清理钩子（契约 X4/X5/X12）。

验收判据对应（计划 §2.3）：
- 跨长者 `chunk_id` → **审计 + 领域异常**（闸门②，AC-39）；
- 纠正：旧值 `superseded` 保留、新值生效、`supersedes_id` 指向旧值（FR-MEM-03/06）；
- 删除：立即不再召回（FR-MEM-05/08）；
- X4/X12 幂等：同 `(elder_id, reason)` 第二次调用返回 0 行（契约 X4/X12）；
- `reason` 只接受 `checkout` / `teaching_reset`。

DB 用例连测试库（PG 不可达自动 skip），每用例一事务、结束回滚。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import AuditLog
from app.models.medication import MedicationAttempt, MedicationEvent, MedicationPlan
from app.models.memory import MemoryChunk
from app.models.organization import Elder
from app.models.staff import StaffAccount
from app.services import medication_service
from app.services import memory_service as memory_service_module
from app.services.memory_service import (
    MemoryAccessDenied,
    MemoryValidationError,
    correct_memory,
    delete_memory,
    list_memories,
    purge_elder_memories,
    recall_active_chunks,
)

# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    try:
        conn = await db_engine.connect()
    except Exception as exc:  # pragma: no cover - 本地无 PG 时跳过
        pytest.skip(f"PostgreSQL 不可达，跳过记忆管理 DB 用例：{exc!r}")

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


async def _elder(session: AsyncSession, name: str) -> uuid.UUID:
    elder = Elder(name=name, status="active")
    session.add(elder)
    await session.flush()
    return elder.elder_id


async def _chunk(
    session: AsyncSession,
    elder: uuid.UUID,
    *,
    content: str,
    status: str = "active",
    predicate: str = "preferred_opera_genre",
) -> MemoryChunk:
    chunk = MemoryChunk(
        elder_id=elder,
        subject=str(elder),
        predicate=predicate,
        object_value=content[-2:],
        polarity="positive",
        content=content,
        confidence=0.8,
        sensitivity="private",
        source_type="elder_explicit",
        source_turn_ids=["tr-1"],
        status=status,
        valid_from=datetime.datetime.now(datetime.UTC),
    )
    session.add(chunk)
    await session.flush()
    return chunk


def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_embed(texts: Any, **_kwargs: Any) -> list[list[float]]:
        return [[0.0] * 1535 + [1.0] for _ in texts]

    monkeypatch.setattr(memory_service_module, "_embed_texts", fake_embed)


# ------------------------------------------------------------------ X5 · list_memories


class TestListMemories:
    async def test_only_own_rows_and_deleted_excluded(self, session: AsyncSession) -> None:
        mei = await _elder(session, "记忆甲")
        other = await _elder(session, "记忆乙")
        await _chunk(session, mei, content="长者甲喜欢越剧")
        await _chunk(session, mei, content="长者甲喜欢京剧", status="superseded")
        await _chunk(session, mei, content="长者甲喜欢黄梅戏", status="deleted")
        await _chunk(session, other, content="长者乙喜欢越剧")

        views = await list_memories(session, elder_id=mei)

        assert {view.content for view in views} == {"长者甲喜欢越剧", "长者甲喜欢京剧"}
        assert all(view.elder_id == mei for view in views)

    async def test_pagination(self, session: AsyncSession) -> None:
        elder = await _elder(session, "分页长者")
        for index in range(3):
            await _chunk(session, elder, content=f"记忆{index}", predicate=f"pred_{index}")

        page = await list_memories(session, elder_id=elder, limit=1, offset=1)
        assert len(page) == 1


# ------------------------------------------------------------------ X5 · correct_memory


class TestCorrectMemory:
    async def test_keeps_old_as_superseded_and_new_active(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_embedding(monkeypatch)
        elder = await _elder(session, "纠正长者")
        old = await _chunk(session, elder, content="长者喜欢越剧")

        result = await correct_memory(
            session, elder_id=elder, chunk_id=old.chunk_id, new_text="长者不喜欢越剧了"
        )

        old_row = await session.get(MemoryChunk, old.chunk_id)
        new_row = await session.get(MemoryChunk, result.chunk_id)
        assert old_row is not None and new_row is not None
        assert old_row.status == "superseded"
        assert old_row.valid_to is not None
        assert old_row.content == "长者喜欢越剧"  # 旧值保留（不物理删）
        assert new_row.status == "active"
        assert new_row.supersedes_id == old.chunk_id
        assert new_row.content == "长者不喜欢越剧了"
        assert result.status == "active"
        assert result.supersedes_id == old.chunk_id

        # 召回只剩新值（旧值 superseded 退出）
        rows = await recall_active_chunks(
            elder_id=str(elder), is_generic=False, query="越剧", session=session
        )
        assert [r.content for r in rows] == ["长者不喜欢越剧了"]

    async def test_cross_elder_denied_with_audit(self, session: AsyncSession) -> None:
        mei = await _elder(session, "纠正甲")
        other = await _elder(session, "纠正乙")
        target = await _chunk(session, other, content="长者乙喜欢越剧")

        with pytest.raises(MemoryAccessDenied):
            await correct_memory(session, elder_id=mei, chunk_id=target.chunk_id, new_text="篡改")

        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "memory_cross_elder_access_denied",
                    AuditLog.resource_id == target.chunk_id,
                )
            )
        ).scalar_one()
        assert audit.detail["operation"] == "correct"
        assert audit.detail["gate"] == "elder_id"

        untouched = await session.get(MemoryChunk, target.chunk_id)
        assert untouched.status == "active"
        assert untouched.content == "长者乙喜欢越剧"

    async def test_empty_new_text_rejected(self, session: AsyncSession) -> None:
        elder = await _elder(session, "空文本长者")
        chunk = await _chunk(session, elder, content="长者喜欢越剧")
        with pytest.raises(MemoryValidationError):
            await correct_memory(session, elder_id=elder, chunk_id=chunk.chunk_id, new_text="  ")


# ------------------------------------------------------------------ X5 · delete_memory


class TestDeleteMemory:
    async def test_delete_excludes_from_recall(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_embedding(monkeypatch)
        elder = await _elder(session, "删除长者")
        chunk = await _chunk(session, elder, content="长者喜欢越剧")
        await _chunk(session, elder, content="长者喜欢京剧", predicate="preferred_movie_genre")

        result = await delete_memory(session, elder_id=elder, chunk_id=chunk.chunk_id)

        assert result.status == "deleted"
        row = await session.get(MemoryChunk, chunk.chunk_id)
        assert row.status == "deleted"
        assert row.valid_to is not None

        rows = await recall_active_chunks(
            elder_id=str(elder), is_generic=False, query="越剧", session=session
        )
        assert [r.content for r in rows] == ["长者喜欢京剧"]  # 删除条不再注入

        # 幂等：重复删同一条不报错、不改语义
        again = await delete_memory(session, elder_id=elder, chunk_id=chunk.chunk_id)
        assert again.status == "deleted"

    async def test_cross_elder_denied_with_audit(self, session: AsyncSession) -> None:
        mei = await _elder(session, "删除甲")
        other = await _elder(session, "删除乙")
        target = await _chunk(session, other, content="长者乙喜欢越剧")

        with pytest.raises(MemoryAccessDenied):
            await delete_memory(session, elder_id=mei, chunk_id=target.chunk_id)

        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "memory_cross_elder_access_denied",
                    AuditLog.resource_id == target.chunk_id,
                )
            )
        ).scalar_one()
        assert audit.detail["operation"] == "delete"

        untouched = await session.get(MemoryChunk, target.chunk_id)
        assert untouched.status == "active"


# ------------------------------------------------------------------ X4 · purge_elder_memories


class TestPurgeMemories:
    async def test_deletes_all_rows_and_is_idempotent(self, session: AsyncSession) -> None:
        mei = await _elder(session, "清理甲")
        other = await _elder(session, "清理乙")
        await _chunk(session, mei, content="长者甲喜欢越剧")
        await _chunk(session, mei, content="长者甲喜欢京剧", status="superseded")
        await _chunk(session, other, content="长者乙喜欢越剧")

        first = await purge_elder_memories(session, elder_id=mei, reason="checkout")
        assert first.domain == "memory"
        assert first.affected_rows == 2
        assert first.deleted_chunks == 2
        assert first.audit_id is None  # 审计由 A 统一写

        remaining = (
            (await session.execute(select(MemoryChunk).where(MemoryChunk.elder_id == mei)))
            .scalars()
            .all()
        )
        assert remaining == []
        other_rows = (
            (await session.execute(select(MemoryChunk).where(MemoryChunk.elder_id == other)))
            .scalars()
            .all()
        )
        assert len(other_rows) == 1  # 不误删他人

        second = await purge_elder_memories(session, elder_id=mei, reason="checkout")
        assert second.affected_rows == 0  # 幂等

    async def test_invalid_reason_rejected(self, session: AsyncSession) -> None:
        elder = await _elder(session, "原因校验长者")
        with pytest.raises(ValueError, match="invalid purge reason"):
            await purge_elder_memories(session, elder_id=elder, reason="manual")


# ------------------------------------------------------------------ X12 · medication purge_elder_data


async def _seed_medication(session: AsyncSession, elder: uuid.UUID, tag: str) -> uuid.UUID:
    doctor = StaffAccount(name=f"医生{tag}", role="doctor", password_hash="x" * 32)
    session.add(doctor)
    await session.flush()
    plan = MedicationPlan(
        elder_id=elder,
        drug_name="氨氯地平",
        dose="5mg 一片",
        schedule_cron="0 8 * * *",
        start_date=datetime.date(2026, 11, 1),
        created_by=doctor.staff_id,
    )
    session.add(plan)
    await session.flush()
    reminder_id = await medication_service.create_event(
        session,
        plan_id=plan.plan_id,
        elder_id=elder,
        scheduled_at=datetime.datetime(2026, 11, 12, 8, 0, tzinfo=datetime.UTC),
        drug_snapshot={"drug_name": "氨氯地平", "dose": "5mg 一片"},
        triggered_by="scheduler",
    )
    await medication_service.record_attempt(
        session, event_id=reminder_id, attempt_no=1, receipt="voice_dispatched"
    )
    return reminder_id


class TestPurgeMedication:
    async def test_deletes_attempts_events_plans_and_is_idempotent(
        self, session: AsyncSession
    ) -> None:
        mei = await _elder(session, "用药甲")
        other = await _elder(session, "用药乙")
        reminder_id = await _seed_medication(session, mei, "甲")
        await _seed_medication(session, other, "乙")

        first = await medication_service.purge_elder_data(
            session, elder_id=mei, reason="teaching_reset"
        )
        assert first.domain == "medication"
        assert first.affected_rows == 3  # 1 attempt + 1 event + 1 plan
        assert first.archived_rows == 0

        assert await session.get(MedicationEvent, reminder_id) is None
        attempts = (
            (
                await session.execute(
                    select(MedicationAttempt).where(MedicationAttempt.event_id == reminder_id)
                )
            )
            .scalars()
            .all()
        )
        assert attempts == []
        plans = (
            (await session.execute(select(MedicationPlan).where(MedicationPlan.elder_id == mei)))
            .scalars()
            .all()
        )
        assert plans == []
        # 他人数据保留
        other_plans = (
            (await session.execute(select(MedicationPlan).where(MedicationPlan.elder_id == other)))
            .scalars()
            .all()
        )
        assert len(other_plans) == 1

        second = await medication_service.purge_elder_data(
            session, elder_id=mei, reason="teaching_reset"
        )
        assert second.affected_rows == 0  # 幂等

    async def test_invalid_reason_rejected(self, session: AsyncSession) -> None:
        elder = await _elder(session, "用药原因长者")
        with pytest.raises(medication_service.MedicationError, match="invalid purge reason"):
            await medication_service.purge_elder_data(session, elder_id=elder, reason="manual")


__all__: list[str] = []
