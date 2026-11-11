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


# ------------------------------------------------------------------ memory_recall


def make_memory_recall_executor(
    *,
    session_factory: Callable[[], Any],
) -> Callable[[dict[str, Any], Identity], Awaitable[dict[str, Any]]]:
    """返回 `memory_recall` 工具的本地执行器。

    依赖 dev-B 的 `recall_for_turn(elder_id, query, limit)` 接口。
    B2 还没落地时会抛 ImportError——**降级为返回空列表（succeeded + notes=[]），不报错**。
    原因：memory_recall 是只读查询，没有副作用，返回空列表不会破坏任何业务事实。
    """

    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        elder_id = runtime.elder_id
        if elder_id is None:
            raise ToolForbiddenError("无法识别长者身份")

        query = args.get("query", "")
        category = args.get("category")

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
            async with session_factory() as session:
                notes = await recall_for_turn(
                    session, elder_id=elder_id, query=query, category=category
                )
        except ToolError:  # 内层 ToolError 直接上抛让 ToolExecutor 收敛
            raise
        except Exception as exc:
            logger.warning(
                "memory_recall_error elder_id=%s error_type=%s", elder_id, type(exc).__name__
            )
            raise ToolTransientError(str(exc)) from exc

        # recall_for_turn 的返回类型：MemoryNote 列表 → 转 dict 给 Agent
        note_dicts: list[dict[str, Any]] = []
        for n in notes:
            note_dicts.append(
                {
                    "content": getattr(n, "content", str(n)),
                    "category": getattr(n, "category", ""),
                }
            )
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


#: 规则/模型播放控制词 → pad 侧七动作名（契约 §6.3：next→next_track、repeat→replay）
_OPERA_ACTION_ALIASES: dict[str, str] = {
    "pause": "pause",
    "resume": "resume",
    "stop": "stop",
    "next": "next_track",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "repeat": "replay",
    "interrupt": "interrupt",
}

#: 每长者最近一次下发的曲目（仅本进程内存，用于「换一段」；不落库，history 归 C4）
_LAST_OPERA_BY_ELDER: dict[str, uuid.UUID] = {}


def make_play_opera_executor(
    *,
    session_factory: Callable[[], Any],
) -> Callable[[dict[str, Any], Identity], Awaitable[dict[str, Any]]]:
    """返回 `play_opera` 工具的本地执行器（X8 口径②冻结 schema）。

    - **两种入参**：`query`（点播检索）或 `action`（七动作控制），至少给一个；
      `action` 优先于 `query`（规则层 R-01~R-08 只传 action）。
    - **控制动作**：pause/resume/stop/volume_up/volume_down/repeat→replay 返回
      `{"found": true, "action": <pad 动作名>, "media": null}`，由 companion 经
      `on_media_control` 下发 `media_control` 帧；interrupt 由 pad 端 barge-in
      自行收口，不下发媒体帧；next→next_track 由服务端取下一段随帧携带 media。
    - **点播检索**：只读检索 media_library（type=opera），按 title/genre/era/artist
      匹配检索词；逐级兜底：原词 → 剥点播口语前缀 → 剥书名号/标点，兜住 LLM
      整句或带标点传参；并以 `play_opera_query` 日志记录实际入参。
    - 返回 `{"found": bool, "media": {...} | None}`，media_url 严格走 X8 稳定路径
      `/static/media/opera/<media_id>.<ext>`（口径①，v1.3 扩展名放开）；
    - **不写 media_history**——播放历史归 C4 media_service.record_playback
      （X8 ②：Tool 层不直接写 history，FR-CON-06 P1）；
    - 查询直接用 ORM 读模型：media_service 未交付前的过渡实现，
      C4 交付后检索入口迁往 media_service.search_opera（只读迁移，零接口变化）。
    """

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

    def _media_payload(row: Any) -> dict[str, Any]:
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

    def _elder_key(runtime: Identity) -> str:
        return runtime.elder_id or runtime.device_id or ""

    async def _next_track_payload(runtime: Identity, model: Any) -> dict[str, Any] | None:
        """「换一段」：按目录顺序取上一段之后的一条；无上下文时从头开始。

        状态只存本进程内存（`_LAST_OPERA_BY_ELDER`），不落库——播放历史归 C4
        `media_service`，Tool 层不写 history（X8 ②）。
        """
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(model)
                        .where(model.type == "opera")
                        .order_by(model.title, model.media_id)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return None
            key = _elder_key(runtime)
            last = _LAST_OPERA_BY_ELDER.get(key)
            index = next((i for i, row in enumerate(rows) if row.media_id == last), -1)
            picked = rows[(index + 1) % len(rows)]
            _LAST_OPERA_BY_ELDER[key] = picked.media_id
            return _media_payload(picked)

    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        from app.models.media import MediaLibrary

        action = str(args.get("action") or "").strip()
        query = str(args.get("query") or "").strip()

        # 控制动作优先：规则层 R-01~R-08 只传 action（不传 query）
        if action:
            pad_action = _OPERA_ACTION_ALIASES.get(action)
            if pad_action is None:
                raise ToolArgumentError(f"不支持的播放动作：{action}")
            if pad_action == "interrupt":
                # 「别说了」由 pad 端 speechstart/barge-in 收口，不下发媒体帧
                return {"found": True, "action": "interrupt", "media": None}
            if pad_action == "next_track":
                payload = await _next_track_payload(runtime, MediaLibrary)
                if payload is None:
                    return {"found": False, "media": None, "action": "next_track"}
                return {"found": True, "action": "next_track", "media": payload}
            return {"found": True, "action": pad_action, "media": None}

        if not query:
            raise ToolArgumentError("query 与 action 至少给一个")

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

        _LAST_OPERA_BY_ELDER[_elder_key(runtime)] = row.media_id
        return {"found": True, "media": _media_payload(row)}

    return _executor


__all__ = [
    "make_medication_confirm_executor",
    "make_memory_recall_executor",
    "make_play_opera_executor",
]
