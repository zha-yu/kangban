"""System One：Laya 本地分类器客户端 + 四信号（intent / is_sensitive / privacy_scope / 域外）。

Day 7 起模型从「Laya 配置项」换成**本地 Laya multilingual 权重 + sidecar**：

- 权重：`convaiinnovations/laya-multilingual`（322M/F16/615MB/apache-2.0，CPU 推理）
- 入口：模型仓库自带的 `rl_agent_api.RLAgent.system_one(state, questions)`，
  **不依赖 pip 的 `laya` 包**（详见 `spikes/chinese_jev_hello/README.md`）
- 契约：`schemas.py`（七类意图 + 全量降级不变量）、`questions.py`（题面唯一入口）
- 传输：`client.py`（快路径 200ms / shadow 400ms / 熔断降级）

实测口径（16 逻辑核 CPU）：单问 p95 167ms（达标 200ms），两问 285ms、四问 668ms（超标）。
因此快路径只问 `intent`，`is_sensitive` 走 shadow，不阻塞路由（计划 §10-10 裁决）。

**导出纪律**：本模块**不导出** `backend` / `server`。`backend` 会在加载时依赖
`torch` + `rl_agent_api`（只在函数内延迟导入，但一旦有人 `from app.system_one import
LayaBackend` 就会把这条依赖拉到主服务进程），`server` 是 sidecar 专用 FastAPI 应用。
主 `.venv` 里 `import app.system_one` 必须永远不碰 torch。
"""

from __future__ import annotations

from app.system_one.client import SystemOneClient
from app.system_one.questions import (
    QUESTIONSET_VERSION,
    build_fast_questions,
    build_questions,
    build_shadow_questions,
    build_state,
    intent_of_key,
)
from app.system_one.schemas import (
    INTENTS,
    Intent,
    PrivacyScope,
    QuestionSpec,
    SystemOneRequest,
    SystemOneResponse,
    SystemOneResult,
    degraded_result,
)

__all__ = [
    "INTENTS",
    "QUESTIONSET_VERSION",
    "Intent",
    "PrivacyScope",
    "QuestionSpec",
    "SystemOneClient",
    "SystemOneRequest",
    "SystemOneResponse",
    "SystemOneResult",
    "build_fast_questions",
    "build_questions",
    "build_shadow_questions",
    "build_state",
    "degraded_result",
    "intent_of_key",
]
