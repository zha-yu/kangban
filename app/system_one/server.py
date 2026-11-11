"""System One sidecar（Day 7 · A2）：`GET /health` + `POST /v1/decide`。

为什么是**独立进程**：模型权重 615 MB、`torch` 一套依赖，塞进主服务会让「语音链路」和
「分类推理」共用同一个进程的 CPU 与 GIL，且主 `.venv` 全程无 torch（AGENTS §1）。
sidecar 只做一件事——收 state + questions，回四信号；**不连业务库、不写 `decision_logs`、
不读 runtime_context**（那些是 Router/turn_service 的职责，混进来就等于把 sidecar 变成第二写口）。

启动（**必须用 jev 环境**，主 `.venv` 没有 torch）：
```bash
PYTHONPATH=D:/Desktop/kangban D:/Users/Alienware/anaconda3/envs/jev/python.exe \
  -m uvicorn app.system_one.server:app --port 7001
```

预算：客户端 200ms（PRD §5.3）→ sidecar 内部 180ms（= 200 − 20 缓冲，实测单问 p95 167ms）。
超时回 503 + `{"status":"timeout"}`，**绝不返回半成品结果**——半成品会让下游同时走降级又执行工具。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.system_one.backend import (
    WARMUP_TEXT,
    DecisionBackend,
    ModelNotConfigured,
    build_backend,
    map_answers,
    text_fingerprint,
)
from app.system_one.questions import build_fast_questions
from app.system_one.schemas import SystemOneRequest, SystemOneResponse

logger = logging.getLogger("app.system_one.server")

#: sidecar 内部推理预算（秒）。**195ms**（2026-11-10 主理人裁决：在 190–195 区间内放宽，
#: 仍在 PRD §5.3 的 200ms 之内）。
#:
#: ⚠️ 它是**软预算**：判定看真实耗时（`elapsed > INTERNAL_BUDGET_S`），不靠 asyncio 定时器。
#: 原因（2026-11-10 实测）：Windows 上 `asyncio.wait_for(0.195)` 的定时器会**提前约 15ms**
#: 触发——175ms 的任务有 4/6 次被判超时（177ms 就抛 TimeoutError）。用定时器承担 200ms
#: 预算的精度职责，等于把预算悄悄缩到 ~180ms，表现为「推理明明 180ms 完成却 503」。
#: 现在定时器只做 `HARD_DEADLINE_S`（防线程卡死），预算判定改看墙钟。
#:
#: 为什么从 180 改成 195：实测单问纯前向 p50 161 / p95 167ms，加上服务层开销（JSON 解析、
#: Pydantic 校验、`to_thread` 调度、两条日志、响应序列化）后端到端 p50 188 / max 193ms——
#: 180ms 正好压在中位数上，HTTP 通道稳定 503。放宽到 195 后仍留 5ms 给网络与客户端序列化，
#: **没有动 `system_one_timeout_ms`（客户端仍 200ms）**，也没有动 PRD 的 200ms 口径。
#:
#: 注意 5ms 缓冲很薄：机器被别的进程抢核时仍可能撞客户端 200ms——那属于降级路径的正常触发
#: （`fallback` + Agent 兜底），不是本常量该继续放大的理由。
INTERNAL_BUDGET_S: Final[float] = 0.195

#: 硬截止（秒）：只防「推理线程永远不返回」，**不承担 200ms 预算的精度职责**（见上）。
#: 取 1.0s：正常前向 160~190ms，超过 1s 必然是异常（死锁/内存抖动），此时如实 503。
HARD_DEADLINE_S: Final[float] = 1.0

#: 权重加载上限（秒）。615MB 权重 + 建图实测 40s，**加载与推理是两件事**，
#: 不能用 180ms 去卡加载——卡了就会取消加载、留下半截状态（曾在实测中触发
#: 「两次加载、两个模型抢线程、单问掉到 200ms+」，见 A2 基线报告）。
LOAD_TIMEOUT_S: Final[float] = 180.0
#: 预热那一次前向的上限：首次前向含 kernel 预热，比稳态慢，给 2s 足够又不无限等。
WARMUP_INFER_TIMEOUT_S: Final[float] = 2.0


def _semaphore_for(app: FastAPI) -> asyncio.Semaphore:
    semaphore = getattr(app.state, "system_one_semaphore", None)
    if semaphore is None:  # 未走 lifespan（测试直接调路由）时的兜底
        semaphore = asyncio.Semaphore(get_settings().system_one_max_concurrency)
        app.state.system_one_semaphore = semaphore
    return semaphore


@asynccontextmanager
async def lifespan(app: FastAPI):
    """预热 1 条：首句不背「加载 + 首次前向」的冷启动成本。

    预热失败**只影响 `/health`**（它会把 model_loaded 报 false / 503），不让进程崩——
    业务侧本来就有降级路径，sidecar 崩掉反而让 503 变成 connection refused。
    """
    settings = get_settings()
    app.state.system_one_semaphore = asyncio.Semaphore(max(1, settings.system_one_max_concurrency))
    backend: DecisionBackend = getattr(app.state, "system_one_backend", None) or build_backend(
        settings
    )
    app.state.system_one_backend = backend

    if settings.system_one_warmup:
        started = time.perf_counter()
        load_ms = 0.0
        try:
            ensure_loaded = getattr(backend, "ensure_loaded", None)
            if callable(ensure_loaded):
                load_started = time.perf_counter()
                await asyncio.wait_for(ensure_loaded(), timeout=LOAD_TIMEOUT_S)
                load_ms = (time.perf_counter() - load_started) * 1000
            await asyncio.wait_for(
                backend.score(state=WARMUP_TEXT, questions=build_fast_questions()),
                timeout=WARMUP_INFER_TIMEOUT_S,
            )
        except ModelNotConfigured as exc:
            logger.warning("system_one_warmup_model_missing reason=%s", exc)
        except Exception as exc:  # 预热失败不影响服务可用性，只要 /health 说实话
            logger.warning("system_one_warmup_failed error=%s", f"{type(exc).__name__}: {exc}")
        else:
            logger.info(
                "system_one_warmup_ok load_ms=%.0f total_ms=%.0f",
                load_ms,
                (time.perf_counter() - started) * 1000,
            )
    yield


app = FastAPI(title="SilverMate System One sidecar", version="1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    """模型可用 → 200 + `model_loaded=true`；缺失/加载失败 → **503 + `model_missing`**。

    禁止 200 + `model_loaded=false`：那等于假 ok，运维看不出分类器其实没上线。
    """
    settings = get_settings()
    backend: DecisionBackend = getattr(app.state, "system_one_backend", None) or build_backend(
        settings
    )
    info: dict[str, Any] = await backend.health()
    body = {
        "status": "ok" if info.get("model_loaded") else "model_missing",
        "model_id": info.get("model_id", settings.system_one_model_id),
        "model_version": info.get("model_version", settings.system_one_model_version),
        "model_loaded": bool(info.get("model_loaded")),
        "device": info.get("device", settings.system_one_device),
        "provider": info.get("provider", settings.system_one_provider),
    }
    if "model_missing_reason" in info:
        body["reason"] = info["model_missing_reason"]
    return JSONResponse(status_code=200 if body["model_loaded"] else 503, content=body)


@app.post("/v1/decide", response_model=SystemOneResponse, response_model_exclude_none=False)
async def decide(request: SystemOneRequest) -> Any:
    """一次决策：校验 → 限并发 → 195ms 预算 → 映射 → 返回。

    失败一律 503 + 明确 status（`timeout` / `inference_failed`），**不返回半成品**。
    """
    settings = get_settings()
    backend: DecisionBackend = getattr(app.state, "system_one_backend", None) or build_backend(
        settings
    )
    started = time.perf_counter()
    text_len, digest = text_fingerprint(_state_text(request.state))

    # ---- 预算与并发：许可必须**跟着线程走**，不能跟着 await 走 ----
    # 坑（实测踩到）：`wait_for` 超时只取消"等待"，`to_thread` 里的前向仍在算。若此时就
    # 释放 Semaphore，下一个请求立刻再起一次前向 → 两路并行抢同一批 torch 线程 → 各自更慢
    # → 更多超时 → 雪崩（实测从 5/6 成功一路掉到 0/20 全 503）。
    # 正确做法：把 score 包成 Task 用 shield 等；超时只放弃等待，**许可等 Task 真正结束
    # 才由回调释放**，于是"同时在算的前向"恒等于 max_concurrency（本项目为 1）。
    semaphore = _semaphore_for(app)
    await semaphore.acquire()
    released = False

    def _release(fut: asyncio.Future[Any]) -> None:
        nonlocal released
        if released:  # 双保险：正常路径已释放过就不再重复 release（否则计数会超过上限）
            return
        released = True
        if not fut.cancelled():
            fut.exception()  # 消费异常，避免 "Task exception was never retrieved"
        semaphore.release()

    inference = asyncio.create_task(backend.score(state=request.state, questions=request.questions))
    inference.add_done_callback(_release)
    try:
        answers = await asyncio.wait_for(asyncio.shield(inference), timeout=HARD_DEADLINE_S)
    except TimeoutError:
        # 坑：`TimeoutError` 有两个来源，混在一起会误导排障。
        #   1) 预算到期（wait_for 自己抛的）—— 真·慢；
        #   2) **推理任务自己抛了 TimeoutError**（`concurrent.futures.TimeoutError`
        #      在 3.11+ 就是内置 `TimeoutError` 的别名，模型侧线程池/取数超时会抛它）。
        # 第 2 种本质是「推理失败」，若记成 timeout，运维会去调预算——方向就错了。
        # 实测症状：客户端 178ms 收到 503 `timeout`，而预算写的是 195ms，凭空少了 17ms。
        task_error = None
        if inference.done() and not inference.cancelled():
            task_error = inference.exception()
        if task_error is not None:
            logger.error(
                "system_one_decide_failed trace_id=%s text_len=%d text_sha8=%s error=%s",
                request.trace_id or "-",
                text_len,
                digest,
                f"{type(task_error).__name__}: {task_error}",
            )
            return JSONResponse(status_code=503, content={"status": "inference_failed"})
        logger.warning(
            "system_one_decide_timeout trace_id=%s text_len=%d text_sha8=%s "
            "elapsed_ms=%d budget_ms=%d",
            request.trace_id or "-",
            text_len,
            digest,
            int((time.perf_counter() - started) * 1000),
            int(INTERNAL_BUDGET_S * 1000),
        )
        return JSONResponse(status_code=503, content={"status": "timeout"})
    except Exception as exc:  # 推理异常：如实 503，不编造答案
        logger.error(
            "system_one_decide_failed trace_id=%s text_len=%d text_sha8=%s error=%s",
            request.trace_id or "-",
            text_len,
            digest,
            f"{type(exc).__name__}: {exc}",
        )
        return JSONResponse(status_code=503, content={"status": "inference_failed"})

    latency_ms = int((time.perf_counter() - started) * 1000)
    if latency_ms > INTERNAL_BUDGET_S * 1000:
        # 结果出来了但已超预算：客户端 200ms 早已放弃，如实 503，不装作成功
        logger.warning(
            "system_one_decide_timeout trace_id=%s text_len=%d text_sha8=%s "
            "elapsed_ms=%d budget_ms=%d overtime=true",
            request.trace_id or "-",
            text_len,
            digest,
            latency_ms,
            int(INTERNAL_BUDGET_S * 1000),
        )
        return JSONResponse(status_code=503, content={"status": "timeout"})

    result = map_answers(
        answers,
        model_version=request.model_version or settings.system_one_model_version,
        latency_ms=latency_ms,
        asked=tuple(request.questions),
    )
    logger.info(
        "system_one_decide_ok trace_id=%s text_len=%d text_sha8=%s intent=%s conf=%.3f ms=%d",
        request.trace_id or "-",
        text_len,
        digest,
        result.intent,
        result.confidence,
        latency_ms,
    )
    return SystemOneResponse(request_id=request.request_id, result=result)


def _state_text(state: Any) -> str:
    if isinstance(state, str):
        return state
    return str(state)


__all__ = [
    "HARD_DEADLINE_S",
    "INTERNAL_BUDGET_S",
    "LOAD_TIMEOUT_S",
    "WARMUP_INFER_TIMEOUT_S",
    "app",
    "decide",
    "health",
]
