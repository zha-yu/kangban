"""Day 7 C1 · 本地工具执行器（闭包工厂）。

**为什么闭包**：ToolExecutorFn 签名被 ToolRegistry 钉死在 `(args, runtime) -> dict`。
medication_confirm 需要 session + checkpointer 才能调子图；memory_recall 需要 session
才能查记忆。这俩都是外部依赖，executor 函数本身拿不到。工厂把依赖闭包捕获进去，
返回的 executor 签名不变——ToolRegistry 和 ToolExecutor 都不知道背后接了啥，干净。

**两层职责**：

- 身份收敛：elder_id 只从 `runtime.elder_id` 取，args 里的同名参数已被 ToolExecutor
  在 `_strip_identity` 里丢了（并审计），这里再兜底检查一次避免未来漏改。
- 四类失败：抛 ToolNotFoundError / ToolForbiddenError / ToolTransientError /
  ToolArgumentError —— ToolExecutor._run 会按 error_class 收敛成 ToolResult，
  本模块不直接返回 ToolResult。

**依赖方向**：tool_executor.py → 本模块 → services/ + graphs/ —— 不反向。
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Final

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import desc, or_, select

from app.core.runtime_context import Identity
from app.graphs.health_checkup_graph import (
    RED_ALERT_TTS_TEXT,
    build_health_checkup_graph,
    default_validate_answers,
    notify_red_dual_adapter,
    resume_health_checkup,
    start_health_checkup,
)
from app.graphs.health_checkup_graph import (
    default_thread_id as health_default_thread_id,
)
from app.graphs.health_checkup_graph import (
    graph_config as health_graph_config,
)
from app.graphs.medication_confirmation_graph import (
    build_medication_graph,
    graph_config,
    resume_medication_task,
)
from app.services.medication_due_service import list_waiting_confirm_events
from app.services.tool_executor import (
    ToolArgumentError,
    ToolForbiddenError,
    ToolNotFoundError,
    ToolTransientError,
)

logger = logging.getLogger(__name__)

#: 图状态「等待长者回答」的节点名（子图 node id；不能瞎改——与 graphs 目录同源）
_WAITING_NODE: str = "wait_confirm"


# ------------------------------------------------------------------ medication_confirm


def make_medication_confirm_executor(
    *,
    session_factory: Callable[[], Any],
    checkpointer: AsyncPostgresSaver,
) -> Callable[[dict[str, Any], Identity], Awaitable[dict[str, Any]]]:
    """返回 `medication_confirm` 工具的本地执行器。

    工厂注入：
    - ``session_factory``：每次调用开短会话（不持有，避免跨请求串 session）
    - ``checkpointer``：**必须是与 SchedulerRuntime 同一个**——对话侧 resume 和
      调度侧 resume 共享图状态才能做「先到先得」双驱动守卫

    核心逻辑（三跳）：
    1. 查「这位长者当前是否有 waiting_for=taken_answer 的事件」（service 层只读查询）
    2. 无 → 抛 ToolNotFoundError（ToolExecutor 收敛成 readable not_found）
    3. 有 → build graph + resume(answer) → 返回 terminal_state + 播报文本
    """

    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        # --- 身份收敛（兜底：ToolExecutor 已丢 args.elder_id，但 runtime 应该有） ---
        elder_id = runtime.elder_id
        if elder_id is None:
            raise ToolForbiddenError("无法识别长者身份")

        # --- 参数校验（ToolExecutor._validate_args 已过 schema，这里只做业务兜底） ---
        answer = args.get("answer", "")
        if not isinstance(answer, str) or not answer.strip():
            raise ToolArgumentError("answer 不能为空")

        # --- 1. 查等待中的事件（只读，service 层） ---
        async with session_factory() as session:
            waiting = await list_waiting_confirm_events(session)
            # 过滤：属于当前长者的、有图状态的。
            # 注意 _elder_matches_event 是 async 且 elder_id 入参口径是 UUID——
            # 原 bug：漏 await 使协程对象恒真（过滤失效）+ str 直接比对恒 False
            # （两个错误叠加恰好把所有事件都放进来，跨长者防护形同虚设）。
            elder_uuid = uuid.UUID(str(elder_id))
            matched = [
                e for e in waiting if await _elder_matches_event(session, e.reminder_id, elder_uuid)
            ]
            my_events = matched

        if not my_events:
            raise ToolNotFoundError("现在没有待确认的用药提醒")

        # 取第一条（同一长者不应有多条同时 waiting；有多条就处理最早的，其余
        # 按「对话侧只能确认一条」的业务口径走——不报错，不批量处理）
        event = my_events[0]
        reminder_id = event.reminder_id

        # --- 2. build graph + resume ---
        async with session_factory() as graph_session:
            graph = build_medication_graph(
                session=graph_session,
                checkpointer=checkpointer,
            )
            # resume 会推进到下一个阻塞点（taken_reported → END；或 refuse → END）
            # 如果子图已经终态（调度侧先 finalize 了），resume 会直接到 END 不报错
            try:
                await resume_medication_task(graph, reminder_id=reminder_id, answer=answer.strip())
            except Exception as exc:
                # 子图 resume 抛异常（比如终态已互斥）——按 transient 处理
                # 让 ToolExecutor 重试；重试仍失败后降级话术
                logger.warning(
                    "medication_confirm_resume_error reminder_id=%s error_type=%s",
                    reminder_id,
                    type(exc).__name__,
                )
                raise ToolTransientError(str(exc)) from exc

            # 事务提交（L6#6 联调裁决 2026-11-10）：mark_taken/mark_refused/finalize
            # 的业务写在 graph_session 上，service 层按「调用方提交」规范不自行
            # commit——这里就是调用方，不提交则 session 关闭即 rollback，确认结果
            # 凭空消失。
            await graph_session.commit()

            # 读最终状态
            state = await graph.aget_state(graph_config(reminder_id))

        values = (state.values if state is not None else None) or {}
        terminal = values.get("terminal")

        # --- 3. 组装播报文本（不返回技术字段） ---
        speech = _terminal_to_speech(terminal)
        logger.info(
            "medication_confirm_done elder_id=%s reminder_id=%s terminal=%s",
            elder_id,
            reminder_id,
            terminal,
        )

        return {
            "terminal_state": terminal,
            "message": speech,
        }

    return _executor


async def _elder_matches_event(session: Any, reminder_id: uuid.UUID, elder_id: uuid.UUID) -> bool:
    """验证 medication_event.reminder_id 确实属于这位长者（跨长者防护）。

    list_waiting_confirm_events 没带 elder_id 过滤（service 层通用），这里补上。
    """
    from sqlalchemy import select

    from app.models.medication import MedicationEvent

    row = (
        (
            await session.execute(
                select(MedicationEvent.elder_id).where(MedicationEvent.reminder_id == reminder_id)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return False
    return uuid.UUID(str(row)) == elder_id


def _terminal_to_speech(terminal: Any) -> str:
    """终态 → 可播报文本（不把 taken_reported 等术语直接念给长者）。"""
    mapping: dict[str, str] = {
        "taken_reported": "好的，我帮您记下来了。",
        "refused": "好的，我帮您记下来了。",
        "missed_unconfirmed": "好的，我帮您记下来了。",
        "notification_failed": "好的，我帮您记下来了。",
    }
    if not isinstance(terminal, str):
        return "好的，我帮您记下来了。"
    return mapping.get(terminal, "好的，我帮您记下来了。")


# ------------------------------------------------------------------ health_checkup（B3 · F14）

#: 语音健康采集活跃线程的 Redis TTL（主理人裁决 D9：15 分钟，每次 resume 续期）
_HEALTH_ACTIVE_TTL_SECONDS: Final[int] = 15 * 60

#: 健康子图停在 HITL（等护工/医生确认）时的 interrupt kind：
#: 对长者而言这轮采集已结束，**不能**再用下一句回答去 resume（会把确认 payload 冲掉）。
_HEALTH_HITL_KINDS: Final[frozenset[str]] = frozenset({"yellow_review", "red_confirm"})
#: 图侧 HITL 节点名（用于 resume 前的二次防护：陈旧 key 指向 HITL 时直接收口）
_HEALTH_HITL_NODES: Final[frozenset[str]] = frozenset({"hitl_yellow", "hitl_red"})


def make_health_checkup_executor(
    *,
    session_factory: Callable[[], Any],
    checkpointer: AsyncPostgresSaver,
    redis_client: Any,
) -> Callable[[dict[str, Any], Identity], Awaitable[dict[str, Any]]]:
    """返回 `health_checkup` 工具的本地执行器（AC-09/10/21/22/34 · F14）。

    多轮状态**只放 Redis**：key `<redis_key_prefix>:health:active:<elder_id>` → `thread_id`，
    TTL 15 分钟，每次 resume 续期。`thread_id` 只从这里读，**不从模型/客户端入参取**
    （AGENTS §4.9 身份铁律）。

    三跳语义：
    1. 无活跃线程 → `start_health_checkup(source="voice_active")`，把第一问返回给模型；
    2. 有活跃线程且传了 `answer` → `resume_health_checkup(thread_id, payload=answer)`；
    3. 到终态（无 `__interrupt__`）或停在 HITL → 删 key，返回 `severity`/`record_id`；
       停在 HITL 时删 key 是安全需要：后续对话句不能再 resume 进确认分支。
    """

    def _key(elder_id: Any) -> str:
        from app.core.config import get_settings

        return f"{get_settings().redis_key_prefix}:health:active:{elder_id}"

    async def _redis_get(key: str) -> str | None:
        try:
            raw = await redis_client.get(key)
        except Exception as exc:
            logger.warning("health_checkup_redis_get_failed error_type=%s", type(exc).__name__)
            raise ToolTransientError("健康采集会话存储暂时不可用") from exc
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)

    async def _redis_set(key: str, thread_id: str) -> None:
        try:
            await redis_client.set(key, thread_id, ex=_HEALTH_ACTIVE_TTL_SECONDS)
        except Exception as exc:
            logger.warning("health_checkup_redis_set_failed error_type=%s", type(exc).__name__)
            raise ToolTransientError("健康采集会话存储暂时不可用") from exc

    async def _redis_delete(key: str) -> None:
        try:
            await redis_client.delete(key)
        except Exception as exc:  # 删不掉只会多留 15 分钟，不影响本轮结论
            logger.warning("health_checkup_redis_delete_failed error_type=%s", type(exc).__name__)

    def _build_graph(session: Any) -> Any:
        # 语音路径与调度路径共用同一套注入：双通知走 X3 v0.6；
        # 红档落库走 P10 已闭合的 `create_red_landing_adapter`（只落库、不通知）。
        from app.graphs.health_checkup_graph import create_red_landing_adapter

        return build_health_checkup_graph(
            session=session,
            checkpointer=checkpointer,
            validate_fn=default_validate_answers,
            notify_red_fn=notify_red_dual_adapter,
            create_red_order_fn=create_red_landing_adapter,
        )

    def _awaiting(value: Any) -> dict[str, Any]:
        payload = value if isinstance(value, dict) else {}
        prompt = str(payload.get("prompt") or "您再说一下这个数就好。")
        return {
            "status": "awaiting_answer",
            "question": payload.get("question"),
            "prompt": prompt,
            "attempt_no": payload.get("attempt_no"),
            "message": prompt,
        }

    def _terminal_message(result: dict[str, Any]) -> str:
        severity = str(result.get("grade_level") or "")
        notified = result.get("notified") or {}
        if severity == "red":
            return str(notified.get("tts_text") or RED_ALERT_TTS_TEXT)
        if severity == "yellow":
            return "记下来了，我让护士再看一眼。"
        return "好，我记下来了。"

    async def _finish(result: dict[str, Any], *, key: str, thread_id: str) -> dict[str, Any]:
        interrupts = tuple(result.get("__interrupt__") or ())
        severity = str(result.get("grade_level") or "")
        record_ref = dict(result.get("record_ref") or {})
        order_ref = dict(result.get("order_ref") or {})
        record_id = record_ref.get("record_id") or order_ref.get("record_id")

        if interrupts:
            payload = interrupts[0].value
            kind = payload.get("kind") if isinstance(payload, dict) else None
            if kind in _HEALTH_HITL_KINDS:
                await _redis_delete(key)
                message = (
                    RED_ALERT_TTS_TEXT if kind == "red_confirm" else "记下来了，我让护士再看一眼。"
                )
                return {
                    "status": "handed_off",
                    "severity": severity or ("red" if kind == "red_confirm" else "yellow"),
                    "record_id": str(record_id) if record_id else None,
                    "message": message,
                }
            await _redis_set(key, thread_id)  # 还有下一问：续期
            return _awaiting(payload)

        await _redis_delete(key)  # 终态：清活跃线程
        return {
            "status": "completed",
            "severity": severity,
            "record_id": str(record_id) if record_id else None,
            "rule_version": result.get("rule_version"),
            "message": _terminal_message(result),
        }

    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        elder_id = runtime.elder_id
        if elder_id is None:
            raise ToolForbiddenError("无法识别长者身份")

        raw_answer = args.get("answer")
        if raw_answer is not None:
            if not isinstance(raw_answer, str):
                raise ToolArgumentError("answer 必须是字符串")
            answer = raw_answer.strip()
            if len(answer) > 200:
                raise ToolArgumentError("answer 最多 200 字")
        else:
            answer = None

        key = _key(elder_id)
        thread_id = await _redis_get(key)

        # --- 有活跃线程但本轮没带回答：不推进，把当前问题再交给模型（幂等） ---
        if thread_id and not answer:
            async with session_factory() as session:
                graph = _build_graph(session)
                state = await graph.aget_state(health_graph_config(thread_id))
                if not state.values:
                    await _redis_delete(key)  # 陈旧 key（线程已不存在）→ 重新开始
                    thread_id = None
                elif state.interrupts:
                    await _redis_set(key, thread_id)
                    return _awaiting(state.interrupts[0].value)
                else:
                    # 图已跑完但 key 未清（异常窗口）→ 收口
                    values = dict(state.values)
                    await _redis_delete(key)
                    return {
                        "status": "completed",
                        "severity": values.get("grade_level"),
                        "record_id": (values.get("record_ref") or {}).get("record_id")
                        or (values.get("order_ref") or {}).get("record_id"),
                        "message": _terminal_message(values),
                    }

        # --- 无活跃线程：开新一轮（answer 丢弃，第一问才是采集起点） ---
        if not thread_id:
            thread_id = health_default_thread_id(elder_id, "voice_active")
            async with session_factory() as session:
                graph = _build_graph(session)
                result = await start_health_checkup(
                    graph,
                    elder_id=elder_id,
                    source="voice_active",
                    thread_id=thread_id,
                )
                await session.commit()  # 会话管理豁免：节点写库在 session 上
            await _redis_set(key, thread_id)
            outcome = await _finish(result, key=key, thread_id=thread_id)
            logger.info(
                "health_checkup_started elder_id=%s thread=%s status=%s",
                elder_id,
                thread_id,
                outcome.get("status"),
            )
            return outcome

        # --- 有活跃线程且带回答：resume 下一问/终态 ---
        async with session_factory() as session:
            graph = _build_graph(session)
            # 二次防护：key 尚未过期但图已停在 HITL（异常窗口）→ 不能把长者的话
            # 当成护工/医生的确认 payload
            state = await graph.aget_state(health_graph_config(thread_id))
            if state.values and set(state.next) & _HEALTH_HITL_NODES:
                await _redis_delete(key)
                return {
                    "status": "handed_off",
                    "severity": state.values.get("grade_level"),
                    "record_id": (state.values.get("order_ref") or {}).get("record_id"),
                    "message": "这轮已经交给护士/医生确认了，您先歇会儿。",
                }
            try:
                result = await resume_health_checkup(graph, thread_id=thread_id, payload=answer)
            except Exception as exc:
                logger.warning(
                    "health_checkup_resume_failed elder_id=%s thread=%s error_type=%s",
                    elder_id,
                    thread_id,
                    type(exc).__name__,
                )
                raise ToolTransientError(str(exc)) from exc
            await session.commit()
        outcome = await _finish(result, key=key, thread_id=thread_id)
        logger.info(
            "health_checkup_resumed elder_id=%s thread=%s status=%s",
            elder_id,
            thread_id,
            outcome.get("status"),
        )
        return outcome

    return _executor


# ------------------------------------------------------------------ F22 黄档复核出口


def make_boundary_review_executor(
    *,
    session_factory: Callable[[], Any],
) -> Callable[..., Awaitable[Any]]:
    """返回 Router 的 `boundary_review` 注入件（FR-DLG-07 历史/他人/模糊）。

    Router 不持有 session、不 import services；本工厂把「黄档 follow_up 建单」
    收在 B 侧，装配时一行注入：

    ```python
    Router(..., boundary_review=make_boundary_review_executor(session_factory=session_factory))
    ```

    - `order_type="follow_up"`、`severity="yellow"`、`dedupe_token="boundary:<turn_id>"`；
    - **不建 risk_events**（避免污染红色统计）；
    - `elder_id` 缺省（generic）→ 不建单，返回 None。
    """

    async def _review(
        *,
        elder_id: str | None,
        thread_id: str,
        turn_id: str,
        dedupe_token: str,
        reason: str,
        matched_rule: str | None,
    ) -> Any:
        if elder_id is None:
            return None
        from app.services.work_order_service import open_work_order

        async with session_factory() as session:
            ref = await open_work_order(
                session,
                elder_id=uuid.UUID(str(elder_id)),
                order_type="follow_up",
                severity="yellow",
                dedupe_token=dedupe_token,
            )
            await session.commit()
        logger.info(
            "boundary_review_order_opened elder_id=%s thread_id=%s turn_id=%s "
            "reason=%s order_id=%s",
            elder_id,
            thread_id,
            turn_id,
            reason,
            ref.order_id,
        )
        return ref

    return _review


# ------------------------------------------------------------------ memory_recall


def make_memory_recall_executor(
    *,
    session_factory: Callable[[], Any],
) -> Callable[[dict[str, Any], Identity], Awaitable[dict[str, Any]]]:
    """返回 `memory_recall` 工具的本地执行器。

    依赖 dev-B 的 `recall_for_turn(*, elder_id, query, limit=5) -> list[str]` 接口
    （C1 交接契约，2026-11-13 修正调用侧对齐冻结签名）。
    B2 还没落地时会抛 ImportError——**降级为返回空列表（succeeded + notes=[]），不报错**。
    原因：memory_recall 是只读查询，没有副作用，返回空列表不会破坏任何业务事实。
    """

    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        elder_id = runtime.elder_id
        if elder_id is None:
            raise ToolForbiddenError("无法识别长者身份")

        query = args.get("query", "")

        # --- 软依赖 B2 的 recall_for_turn ---
        try:
            from app.services.memory_service import recall_for_turn
        except ImportError:
            logger.info(
                "memory_recall_stub_no_impl elder_id=%s query=%r "
                "(dev-B recall_for_turn not yet wired)",
                elder_id,
                query,
            )
            return {"notes": []}

        try:
            # C1 交接契约（签名冻结）：recall_for_turn(*, elder_id, query, limit=5) -> list[str]，
            # 自管 DB 会话（不收 session），无 category 参数。此前的
            # recall_for_turn(session, ..., category=...) 是旧契约，每次调用必 TypeError。
            notes = await recall_for_turn(elder_id=elder_id, query=query)
        except ToolError:  # 内层 ToolError 直接上抛让 ToolExecutor 收敛
            raise
        except Exception as exc:
            logger.warning(
                "memory_recall_error elder_id=%s error_type=%s", elder_id, type(exc).__name__
            )
            raise ToolTransientError(str(exc)) from exc

        # recall_for_turn 返回 chunk 文本列表 → 包 dict 形态给 Agent（保持既有消费格式）
        note_dicts: list[dict[str, Any]] = [{"content": chunk} for chunk in notes]
        return {"notes": note_dicts}

    return _executor


class ToolError(Exception):
    """占位：本模块自己不定义 ToolError 基类——用 tool_executor 里的四类域异常即可。
    这个类只是让 import 不出错（B2 接线后如果 recall_for_turn 也抛 ToolError，
    它应该是 tool_executor 那个——让它冒泡让父 except 接）。
    """


# ------------------------------------------------------------------ play_opera（Day 8 B4 / X8 ②）

#: 点播口语前缀（最长优先）：LLM 有时把整句「播放茉莉雨」原样塞进 query，
#: 中文无空格拆不开词导致 ILIKE 落空——整词 miss 后剥前缀重试一次兜底
_PLAY_QUERY_PREFIXES: Final = (
    "我要听",
    "我想听",
    "我想看",
    "来一首",
    "来一段",
    "放一首",
    "放一段",
    "给我放",
    "给我唱",
    "播放一下",
    "播放",
    "放一下",
    "唱一段",
    "唱一下",
    "听一下",
    "听一首",
    "来个",
    "请放",
    "我要",
    "我想",
    "给我",
    "帮我",
    "听",
    "放",
)


def _strip_play_prefixes(query: str) -> str:
    """剥掉点播口语前缀（可叠多次，如「我要播放茉莉雨」），剥空则原样返回。"""
    result = query.strip()
    changed = True
    while changed:
        changed = False
        for prefix in _PLAY_QUERY_PREFIXES:  # 长前缀在前；剥中即断，防「放一首」被「放」二次误剥
            if result.startswith(prefix) and len(result) > len(prefix):
                result = result[len(prefix) :].strip()
                changed = True
                break
    return result or query


#: 检索词标点清洗：书名号/引号/句读——「《素颜》」「素颜。」都会让 ILIKE 落空
_SEARCH_PUNCT_RE: Final = re.compile(r"[《》〈〉「」『』“”‘’\"'。．.，,！!？?～~：:；;]")

#: play_opera 的播放控制动作（契约 §6.3 七动作 + interrupt）：pad 本地执行，
#: 后端执行器只返回 {"control": action} 标记、由下发层转 media_control 帧
_PLAY_CONTROL_ACTIONS: Final[frozenset[str]] = frozenset(
    {"pause", "resume", "stop", "repeat", "replay", "interrupt", "volume_up", "volume_down"}
)

#: 规则/Tool 入参动作 → pad 七动作名（§6.5 冻结：repeat→replay）
_CONTROL_ACTION_ALIASES: Final[dict[str, str]] = {"repeat": "replay"}


def make_play_opera_executor(
    *,
    session_factory: Callable[[], Any],
) -> Callable[[dict[str, Any], Identity], Awaitable[dict[str, Any]]]:
    """返回 `play_opera` 工具的本地执行器（X8 口径②冻结 schema）。

    - **action=play**（缺省）：只读检索 media_library（type=opera），按
      title/genre/era/artist 匹配检索词；逐级兜底：原词 → 剥点播口语前缀 →
      剥书名号/标点，兜住 LLM 整句或带标点传参；
      并以 `play_opera_query` 日志记录实际入参（排查 LLM 传参行为用）；
    - **action=next**（换一段，X8 口径③定稿）：顺序续播迁往
      `media_service.get_next_media`——锚点 = 该长者 `media_history` 最近一条
      （started_at 倒序），同剧种内 media_id 升序下一段、末尾回绕；
      无锚点（generic / 无播放历史）→ 从全集头部开始（media_id 升序），不再随机；
    - **其余控制动作**（pause/resume/stop/replay/interrupt/volume_up/volume_down）：
      不查库，返回 `{"control": <action>}` 标记，由下发层转 media_control 帧推 pad
      （七动作在 pad OperaPlayer 本地执行，协议 §D4 media_control）；
    - play/next 返回 `{"found": bool, "media": {...} | None}`，media_url 严格走
      X8 稳定路径 `/static/media/opera/<media_id>.<ext>`（口径①，v1.3 扩展名放开）；
    - **不写 media_history**——播放历史归 C4 media_service.record_playback
      （X8 ②：Tool 层不直接写 history，FR-CON-06 P1）；
    - 查询直接用 ORM 读模型：media_service 未交付前的过渡实现，
      C4 交付后检索入口迁往 media_service.search_opera（只读迁移，零接口变化）。
    """

    def _media_of(row: Any) -> dict[str, Any]:
        return {
            "media_id": str(row.media_id),
            "title": row.title,
            "genre": row.genre,
            "era": row.era,
            "artist": row.artist,
            "media_url": row.media_url,
            "duration": row.duration,
            "copyright_status": row.copyright_status,
        }

    async def _search(session: Any, raw_query: str, terms: list[str], model: Any) -> Any | None:
        """按检索词 ILIKE 匹配 title/genre/era/artist，全词精确命中优先，取 1 行。"""
        stmt = select(model).where(model.type == "opera")
        if terms:
            conds = []
            for term in terms:
                pattern = f"%{term}%"
                conds.extend(
                    (
                        model.title.ilike(pattern),
                        model.genre.ilike(pattern),
                        model.era.ilike(pattern),
                        model.artist.ilike(pattern),
                    )
                )
            stmt = stmt.where(or_(*conds))
        stmt = stmt.order_by(desc(model.title == raw_query), model.title).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        action = str(args.get("action") or "play")

        if action in _PLAY_CONTROL_ACTIONS:
            # 播放控制：pad 本地执行，后端只转发（不查库、不写库）
            # repeat 是规则层 R-08 的入参写法，落到协议上用 pad 的 replay（§6.5）
            return {"control": _CONTROL_ACTION_ALIASES.get(action, action)}

        from app.models.media import MediaLibrary

        if action == "next":
            # 换一段（X8 口径③定稿）：**顺序续播**迁往 media_service.get_next_media
            # （C4 已交付，替代过渡期随机取一首）——锚点 = 该长者最近一次播放
            # （media_history started_at 倒序第一条），同剧种内 media_id 升序取
            # 下一段、末尾回绕；generic 设备（elder_id 缺失）或尚无播放历史 →
            # 从全集头部开始（media_id 升序第一段，与 get_next_media 同序）。
            from app.services import media_service

            async with session_factory() as session:
                anchor_id: uuid.UUID | None = None
                raw_elder = getattr(runtime, "elder_id", None)
                if raw_elder:
                    try:
                        elder_uuid = uuid.UUID(str(raw_elder))
                    except ValueError:
                        elder_uuid = None  # 身份闸门③已校验过 UUID 规范形；兜底不猜
                    if elder_uuid is not None:
                        history = await media_service.list_media_history(
                            session, elder_id=elder_uuid, limit=1
                        )
                        if history:
                            anchor_id = history[0].media_id
                if anchor_id is not None:
                    row = await media_service.get_next_media(session, current_media_id=anchor_id)
                else:
                    row = (
                        await session.execute(
                            select(MediaLibrary)
                            .where(MediaLibrary.type == "opera")
                            .order_by(MediaLibrary.media_id)
                            .limit(1)
                        )
                    ).scalar_one_or_none()
            if row is None:
                return {"found": False, "media": None}
            return {"found": True, "media": _media_of(row)}

        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolArgumentError("query 不能为空")

        from app.models.media import MediaLibrary

        # 逐级候选：原词 → 剥口语前缀 → 剥书名号/标点再剥前缀（LLM/ASR 常给歌名加《》或句读）
        candidates: list[str] = [query]
        stripped = _strip_play_prefixes(query)
        if stripped != query:
            candidates.append(stripped)
        cleaned = _SEARCH_PUNCT_RE.sub("", stripped)
        if cleaned != stripped:
            candidates.append(_strip_play_prefixes(cleaned))
        logger.info("play_opera_query query=%r candidates=%r", query, candidates)

        try:
            async with session_factory() as session:
                row: Any | None = None
                for cand in candidates:
                    cand_terms = [
                        t for t in cand.replace("，", " ").replace("　", " ").split() if t
                    ]
                    row = await _search(session, cand, cand_terms, MediaLibrary)
                    if row is not None:
                        break
        except ToolError:
            raise
        except Exception as exc:
            logger.warning(
                "play_opera_query_error query=%r error_type=%s", query, type(exc).__name__
            )
            raise ToolTransientError(str(exc)) from exc

        if row is None:
            # 未找到：X8 ② 明确返回 {"found": False, "media": null}，
            # 上层按 FR-CON-04 提示+推荐，不算工具失败
            return {"found": False, "media": None}

        return {"found": True, "media": _media_of(row)}

    return _executor


__all__ = [
    "make_boundary_review_executor",
    "make_health_checkup_executor",
    "make_medication_confirm_executor",
    "make_memory_recall_executor",
    "make_play_opera_executor",
]
