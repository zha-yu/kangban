"""用药确认子图（N10 / 讲义 §10 · 技术设计 §7）。

一张图两个入口（`triggered_by=scheduler|dialog`），因为「定时提醒」和「长者主动说
我吃过药了」走的是**同一套状态机**——分成两张图，两边的终态判定迟早不一致。

图的形状：

```
START → remind → wait_confirm ─┬─ 说了"吃了"   → mark_taken  ┐
                               ├─ 说了"没吃"   → mark_refused┤
                               └─ 没回应       → retry ─┬─ 还有次数 → remind
                                                          └─ 用完     → finalize
                                                                         ↓
                                                                        END
```

四个决定：

1. **`wait_confirm` 用 `interrupt()` 而不是轮询**。轮询要么占一个协程等 15 分钟，
   要么靠 `sleep` 猜时机；`interrupt()` 把状态写进 checkpointer 后**让出执行**，
   进程重启后 `graph.get_state` 仍能读回「卡在第几次触达的等待上」。
2. **`remind` 记录触达是幂等的**（`(event_id, attempt_no)` 唯一）。图恢复会重放这一步，
   重放不该让长者收到"第 2 次提醒"其实只是同一次的重试。
3. **重试上限 4 次触达（T0/+5/+10/+15）**，对应 PRD 的四次触达；`retry` 只加计数，
   到点转 `finalize`，不在节点里写 `sleep`——间隔由 scheduler 决定，图只负责"下一次"。
4. **终态只写一次**。`finalize` 调 `medication_service.set_terminal`，后者对"已定终态再改"
   抛 `TerminalStateConflict`；补确认（AC-08）走 `late_taken` 追加，不覆盖原终态。

**本节点不做**（A12 的范围）：升级工单。`finalize` 只在状态里置 `needs_escalation=True`，
由 A12 的 `escalate` 节点消费——先建工单再补这里，会让「谁来建单」出现两个写口。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.services import medication_service

logger = logging.getLogger(__name__)

#: 四次触达：T0 / +5 / +10 / +15（PRD FR-MED-05）
MAX_ATTEMPTS = 4

#: 长者回答的口语归一（只做判别，不做语义理解——语义归 Agent）
_TAKEN_WORDS: frozenset[str] = frozenset(
    {"taken", "taken_reported", "吃了", "吃过了", "吃完了", "已经吃了", "yes"}
)
_REFUSED_WORDS: frozenset[str] = frozenset({"refused", "没吃", "不吃", "不想吃", "no"})
_NO_ANSWER_WORDS: frozenset[str] = frozenset({"", "no_response", "timeout", "none"})


class MedicationState(TypedDict, total=False):
    """子图状态。字段与主计划 §3 的 `MedicationState` 一一对应。

    `needs_escalation` 是**给 A12 的交接位**：本节点只置位不建单，
    免得「谁负责建工单」出现两个写口（N27 的安全层也要建单）。
    """

    reminder_id: str
    plan_id: str
    elder_id: str
    attempt_no: int
    deadline: str
    answer: str | None
    terminal: str | None
    needs_escalation: bool
    graph_thread_id: str


def graph_config(reminder_id: uuid.UUID | str) -> dict[str, Any]:
    """图的运行配置。`thread_id` 与用药事件绑定，重启后能找回同一条状态。"""
    return {"configurable": {"thread_id": f"medication:{reminder_id}"}}


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _normalize(answer: Any) -> str:
    if answer is None:
        return ""
    if isinstance(answer, dict):
        answer = answer.get("answer", "")
    return str(answer).strip().lower()


def build_medication_graph(*, session: Any, checkpointer: Any) -> Any:
    """编译子图。`session` 是本次运行的事务会话，`checkpointer` 由 N10 的 A08 提供。"""

    async def remind(state: MedicationState) -> dict[str, Any]:
        event_id = _uuid(state["reminder_id"])
        attempt_no = int(state.get("attempt_no") or 1)

        # TODO(E2)：这里应调用 dev-C 的 deliver(reminder) 下发语音/卡片并取回回执。
        # 目前以「卡片已渲染」占位，等 N07 的触达出口到位后替换，接口形状见契约 §5.1。
        await medication_service.record_attempt(
            session, event_id=event_id, attempt_no=attempt_no, receipt="card_rendered"
        )
        await medication_service.set_waiting_confirm(
            session, event_id=event_id, graph_thread_id=state.get("graph_thread_id")
        )
        return {"attempt_no": attempt_no}

    async def wait_confirm(state: MedicationState) -> dict[str, Any]:
        # 阻塞点：恢复时 `interrupt()` 的返回值就是 `Command(resume=...)` 的载荷
        payload = interrupt(
            {
                "reminder_id": state["reminder_id"],
                "attempt_no": int(state.get("attempt_no") or 1),
                "deadline": state.get("deadline"),
            }
        )
        return {"answer": _normalize(payload)}

    async def mark_taken(state: MedicationState) -> dict[str, Any]:
        return {"terminal": "taken_reported", "needs_escalation": False}

    async def mark_refused(state: MedicationState) -> dict[str, Any]:
        return {"terminal": "refused", "needs_escalation": False}

    async def retry(state: MedicationState) -> dict[str, Any]:
        attempt_no = int(state.get("attempt_no") or 1)
        if attempt_no + 1 > MAX_ATTEMPTS:
            logger.info("medication_attempts_exhausted reminder_id=%s", state.get("reminder_id"))
            # 刻意**不**把 attempt_no 加到 5：四次触达就是四次，末态的 retry_count
            # 必须等于「实际重试次数（3）」，而不是「下一次本该是第几次」
            return {"terminal": "missed_unconfirmed", "needs_escalation": True}
        return {"attempt_no": attempt_no + 1}

    async def finalize(state: MedicationState) -> dict[str, Any]:
        terminal = state.get("terminal") or "missed_unconfirmed"
        attempts = int(state.get("attempt_no") or 1)
        if state.get("answer"):
            # 只补写回答摘要，不改终态（终态由 set_terminal 独占写入）
            await medication_service.record_response(
                session,
                event_id=_uuid(state["reminder_id"]),
                attempt_no=attempts,
                response=str(state["answer"]),
            )
        await medication_service.set_terminal(
            session,
            event_id=_uuid(state["reminder_id"]),
            terminal_state=terminal,
            failure_reason=None if terminal in ("taken_reported", "refused") else "no_answer",
            retry_count=max(0, attempts - 1),
            confirm_channel="voice" if terminal == "taken_reported" else None,
        )
        return {}

    def route_answer(state: MedicationState) -> Literal["mark_taken", "mark_refused", "retry"]:
        answer = _normalize(state.get("answer"))
        if answer in _TAKEN_WORDS:
            return "mark_taken"
        if answer in _REFUSED_WORDS:
            return "mark_refused"
        if answer in _NO_ANSWER_WORDS:
            return "retry"
        # 说了别的话（"等会儿"、"我找找"）：不进终态，按未确认重试
        return "retry"

    def route_retry(state: MedicationState) -> Literal["remind", "finalize"]:
        return "finalize" if state.get("terminal") else "remind"

    builder = StateGraph(MedicationState)
    builder.add_node("remind", remind)
    builder.add_node("wait_confirm", wait_confirm)
    builder.add_node("mark_taken", mark_taken)
    builder.add_node("mark_refused", mark_refused)
    builder.add_node("retry", retry)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "remind")
    builder.add_edge("remind", "wait_confirm")
    builder.add_conditional_edges(
        "wait_confirm",
        route_answer,
        {"mark_taken": "mark_taken", "mark_refused": "mark_refused", "retry": "retry"},
    )
    builder.add_conditional_edges(
        "retry", route_retry, {"remind": "remind", "finalize": "finalize"}
    )
    builder.add_edge("mark_taken", "finalize")
    builder.add_edge("mark_refused", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)


async def start_medication_task(
    graph: Any,
    *,
    reminder_id: uuid.UUID,
    plan_id: uuid.UUID,
    elder_id: uuid.UUID,
    deadline: str,
    triggered_by: str = "scheduler",
) -> dict[str, Any]:
    """首轮启动：跑到第一次 `wait_confirm` 就停住（返回里带 `__interrupt__`）。"""
    if triggered_by not in ("scheduler", "dialog"):
        raise ValueError(f"unknown triggered_by: {triggered_by!r}")
    state: MedicationState = {
        "reminder_id": str(reminder_id),
        "plan_id": str(plan_id),
        "elder_id": str(elder_id),
        "attempt_no": 1,
        "deadline": deadline,
        "graph_thread_id": f"medication:{reminder_id}",
    }
    return await graph.ainvoke(state, graph_config(reminder_id))


async def resume_medication_task(
    graph: Any, *, reminder_id: uuid.UUID, answer: str
) -> dict[str, Any]:
    """把长者的回答送回去，继续跑到下一次阻塞或结束。"""
    return await graph.ainvoke(Command(resume={"answer": answer}), graph_config(reminder_id))


__all__ = [
    "MAX_ATTEMPTS",
    "MedicationState",
    "build_medication_graph",
    "graph_config",
    "resume_medication_task",
    "start_medication_task",
]
