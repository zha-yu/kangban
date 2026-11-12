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

**E2 已接（2026-11-09 21:10）**：`remind` 经信令层 `deliver()` 把提醒下发到长者绑定设备——
不带 `response_id`/`epoch`/`segment`（它不属于任何回复流，客户端按整段一播，契约 §4.1 的
可选字段口径）。触达回执 = **投递**结果：`voice_dispatched`（任一台设备收下）/
`device_offline`（都离线）/ `no_bound_device`（没绑设备）。

**TD-1/TD-2 已接（2026-11-09，day07/contracts.md v1.0）**：触达从「只有字幕」升级为
「语音 + 字幕」——下发前经 `synthesize_touch_audio`（信令层 TTS 出口，可注入替换）合成
整段 PCM，随消息 `audio` 字段（base64）下发；合成失败降级纯字幕并把
`medication_attempts.tts_result` 记 `tts_failed`。触达消息带 `touch_id` /
`touch_event_id` / `touch_attempt_no`，pad 播完后回 `touch_played` →
`medication_service.mark_attempt_played` 把 `receipt` 从 `voice_dispatched` 幂等升级为
`played`（留痕闭环，**不参与**重试与终态判定）。

**重放防护已接（2026-11-10，develop 侧 N10 增量合入）**：`remind` 投递前先
`get_attempt` 问这次触达是否已发生过——图恢复会重放本节点，`record_attempt` 的幂等
只挡**记录**、挡不住**投递**，不先查就会给长者再播一遍同一次提醒；已有记录 → 跳过
投递只推进状态（同一 reminder 按 thread 串行，「先查后发」无竞态）。

**A12 已接（2026-11-09）**：`finalize` 在写完结态后，对 `missed_unconfirmed` 建漏服工单
（`work_order_service.open_missed_medication`）。**先写终态、再建单**——终态是事实，
建单是派单动作；建单失败时事实仍成立，重跑 `finalize` 可凭终态补建（两侧都幂等）。
主理人裁决走 B：与 dev-C 的 `safety_service`（急症）**两套并存**，已登记技术债 WD-1。

**建单口径扩到 `notification_failed`（2026-11-10 主理人裁决）**：AC-20 把「送到了没吃」
（`missed_unconfirmed`）与「根本没送到」（`notification_failed`）拆成两种终态后，
没送到**更需要**人工跟进（补送/代确认），漏服单照建——两种终态都升级，区别只在
`terminal_state` 字段本身。

