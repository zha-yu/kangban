"""FastAPI 装配层（技术文档 §2.1：`app/main.py` 承担 FastAPI 装配、`/health`、trace_id 中间件）。

分层约束（AGENTS.md §2）：本文件只做装配，不写业务逻辑；路由在 `app/api/`，
领域逻辑与写库在 `app/services/`。

启动：`uvicorn app.main:app --reload --port 8000`
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Any, Literal
from uuid import UUID

import redis.asyncio as redis_asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.agents.companion import DEFAULT_TOOLS, CompanionAgent
from app.agents.guardrails import Guardrails
from app.agents.session_coordinator import SessionCoordinator
from app.agents.tool_registry import default_registry
from app.api import ROUTERS
from app.api.elders import _is_request_format_error
from app.api.schemas.auth import ErrorResponse
from app.audio import signaling  # Day 3 T2：WS 信令路由（/ws/signal）
from app.audio.signaling import _ERROR_TALKBACK  # 失败终态话术表（C-1/C-D4）
from app.audio.tts import TTSUnavailable, build_tts
from app.audio.webrtc import (  # Day 3 T3：A3 媒体层接线
    enqueue_tts_for_device,
    install_signaling_handler,
)
from app.core.auth import (
    CredentialExpiredError,
    CredentialInvalidError,
    CredentialMissingError,
    CredentialRevokedError,
)
from app.core.config import Settings, get_settings
from app.core.context import TRACE_HEADER, get_trace_id
from app.core.logging import configure_logging
from app.core.metrics import get_metrics_recorder
from app.core.runtime_context import Identity
from app.core.security import TokenExpiredError
from app.core.trace import TraceIdMiddleware
from app.db import session_factory
from app.mcp_client import MCPClient
from app.router.decision import Router
from app.router.safety import SafetyDecision
from app.scheduler.medication_jobs import install_scheduler  # Day 7 C2：用药到点调度接线
from app.services import (
    history_service,
    identity_service,
    medication_service,
    memory_service,
    voiceprint_service,
)
from app.services.auth_service import (
    AuthConflictError,
    AuthExpiredError,
    AuthInvalidError,
    AuthRevokedError,
)
from app.services.dialogue_service import (
    DialogueOrchestrator,
    DialogueService,
    IdentityConfirmationRequired,
    IdentityThreadResult,
    RedisTurnStore,
    identity_decision_to_result,
)
from app.services.llm_service import LLMService
from app.services.response_service import RedisEpochPersistence, ResponseService
from app.services.safety_service import escalate_safety_event
from app.services.state_service import StateCenter
from app.services.tool_executor import ToolExecutor, ToolResult
from app.services.turn_service import (
    ContextBuilder,
    CoordinatorStateCenterAdapter,
    HistoryEventSink,
    TurnService,
)
from app.system_one.client import SystemOneClient


class _SessionToolExecutor:
    """CompanionAgent 的 ToolExecutor 协议 → 每次调用独立短会话（装配层适配）。

    - `ToolExecutor` 是「一个会话一个实例」（N22 契约），而 Agent 持单例；
      故在此按调用开短会话并在成功路径补 commit（ToolExecutor 只 flush 不
      commit，事务边界与 `_escalate_safety` 同口径在装配层闭合）；
    - Guardrails 不传：预算/熔断判定权在 Agent 侧 Guardrails（每轮 reset），
      执行器侧再判一次会把同一份预算双计；
    - ToolExecutor 不抛业务异常，仅编程错误上抛 → 原样透传，会话内已 flush
      的行随 rollback 丢弃。
    - `registry`（Day 7 A3）：**必须传装配层那一个实例**。`default_registry()`
      每次返回新对象，`bind_dev_c_tools()` 绑的是传入的那个实例——不传就会
      「工具看着绑了、执行时却是未绑定」，用药确认直接掉进 not_found。
    """

    def __init__(
        self,
        factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        mcp_client: Any | None = None,
        registry: Any | None = None,
    ) -> None:
        self._factory = factory
        #: N26 最后一环：MCP 工具客户端（weather），透传给每次调用的 ToolExecutor
        self._mcp = mcp_client
        #: Day 7：装配层唯一的工具注册表（已 bind 过 C1 执行器）
        self._registry = registry

    async def execute(
        self,
        *,
        tool_name: str,
        args: Mapping[str, object],
        runtime: Identity,
        turn_id: str,
        idempotency_key: str,
    ) -> ToolResult:
        async with self._factory() as session:
            try:
                result = await ToolExecutor(
                    session, registry=self._registry, mcp_client=self._mcp
                ).execute(
                    tool_name=tool_name,
                    args=args,
                    runtime=runtime,
                    turn_id=turn_id,
                    idempotency_key=idempotency_key,
                )
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


class _MemoryExtractionAdapter:
    """B1↔B2 记忆提取端口适配（裁决 O-B2）：终态结算后台调 extract_after_turn。

    ResponseService 侧已用 create_task 调度（不阻塞 ack/interrupt 返回），
    本适配只做一次性转发；generic（elder_id=None）由 ResponseService 闸门拦截，
    不会到达此处。失败在 response_service 侧记日志，不回滚终态。
    """

    async def extract_after_turn(
        self,
        *,
        elder_id: str | None,
        thread_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
    ) -> int:
        return await memory_service.extract_after_turn(
            elder_id=elder_id,
            thread_id=thread_id,
            turn_id=turn_id,
            user_text=user_text,
            assistant_text=assistant_text,
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程级生命周期：Redis/LLM/Dialogue 运行时与 FINAL seam 的唯一装配点。

    Redis / HTTP 客户端在本进程创建并关闭；两者都是懒连接，外部依赖未起时不阻塞
    启动（`/health` 与 Day2 验收口径不变）。`conversation_threads` 仍由 A1
    `identity_service` 独占创建，A2 只消费其 `IdentityDecision`。
    """
    logger = logging.getLogger("app.main")
    settings: Settings = app.state.settings

    redis_client = redis_asyncio.from_url(
        settings.redis_url, encoding="utf-8", decode_responses=False
    )
    llm_service = LLMService(settings)
    tts_provider = build_tts(settings)
    turn_store = RedisTurnStore(client=redis_client, key_prefix=settings.redis_key_prefix)
    dialogue_service = DialogueService(llm=llm_service, store=turn_store)

    # 每 (device_id, trace_id) 缓存已打开的 thread + 身份：同一 WS 会话内复用，
    # 避免每个 FINAL 都新建 conversation_thread；换连接由新 trace_id 重新首轮校验。
    thread_cache: OrderedDict[tuple[str, str], IdentityThreadResult] = OrderedDict()
    thread_cache_max = 128

    async def _publish_identity(
        device_id: str, decision: identity_service.IdentityDecision
    ) -> None:
        """A1 终态 → C5 identity_result；confirm 分支由 signaling confirm 回调负责。"""
        if decision.source == "voice" and decision.elder_id is not None:
            await signaling.apply_voice_identity(device_id, elder_id=str(decision.elder_id))
        elif decision.source == "generic":
            await signaling.apply_generic_identity(device_id)

    async def _resolve_identity(
        *,
        device_id: str,
        turn_id: str,
        text: str,
        trace_id: str,
        audio_bytes: bytes | None,
    ) -> IdentityThreadResult:
        # A1 identity_service 独占创建 thread；A2 只消费终态 IdentityDecision。
        cache_key = (device_id, trace_id)
        cached = thread_cache.get(cache_key)
        if cached is not None:
            thread_cache.move_to_end(cache_key)
            # 静默复检已移除（TD v1.9 主理人裁决，2026-11-09）：会话内身份以首轮
            # 校验与主动确认为准，后续 utterance 直接沿用当前 thread
            return cached

        async with session_factory() as session:
            try:
                decision = await identity_service.verify_session_identity(
                    session,
                    device_id=UUID(device_id),
                    audio_bytes=audio_bytes,
                )
            except voiceprint_service.VoiceprintInvalidAudio:
                # 声纹容器拒绝该段（过短/格式异常）时降级为无音频主动确认
                logger.warning(
                    "voiceprint_invalid_audio",
                    extra={"trace_id": trace_id, "device_id": device_id},
                )
                decision = await identity_service.verify_session_identity(
                    session,
                    device_id=UUID(device_id),
                    audio_bytes=None,
                )
        await _publish_identity(device_id, decision)
        result = identity_decision_to_result(decision)
        thread_cache[cache_key] = result
        while len(thread_cache) > thread_cache_max:
            thread_cache.popitem(last=False)
        return result

    async def _on_identity_confirm(device_id: str, trace_id: str, elder_id: str) -> None:
        # signaling 已校验 elder_id == 设备绑定候选；A1 事务内开 confirm thread
        async with session_factory() as session:
            decision = await identity_service.open_thread_from_confirm(
                session,
                device_id=UUID(device_id),
                confirmed_elder_id=UUID(elder_id),
                candidate_elder_id=UUID(elder_id),
            )
        result = identity_decision_to_result(decision)
        thread_cache[(device_id, trace_id)] = result
        while len(thread_cache) > thread_cache_max:
            thread_cache.popitem(last=False)

    async def _on_identity_timeout(device_id: str, trace_id: str) -> None:
        # 30s 超时：A1 开 generic thread；signaling 随后下发 identity_result(generic)
        async with session_factory() as session:
            decision = await identity_service.open_thread_as_generic_on_timeout(
                session,
                device_id=UUID(device_id),
            )
        result = identity_decision_to_result(decision)
        thread_cache[(device_id, trace_id)] = result
        while len(thread_cache) > thread_cache_max:
            thread_cache.popitem(last=False)

    # Day 6 N14：L0 安全一票否决（N27）的三表落库面——红档命中时 orchestrator
    # 调本回调，事务内写 risk_events / work_orders / notifications 后 commit。
    # 事务边界在本回调内闭合：escalation 失败上抛由 orchestrator 记日志不反抛。
    async def _escalate_safety(
        *,
        elder_id: str,
        decision: SafetyDecision,
        thread_id: str,
    ) -> bool:
        async with session_factory() as session:
            await escalate_safety_event(
                session,
                elder_id=UUID(elder_id),
                decision=decision,
                thread_id=thread_id,
            )
            await session.commit()
        return True

    # ================== Day 7 A3：三级路由 + 决策日志 + 调度/工具装配 ==================
    # 本段是**唯一装配点**：C1/C2 只给函数，A3 在前端装配（main.py 由 A3 独占写锁）。

    # ---- D2 工具注册表：一次构造，后续复用（`default_registry()` 每次返回新实例，
    #      bind 到临时对象上等于没绑，见 `_SessionToolExecutor` docstring） ----
    tool_registry = default_registry()
    mcp_client = MCPClient(settings)
    session_tool_executor = _SessionToolExecutor(
        session_factory, registry=tool_registry, mcp_client=mcp_client
    )

    # ---- D2/D3：checkpointer（与调度侧同一 DSN + 同一 schema）+ 绑定 C1 执行器 ----
    # app_env=test 时跳过：单元测试没有 PG，装配期连库会让「无库也能跑」的用例集体失败。
    checkpointer_cm = None
    if settings.app_env.lower() != "test":
        try:
            from app.graphs.checkpointer import medication_checkpointer

            checkpointer_cm = medication_checkpointer(
                dsn=settings.langgraph_checkpoint_database_url,
                schema=settings.langgraph_checkpoint_schema,
            )
            checkpointer = await checkpointer_cm.__aenter__()
            from app.agents.bind_tools import bind_dev_c_tools

            bind_dev_c_tools(
                tool_registry, session_factory=session_factory, checkpointer=checkpointer
            )
        except Exception as exc:
            # 工具绑定/checkpointer 不可用不阻塞对话：用药确认由 Agent 的 not_found 话术兜住
            logger.error(
                "dev_c_tools_bind_failed",
                extra={"error_type": type(exc).__name__, "detail": str(exc)[:200]},
            )
            checkpointer_cm = None

    # ---- G1 升级口：分类器只给「疑似敏感」，不给急症类别 ----
    # `escalate_safety_event()` 要求 category ∈ {chest_pain, breathing, fall, stroke, self_harm}
    # 才能建红色事件+工单（见 safety_service._ORDER_TYPE_BY_CATEGORY）。分类器给不出这个分类，
    # 在 Router 里编一个就等于伪造病历 → 本口只落 audit_logs + 告警日志，不建红色事件。
    # 待办（已登记）：要真正建单，需 C 线给出「疑似急症」的类别口径（J4/J6 联调项）。
    async def _escalate_system_one_signal(
        *,
        elder_id: str | None,
        thread_id: str,
        probability: float | None,
        reason: str,
    ) -> bool:
        logger.warning(
            "system_one_sensitive_signal",
            extra={
                "trace_id": get_trace_id() or "-",
                "thread_id": thread_id,
                "elder_id": elder_id,
                "probability": probability,
                "reason": reason,
            },
        )
        if elder_id is None:
            return False
        try:
            from app.services.audit_log_service import record_audit

            async with session_factory() as session:
                await record_audit(
                    session,
                    actor_type="system",
                    actor_id=None,
                    action="system_one_sensitive_signal",
                    resource_type="conversation_thread",
                    resource_id=None,
                    detail={
                        "thread_id": thread_id,
                        "sensitive_probability": probability,
                        "reason": reason,
                        "model_version": settings.system_one_model_version,
                    },
                )
                await session.commit()
        except Exception as exc:
            logger.error(
                "system_one_signal_audit_failed",
                extra={"thread_id": thread_id, "error_type": type(exc).__name__},
            )
            return False
        return True

    async def _was_clarified(thread_id: str) -> bool:
        """同 thread_id 上一轮是否已澄清（低置信且未升级 → 是）。失败按「未澄清」保守处理。"""
        from app.services.decision_log_service import was_clarified as _wc

        async with session_factory() as session:
            return await _wc(
                session,
                thread_id,
                confidence_threshold=settings.system_one_confidence_threshold,
            )

    # ---- L2 客户端 + Router（规则 → System One → Agent） ----
    system_one_client = SystemOneClient(settings)
    router = Router(
        system_one=system_one_client,
        escalate=_escalate_system_one_signal,
        was_clarified=_was_clarified,
        # 直连工具与 Agent 共用**同一个**执行器实例（G2：同一工具不得有两套实现）
        tool_executor_factory=lambda: session_tool_executor,
        confidence_threshold=settings.system_one_confidence_threshold,
        enabled=settings.system_one_enabled,
    )

    # ---------- Day 6 S3 对齐：N14 统一出口真身装配（N05 真状态中心 + N06 真构建器） ----------
    # N05：真 StateCenter（Redis 存储 + Lua CAS；构造不持锁，Redis 不可用时读写自动
    # 降级进程内存——闲聊可用性优先）→ SessionCoordinator 按 thread 串行化提交 →
    # CoordinatorStateCenterAdapter 贴到七步契约（显式 CAS，不走 submit，见适配器 docstring）。
    state_center = StateCenter(redis_client, key_prefix=settings.redis_key_prefix)
    coordinator = SessionCoordinator(state_center)

    async def _agent_event_sink(event_type: str, payload: Mapping[str, object]) -> None:
        # CompanionAgent 工具事件 → 统一出口缓冲回灌（turn_service 在下方构造，
        # 闭包按调用时解析；Agent 只在轮次内调用，不存在未定义窗口）
        await turn_service.agent_event_sink(event_type, payload)

    agent = CompanionAgent(
        llm=llm_service,
        # AGENTS §7 硬性数值：单轮 ≤8 次工具调用；同一 Tool 1 分钟内第 3 次熔断
        guardrails=Guardrails(max_tool_calls=8, repeat_window_s=60, repeat_threshold=3),
        # N26 收口：weather MCP 客户端进执行器（懒连接，server 未起时按
        # mcp_client 四分类降级「查不到」，不阻塞启动）
        tool_executor=session_tool_executor,
        # N26 装配收口：把工具清单注册进 ReAct 协议（缺省 () 时模型根本不知道
        # 有 get_weather 可用，天气问题只能靠常识编答 → 实时天气不可用）
        tools=DEFAULT_TOOLS,
        on_event=_agent_event_sink,
    )
    # Day 6 S4：ResponseService 提升为 lifespan 局部变量——TurnService 持有做第 7 步
    # 分段下发，装配层另持引用注册 segment_ack 回调（同一实例，ack 才能命中会话 epoch）
    response_service = ResponseService(
        deliver=signaling.deliver,
        events=HistoryEventSink(session_factory),
        # Day 7 B1（P1-1/P1-3）：回合终态结算——commit_response 投影 + finish_turn 终态
        history_store=history_service.HistorySettlementAdapter(session_factory),
        # Day 7 B1（P2-9）：epoch/水位回写 Redis（重启恢复；单进程范围，多 worker 登记 Day 9）
        persistence=RedisEpochPersistence(redis_client),
        # Day 7 B1↔B2（裁决 O-B2）：终态结算后调度记忆提取（真实现；Step 7 统一验收落库）
        memory_port=_MemoryExtractionAdapter(),
    )
    turn_service = TurnService(
        session_factory=session_factory,
        agent=agent,
        response_stream=response_service,
        state_center=CoordinatorStateCenterAdapter(
            coordinator=coordinator, state_center=state_center
        ),
        context_builder=ContextBuilder(),
        # Day 7 A3：三级路由 + 决策日志版本位（rule 行会再拼 ruleset:r1）
        router=router,
        model_version=settings.system_one_model_version,
        confidence_threshold=settings.system_one_confidence_threshold,
    )

    orchestrator = DialogueOrchestrator(
        service=dialogue_service,
        identity_thread_resolver=_resolve_identity,
        turn_service=turn_service,
        safety_escalator=_escalate_safety,
    )

    async def _on_dialogue_final(
        device_id: str,
        turn_id: str,
        trace_id: str,
        text: str,
        audio_bytes: bytes | None,
    ) -> None:
        # ---------- Day 5 B-2：LLM 并行 watchdog（覆盖 FINAL → reply 总耗时） ----------
        # 语义：覆盖 ASR FINAL 到 reply 返回总耗时（含身份校验 + Redis history + LLM + Redis 写回）。
        # 任何环节慢（含非 LLM 环节）都适用"我正在想"话术，用户感知正确。
        _llm_state: dict[str, bool] = {"slow_degraded": False}
        _llm_watchdog: asyncio.Task[None] | None = None
        _settings = get_settings()

        async def _llm_slow_watchdog() -> None:
            from app.audio.signaling import _ERROR_TALKBACK

            await asyncio.sleep(_settings.degrade_llm_slow_ms / 1000)
            if not _llm_state["slow_degraded"]:
                _llm_state["slow_degraded"] = True
                signaling.deliver(
                    device_id,
                    {
                        "type": "error",
                        "code": "llm_slow",
                        "message": _ERROR_TALKBACK["llm_slow"],
                    },
                )

        _llm_watchdog = asyncio.create_task(_llm_slow_watchdog())

        try:
            try:
                reply = await orchestrator.handle_asr_final(
                    device_id=device_id,
                    trace_id=trace_id,
                    turn_id=turn_id,
                    text=text,
                    audio_bytes=audio_bytes,
                )
            finally:
                # ★ try/finally 强制清理
                if _llm_watchdog is not None and not _llm_watchdog.done():
                    _llm_watchdog.cancel()
                    with suppress(asyncio.CancelledError):
                        await _llm_watchdog
        except IdentityConfirmationRequired as exc:
            # A1 reason=no_audio 等不在 C5 两值内时，线上统一按 low_confidence 询问；
            # 原始 reason 只进服务端日志，不改变 C5 契约。
            ask_reason: Literal["low_confidence", "vp_unavailable"] = (
                "vp_unavailable" if exc.reason == "vp_unavailable" else "low_confidence"
            )
            logger.info(
                "identity_confirmation_requested",
                extra={
                    "trace_id": trace_id,
                    "device_id": device_id,
                    "turn_id": turn_id,
                    "reason": ask_reason,
                    "original_reason": exc.reason,
                },
            )
            await signaling.request_identity_confirmation(device_id, reason=ask_reason)
            return
        except asyncio.CancelledError:
            # 上层 session 清理（旧 WS superseded 等）对 shield 内部协程的间接取消：
            # 连接已关，无需向前端 deliver，静默 return 即可
            logger.info(
                "dialogue_final_cancelled",
                extra={"trace_id": trace_id, "device_id": device_id, "turn_id": turn_id},
            )
            return
        except Exception:
            logger.exception(
                "dialogue_final_failed",
                extra={"trace_id": trace_id, "device_id": device_id, "turn_id": turn_id},
            )
            # C-1：失败必须显性告知（C-D4 方案 A：internal 为失败终态），
            # 否则 asr_final 已置前端「正在想」，静默 return 会让状态条永久卡住
            signaling.deliver(
                device_id,
                {"type": "error", "code": "internal", "message": _ERROR_TALKBACK["internal"]},
            )
            return

        if reply is not None:
            # ---------- Day 5 B-2：TTS 并行 watchdog（方案2：先合成再下发） ----------
            # ★ 顺序反转：watchdog + synthesize → 成功后才 deliver tts_text
            # watchdog 只在"文字还没下发"期间允许发 slow notice（用户还在等，合理）；
            # 合成完成后文字立即下发，watchdog cancel，不会再触发 slow 提示
            # TTS 失败时仍需下发文字保持 C1 降级边界（大字幕不丢）
            _tts_state: dict[str, bool] = {"slow_degraded": False}
            _tts_watchdog: asyncio.Task[None] | None = None
            _settings = get_settings()

            async def _tts_slow_watchdog() -> None:
                from app.audio.signaling import _ERROR_TALKBACK

                await asyncio.sleep(_settings.degrade_tts_slow_ms / 1000)
                # N14 分支：文字已随 tts_text v2 分段先行到达（第 7 步），
                # 「文字尚未下发」前提不成立 → slow notice 语义不适用
                if reply.response_id is not None:
                    return
                # 此时文字尚未下发 → 用户还在等待回复 → slow notice 语义成立
                if not _tts_state["slow_degraded"]:
                    _tts_state["slow_degraded"] = True
                    signaling.deliver(
                        device_id,
                        {
                            "type": "error",
                            "code": "tts_slow",
                            "message": _ERROR_TALKBACK["tts_slow"],
                        },
                    )

            _tts_watchdog = asyncio.create_task(_tts_slow_watchdog())
            pcm: bytes | None = None
            synthesize_ok = False

            try:
                pcm = await tts_provider.synthesize(reply.text)
                synthesize_ok = True
            except TTSUnavailable as exc:
                # C1 降级边界：保留大字幕，不因 TTS 失败静默
                logger.warning(
                    "tts_unavailable",
                    extra={
                        "trace_id": trace_id,
                        "device_id": device_id,
                        "turn_id": turn_id,
                        "error_type": type(exc).__name__,
                    },
                )
            except Exception as exc:
                logger.error(
                    "tts_failed",
                    extra={
                        "trace_id": trace_id,
                        "device_id": device_id,
                        "turn_id": turn_id,
                        "error_type": type(exc).__name__,
                    },
                )
            finally:
                # ★ try/finally 强制清理 watchdog
                if _tts_watchdog is not None and not _tts_watchdog.done():
                    _tts_watchdog.cancel()
                    with suppress(asyncio.CancelledError):
                        await _tts_watchdog

            # ★ 合成（无论成败）完成后再下发文字（顺序反转的核心）
            # N14 统一出口分支（response_id 非 None）：第 7 步 ResponseService.start
            # 已按 tts_text v2 分段下发，此处不再整段重发（防双发/重复播放）；
            # TTS 音频仍走 Day 5 整段合成入队，文字分段与音频互不影响。
            if reply.response_id is not None:
                logger.info(
                    "dialogue_reply_already_streamed",
                    extra={
                        "trace_id": trace_id,
                        "device_id": device_id,
                        "turn_id": turn_id,
                        "response_id": reply.response_id,
                    },
                )
            else:
                delivered = signaling.deliver(
                    device_id,
                    {"type": "tts_text", "text": reply.text, "degraded": reply.degraded},
                )
                logger.info(
                    "dialogue_reply_delivered",
                    extra={
                        "trace_id": trace_id,
                        "device_id": device_id,
                        "turn_id": turn_id,
                        "degraded": reply.degraded,
                        "delivered": delivered,
                        "text_len": len(reply.text),
                    },
                )
                if not delivered:
                    # WS 已断：正文都没送到，不再 enqueue 音频
                    logger.warning(
                        "dialogue_text_not_delivered",
                        extra={"trace_id": trace_id, "device_id": device_id, "turn_id": turn_id},
                    )
                    return

            # 只有合成成功时才 enqueue 音频
            if synthesize_ok and pcm is not None:
                audio_enqueued = await enqueue_tts_for_device(device_id, turn_id, pcm)
                logger.info(
                    "dialogue_audio_enqueued",
                    extra={
                        "trace_id": trace_id,
                        "device_id": device_id,
                        "turn_id": turn_id,
                        "audio_enqueued": audio_enqueued,
                        "pcm_bytes": len(pcm),
                    },
                )

    async def _on_segment_ack(
        conn: signaling.ConnectionContext, message: Mapping[str, Any]
    ) -> None:
        """C→S segment_ack：解包 device_id 后转交 ResponseService 四步校验（N07 契约）。

        形状校验已在 signaling 层完成；此处只做连接身份解包，四步契约校验
        （含 rejected_reason 四枚举与单调水位）全部在 response_service。
        """
        if conn.device_id is None:
            logger.warning(
                "segment_ack_without_device",
                extra={"trace_id": conn.trace_id, "response_id": str(message.get("response_id"))},
            )
            return
        await response_service.handle_segment_ack(device_id=conn.device_id, message=message)

    async def _on_interrupt(conn: signaling.ConnectionContext, message: Mapping[str, Any]) -> None:
        """C→S interrupt（day07/README §5.4 冻结口径）：解包 device_id 后转交打断五步。

        设备/会话归属校验在 ResponseService.interrupt（response 必须属于本设备，
        否则按 unknown_response no-op、不落库）；形状校验已在 signaling 层完成。
        """
        if conn.device_id is None:
            logger.warning(
                "interrupt_without_device",
                extra={"trace_id": conn.trace_id, "response_id": str(message.get("response_id"))},
            )
            return
        logger.info(
            "interrupt_requested",
            extra={
                "trace_id": conn.trace_id,
                "device_id": conn.device_id,
                "response_id": str(message["response_id"]),
                "reason": str(message.get("reason", "-")),
            },
        )
        await response_service.interrupt(str(message["response_id"]), device_id=conn.device_id)

    async def _on_touch_played(
        conn: signaling.ConnectionContext, message: Mapping[str, Any]
    ) -> None:
        """C→S touch_played（day07/contracts.md §2）：pad 播完触达语音 → 幂等落库。

        **独立短事务**：绝不复用 WS 长连接的请求会话写药单行——子图事务窗口若还
        持有同一行，短事务才不会与之互锁（Day 6 死锁教训的同款应用）。回执是尽力
        而为的上报：落库失败只记日志，不回 error、不断连接。
        """
        if conn.device_id is None:
            logger.warning(
                "touch_played_without_device",
                extra={"trace_id": conn.trace_id, "touch_id": str(message.get("touch_id"))},
            )
            return
        try:
            event_id = UUID(str(message["event_id"]))
            attempt_no = int(message["attempt_no"])
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "touch_played_invalid_coordinates",
                extra={"trace_id": conn.trace_id, "touch_id": str(message.get("touch_id"))},
            )
            return
        try:
            async with session_factory() as session:
                upgraded = await medication_service.mark_attempt_played(
                    session, event_id=event_id, attempt_no=attempt_no
                )
                await session.commit()
        except Exception as exc:
            logger.error(
                "touch_played_persist_failed",
                extra={"trace_id": conn.trace_id, "error_type": type(exc).__name__},
            )
            return
        logger.info(
            "touch_played_recorded",
            extra={
                "trace_id": conn.trace_id,
                "event_id": str(event_id),
                "attempt_no": attempt_no,
                "upgraded": upgraded,
            },
        )

    signaling.set_dialogue_final_handler(_on_dialogue_final)
    signaling.set_identity_confirm_handler(_on_identity_confirm)
    signaling.set_identity_timeout_handler(_on_identity_timeout)
    signaling.set_segment_ack_handler(_on_segment_ack)
    signaling.set_interrupt_handler(_on_interrupt)
    signaling.set_touch_played_handler(_on_touch_played)
    app.state.redis = redis_client
    app.state.llm_service = llm_service
    app.state.tts_provider = tts_provider
    app.state.dialogue_service = dialogue_service
    app.state.dialogue_orchestrator = orchestrator
    app.state.turn_service = turn_service  # Day 6 S3 对齐：N14 统一出口真身（检视/联调用）

    # ---- System One 预热：只确认 sidecar 可达；不可达只打日志（路由走 fallback，不阻塞启动） ----
    if settings.system_one_warmup and settings.system_one_enabled:
        warm = await system_one_client.decide_fast("今天天气怎么样", trace_id=get_trace_id())
        logger.info(
            "system_one_warmup",
            extra={"degraded": warm.degraded, "intent": warm.intent, "provider": "sidecar"},
        )

    # Day 7 C2：用药到点调度接线——install_scheduler 构造 SchedulerRuntime 并存到
    # app.state.medication_scheduler；FastAPI 0.110+ 的 lifespan 与旧式 @app.on_event
    # 互斥，所以 start/stop 必须手动管理（不在装饰器里）。
    # Day 7 A3（合并上游后）：补 checkpointer 两参——用药确认子图与 Agent 必须同一 DSN、
    # 同一 schema（J6 联调项），不传则各建一套连接池。
    medication_scheduler = install_scheduler(
        app,
        session_factory=session_factory,
        checkpointer_dsn=settings.langgraph_checkpoint_database_url,
        checkpointer_schema=settings.langgraph_checkpoint_schema,
        timezone_name=settings.scheduler_timezone,
    )
    await medication_scheduler.start()

    logger.info("application starting", extra={"app_env": settings.app_env})
    try:
        yield
    finally:
        await medication_scheduler.stop()
        signaling.set_dialogue_final_handler(None)
        signaling.set_identity_confirm_handler(None)
        signaling.set_identity_timeout_handler(None)
        signaling.set_segment_ack_handler(None)
        signaling.set_interrupt_handler(None)
        signaling.set_touch_played_handler(None)
        if checkpointer_cm is not None:
            try:
                await checkpointer_cm.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning(
                    "checkpointer_close_failed", extra={"error_type": type(exc).__name__}
                )
        await system_one_client.aclose()
        # Day 5：metrics 批量缓冲冲刷（裁决 B6/D2）——先停 worker 再 drain，避免丢批
        await get_metrics_recorder().stop()
        await llm_service.aclose()
        # redis-py 的 asyncio 关闭方法；当前 stubs 尚未同步 aclose
        await redis_client.aclose()  # type: ignore[attr-defined]
        logger.info("application stopped")


