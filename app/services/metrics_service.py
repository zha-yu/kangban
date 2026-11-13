"""metrics 分段耗时打点的**唯一写库入口**（裁决 B6/D2，第 27 张表）。

设计要点（docs/plans/day05/a-后端观测与数据.md §2.1 / 自裁 Z3、Z4）：
- **异步批量缓冲**：`record_stage()` 只做校验 + 进程内入队（同步、非阻塞），
  后台任务按 `FLUSH_INTERVAL_SECONDS` 周期批量 INSERT——观测旁路，不阻塞对话主链路；
- **失败降级**：落库失败丢弃该批并记 warning 日志（打点允许丢失，不允许拖垮主链路）；
- **非法值不入库**：stage 不在九值白名单、trace_id 缺失、duration_ms < 0 的行
  直接丢弃记日志（对齐 ER §4.4.7 CHECK 约束，不让 DB 报错回流调用方）；
- **只写指标不写内容**：调用方不应传转写全文/隐私字段；本服务不主动截取任何业务内容。

**LLM token 用量（P15 / dev-C 需求，0021 已落地）**：`record_llm_usage()` =
「结构化日志 + 进程内计数 + 落库一行 stage=llm_tokens（duration_ms=0，
token 数进 0021 新增的可空列，NULL = 供应商未回 usage）」。跨重启 / 多 worker
的聚合查库即得；`snapshot_llm_usage()` 的进程内计数继续服务单实例即时读数。
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_trace_id
from app.db import session_factory
from app.models.metrics import Metric

logger = logging.getLogger("app.services.metrics")

#: stage 九值白名单（对齐 models/metrics.py CHECK 约束；0014 加 response_segment，
#: 0021 加 llm_tokens——P15 LLM token 用量落库，主理人授权 B 自行交付）
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
        "llm_tokens",
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
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> bool:
        """校验并入队一条打点；返回是否被接受（拒绝 = 丢弃 + 日志，不抛异常）。

        `*_tokens` 仅 stage=llm_tokens 使用（P15）：None = 供应商未回 usage，
        落 NULL 不补零；非法值（非 int/负数/bool）防御性置 None。
        """
        row = self._normalize_row(
            trace_id,
            elder_id,
            stage,
            duration_ms,
            source,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
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
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
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
            "prompt_tokens": _token_or_none(prompt_tokens),
            "completion_tokens": _token_or_none(completion_tokens),
            "total_tokens": _token_or_none(total_tokens),
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


# ------------------------------------------------------------------ F26 /metrics 读侧
# TD:1182 口径：/metrics 输出分层命中率、S1 闭环率与 token 消耗。进程内计数
# （snapshot_llm_usage）提供 available 语义；持久化经 0021（P15）落
# `metrics` 表 stage=llm_tokens——跨重启/多 worker 的成本统计查库即得。


async def read_decision_metrics(
    session: AsyncSession,
    *,
    since: datetime.datetime | None = None,
) -> dict[str, Any]:
    """路由决策聚合（只读）：分层命中率 + S1 闭环率 + S2 升级率。

    口径（TD §8.3 Day 10 技术验收行）：
    - **分层命中率**：``GROUP BY route_layer`` 计数与占比（含历史 ``laya``）；
    - **S1 闭环率**：``route_layer='system_one'`` 且**未升级** S2 的轮次 / 总轮次
      ——S1（Laya）直接闭环说明未惊动大模型；
    - **S2 升级率**：``escalated_to_s2=true`` 占比（低置信澄清后升级与敏感升级均计）。
    """
    from sqlalchemy import select

    from app.models.decision import DecisionLog

    base = select(DecisionLog.route_layer, DecisionLog.escalated_to_s2)
    if since is not None:
        base = base.where(DecisionLog.created_at >= since)
    rows = (await session.execute(base)).all()

    total = len(rows)
    by_layer: dict[str, int] = {}
    s1_closed = 0
    escalated = 0
    for layer, esc in rows:
        by_layer[layer] = by_layer.get(layer, 0) + 1
        if esc:
            escalated += 1
        elif layer == "system_one":
            s1_closed += 1

    def _ratio(n: int) -> float | None:
        return round(n / total, 4) if total else None

    return {
        "total": total,
        "by_layer": {
            layer: {"count": count, "ratio": _ratio(count)}
            for layer, count in sorted(by_layer.items())
        },
        "s1_close_rate": _ratio(s1_closed),
        "s2_escalation_rate": _ratio(escalated),
    }


# ------------------------------------------------------------------ LLM token 用量（P15）


#: 进程内累计（**非持久**）：单进程可见，跨重启/多 worker 必须等 `llm_tokens` 迁移。
_llm_usage: dict[str, int] = {
    "calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
}
#: 至少有一次调用带回了 usage 才置 True——C 侧的 `available` 只看这个，不看 calls。
_llm_usage_seen = False


def _token_or_none(value: Any) -> int | None:
    """token 值校验：`bool` 是 `int` 子类必须排除；负数/非法/缺失 → None（不补零）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def record_llm_usage(
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """记一次成功 LLM 调用的 token 用量：**一条结构化日志 + 进程内计数 + 落库一行**。

    - 缺 usage（双 None）也计入 `calls`，但 token 计数与列都不动（NULL = 不知道，
      0 = 确认为零——「缺/非法不补零」是 LLMReply 的既定口径）；
    - 返回 `snapshot()`，便于调用方即时读取或测试断言；
    - **落库**（0021 后生效，P15 闭合）：stage=`llm_tokens`、duration_ms=0（非耗时
      打点）、source=`llm_usage`，provider/model 明细在结构化日志里（metrics 表
      只写指标不写内容）。trace_id 缺失的行按 Z4 口径丢弃（进程内计数照记）；
      落库失败由 recorder 兜底降级，不影响主链路。

    日志字段固定为 `trace_id/provider/model/prompt_tokens/completion_tokens/total_tokens`，
    `total_tokens` 只在两侧都已知时给出，否则 None。
    """
    global _llm_usage_seen
    prompt = _token_or_none(prompt_tokens)
    completion = _token_or_none(completion_tokens)
    total = prompt + completion if prompt is not None and completion is not None else None

    _llm_usage["calls"] += 1
    if prompt is not None:
        _llm_usage["prompt_tokens"] += prompt
    if completion is not None:
        _llm_usage["completion_tokens"] += completion
    if total is not None:
        _llm_usage["total_tokens"] += total
        _llm_usage_seen = True

    get_metrics_recorder().record_stage(
        get_trace_id(),
        None,
        "llm_tokens",
        0,
        "llm_usage",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )

    logger.info(
        "llm_usage",
        extra={
            "trace_id": get_trace_id() or "-",
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        },
    )
    return snapshot_llm_usage()


def snapshot_llm_usage() -> dict[str, Any]:
    """进程内 token 计数快照（拷贝）：`calls/prompt_tokens/completion_tokens/total_tokens`。

    `tokens_available` 表示**至少一次**调用带回了 usage——dev-C 的 `/api/metrics`
    用它决定 `available`，不要用 `calls > 0` 代替。
    """
    return {**_llm_usage, "tokens_available": _llm_usage_seen}


def reset_llm_usage() -> None:
    """清零进程内计数（测试与演示用；生产无调用方，重启即自然归零）。"""
    global _llm_usage_seen
    for key in _llm_usage:
        _llm_usage[key] = 0
    _llm_usage_seen = False