`needs_escalation` 仍保留：它表达「这一轮需要人介入」，与「有没有建成单」是两件事——
建单成功不代表护工已看到，建单失败也不代表这事不需要人管。
"""

from __future__ import annotations

import base64
import datetime
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.services import medication_service, notify_service, work_order_service

# PRD §4.5 硬性数值：每次触达等待长者确认 60s（卡片窗口与图内 retry 间隔一致）
_REMIND_WINDOW_SECONDS_PRD = 60

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


#: 「消息没出去」的回执白名单（迁移 0013 后 receipt 共七值）：四次全落在这里才判通知失败。
UNDELIVERED_RECEIPTS = frozenset({"device_offline", "no_bound_device"})


async def _split_missed_by_receipt(
    session: Any, reminder_id: Any, default_reason: str | None
) -> tuple[str, str | None]:
    """AC-20：把「四次无回答」拆成 `missed_unconfirmed` 与 `notification_failed`。

    判据只看**回执**，不看有没有人回答（后者已经是终态本身）：

    - 四次回执全部落在 `UNDELIVERED_RECEIPTS` → `notification_failed` + `failure_reason=offline`；
    - 只要有一条说明消息出过门（`card_rendered` / `tts_finished` / `voice_dispatched` / `played`）
      → 仍是 `missed_unconfirmed`（人没确认，而不是没通知到）。

    查不到回执（异常或空）时**保守按 `missed_unconfirmed`**：不能因为一次查询失败就把
    「没通知到」这种更重的结论写进库——那是给家属看的归因，不能猜。
    """
    try:
        rows = await medication_service.list_attempts(session, _uuid(str(reminder_id)))
    except Exception:  # 终态判定不因查询失败升级成更重的结论
        logger.warning(
            "medication_receipt_lookup_failed reminder_id=%s", reminder_id, exc_info=True
        )
        return "missed_unconfirmed", default_reason
    receipts = {str(row.get("receipt") or "none") for row in rows}
    if receipts and receipts <= UNDELIVERED_RECEIPTS:
        return "notification_failed", "offline"
    return "missed_unconfirmed", default_reason


async def _escalate_missed_medication(session: Any, state: MedicationState) -> None:
    """漏服 → 升级工单（A12）。

    **失败不吞**：漏服是安全相关事件，静默失败会让「这轮没人管」变成看板上看不见的事故。
    抛出后 scheduler 重试会重跑 `finalize`——`set_terminal` 对同值幂等、建单按确定性
    单号幂等，所以重跑安全，不会产生第二张单。
    """
    raw_elder = state.get("elder_id")
    reminder_id = state.get("reminder_id")
    if not raw_elder or not reminder_id:
        # 缺 elder_id 就没法建单（work_orders.elder_id 非空）。这是数据问题，
        # 不是本轮该静默跳过的情况，留痕以便回查是哪条事件少了字段。
        logger.warning(
            "medication_escalation_skipped reason=missing_identity reminder_id=%s",
            reminder_id,
        )
        return

    elder_id = _uuid(raw_elder)
    assignee_id = await work_order_service.resolve_primary_care_staff(session, elder_id=elder_id)
    ref = await work_order_service.open_missed_medication(
        session,
        elder_id=elder_id,
        reminder_id=_uuid(reminder_id),
        assignee_id=assignee_id,
    )
    logger.info(
        "medication_escalated reminder_id=%s order_id=%s order_no=%s created=%s assignee=%s",
        reminder_id,
        ref.order_id,
        ref.order_no,
        ref.created,
        assignee_id,
    )


async def _deliver_reminder(
    session: Any,
    deliver_fn: Any,
    tts_fn: Callable[[str], Awaitable[bytes | None]],
    *,
    elder_id: uuid.UUID,
    reminder_id: uuid.UUID,
    attempt_no: int,
) -> tuple[str, str | None]:
    """把用药提醒下发到长者绑定设备，返回 (触达回执, TTS 结果)（E2 + TD-1）。

    消息形状（day07/contracts.md §1）：`{"type": "tts_text", "text": ..., "degraded": false,
    "touch_id", "touch_event_id", "touch_attempt_no", "audio"?}`——**不带**
    `response_id` / `epoch` / `segment_id`。它不属于任何回复流，是子图的主动触达；
    `touch_id` 是 played 回执（TD-2）的关联 id，`audio` 仅合成成功时携带，
    缺省即 Day 6 纯字幕形状，旧客户端零改动。

    回执三态（如实记录，不做美化和重试——重试是 scheduler 的职责）：

    - `voice_dispatched`：至少一台绑定设备把消息收进了发送队列（**文本已投递**，
      不代表已出声——WS 侧 TTS 仍是 `NullTtsQueue` 桩）；
    - `device_offline`：有绑定设备但全部离线/未鉴权；
    - `no_bound_device`：根本没绑设备——运维缺口，同样不算本轮失败。

    TTS 结果（与投递回执是两个独立事实）：`ok` = 合成成功且随消息下发；
    `tts_failed` = 合成失败、降级纯字幕；没绑设备时不做合成（None，省一次调用）。
    """
    drug_name = await medication_service.get_drug_name(session, event_id=reminder_id)
    text = (
        f"到吃药时间了，请确认{drug_name}是否已经服用。"
        if drug_name
        else "到吃药时间了，请确认您的药是否已经服用。"
    )
    device_ids = await medication_service.list_bound_device_ids(session, elder_id=elder_id)
    if not device_ids:
        logger.warning(
            "medication_reminder_no_device reminder_id=%s elder_id=%s", reminder_id, elder_id
        )
        return "no_bound_device", None

    # X1：触达通知行——pad 回执（notification_id 必填）据此绑定真值；幂等键
    # medication_touch:{reminder_id}:{attempt_no}。通知行失败不阻断触达（触达是
    # 安全链路，字幕/语音是底线；无行时帧置 null，pad 回执按纯播报语义处理）。
    touch_nid: uuid.UUID | None = None
    try:
        touch_nid = await notify_service.send_medication_touch_notification(
            session,
            elder_id=elder_id,
            reminder_id=reminder_id,
            attempt_no=attempt_no,
            drug_name=drug_name,
        )
    except Exception:  # 通知行失败不阻断触达（见上注释）
        logger.exception(
            "medication_touch_notification_failed reminder_id=%s attempt_no=%s",
            reminder_id,
            attempt_no,
        )

    # TD-1：先合成再下发——合成失败降级纯字幕，触达照常投递（字幕是底线）。
    pcm = await tts_fn(text)
    message: dict[str, Any] = {
        "type": "tts_text",
        "text": text,
        "degraded": False,
        # D6：用药触达不经路由层，route_degraded 恒 false（字段恒下发）
        "route_degraded": False,
        "touch_id": uuid.uuid4().hex,
        "touch_event_id": str(reminder_id),
        "touch_attempt_no": attempt_no,
        "notification_id": str(touch_nid) if touch_nid else None,
    }
    if pcm:
        message["audio"] = {
            "format": "pcm_s16le",
            "sample_rate": 16000,
            "data_b64": base64.b64encode(pcm).decode("ascii"),
        }
    tts_result = "ok" if pcm else "tts_failed"

    # Day 7 L6#9 联调裁决：触达卡片命令与 tts_text 同发（同 taskId=reminder_id，
    # 前端按 taskId 幂等）。快照取不到（无 drug_snapshot）就不发卡片——只降级为
    # 纯语音触达，不影响投递回执。
    card_snapshot = await medication_service.get_medication_card_snapshot(
        session, event_id=reminder_id
    )
    card_message: dict[str, Any] | None = None
    if card_snapshot:
        card_message = {
            "type": "task_command",
            "task_id": str(reminder_id),
            "kind": "medication",
            "source": "scheduler",
            "hint": "该吃药了，请看屏幕上的用药提醒",
            "notification_id": str(touch_nid) if touch_nid else None,
            "payload": {
                "kind": "medication",
                "snapshot": {
                    **card_snapshot,
                    "attemptNo": attempt_no,
                    "windowSeconds": _REMIND_WINDOW_SECONDS_PRD,
                    "snapshotAt": datetime.datetime.now(datetime.UTC).isoformat(),
                },
            },
            "issued_at": int(datetime.datetime.now(datetime.UTC).timestamp() * 1000),
        }

    dispatched = False
    for device_id in device_ids:
        if deliver_fn(str(device_id), message):
            dispatched = True
        if card_message:
            deliver_fn(str(device_id), card_message)
    if not dispatched:
        logger.info(
            "medication_reminder_offline reminder_id=%s devices=%s", reminder_id, len(device_ids)
        )
        return "device_offline", tts_result
    logger.info(
        "medication_reminder_dispatched reminder_id=%s devices=%s tts=%s",
        reminder_id,
        len(device_ids),
        tts_result,
    )
    return "voice_dispatched", tts_result


def _default_deliver(device_id: str, message: dict[str, Any]) -> bool:
    """信令层的下行投递（延迟 import：graphs 层不在 import 期依赖 audio 层）。"""
    from app.audio.signaling import deliver

    return deliver(device_id, message)


async def _default_tts(text: str) -> bytes | None:
    """触达 TTS 缺省出口（延迟 import，理由同上）；失败返回 None 降级纯字幕。"""
    from app.audio.signaling import synthesize_touch_audio

    return await synthesize_touch_audio(text)


def build_medication_graph(
    *, session: Any, checkpointer: Any, deliver_fn: Any | None = None, tts_fn: Any | None = None
) -> Any:
    """编译子图。`session` 是本次运行的事务会话，`checkpointer` 由 N10 的 A08 提供。

    `deliver_fn(device_id, message) -> bool` 是 **E2 触达出口**（信令层
    `app.audio.signaling.deliver` 的同形接口）：设备不在线返回 False、不抛异常。
    `tts_fn(text) -> bytes | None` 是 **TD-1 合成出口**（信令层
    `synthesize_touch_audio` 的同形接口）：失败返回 None、不抛异常，触达降级纯字幕。
    两者缺省用真实信令层；测试注入假函数即可覆盖「在线 / 离线 / 未绑设备 /
    合成成功 / 合成失败」各路。
    """
    _deliver = deliver_fn if deliver_fn is not None else _default_deliver
    _synthesize = tts_fn if tts_fn is not None else _default_tts

    async def remind(state: MedicationState) -> dict[str, Any]:
        event_id = _uuid(state["reminder_id"])
        attempt_no = int(state.get("attempt_no") or 1)

        # E2 触达：**先问这次触达是否已经发生过**（重放防护）。
        # 图恢复会重放本节点；`record_attempt` 的幂等只挡记录、挡不住投递——
        # 不先查就会给长者再播一遍同一次提醒。已有记录 → 跳过投递，只推进状态。
        # （同一 reminder 不会并发执行：图按 thread 串行，故「先查后发」无竞态。）
        already = await medication_service.get_attempt(
            session, event_id=event_id, attempt_no=attempt_no
        )
        if already is not None:
            logger.info(
                "medication_reminder_skipped_replay reminder_id=%s attempt_no=%s receipt=%s",
                state["reminder_id"],
                attempt_no,
                already.get("receipt"),
            )
        else:
            # TD-1 触达：先合成、再下发、再记回执。顺序不可换——record_attempt 对
            # (event_id, attempt_no) 幂等，重放安全；下发失败也要记（触达尝试是事实）。
            receipt, tts_result = await _deliver_reminder(
                session,
                _deliver,
                _synthesize,
                elder_id=_uuid(state["elder_id"]),
                reminder_id=event_id,
                attempt_no=attempt_no,
            )
            await medication_service.record_attempt(
                session,
                event_id=event_id,
                attempt_no=attempt_no,
                receipt=receipt,
                tts_result=tts_result,
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
        failure_reason = None if terminal in ("taken_reported", "refused") else "no_answer"
        if terminal == "missed_unconfirmed":
            # AC-20 的**唯一分流口**（2026-11-10 合并 dev-C WIP 时收敛）：
            # 此前 finalize 里有两处重叠——旧的看「最后一条 receipt」改 terminal 不改
            # failure_reason，新的 `_split_missed_by_receipt` 因此永远不触发，归因恒
            # `no_answer`。现在只留按回执集合判定的这一处：四次全没出过门 →
            # notification_failed + offline；任一条出过门 → missed_unconfirmed；
            # 查询失败保守不升级结论（见 split docstring）。
            terminal, failure_reason = await _split_missed_by_receipt(
                session, state["reminder_id"], failure_reason
            )
        await medication_service.set_terminal(
            session,
            event_id=_uuid(state["reminder_id"]),
            terminal_state=terminal,
            failure_reason=failure_reason,
            retry_count=max(0, attempts - 1),
            confirm_channel="voice" if terminal == "taken_reported" else None,
        )
        # 终态写完才建单（顺序不可交换）：见文件头 A12 段的说明。
        # notification_failed（根本没送到）同样建单——2026-11-10 主理人裁决：
        # 没送到更需要护工补送/代确认，不能因为「通知失败」就没人管（FR-MED-02/AC-20）。
        if terminal in ("missed_unconfirmed", "notification_failed"):
            await _escalate_missed_medication(session, state)
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
    result: dict[str, Any] = await graph.ainvoke(state, graph_config(reminder_id))
    return result


async def resume_medication_task(
    graph: Any, *, reminder_id: uuid.UUID, answer: str
) -> dict[str, Any]:
    """把长者的回答送回去，继续跑到下一次阻塞或结束。"""
    result: dict[str, Any] = await graph.ainvoke(
        Command(resume={"answer": answer}), graph_config(reminder_id)
    )
    return result


__all__ = [
    "MAX_ATTEMPTS",
    "MedicationState",
    "build_medication_graph",
    "graph_config",
    "resume_medication_task",
    "start_medication_task",
]
