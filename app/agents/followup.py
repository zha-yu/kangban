"""高危工具被拒后的**真实出口**（F03「假承诺拆除」· 总览 Q6）。

**问题**：`CompanionAgent` 拒绝改药单/关工单/删数据时，话术直接声称「已帮您转告护工」，
但 `main.py:512` 构造时没传 `followup_sink` → 只打 `react_followup_sink_missing` 警告，
**没有任何人收到需求**。承诺是假的。

**本模块的职责**：实现 `followup_sink` 的落库实现，交给装配层注入
（`CompanionAgent(..., followup_sink=make_followup_sink(session_factory))`）。
- 分层：agents 不写库（AGENTS §2），落库经 `work_order_service.open_work_order`；
- 幂等：`dedupe_token=f"react_followup:{turn_id}:{tool_name}"`——同一轮同一工具只建一张单，
  Agent 重试/重复回调不会堆单；
- 身份：`elder_id` 只来自服务端 `runtime_context`（`FollowupTask.elder_id`）；
  为空（员工/家属/未识别）→ **warning 跳过**，不猜、不填假 ID；
- 事务：短会话内 `open_work_order` + `commit`，不把工单挂在对话主事务上——
  对话回滚不应该把「护工要跟进」这件事一起抹掉。

**装配片段**（归装配窗口，B4 不改 `main.py`）::

    from app.agents.followup import make_followup_sink

    agent = CompanionAgent(
        ...,
        followup_sink=make_followup_sink(session_factory),
    )

接入后那句「已转告护工」才成立（工单真实存在），不需要改话术。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.services import work_order_service

if TYPE_CHECKING:  # 仅类型引用，避免 agents 之间运行期耦合
    from app.agents.companion import FollowupSink, FollowupTask

logger = logging.getLogger("app.agents.followup")

#: 工单类型/严重度（PRD FR-DLG-04：高危被拒是护工跟进，不是风险事件，故黄档）
FOLLOWUP_ORDER_TYPE = "follow_up"
FOLLOWUP_SEVERITY = "yellow"

#: 幂等键前缀（同一轮同一工具只建一张单）
_DEDUPE_PREFIX = "react_followup"


def followup_dedupe_token(*, turn_id: str, tool_name: str) -> str:
    """`react_followup:{turn_id}:{tool_name}`——与契约/计划一致，测试也用它断言。"""
    return f"{_DEDUPE_PREFIX}:{turn_id}:{tool_name}"


def make_followup_sink(session_factory: Callable[[], Any]) -> FollowupSink:
    """构造 `FollowupSink`：把 `FollowupTask` 落成一张 `follow_up` / yellow 工单。

    :param session_factory: 装配层的会话工厂（如 `app.db.session_factory`），
        每次回调开一个**短会话**并自行 commit。
    """

    async def _sink(task: FollowupTask) -> None:
        raw_elder = (task.elder_id or "").strip()
        if not raw_elder:
            # 员工/家属/未识别身份没有 elder_id：没有归属就没法建单，如实留痕
            logger.warning(
                "react_followup_skipped reason=missing_elder_id turn_id=%s tool_name=%s",
                task.turn_id,
                task.tool_name,
            )
            return

        try:
            elder_id = uuid.UUID(raw_elder)
        except ValueError:
            logger.warning(
                "react_followup_skipped reason=bad_elder_id turn_id=%s tool_name=%s elder_id=%s",
                task.turn_id,
                task.tool_name,
                raw_elder,
            )
            return

        dedupe_token = followup_dedupe_token(turn_id=task.turn_id, tool_name=task.tool_name)
        async with session_factory() as session:
            ref = await work_order_service.open_work_order(
                session,
                elder_id=elder_id,
                order_type=FOLLOWUP_ORDER_TYPE,
                severity=FOLLOWUP_SEVERITY,
                dedupe_token=dedupe_token,
            )
            await session.commit()

        logger.info(
            "react_followup_order_opened order_no=%s created=%s turn_id=%s tool_name=%s",
            ref.order_no,
            ref.created,
            task.turn_id,
            task.tool_name,
        )

    return _sink


__all__ = [
    "FOLLOWUP_ORDER_TYPE",
    "FOLLOWUP_SEVERITY",
    "followup_dedupe_token",
    "make_followup_sink",
]
