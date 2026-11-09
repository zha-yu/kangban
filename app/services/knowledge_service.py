"""权威业务事实的结构化查询服务（Day 6 N11 · dev-C）。

边界（dev-C 计划 C-6 / 技术设计文档 §6.1 口径）：

- 只做**权威业务事实**的结构化查询：本日仅用药计划（``medication_plans``）按
  ``elder_id`` 查询；**不做向量检索、不做 RAG**——机构文档检索属 Day 7 技术债
  （登记于 dev-C 计划 C-6，是节点内边界，不是裁减 N11）；
- **强制 WHERE elder_id**：调用方传入的 ``elder_id`` 必须来自服务端
  ``runtime_context``（AGENTS §4-9：客户端与模型传入的值一律丢弃，由
  ToolExecutor 层保证）；其他长者的行在本服务不可见；
- 读不到 / 无结果 → 返回空列表，**不编造**（FR-CON-02 同口径：Agent 拿到空
  就说「暂时查不到」）；
- 每次返回带元数据：``source``（数据来源表）、``queried_at``（UTC）、
  ``filters``（实际查询条件）——Agent 不接裸行；
- 只读不写：本模块不产生任何 INSERT/UPDATE（写库仍由各领域服务唯一承担，
  AGENTS §2）。

关于 ORM：``medication_plans`` 的 ORM 模型尚未建立（0010 迁移只建了表；
``app/models/**`` 本日禁触），故照 ``app/services/safety_service.py`` 的先例用
**模块私有 Core Table**（独立 ``MetaData``、不挂 ``Base.metadata``，不影响
autogenerate）做只读查询，列定义逐列对齐迁移 0010。ORM 建立后应改回 ORM 查询
并删除本模块的 Table 定义。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_trace_id

logger = logging.getLogger(__name__)

#: ``KnowledgeQueryResult.source`` 的取值：数据来源表名
MEDICATION_PLANS_SOURCE: Final[str] = "medication_plans"

# ---------------------------------------------------------------------------
# medication_plans 的只读 Core 定义（列对齐迁移 0010；独立 MetaData，
# 绝不挂 Base.metadata，避免 autogenerate 把它当待建表产生漂移 diff）
# ---------------------------------------------------------------------------

_LOCAL_META = sa.MetaData()

_medication_plans = sa.Table(
    "medication_plans",
    _LOCAL_META,
    sa.Column("plan_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("elder_id", UUID(as_uuid=True), nullable=False),
    sa.Column("drug_name", sa.Text(), nullable=False),
    sa.Column("dose", sa.Text(), nullable=False),
    sa.Column("schedule_cron", sa.Text(), nullable=False),
    sa.Column("start_date", sa.Date(), nullable=False),
    sa.Column("end_date", sa.Date(), nullable=True),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("created_by", UUID(as_uuid=True), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True, slots=True)
class MedicationPlanFact:
    """一条用药计划事实（面向 Agent 播报的最小字段集）。

    ``elder_id`` / ``created_by`` 不进事实：查询本身已按 ``elder_id`` 收敛，
    元数据 ``filters`` 记录了实际条件；创建人是内部字段，长者无需感知。
    """

    plan_id: uuid.UUID
    drug_name: str
    dose: str
    schedule_cron: str
    start_date: datetime.date
    end_date: datetime.date | None
    version: int
    status: str


@dataclass(frozen=True, slots=True)
class KnowledgeQueryResult:
    """一次权威事实查询的结果 + 元数据。

    ``items`` 为空表示「查不到」，调用方（Agent / ToolExecutor）必须如实告知，
    不得用模型常识补齐（FR-CON-02）。
    """

    source: str
    queried_at: datetime.datetime  # 一律 UTC
    filters: dict[str, object]
    items: list[MedicationPlanFact]


def _utc_now_or(now: datetime.datetime | None) -> datetime.datetime:
    """元数据时间戳：缺省取当前 UTC；naive 输入按 UTC 解释（存储一律 UTC）。"""
    if now is None:
        return datetime.datetime.now(datetime.UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=datetime.UTC)
    return now.astimezone(datetime.UTC)


async def query_medication_plans(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    status: str | None = "active",
    now: datetime.datetime | None = None,
) -> KnowledgeQueryResult:
    """按 ``elder_id`` 查询用药计划（强制过滤，无结果返回空列表）。

    Args:
        session: 调用方拥有的 async session（本函数只读、不 commit）。
        elder_id: 长者 ID；**必须来自服务端 runtime_context**，其他长者的行不可见。
        status: 计划状态过滤（0010 枚举 ``active``/``stopped``）；缺省只取
            ``active``，传 ``None`` 表示不过滤状态。
        now: 元数据时间戳；测试可注入固定值，缺省取当前 UTC。

    Returns:
        带 ``source`` / ``queried_at``（UTC） / ``filters`` 元数据的结果；
        无匹配行时 ``items`` 为空列表，不抛「查不到」类异常。
    """
    filters: dict[str, object] = {"elder_id": str(elder_id), "status": status}
    stmt = sa.select(
        _medication_plans.c.plan_id,
        _medication_plans.c.drug_name,
        _medication_plans.c.dose,
        _medication_plans.c.schedule_cron,
        _medication_plans.c.start_date,
        _medication_plans.c.end_date,
        _medication_plans.c.version,
        _medication_plans.c.status,
    ).where(_medication_plans.c.elder_id == elder_id)
    if status is not None:
        stmt = stmt.where(_medication_plans.c.status == status)
    stmt = stmt.order_by(_medication_plans.c.start_date, _medication_plans.c.drug_name)

    rows = (await session.execute(stmt)).all()
    items = [
        MedicationPlanFact(
            plan_id=row.plan_id,
            drug_name=row.drug_name,
            dose=row.dose,
            schedule_cron=row.schedule_cron,
            start_date=row.start_date,
            end_date=row.end_date,
            version=row.version,
            status=row.status,
        )
        for row in rows
    ]

    logger.info(
        "knowledge_medication_plans_queried",
        extra={
            "trace_id": get_trace_id() or "-",
            "elder_id": str(elder_id),
            "status": status,
            "result_count": len(items),
        },
    )
    return KnowledgeQueryResult(
        source=MEDICATION_PLANS_SOURCE,
        queried_at=_utc_now_or(now),
        filters=filters,
        items=items,
    )


__all__ = [
    "MEDICATION_PLANS_SOURCE",
    "KnowledgeQueryResult",
    "MedicationPlanFact",
    "query_medication_plans",
]
