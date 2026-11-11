"""健康采集与分级子图（SG2 / 技术设计 §7.2，FR-HLT-01/03/07）。

一张图双入口（`source=voice_scheduled|staff_scheduled|voice_active|staff_active`）：
每日 19:00 定时采集 / 护工代填 与 长者主动主诉（"我头晕"→ 主控 Agent，AC-34）走
**同一套状态机**——分开两张图，绿/黄/红三分支的终态判定迟早不一致。

图的形状（技术设计 §7.2 状态机）：

```
START → collect（5 问 interrupt 循环：收缩压/舒张压/睡眠/情绪/主诉，
                每问等 60s（PRD §4.6），无效重问一次，再失败标缺失）
      → validate（Pydantic HealthRecord 结构化；无效回 collect 重问）
      → grade（X1 分级引擎引用，阈值在引擎不在图内）
      ├─ green  → persist → END
      ├─ yellow → create_yellow_order（工单+黄色复核任务）→ hitl_yellow（护工 interrupt）
      │            └─ confirmed → persist ／ corrected → 回 grade 纠错重分级
      └─ red    → notify_red（护工+监护人双通知）→ create_red_order（紧急工单）
                   → hitl_red（医生 interrupt）→ persist（含实际处置时间）
```

四个决定：

1. **`collect` 逐问 `interrupt()`**。每问把状态写进 checkpointer 后让出执行，
   60s 窗口的计时由调度器/客户端执行（图只记录 deadline，不在节点里 `sleep`，
   与用药子图「间隔由 scheduler 决定」同一口径）；进程重启后 `get_state`
   仍能读回「问到第几问」。
2. **分级阈值不进图**（技术设计 §7.2「分级规则在代码不在 prompt」）：
   `grade` 节点只调用可替换的引擎引用（缺省 lazy import A2 的
   `health_service.grade_health_record`，即 X1 分级纯函数）；图内没有任何
   红/黄/绿数值。引擎输出非法 level 时直接抛错，绝不让图静默按绿处理。
3. **结构化校验在图内做结构层**（Pydantic，字段边界与 0017 迁移的 CHECK
   约束同源：血压 0–300 / 睡眠 0–24 / 情绪 1–5）；口语归一与「收缩压>舒张压」
   这类跨字段语义校验归 A2（`validate_fn` 可注入替换），图不重复实现。
4. **图节点零 SQL**：persist / 建单 / 双通知全部经注入式服务出口（缺省
   lazy import `health_service` / `work_order_service` / `notify_service`）；
   测试注入假出口即可覆盖三分支，无需真实服务在位。

**HITL**：黄档等护工确认（防 130 输成 180 的误报）、红档等医生确认，
都是 `interrupt()` 暂停 + `Command(resume=...)` 恢复；checkpointer 复用
`app.graphs.checkpointer`（独立 schema `langgraph`）。纠错重分级回环：
护工纠错后新记录回 `grade` 重新分级——补录达红阈值走同一回环（AC-22 图内侧）。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

#: 采集来源四值（TD §3.4 health_records.source CHECK；B1 裁决以四值为准）
SOURCE_VALUES = ("voice_scheduled", "staff_scheduled", "voice_active", "staff_active")

#: 分级三档（引擎输出值域；阈值不在本模块）
GRADE_LEVELS = ("green", "yellow", "red")

# PRD §4.6：每问等 60s（窗口计时由调度器/客户端执行，图只记录 deadline）
_QUESTION_WINDOW_SECONDS_PRD = 60

#: 5 问清单（键与 TD §3.4 health_records 采集字段一一对应；血压两问 = "5 问"）
QUESTIONS: tuple[tuple[str, str], ...] = (
    ("systolic", "先量一下血压，收缩压（高压）是多少？"),
    ("diastolic", "舒张压（低压）是多少？"),
    ("sleep_hours", "昨晚睡了几个小时？"),
    ("mood_score", "给今天的心情打个分，1 分最差、5 分最好，您打几分？"),
    ("complaint", "今天有没有哪里不舒服？"),
)

_QUESTION_PROMPTS: dict[str, str] = dict(QUESTIONS)


class HealthRecordDraft(BaseModel):
    """健康采集 5 字段结构化契约（TD §3.4 采集面；X1 HealthRecord 对齐口径）。

    字段边界与迁移 0017 的 CHECK 约束同源（血压 0–300、睡眠 0–24、情绪 1–5）；
    这是**输入合法性**，不是分级阈值——红/黄/绿判定只属于 X1 引擎。
    """

    systolic: int | None = Field(default=None, ge=0, le=300)
    diastolic: int | None = Field(default=None, ge=0, le=300)
    sleep_hours: float | None = Field(default=None, ge=0, le=24)
    mood_score: int | None = Field(default=None, ge=1, le=5)
    complaint: str | None = None


class HealthCheckState(TypedDict, total=False):
    """子图状态。决策依据、分级结果、HITL 结果、通知/工单返回全部入 state（可回溯）。"""

    source: str  # 入口来源（四值之一；AC-34 双入口）
    elder_id: str
    graph_thread_id: str
    answers: dict[str, Any]  # 逐问原始回答（问题键 → 值；未答的问题不在 dict 里）
    attempts: dict[str, int]  # 逐问已问次数（无效重问一次的上限判断）
    record: dict[str, Any]  # validate 后的结构化 5 字段（无效/未答标 None 缺失）
    invalid_fields: list[str]  # 重问一次后仍无效、最终标缺失的字段
    grade_level: str  # green/yellow/red（引擎写入，图内零阈值）
    grade_reasons: list[str]  # 决策依据（引擎返回，可回溯）
    rule_version: str  # 分级规则版本（引擎返回，可回溯）
    order_ref: dict[str, Any]  # 工单返回（order_id/order_no 等）
    notified: dict[str, Any]  # 双通知返回（AC-38 回执）
    hitl_result: dict[str, Any]  # 黄=护工确认 / 红=医生确认 payload
    record_ref: dict[str, Any]  # persist 返回（record_id/version）


# ---------------------------------------------------------------- 服务出口
# 出口全部可注入；缺省 lazy import 对应 service（A2/C 侧交付后生效）。
# 函数名预告口径在 CP1 与 dev-A/dev-C 口头对齐后如需调整，只改 _default_* 这一层。


def _default_grade(record: dict[str, Any]) -> dict[str, Any]:
    """X1 分级引擎引用（A2 纯函数，签名 12:00 CP1 以 docstring 冻结）。

    预告口径：入参 5 字段 record dict；出参
    ``{"level": "green|yellow|red", "reasons": [...], "rule_version": "..."}``。
    Step 4 接真实签名时**只改本函数**，图结构与阈值位置不动。
    """
    from app.services.health_service import grade_health_record  # A2 交付后生效（X1）

    return grade_health_record(record)


async def _default_persist(session: Any, state: HealthCheckState) -> dict[str, Any]:
    """落库 health_records（含 graph_thread_id 回指、review_status、处置时间）。"""
    from app.services.health_service import persist_record  # A2 交付后生效

    return await persist_record(session, state)


async def _default_create_yellow_order(session: Any, state: HealthCheckState) -> dict[str, Any]:
    """黄档：创建工单 + 黄色复核任务（通知护工，AC-09）。"""
    from app.services import work_order_service

    return await work_order_service.open_yellow_review(session, state)


async def _default_notify_red(session: Any, state: HealthCheckState) -> dict[str, Any]:
    """红档：紧急通知护工 + 监护人双通知（AC-38）。"""
    from app.services import notify_service

    return await notify_service.notify_red_event(session, state)


async def _default_create_red_order(session: Any, state: HealthCheckState) -> dict[str, Any]:
    """红档：创建紧急工单（看板红档置顶）。"""
    from app.services import work_order_service

    return await work_order_service.open_emergency(session, state)


# ---------------------------------------------------------------- 节点辅助


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _coerce_field(key: str, raw: Any) -> tuple[Any, str | None]:
    """单字段结构化。数值字段接受 "180" 这类口语数字串；返回 (值, 错误码)。"""
    if key == "complaint":
        text = str(raw).strip()
        return (text or None), None
    if isinstance(raw, bool) or raw is None:
        return None, "not_numeric"
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None, "not_numeric"
    if key == "sleep_hours":
        coerced: Any = value
    else:
        coerced = int(value)
    try:
        HealthRecordDraft(**{key: coerced})
    except ValidationError:
        return None, "out_of_range"
    return coerced, None


def _pending_questions(state: HealthCheckState) -> list[str]:
    answers = state.get("answers") or {}
    return [key for key, _ in QUESTIONS if key not in answers]


def graph_config(thread_id: str) -> dict[str, Any]:
    """图的运行配置。`thread_id` 与一次采集绑定，重启后能找回同一条状态。"""
    return {"configurable": {"thread_id": thread_id}}


def default_thread_id(
    elder_id: str | uuid.UUID, source: str, *, on_date: datetime.date | None = None
) -> str:
    """thread_id 口径：scheduled 同日同长者幂等（重启可恢复、天然防重复采集）；
    active（主动主诉）一次一事，追加 8 位随机段。"""
    if source in ("voice_scheduled", "staff_scheduled"):
        day = on_date or datetime.datetime.now(datetime.UTC).date()
        return f"health:{_uuid(elder_id)}:{day:%Y%m%d}"
    return f"health:{_uuid(elder_id)}:{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------- 节点


def build_health_checkup_graph(
    *,
    session: Any,
    checkpointer: Any,
    validate_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    grade_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    persist_fn: Callable[[Any, HealthCheckState], Awaitable[dict[str, Any]]] | None = None,
    create_yellow_order_fn: Callable[[Any, HealthCheckState], Awaitable[dict[str, Any]]]
    | None = None,
    notify_red_fn: Callable[[Any, HealthCheckState], Awaitable[dict[str, Any]]] | None = None,
    create_red_order_fn: Callable[[Any, HealthCheckState], Awaitable[dict[str, Any]]] | None = None,
) -> Any:
    """编译子图。`session` 是本次运行的事务会话，`checkpointer` 复用独立 schema 池。

    服务出口全部可注入：`validate_fn(answers) -> {"record", "invalid"}`、
    `grade_fn(record) -> {"level", "reasons", "rule_version"}`、
    `persist_fn(session, state)`、`create_yellow_order_fn` / `notify_red_fn` /
    `create_red_order_fn(session, state)`。缺省用真实 service（lazy import）；
    测试注入假函数即可覆盖三分支与重问循环。
    """
    _validate = validate_fn
    _grade = grade_fn if grade_fn is not None else _default_grade
    _persist = persist_fn if persist_fn is not None else _default_persist
    _yellow_order = (
        create_yellow_order_fn
        if create_yellow_order_fn is not None
        else _default_create_yellow_order
    )
    _notify_red = notify_red_fn if notify_red_fn is not None else _default_notify_red
    _red_order = (
        create_red_order_fn if create_red_order_fn is not None else _default_create_red_order
    )

    async def collect(state: HealthCheckState) -> dict[str, Any]:
        """5 问循环：一次 interrupt 问一题；60s 窗口与超时重试由外部执行。"""
        answers = dict(state.get("answers") or {})
        attempts = dict(state.get("attempts") or {})
        current = next((key for key, _ in QUESTIONS if key not in answers), None)
        if current is None:
            return {}
        attempt_no = int(attempts.get(current) or 0) + 1
        deadline = (
            datetime.datetime.now(datetime.UTC)
            + datetime.timedelta(seconds=_QUESTION_WINDOW_SECONDS_PRD)
        ).isoformat()
        payload = interrupt(
            {
                "question": current,
                "prompt": _QUESTION_PROMPTS[current],
                "attempt_no": attempt_no,
                "deadline": deadline,
                "source": state["source"],
            }
        )
        value = payload.get("value") if isinstance(payload, dict) else payload
        return {
            "answers": {**answers, current: value},
            "attempts": {**attempts, current: attempt_no},
        }

    async def validate(state: HealthCheckState) -> dict[str, Any]:
        """结构化 5 字段（Pydantic，与 0017 CHECK 同源）；无效且未重问 → 回 collect。"""
        answers = dict(state.get("answers") or {})
        attempts = dict(state.get("attempts") or {})
        if _validate is not None:
            result = _validate(answers)
            record = dict(result.get("record") or {})
            invalid = [str(k) for k in (result.get("invalid") or [])]
        else:
            record, invalid = {}, []
            for key, _ in QUESTIONS:
                raw = answers.get(key)
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    record[key] = None  # 未答/空 → 缺失（缺失判黄是引擎的规则，图不判）
                    continue
                value, error = _coerce_field(key, raw)
                if error:
                    invalid.append(key)
                else:
                    record[key] = value
        # 无效重问一次（TD §7.2「无效重问一次，再失败标缺失」）：
        reask = [key for key in invalid if int(attempts.get(key) or 0) < 2]
        if reask:
            reopened = dict(answers)
            for key in reask:
                reopened.pop(key, None)
            return {
                "answers": reopened,
                "attempts": {**attempts, **{key: 2 for key in reask}},
            }
        for key in invalid:  # 重问过仍无效 → 标缺失，不阻断
            record[key] = None
        return {"record": record, "invalid_fields": invalid}

    async def grade(state: HealthCheckState) -> dict[str, Any]:
        """X1 引擎引用调用点：图内零阈值，输出非法即抛错（绝不静默按绿）。"""
        result = _grade(dict(state.get("record") or {}))
        level = str(result.get("level") or "")
        if level not in GRADE_LEVELS:
            raise ValueError(f"grade engine returned invalid level: {level!r}")
        return {
            "grade_level": level,
            "grade_reasons": [str(r) for r in (result.get("reasons") or [])],
            "rule_version": str(result.get("rule_version") or "unknown"),
        }

    async def create_yellow_order(state: HealthCheckState) -> dict[str, Any]:
        ref = await _yellow_order(session, state)
        return {"order_ref": dict(ref or {})}

    async def hitl_yellow(state: HealthCheckState) -> dict[str, Any]:
        payload = interrupt(
            {
                "kind": "yellow_review",
                "elder_id": state["elder_id"],
                "record": state.get("record"),
                "order_ref": state.get("order_ref"),
            }
        )
        result = payload if isinstance(payload, dict) else {"decision": payload}
        # 纠错（corrected）：payload 可携带修正后的字段，合并后回 grade 重分级
        if result.get("decision") == "corrected" and isinstance(result.get("record"), dict):
            return {
                "hitl_result": result,
                "record": {**dict(state.get("record") or {}), **result["record"]},
            }
        return {"hitl_result": result}

    async def notify_red(state: HealthCheckState) -> dict[str, Any]:
        notified = await _notify_red(session, state)
        return {"notified": dict(notified or {})}

    async def create_red_order(state: HealthCheckState) -> dict[str, Any]:
        ref = await _red_order(session, state)
        return {"order_ref": dict(ref or {})}

    async def hitl_red(state: HealthCheckState) -> dict[str, Any]:
        payload = interrupt(
            {
                "kind": "red_confirm",
                "elder_id": state["elder_id"],
                "record": state.get("record"),
                "order_ref": state.get("order_ref"),
                "notified": state.get("notified"),
            }
        )
        result = payload if isinstance(payload, dict) else {"decision": payload}
        return {"hitl_result": result}

    async def persist(state: HealthCheckState) -> dict[str, Any]:
        ref = await _persist(session, state)
        return {"record_ref": dict(ref or {})}

    def route_collect(state: HealthCheckState) -> Literal["collect", "validate"]:
        return "collect" if _pending_questions(state) else "validate"

    def route_validate(state: HealthCheckState) -> Literal["collect", "grade"]:
        # 无效重问：validate 清掉无效答案后仍有未答问题 → 回 collect；否则进分级
        return "collect" if _pending_questions(state) else "grade"

    def route_grade(
        state: HealthCheckState,
    ) -> Literal["persist", "create_yellow_order", "notify_red"]:
        level = state.get("grade_level")
        if level == "green":
            return "persist"
        if level == "yellow":
            return "create_yellow_order"
        if level == "red":
            return "notify_red"
        raise ValueError(f"unknown grade level in state: {level!r}")

    def route_hitl_yellow(state: HealthCheckState) -> Literal["grade", "persist"]:
        decision = (state.get("hitl_result") or {}).get("decision")
        return "grade" if decision == "corrected" else "persist"

    builder = StateGraph(HealthCheckState)
    builder.add_node("collect", collect)
    builder.add_node("validate", validate)
    builder.add_node("grade", grade)
    builder.add_node("create_yellow_order", create_yellow_order)
    builder.add_node("hitl_yellow", hitl_yellow)
    builder.add_node("notify_red", notify_red)
    builder.add_node("create_red_order", create_red_order)
    builder.add_node("hitl_red", hitl_red)
    builder.add_node("persist", persist)

    builder.add_edge(START, "collect")
    builder.add_conditional_edges(
        "collect", route_collect, {"collect": "collect", "validate": "validate"}
    )
    builder.add_conditional_edges(
        "validate", route_validate, {"collect": "collect", "grade": "grade"}
    )
    builder.add_conditional_edges(
        "grade",
        route_grade,
        {
            "persist": "persist",
            "create_yellow_order": "create_yellow_order",
            "notify_red": "notify_red",
        },
    )
    builder.add_edge("create_yellow_order", "hitl_yellow")
    builder.add_conditional_edges(
        "hitl_yellow", route_hitl_yellow, {"grade": "grade", "persist": "persist"}
    )
    builder.add_edge("notify_red", "create_red_order")
    builder.add_edge("create_red_order", "hitl_red")
    builder.add_edge("hitl_red", "persist")
    builder.add_edge("persist", END)

    return builder.compile(checkpointer=checkpointer)


async def start_health_checkup(
    graph: Any,
    *,
    elder_id: uuid.UUID | str,
    source: str,
    thread_id: str | None = None,
    initial_answers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """首轮启动。代填入口可带 `initial_answers` 直接跳过语音 5 问（AC-32 同一张图）。

    `source` 必须是四值枚举之一；`thread_id` 缺省按 scheduled/active 口径派生
    （scheduled 同日幂等；active 一次一事）。跑到第一个阻塞点（问话或 HITL）
    返回（返回里带 `__interrupt__`）。
    """
    if source not in SOURCE_VALUES:
        raise ValueError(f"unknown source: {source!r}; expected one of {SOURCE_VALUES}")
    tid = thread_id or default_thread_id(elder_id, source)
    state: HealthCheckState = {
        "source": source,
        "elder_id": str(elder_id),
        "answers": dict(initial_answers or {}),
        "attempts": {},
        "graph_thread_id": tid,
    }
    result: dict[str, Any] = await graph.ainvoke(state, graph_config(tid))
    return result


async def resume_health_checkup(
    graph: Any, *, thread_id: str, payload: dict[str, Any] | str
) -> dict[str, Any]:
    """把长者回答 / 护工与医生的确认送回去，继续跑到下一次阻塞或结束。"""
    result: dict[str, Any] = await graph.ainvoke(Command(resume=payload), graph_config(thread_id))
    return result


__all__ = [
    "GRADE_LEVELS",
    "QUESTIONS",
    "SOURCE_VALUES",
    "HealthCheckState",
    "HealthRecordDraft",
    "build_health_checkup_graph",
    "default_thread_id",
    "graph_config",
    "resume_health_checkup",
    "start_health_checkup",
]
