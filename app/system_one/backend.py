"""System One 推理后端：`RLAgent` 封装 + Mock（Day 7 · A2）。

**这里只有一件事**：把上游 `rl_agent_api.RLAgent.system_one(state, questions)` 的原始
`answers` 转成业务视图 `SystemOneResult`。原始 JSON 的解析**只在本文件出现一次**，
Router / turn_service / 评测脚本一律只认 `SystemOneResult`——否则「降级怎么判」「confidence
是熵还是概率」这类口径会在四个地方各写一套，然后悄悄漂移。

三条来自实测的硬约束（`spikes/chinese_jev_hello/README.md` §4）：

1. `confidence` 是**候选分布熵归一化**后的值，不是 max prob——这里原样透传，**不重算**；
2. `noul` 题**不返回 confidence**，只返回 `noul`（true 概率），所以 `is_sensitive` 只能
   用阈值判（`SENSITIVE_THRESHOLD`），`sensitive_probability` 存原始概率留痕；
3. `act_probability` 恒为 1、无信号，**契约里不暴露**。

延迟与并发（同一份实测）：单问 p95 167ms、两问 285ms、四问 668ms → 快路径只问 intent。

**重依赖隔离**：`torch` / `rl_agent_api` 只在 `_load_sync()` 内部延迟导入。本模块可以在
项目主 `.venv`（无 torch）里被 import——`uv run pytest` 与主服务装配都不会触发 torch。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Final, Protocol

from app.core.config import Settings, get_settings
from app.system_one.questions import intent_of_key
from app.system_one.schemas import SystemOneResult

logger = logging.getLogger("app.system_one.backend")

#: `noul` 概率 ≥ 此值即判敏感。默认 0.80 是按讲义「原始概率偏自信」的提示取的保守值；
#: 50 例报告出来后按 bad case 标定（改这里必须同步 contracts-system-one.md §1.2）。
SENSITIVE_THRESHOLD: Final[float] = 0.80

#: 域外判据（`noul` 概率过半即域外）。域外只影响「是否澄清」，不参与安全判定。
OUT_OF_DOMAIN_THRESHOLD: Final[float] = 0.5

#: 预热用的固定短句：不读长者真实文本（sidecar 不碰业务数据，连日志都不带原文）。
WARMUP_TEXT: Final[str] = "今天天气怎么样"


class ModelNotConfigured(RuntimeError):
    """模型目录缺失/不可读。sidecar 必须据此回 503 + `model_missing`，不得假 ok。"""


class DecisionBackend(Protocol):
    """sidecar 对推理后端的最小要求：一次前向出 `answers`，外加一个健康自述。"""

    async def score(self, *, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        """返回 `RLAgent.system_one(...)["answers"]` 的原始字典。失败即抛异常。"""
        ...

    async def health(self) -> dict[str, Any]:
        """`{model_loaded, model_id, model_version, device, provider}`；不抛异常。"""
        ...


# ------------------------------------------------------------------ 结果映射（唯一一处）


def map_answers(
    answers: dict[str, Any],
    *,
    model_version: str,
    latency_ms: int,
    asked: tuple[str, ...] = (),
) -> SystemOneResult:
    """原始 `answers` → 业务视图。**缺题不抛异常**：shadow 只问一题时 intent 必缺。

    缺题的默认值不是「降级」：`degraded` 只表达「这次推理没跑成」，而缺题是「这轮本来
    就没问」。两者混用会让 shadow 的正常返回被打成降级，进而污染分层命中率。
    """
    intent_answer = answers.get("intent")
    intent = "other"
    confidence = 0.0
    if isinstance(intent_answer, dict):
        # 模型返回的是中文候选键；未知键落 other（`intent_of_key` 不抛异常）
        intent = intent_of_key(str(intent_answer.get("choice") or ""))
        confidence = _clamp01(intent_answer.get("confidence"))

    sensitive_probability: float | None = None
    is_sensitive = False
    sensitive_answer = answers.get("is_sensitive")
    if isinstance(sensitive_answer, dict) and "noul" in sensitive_answer:
        sensitive_probability = _clamp01(sensitive_answer.get("noul"))
        is_sensitive = sensitive_probability >= SENSITIVE_THRESHOLD

    privacy_scope = "none"
    privacy_answer = answers.get("privacy_scope")
    if isinstance(privacy_answer, dict):
        candidate = str(privacy_answer.get("choice") or "")
        if candidate in {"personal", "shared", "none"}:
            privacy_scope = candidate

    is_out_of_domain = False
    ood_answer = answers.get("is_out_of_domain")
    if isinstance(ood_answer, dict) and "noul" in ood_answer:
        is_out_of_domain = _clamp01(ood_answer.get("noul")) >= OUT_OF_DOMAIN_THRESHOLD

    return SystemOneResult(
        intent=intent,  # type: ignore[arg-type]  # intent_of_key 的返回值就是 Intent
        confidence=confidence,
        is_sensitive=is_sensitive,
        sensitive_probability=sensitive_probability,
        privacy_scope=privacy_scope,  # type: ignore[arg-type]  # 上面已收敛到三值
        is_out_of_domain=is_out_of_domain,
        degraded=False,
        latency_ms=latency_ms,
        model_version=model_version,
    )


def _clamp01(value: Any) -> float:
    """容错取值：模型偶尔给 None / 字符串 / NaN，一律收敛到 [0,1] 的 float。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return min(1.0, max(0.0, number))


