"""metrics 分段耗时打点的**唯一写库入口**（裁决 B6/D2，第 27 张表）。

设计要点（docs/plans/day05/a-后端观测与数据.md §2.1 / 自裁 Z3、Z4）：
- **异步批量缓冲**：`record_stage()` 只做校验 + 进程内入队（同步、非阻塞），
  后台任务按 `FLUSH_INTERVAL_SECONDS` 周期批量 INSERT——观测旁路，不阻塞对话主链路；
- **失败降级**：落库失败丢弃该批并记 warning 日志（打点允许丢失，不允许拖垮主链路）；
- **非法值不入库**：stage 不在八值白名单、trace_id 缺失、duration_ms < 0 的行
  直接丢弃记日志（对齐 ER §4.4.7 CHECK 约束，不让 DB 报错回流调用方）；
- **只写指标不写内容**：调用方不应传转写全文/隐私字段；本服务不主动截取任何业务内容。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import insert

from app.db import session_factory
from app.models.metrics import Metric

logger = logging.getLogger("app.services.metrics")

#: stage 八值白名单（对齐 models/metrics.py CHECK 约束；0014 迁移加值 response_segment）
STAGES: frozenset[str] = frozenset(
    {
        "wakeup",
        "connection",
        "asr",
        "route",
        "llm_first_token",
        "tts_first_packet",
        "playback",
        "response_segment",
    }
)

#: 缓冲刷新周期（秒）与单批上限——观测旁路，数值不追求精确，允许整批丢失
FLUSH_INTERVAL_SECONDS: float = 2.0
BATCH_SIZE: int = 100
#: 队列上限：DB 长时间不可用时丢新保旧，避免无界内存增长
QUEUE_MAX: int = 10_000


class MetricsRecorder:
    """进程内单例：入队（同步）+ 后台批量落库（异步）。

    `session_factory` 可注入替换（测试用测试事务连接），生产用 `app.db.session_factory`。
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        flush_interval_seconds: float = FLUSH_INTERVAL_SECONDS,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._flush_interval = flush_interval_seconds
        self._batch_size = batch_size
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._worker: asyncio.Task[None] | None = None

    # ---------------------------------------------------------------- 入队
    def record_stage(
        self,
        trace_id: str | None,
        elder_id: uuid.UUID | str | None,
        stage: str,
        duration_ms: int | float,
        source: str | None,
    ) -> bool:
        """校验并入队一条打点；返回是否被接受（拒绝 = 丢弃 + 日志，不抛异常）。"""
        row = self._normalize_row(trace_id, elder_id, stage, duration_ms, source)
        if row is None:
            return False
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            logger.warning(
                "metrics 队列已满（%d），丢弃一条 stage=%s trace_id=%s",
                QUEUE_MAX,
                stage,
                trace_id,
            )
            return False
        self._ensure_worker()
        return True

    def _normalize_row(
        self,
        trace_id: str | None,
        elder_id: uuid.UUID | str | None,
        stage: str,
        duration_ms: int | float,
        source: str | None,
    ) -> dict[str, Any] | None:
        """字段级校验；非法行丢弃并记日志（自裁 Z4：trace_id 缺失不入库）。"""
        if not trace_id or not isinstance(trace_id, str) or len(trace_id) > 64:
            logger.warning("metrics 丢弃：trace_id 缺失或非法（stage=%s）", stage)
            return None
        if stage not in STAGES:
            logger.warning("metrics 丢弃：非法 stage=%r（trace_id=%s）", stage, trace_id)
            return None
        if not isinstance(duration_ms, (int, float)) or duration_ms < 0:
            logger.warning(
                "metrics 丢弃：duration_ms=%r 非法（stage=%s trace_id=%s）",
                duration_ms,
                stage,
                trace_id,
            )
            return None

        elder_uuid: uuid.UUID | None = None
        if elder_id is not None:
            if isinstance(elder_id, uuid.UUID):
                elder_uuid = elder_id
            elif isinstance(elder_id, str):
                try:
                    elder_uuid = uuid.UUID(elder_id)
                except ValueError:
                    logger.warning("metrics：elder_id 非法置空（trace_id=%s）", trace_id)
        # elder_id 非法只置空不丢行：耗时数据仍有价值（对齐 ER §4.4.7 可空口径）

        src = None
        if source is not None:
            src = str(source)[:32]  # 超 32 字符截断，避免 DB 报错回流

        return {
            "trace_id": trace_id,
            "elder_id": elder_uuid,
            "stage": stage,
            "duration_ms": round(duration_ms),
            "source": src,
        }

    # ---------------------------------------------------------------- 落库
    async def drain_once(self, limit: int | None = None) -> int:
        """把缓冲中至多 `limit` 行批量 INSERT；失败整批丢弃 + warning。返回落库行数。"""
        rows: list[dict[str, Any]] = []
        while len(rows) < (limit or self._batch_size) and not self._queue.empty():
            rows.append(self._queue.get_nowait())
        if not rows:
            return 0
        try:
            async with self._session_factory() as session:
                await session.execute(insert(Metric), rows)
                await session.commit()
        except Exception:
            # 打点允许丢失：整批丢弃并降级为日志告警，不向调用方抛错（设计要点 2）
            logger.warning(
                "metrics 批量落库失败，丢弃 %d 行（降级为日志告警，不影响主链路）",
                len(rows),
                exc_info=True,
            )
            return 0
        return len(rows)

    # ---------------------------------------------------------------- 后台任务
    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        try:
            self._worker = asyncio.get_running_loop().create_task(self._worker_loop())
        except RuntimeError:
            # 无事件循环（罕见：循环外调用）：暂不起后台任务，留在队列等下次入队时起
            return

    async def _worker_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                await self.drain_once()
            except Exception:  # drain_once 内部已兜底，此处再兜一层保 worker 不死
                logger.warning("metrics 后台刷新异常（已降级）", exc_info=True)

    async def stop(self) -> None:
        """优雅停机：取消后台任务并冲刷剩余缓冲。"""
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        while not self._queue.empty():
            await self.drain_once()


_recorder: MetricsRecorder | None = None


def get_metrics_recorder() -> MetricsRecorder:
    """进程级单例（生产用 `app.db.session_factory`）。"""
    global _recorder
    if _recorder is None:
        _recorder = MetricsRecorder(session_factory)
    return _recorder
