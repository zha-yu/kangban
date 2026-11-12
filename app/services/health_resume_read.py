"""X7 复核 resume 的**只读**查询（B 自持，2026-11-12 裁决 D6）。

为什么单独一个模块：
- `api` 禁 import `graphs`，所以 console 复核只写库、resume 由 scheduler 侧驱动；
- scheduler 又要求「零写库、零 ORM 写」——把这条查询收在一个只读函数里，
  `grep "session.add(|from app.models" app/scheduler/` 才能保持零命中（AGENTS §2.1）。

**只允许 1 个 SELECT**，且**不含任何业务判断**：
- 返回 `record_id / graph_thread_id / review_status / reviewed_by / review_note`；
- 「线程是否仍处 interrupt」由 job 读 checkpointer 判定（DB 无 `reviewed_at` 列，
  扫描水位不依赖时间戳，X7 注）；
- 「confirmed 还是 corrected」也交给 job/图，本模块不过滤 severity。

`thread_id` 只从 `health_records.graph_thread_id` 取，不接受客户端传入。
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health import HealthRecord

#: 已终态的复核状态（X7：`confirmed` / `corrected` 两值触发 resume）
REVIEW_TERMINAL: Final[tuple[str, ...]] = ("confirmed", "corrected")

#: 单次扫描上限：按 recorded_at 倒序取最近的复核记录，避免全表扫。
DEFAULT_RESUME_SCAN_LIMIT: Final[int] = 200


@dataclasses.dataclass(frozen=True, slots=True)
class ResumeRow:
    """一条等待外部复核收口的记录（只读投影，不含分级/状态判断）。"""

    record_id: uuid.UUID
    graph_thread_id: str
    review_status: str
    reviewed_by: uuid.UUID | None
    review_note: str | None


async def list_records_pending_resume(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_RESUME_SCAN_LIMIT,
) -> list[ResumeRow]:
    """列出「复核已终态且带 graph_thread_id」的记录（**唯一 1 条 SELECT**）。

    job 侧再按「对应线程仍 interrupt」二次过滤；这里不判线程状态，
    也不按 severity/日期过滤（X7 明示不靠时间戳水位）。
    """
    stmt = (
        select(
            HealthRecord.record_id,
            HealthRecord.graph_thread_id,
            HealthRecord.review_status,
            HealthRecord.reviewed_by,
            HealthRecord.review_note,
        )
        .where(
            HealthRecord.review_status.in_(REVIEW_TERMINAL),
            HealthRecord.graph_thread_id.is_not(None),
        )
        .order_by(HealthRecord.recorded_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        ResumeRow(
            record_id=row.record_id,
            graph_thread_id=str(row.graph_thread_id),
            review_status=str(row.review_status),
            reviewed_by=row.reviewed_by,
            review_note=row.review_note,
        )
        for row in rows
        if row.graph_thread_id
    ]


__all__ = [
    "DEFAULT_RESUME_SCAN_LIMIT",
    "REVIEW_TERMINAL",
    "ResumeRow",
    "list_records_pending_resume",
]
