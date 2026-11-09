"""Day 6 N11 · KnowledgeService 测试（dev-C 计划 C-6 量化验收）。

覆盖五组断言：
    1. 有数据返回 + 元数据齐（source / queried_at UTC / filters，且 status 缺省
       只取 active、可传 None 放开、可传 stopped 只取停用）；
    2. 无数据返回空列表不编造（查无此人的 elder_id 也只是空，不抛「查不到」）；
    3. 强制 elder_id 过滤（其他长者的行不可见）；
    4. 返回字段与插入值逐项一致（MedicationPlanFact 完整性）；
    5. 只读验证：查询前后该表行数不变（本服务不产生任何写操作）。

DB 用例沿用 tests/test_safety.py 的事务回滚模式：conftest 的 module 级共享引擎 +
每用例一连接一事务，夹具统一回滚；medication_plans 的 ORM 未建（app/models 本日
禁触），种子数据走 ``__table__.insert()`` Core 写入（列对齐迁移 0010）。
库不可达按 conftest 口径 skip 并说明原因，不静默通过。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.models import Elder, StaffAccount
from app.services.knowledge_service import (
    MEDICATION_PLANS_SOURCE,
    KnowledgeQueryResult,
    query_medication_plans,
)

# 占位 UUID：固定值便于断言，均为虚构（AGENTS §4.3，不写真实长者数据）
ELDER_A = uuid.UUID("aaaa1111-2222-3333-4444-555555555555")
ELDER_B = uuid.UUID("bbbb1111-2222-3333-4444-555555555555")
DOCTOR_ID = uuid.UUID("cccc1111-2222-3333-4444-555555555555")

FIXED_NOW = datetime.datetime(2026, 11, 9, 12, 0, 0, tzinfo=datetime.UTC)


@pytest.fixture
async def knowledge_db(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """每用例一个事务、结束回滚；连不上库就 skip 并说明原因。"""
    try:
        conn = await db_engine.connect()
    except Exception as exc:  # 任何连接失败都算「无库」
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")

    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


def _borrow_session(conn: AsyncConnection) -> AsyncSession:
    """把夹具那条未提交的事务借给被测代码（本服务只读、不 commit）。"""
    return AsyncSession(bind=conn, expire_on_commit=False)


async def _seed_plans(conn: AsyncConnection) -> dict[str, uuid.UUID]:
    """种子：两位长者 + 医生 + 药单（甲 2 active + 1 stopped；乙 1 active）。

    medication_plans 无 ORM（0010 只建了表），种子走本地 Core Table（独立
    MetaData，不挂 Base.metadata），列对齐迁移 0010。

    Returns:
        各药单 plan_id（insert 未指定主键时由 gen_random_uuid() 生成）。
    """
    await conn.execute(Elder.__table__.insert().values(elder_id=ELDER_A, name="测试长者甲"))
    await conn.execute(Elder.__table__.insert().values(elder_id=ELDER_B, name="测试长者乙"))
    await conn.execute(
        StaffAccount.__table__.insert().values(
            staff_id=DOCTOR_ID, name="测试医生", role="doctor", password_hash="test-hash"
        )
    )

    plans = sa.Table(
        "medication_plans",
        sa.MetaData(),
        sa.Column(
            "plan_id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("elder_id", sa.UUID(), nullable=False),
        sa.Column("drug_name", sa.Text(), nullable=False),
        sa.Column("dose", sa.Text(), nullable=False),
        sa.Column("schedule_cron", sa.Text(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
    )

    async def _insert_plan(
        elder_id: uuid.UUID, drug: str, status: str, *, start: datetime.date
    ) -> uuid.UUID:
        row = (
            await conn.execute(
                sa.insert(plans)
                .values(
                    elder_id=elder_id,
                    drug_name=drug,
                    dose="5mg 一片",
                    schedule_cron="0 8 * * *",
                    start_date=start,
                    created_by=DOCTOR_ID,
                    status=status,
                )
                .returning(plans.c.plan_id)
            )
        ).one()
        return row.plan_id

    return {
        # 甲的两条 active 用不同 start_date：服务的排序键是 (start_date, drug_name)，
        # 中文药名的次级排序依赖库端 collation，不作为断言依据
        "a_active_1": await _insert_plan(
            ELDER_A, "降压药A", "active", start=datetime.date(2026, 11, 1)
        ),
        "a_active_2": await _insert_plan(
            ELDER_A, "钙片", "active", start=datetime.date(2026, 11, 5)
        ),
        "a_stopped": await _insert_plan(
            ELDER_A, "旧药C", "stopped", start=datetime.date(2026, 11, 1)
        ),
        "b_active_1": await _insert_plan(
            ELDER_B, "长者乙的药", "active", start=datetime.date(2026, 11, 1)
        ),
    }


# ============================================================
# 1. 有数据返回 + 元数据齐
# ============================================================


async def test_returns_plans_with_full_metadata(knowledge_db: AsyncConnection) -> None:
    plan_ids = await _seed_plans(knowledge_db)

    result: KnowledgeQueryResult = await query_medication_plans(
        _borrow_session(knowledge_db), elder_id=ELDER_A, now=FIXED_NOW
    )

    # 元数据齐
    assert result.source == MEDICATION_PLANS_SOURCE == "medication_plans"
    assert result.queried_at == FIXED_NOW
    assert result.queried_at.tzinfo is datetime.UTC
    assert result.filters == {"elder_id": str(ELDER_A), "status": "active"}

    # 缺省 status="active"：甲的 2 条 active，stopped 不混入
    assert [item.plan_id for item in result.items] == [
        plan_ids["a_active_1"],
        plan_ids["a_active_2"],
    ]
    first = result.items[0]
    assert first.drug_name == "降压药A"
    assert first.dose == "5mg 一片"
    assert first.schedule_cron == "0 8 * * *"
    assert first.start_date == datetime.date(2026, 11, 1)
    assert first.end_date is None
    assert first.version == 1
    assert first.status == "active"


async def test_status_none_returns_all_statuses(knowledge_db: AsyncConnection) -> None:
    plan_ids = await _seed_plans(knowledge_db)

    result = await query_medication_plans(
        _borrow_session(knowledge_db), elder_id=ELDER_A, status=None, now=FIXED_NOW
    )

    assert result.filters == {"elder_id": str(ELDER_A), "status": None}
    assert {item.plan_id for item in result.items} == {
        plan_ids["a_active_1"],
        plan_ids["a_active_2"],
        plan_ids["a_stopped"],
    }
    assert {item.status for item in result.items} == {"active", "stopped"}


async def test_status_stopped_returns_only_stopped(knowledge_db: AsyncConnection) -> None:
    """显式传 status="stopped"：只取停用计划，active 不混入。"""
    plan_ids = await _seed_plans(knowledge_db)

    result = await query_medication_plans(
        _borrow_session(knowledge_db), elder_id=ELDER_A, status="stopped", now=FIXED_NOW
    )

    assert result.filters == {"elder_id": str(ELDER_A), "status": "stopped"}
    assert [item.plan_id for item in result.items] == [plan_ids["a_stopped"]]
    assert {item.status for item in result.items} == {"stopped"}


async def test_medication_plan_fact_fields_match_inserted_values(
    knowledge_db: AsyncConnection,
) -> None:
    """返回字段完整性：MedicationPlanFact 各字段与插入值逐项一致。"""
    plan_ids = await _seed_plans(knowledge_db)

    result = await query_medication_plans(
        _borrow_session(knowledge_db), elder_id=ELDER_B, status=None, now=FIXED_NOW
    )

    assert len(result.items) == 1
    fact = result.items[0]
    assert fact.plan_id == plan_ids["b_active_1"]
    assert fact.drug_name == "长者乙的药"
    assert fact.dose == "5mg 一片"
    assert fact.schedule_cron == "0 8 * * *"
    assert fact.start_date == datetime.date(2026, 11, 1)
    assert fact.end_date is None  # 插入未指定 → 库端 NULL
    assert fact.version == 1  # 插入未指定 → server_default 1
    assert fact.status == "active"  # 插入未指定 → server_default 'active'


async def test_query_is_read_only(knowledge_db: AsyncConnection) -> None:
    """只读验证：查询前后 medication_plans 行数不变（服务不产生任何写操作）。"""
    plan_ids = await _seed_plans(knowledge_db)
    plans = sa.Table(
        "medication_plans",
        sa.MetaData(),
        sa.Column("plan_id", sa.UUID(), primary_key=True),
    )
    before = (
        await knowledge_db.execute(sa.select(sa.func.count()).select_from(plans))
    ).scalar_one()
    assert before == 4

    session = _borrow_session(knowledge_db)
    await query_medication_plans(session, elder_id=ELDER_A, status=None)
    await query_medication_plans(session, elder_id=ELDER_A, status="stopped")
    await query_medication_plans(session, elder_id=ELDER_A)  # 缺省 active

    after = (await knowledge_db.execute(sa.select(sa.func.count()).select_from(plans))).scalar_one()

    assert after == before == 4
    assert len(plan_ids) == 4


# ============================================================
# 2. 无数据返回空，不编造
# ============================================================


async def test_unknown_elder_returns_empty_without_fabrication(
    knowledge_db: AsyncConnection,
) -> None:
    """库中完全没有该长者的药单（连长者档案都没有）：items 为空 + 元数据照常。"""
    await _seed_plans(knowledge_db)
    stranger = uuid.UUID("dddd1111-2222-3333-4444-555555555555")

    result = await query_medication_plans(
        _borrow_session(knowledge_db), elder_id=stranger, now=FIXED_NOW
    )

    assert result.items == []
    assert result.source == "medication_plans"
    assert result.queried_at == FIXED_NOW
    assert result.filters == {"elder_id": str(stranger), "status": "active"}


# ============================================================
# 3. 强制 elder_id 过滤（其他长者的行不可见）
# ============================================================


async def test_strict_elder_id_isolation(knowledge_db: AsyncConnection) -> None:
    plan_ids = await _seed_plans(knowledge_db)
    session = _borrow_session(knowledge_db)

    result_a = await query_medication_plans(session, elder_id=ELDER_A, status=None)
    assert all(item.plan_id != plan_ids["b_active_1"] for item in result_a.items)
    assert len(result_a.items) == 3  # 甲的全部药单，没有乙的

    result_b = await query_medication_plans(session, elder_id=ELDER_B, status=None)
    assert [item.plan_id for item in result_b.items] == [plan_ids["b_active_1"]]
    assert all(item.plan_id != plan_ids["a_active_1"] for item in result_b.items)