def questions_to_payload(questions: dict[str, Any]) -> dict[str, Any]:
    """`QuestionSpec`（或已 dump 的 dict）→ `RLAgent` 能吃的纯 dict。

    允许两种入参：Pydantic 实例（sidecar 请求体）与普通 dict（测试/内部构造）。
    `criteria=None` 的题在 `rl_agent_api` 里会被当成缺字段，故这里剔除 None。
    """
    payload: dict[str, Any] = {}
    for name, spec in questions.items():
        data = spec.model_dump(exclude_none=True) if hasattr(spec, "model_dump") else dict(spec)
        payload[name] = {k: v for k, v in data.items() if v is not None}
    return payload


# ------------------------------------------------------------------ 真身：RLAgent


class LayaBackend:
    """`convaiinnovations/laya-multilingual` + 仓库自带 `rl_agent_api.RLAgent`。

    加载纪律：
    - **只加载一次**：`asyncio.Lock` 防并发重复加载（两个请求同时到达时，第二个等第一个）；
    - 加载在线程里做（`asyncio.to_thread`）：`RLAgent(...)` 是几十秒的同步 IO+建图，
      直接在事件循环里跑会把 `/health` 一起卡死；
    - 加载失败**缓存失败原因**（`_load_error`），`/health` 才是稳定的 503，而不是每次
      重试 10 秒后才 503。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._agent: Any | None = None
        self._load_error: str | None = None
        self._lock = asyncio.Lock()
        #: 加载任务只建一次：`asyncio.wait_for` 取消外层等待时，`to_thread` 里的加载
        #: 仍在跑；若不共享同一个任务，下一次请求会**再加载一份 615MB 权重**——
        #: 两个模型互相抢 8 条 torch 线程，单问就从 167ms 掉到 200ms+。
        self._load_task: asyncio.Task[Any] | None = None

    # -- 健康 ----------------------------------------------------------

    @property
    def model_version(self) -> str:
        return self._settings.system_one_model_version

    async def health(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "model_id": self._settings.system_one_model_id,
            "model_version": self.model_version,
            "device": self._settings.system_one_device,
            "provider": self._settings.system_one_provider,
            "model_loaded": self._agent is not None,
        }
        if self._agent is not None:
            return info
        try:
            await self._ensure_loaded()
        except ModelNotConfigured as exc:
            info["model_missing_reason"] = str(exc)
            return info
        except Exception as exc:  # 加载失败：如实写，不吞
            info["model_missing_reason"] = f"{type(exc).__name__}: {exc}"
            return info
        info["model_loaded"] = True
        return info

    # -- 加载 ----------------------------------------------------------

    async def ensure_loaded(self) -> Any:
        """公开的加载入口（lifespan 预热带独立超时用：加载 40s 与推理 180ms 不是一回事）。"""
        return await self._ensure_loaded()

    async def _ensure_loaded(self) -> Any:
        if self._agent is not None:
            return self._agent
        if self._load_error is not None:
            raise ModelNotConfigured(self._load_error)
        async with self._lock:
            if self._agent is not None:  # 双检：等锁期间可能已被别人加载
                return self._agent
            if self._load_task is None:
                self._load_task = asyncio.create_task(asyncio.to_thread(self._load_sync))
            task = self._load_task
        try:
            # shield：等待方被取消（warmup 超时 / 请求超时）**不取消加载本身**，
            # 下一次调用直接复用同一个任务，不会加载第二份权重。
            self._agent = await asyncio.shield(task)
        except ModelNotConfigured as exc:
            self._load_error = str(exc)
            self._load_task = None
            raise
        except Exception as exc:
            self._load_error = f"{type(exc).__name__}: {exc}"
            self._load_task = None
            raise
        logger.info(
            "system_one_model_loaded model_dir=%s device=%s",
            self._settings.system_one_model_path,
            self._settings.system_one_device,
        )
        return self._agent

    def _load_sync(self) -> Any:
        """**同步**加载（在线程里跑）。torch / rl_agent_api 只在这里延迟导入。"""
        model_dir = (self._settings.system_one_model_path or "").strip()
        if not model_dir:
            raise ModelNotConfigured("system_one_model_path 为空：sidecar 未指定权重目录")
        model_path = Path(model_dir)
        if not model_path.is_dir():
            raise ModelNotConfigured(f"模型目录不存在：{model_path}")

        # 代码目录 = 模型目录的父目录（D:\models\laya 里有 rl_agent_api.py / rl_common.py）
        code_dir = model_path.parent
        if str(code_dir) not in sys.path:
            sys.path.insert(0, str(code_dir))

        import torch  # 重依赖：只在加载路径导入
        from rl_agent_api import RLAgent  # 仓库自带推理入口，不是 pip laya

        device = (self._settings.system_one_device or "cpu").strip() or "cpu"
        if device == "cpu" and torch.get_num_threads() > 8:
            # 实测：加线程仅快约 20%（瓶颈在编码器前向），但会与 ASR/TTS 抢核；
            # 钉 8 线程是「单问 p95 167ms」那组数字的复现条件。
            torch.set_num_threads(8)
        return RLAgent(str(model_path), device=device)

    # -- 推理 ----------------------------------------------------------

    async def score(self, *, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        agent = await self._ensure_loaded()
        payload_state = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        payload_questions = questions_to_payload(questions)
        result = await asyncio.to_thread(agent.system_one, payload_state, payload_questions)
        answers = result.get("answers") if isinstance(result, dict) else None
        if not isinstance(answers, dict):
            raise RuntimeError("RLAgent.system_one 未返回 answers 字典")
        return answers


# ------------------------------------------------------------------ Mock（仅测试/显式 mock）


class MockBackend:
    """固定映射后端：**只**在 `system_one_provider=mock` 或测试里用。

    用途是让 A3 的 Router/decision_logs 在没有权重时也能被端到端验证；
    生产环境 `provider=mock` 会在 `Settings` 校验阶段直接拒绝启动（A1 已实现）。
    """

    #: 关键词 → 意图。顺序即优先级，先命中先用。
    KEYWORD_INTENTS: Final[tuple[tuple[str, str], ...]] = (
        ("药", "medication"),
        ("天气", "weather"),
        ("气温", "weather"),
        ("下雨", "weather"),
        ("戏曲", "opera"),
        ("戏", "opera"),
        ("电影", "movie"),
        ("血压", "health_checkup"),
        ("血糖", "health_checkup"),
    )

    #: 敏感词（Mock 专用，**不是**业务词表——业务词表在 `app/router/safety.py`）
    SENSITIVE_MARKERS: Final[tuple[str, ...]] = ("胸口", "胸痛", "喘不上", "摔倒", "不想活")

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def model_version(self) -> str:
        return f"mock:{self._settings.system_one_provider}"

    async def health(self) -> dict[str, Any]:
        return {
            "model_id": self._settings.system_one_model_id,
            "model_version": self.model_version,
            "device": self._settings.system_one_device,
            "provider": self._settings.system_one_provider,
            "model_loaded": True,
        }

    async def score(self, *, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        text = _state_text(state)
        answers: dict[str, Any] = {}
        if "intent" in questions:
            intent = "chat"
            for keyword, candidate in self.KEYWORD_INTENTS:
                if keyword in text:
                    intent = candidate
                    break
            answers["intent"] = {"choice": _intent_key(intent), "confidence": 0.9}
        if "is_sensitive" in questions:
            hit = any(marker in text for marker in self.SENSITIVE_MARKERS)
            answers["is_sensitive"] = {"noul": 0.95 if hit else 0.02}
        if "privacy_scope" in questions:
            answers["privacy_scope"] = {"choice": "personal" if "我" in text else "shared"}
        if "is_out_of_domain" in questions:
            answers["is_out_of_domain"] = {"noul": 0.05}
        return answers


def _state_text(state: Any) -> str:
    if isinstance(state, str):
        return state
    if isinstance(state, dict):
        return str(state.get("asr_final") or "") + " " + json.dumps(state, ensure_ascii=False)
    return json.dumps(state, ensure_ascii=False)


def _intent_key(intent: str) -> str:
    """业务枚举 → 模型候选键（Mock 用；与 `questions.INTENT_BY_KEY` 反向对应）。"""
    from app.system_one.questions import INTENT_BY_KEY  # 局部导入，避免模块级循环

    for key, value in INTENT_BY_KEY.items():
        if value == intent:
            return key
    return "其他"


def build_backend(settings: Settings | None = None) -> DecisionBackend:
    """按 `system_one_provider` 选后端。生产禁 mock（Settings 已校验，这里再兜一层）。"""
    resolved = settings or get_settings()
    provider = (resolved.system_one_provider or "").strip().lower()
    if provider == "mock":
        logger.warning("system_one_backend_mock provider=mock（仅限测试/演示）")
        return MockBackend(resolved)
    return LayaBackend(resolved)


def text_fingerprint(text: str) -> tuple[int, str]:
    """日志用指纹：`(长度, sha256 前 8 位)`。**不返回原文**（sidecar/客户端纪律）。"""
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:8]
    return len(text or ""), digest


__all__ = [
    "OUT_OF_DOMAIN_THRESHOLD",
    "SENSITIVE_THRESHOLD",
    "WARMUP_TEXT",
    "DecisionBackend",
    "LayaBackend",
    "MockBackend",
    "ModelNotConfigured",
    "build_backend",
    "map_answers",
    "questions_to_payload",
    "text_fingerprint",
]