def handle_request_validation(req: Request, exc: RequestValidationError) -> JSONResponse:
    """a0-F-01：RequestValidationError 统一错误体（模块级，便于测试 fixture 复用同一实现）。

    elders/devices 走 ContractAPIRoute 已在路由层分流（格式错→400、字段错→422），
    异常到不了这里；本 handler 兜住 audio/auth 等默认路由类的同款异常，避免
    FastAPI 默认裸 422（无 code/trace_id）。§7 分类逻辑单一来源在
    app/api/elders.py::_is_request_format_error，两边不得分叉。
    """
    if _is_request_format_error(exc):
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                detail="malformed request",
                code="BAD_REQUEST",
                trace_id=get_trace_id() or "-",
            ).model_dump(),
        )
    # detail 只取 loc + msg，不带 Pydantic 原始数组与 ctx（不回显内部细节）
    detail = "; ".join(
        "{}: {}".format(".".join(str(p) for p in error.get("loc") or ()), error.get("msg", ""))
        for error in exc.errors()
    )
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            detail=detail or "request validation failed",
            code="VALIDATION_ERROR",
            trace_id=get_trace_id() or "-",
        ).model_dump(),
    )


def handle_http_exception(req: Request, exc: HTTPException) -> JSONResponse:
    """兜底：业务代码 raise HTTPException 也包成 ErrorResponse（契约 §7）。

    模块级（同 handle_request_validation），便于测试 fixture 复用同一实现。
    """
    code_map = {
        400: "BAD_REQUEST",
        401: "TOKEN_INVALID",
        403: "ROLE_FORBIDDEN",
        404: "NOT_FOUND",
        413: "PAYLOAD_TOO_LARGE",
        422: "VALIDATION_ERROR",
        503: "SERVICE_UNAVAILABLE",
    }
    code = code_map.get(exc.status_code, f"HTTP_{exc.status_code}")
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            detail=str(exc.detail),
            code=code,
            trace_id=get_trace_id() or "-",
        ).model_dump(),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂：便于测试注入不同配置（`create_app(Settings(app_env="test"))`）。"""
    settings = settings or get_settings()
    configure_logging(settings.log_level, service=settings.otel_service_name)

    application = FastAPI(
        title=f"{settings.app_name} backend",
        version=__version__,
        description="康伴（SilverMate）3D 数字人陪伴系统后端",
        lifespan=lifespan,
    )
    application.state.settings = settings

    # 中间件：后添加者在外层，因此 CORS 包住 TraceId，异常响应也带链路头
    application.add_middleware(TraceIdMiddleware)
    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[TRACE_HEADER],
        )

    # REST 路由统一从 `app/api/__init__.py` 的注册表挂载（T0）：
    # 新增路由只需在那张表里追加一行，不再改动本文件的公共代码块。
    for _router, _prefix in ROUTERS:
        application.include_router(_router, prefix=_prefix)
    application.include_router(signaling.router)  # /ws/signal 无 /api 前缀（契约裁决 A-03）
    install_signaling_handler()  # A3 注册 offer/ice/asr_start/asr_end 上行处理器

    # TTS 落盘目录挂载（dev 兜底）：audio.py 把 TTS WAV 存 `<cwd>/static/mock/` 并返回
    # `/static/mock/*.wav`，nginx（Day 10）上线前由这里保证 URL 可取；
    # check_dir=False——目录由 utterance 写入时惰性创建，启动时不存在不报错。
    application.mount(
        "/static",
        StaticFiles(directory=os.path.join(os.getcwd(), "static"), check_dir=False),
        name="static",
    )

    # ---------- 全局 exception handler（契约 §7） ----------
    # auth_service 层异常（双重保险：auth.py 路由已捕获，这里兜底）
    @application.exception_handler(AuthInvalidError)
    async def _handle_auth_invalid(req: Request, exc: AuthInvalidError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail),
                code=exc.kind or "TOKEN_INVALID",
                trace_id=get_trace_id() or "-",
            ).model_dump(),
        )

    @application.exception_handler(AuthExpiredError)
    async def _handle_auth_expired(req: Request, exc: AuthExpiredError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail), code="TOKEN_EXPIRED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(AuthRevokedError)
    async def _handle_auth_revoked(req: Request, exc: AuthRevokedError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc.detail), code="TOKEN_REVOKED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(AuthConflictError)
    async def _handle_auth_conflict(req: Request, exc: AuthConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                detail=str(exc.detail), code="CONFLICT", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    # task-4 auth 层异常（依赖注入阶段抛出，auth_service 还没机会参与）
    @application.exception_handler(CredentialInvalidError)
    async def _handle_cred_invalid(req: Request, exc: CredentialInvalidError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc), code="TOKEN_INVALID", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(CredentialMissingError)
    async def _handle_cred_missing(req: Request, exc: CredentialMissingError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc), code="TOKEN_INVALID", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(CredentialExpiredError)
    async def _handle_cred_expired(req: Request, exc: CredentialExpiredError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc), code="TOKEN_EXPIRED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(CredentialRevokedError)
    async def _handle_cred_revoked(req: Request, exc: CredentialRevokedError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail=str(exc), code="TOKEN_REVOKED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    @application.exception_handler(TokenExpiredError)
    async def _handle_token_expired(req: Request, exc: TokenExpiredError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                detail="token has expired", code="TOKEN_EXPIRED", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    # 兜底：业务代码 raise HTTPException 也包成 ErrorResponse（契约 §7）
    application.add_exception_handler(HTTPException, handle_http_exception)

    # a0-F-01：统一 422 错误体（实现见模块级 handle_request_validation）
    application.add_exception_handler(RequestValidationError, handle_request_validation)

    # 未预期异常 → 500，不回显内部细节（AGENTS §8 失败分类）
    @application.exception_handler(Exception)
    async def _handle_unknown(req: Request, exc: Exception) -> JSONResponse:
        logging.getLogger("app.main").exception("unhandled exception")
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                detail="内部错误", code="INTERNAL_ERROR", trace_id=get_trace_id() or "-"
            ).model_dump(),
        )

    return application


app = create_app()
