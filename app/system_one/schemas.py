"""System One 的数据契约：请求 / 响应 / 业务视图（Day 7 · A1）。

三件事在这里定死，B/C/D 才能不见面并行：

1. **意图只有七类**（`Intent`）。标签是产品决策，不由模型自由生成；新增类别要改 PRD。
2. **降级是显式状态，不是"碰巧失败"**：`degraded=True` 必须同时满足
   `intent="other"` / `confidence=0.0` / `is_out_of_domain=True`。半套降级值会被校验拦下来——
   否则下游拿到"degraded 但 intent=weather"会既走降级又执行工具。
3. **题面形状对齐 `rl_agent_api`**（模型仓库自带推理入口），不做二次翻译：
   `{"type": "choice|noul|score", "instructions": str, "criteria": ...}`。

实测依据（docs/plans/day07/dev-a-三级路由与决策日志.md §5.2.0 与 spikes/chinese_jev_hello/README.md）：
- `confidence` 由**概率分布的熵**算出（`confidence_from_probs`），不是 max prob；
- `noul` 题**不返回置信度**，只有 true 概率 → 业务侧的 `sensitive_probability` 就是它；
- 每个答案还带 `rl_agent.act_probability`，实测恒为 1、无信号，**契约里不暴露**。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: 七类意图（PRD FR-DLG-02/03 的固定意图集合）。顺序与 questions.py 的候选表一致。
Intent = Literal["chat", "medication", "opera", "movie", "weather", "health_checkup", "other"]

#: 隐私收敛范围（四信号的第四路；双人间场景用）。
PrivacyScope = Literal["personal", "shared", "none"]

#: 题面三型，与 rl_agent_api 的 QTYPES 一一对应，不得增删。
QuestionType = Literal["choice", "noul", "score"]

INTENTS: tuple[Intent, ...] = (
    "chat",
    "medication",
    "opera",
    "movie",
    "weather",
    "health_checkup",
    "other",
)


class QuestionSpec(BaseModel):
    """一道题。`criteria` 的形状由 `type` 决定（与 rl_agent_api 完全一致）：

    - `choice`：`{候选键: 描述}`；键就是模型返回的 `choice`
    - `noul`：`{"false": 描述, "true": 描述}`
    - `score`：`[等级0描述, 等级1描述, ...]`（有序）
    """

    model_config = ConfigDict(extra="forbid")

    type: QuestionType
    instructions: str
    criteria: dict[str, str] | list[str] | None = None

    @model_validator(mode="after")
    def _check_criteria_shape(self) -> QuestionSpec:
        crit = self.criteria
        if self.type == "choice":
            if not isinstance(crit, dict) or len(crit) < 2:
                raise ValueError("choice 题的 criteria 必须是 ≥2 项的 {键: 描述}")
        elif self.type == "noul":
            if not isinstance(crit, dict) or set(crit) != {"false", "true"}:
                raise ValueError('noul 题的 criteria 必须恰好是 {"false": ..., "true": ...}')
        else:  # score
            if not isinstance(crit, list) or len(crit) < 2:
                raise ValueError("score 题的 criteria 必须是 ≥2 项的有序描述列表")
        return self


class SystemOneRequest(BaseModel):
    """`POST /v1/decide` 的请求体。

    `state` 与 `questions` 原样转交给 `RLAgent.system_one(state, questions)`：
    `state` 可为 str / dict / list（dict 会被序列化成 JSON 文本），
    **强烈建议带上下文**（当前任务、媒体状态），否则"换一段""声音小一点"这类指令会判错——
    20 条实测里 2 个错例都源于此（见 spike README §5）。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    trace_id: str | None = None
    state: str | dict[str, Any] | list[Any]
    questions: dict[str, QuestionSpec]
    model_version: str | None = None

    @model_validator(mode="after")
    def _check_questions_non_empty(self) -> SystemOneRequest:
        if not self.questions:
            raise ValueError("questions 不能为空：至少问一道")
        return self


class SystemOneResult(BaseModel):
    """业务视图：Router / turn_service 只认这个，不认 sidecar 的原始 JSON。"""

    model_config = ConfigDict(extra="forbid")

    intent: Intent = "other"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    is_sensitive: bool = False
    #: noul 题只给概率、不给置信度，所以这里存概率而不是 bool 的"把握度"
    sensitive_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    privacy_scope: PrivacyScope = "none"
    is_out_of_domain: bool = False
    degraded: bool = False
    latency_ms: int = 0
    model_version: str = ""
    multi_intent: bool = False
    sub_intents: tuple[Intent, ...] = ()

    @model_validator(mode="after")
    def _degraded_is_all_or_nothing(self) -> SystemOneResult:
        if self.degraded:
            if self.intent != "other" or self.confidence != 0.0 or not self.is_out_of_domain:
                raise ValueError(
                    "degraded=True 时必须 intent='other'、confidence=0.0、is_out_of_domain=True；"
                    "半套降级值会让下游既走降级又执行工具"
                )
            if self.is_sensitive or self.sub_intents:
                raise ValueError("degraded 结果不得携带敏感标记或子意图")
        return self


class SystemOneResponse(BaseModel):
    """`POST /v1/decide` 的响应体：`request_id` 回带（便于 sidecar 侧排障）+ 业务视图。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    result: SystemOneResult


def degraded_result(
    *,
    latency_ms: int = 0,
    model_version: str = "",
) -> SystemOneResult:
    """构造一个合法的全量降级结果（三个不变量一次写对，避免调用方各写一套）。"""
    return SystemOneResult(
        intent="other",
        confidence=0.0,
        is_sensitive=False,
        sensitive_probability=None,
        privacy_scope="none",
        is_out_of_domain=True,
        degraded=True,
        latency_ms=latency_ms,
        model_version=model_version,
    )


__all__ = [
    "INTENTS",
    "Intent",
    "PrivacyScope",
    "QuestionSpec",
    "QuestionType",
    "SystemOneRequest",
    "SystemOneResponse",
    "SystemOneResult",
    "degraded_result",
]
