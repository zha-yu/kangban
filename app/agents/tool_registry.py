"""工具白名单（N12 / 契约 §5.3）。

**为什么需要注册表而不是一堆 `if tool_name == ...`**：
模型能说出任意工具名。没有白名单时，「未注册的工具」和「已注册但参数错」会走同一条
失败分支，日志里分不清是「模型幻觉」还是「参数没对上」——两者的处置完全不同。

本模块只描述**工具是什么**（名字、说明、参数 schema、有无副作用、是否幂等）。
怎么执行、怎么记账、怎么降级，全在 `app/services/tool_executor.py`——那里是唯一写库层。

`executor` 允许为空：本日三条工具分属三个人（记忆归 dev-B、天气归 dev-C、用药确认归
dev-A 的 N10），注册表先把 spec 定下来，谁实现谁 `bind()`。没绑定就调用 → `not_found`，
不会静默回退到「让模型自己编一个答案」。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

#: 工具执行函数：`(args, runtime) -> dict`。返回值必须是可 JSON 序列化的 dict，
#: 由 `tool_executor` 统一脱敏后落库；抛异常必须用 `tool_executor` 里的四类域异常。
ToolExecutorFn = Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一条工具定义。字段少而硬：多一个可选开关，就多一条没人测的分支。"""

    name: str
    description: str
    parameters_schema: dict[str, Any]
    side_effect: bool
    idempotent: bool
    executor: ToolExecutorFn | None = None
    #: `local` = 进程内实现（绑 `executor`）；`mcp` = 走 `app/mcp_client` 的远端工具。
    #: 两者不能混：MCP 工具的失败分类必须由 `mcp_client.tool_error_class()` 说了算，
    #: 否则同一个天气超时会出现两套 error_class。
    transport: Literal["local", "mcp"] = "local"

    @property
    def wired(self) -> bool:
        """是否可执行。本地工具看是否绑了实现；MCP 工具看客户端是否注入（由执行器判）。"""
        return self.executor is not None or self.transport == "mcp"


class ToolRegistry:
    """进程内工具白名单。启动时构建一次，运行期只读（`bind` 除外）。"""

    def __init__(self, specs: tuple[ToolSpec, ...] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool already registered: {spec.name!r}")
        self._specs[spec.name] = spec

    def bind(self, name: str, executor: ToolExecutorFn) -> None:
        """绑定本地实现。同名重复绑定直接报错——静默覆盖会让「谁最后 import 谁生效」。

        `transport="mcp"` 的工具**不允许**绑本地实现：那等于给同一个工具留两条执行路径，
        出问题时无法判断走的是哪条、用的哪套超时与失败分类。
        """
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"unknown tool: {name!r}")
        if spec.transport == "mcp":
            raise ValueError(
                f"tool {name!r} is an MCP tool; inject an mcp_client instead of binding"
            )
        if spec.executor is not None:
            raise ValueError(f"tool already bound: {name!r}")
        self._specs[name] = replace(spec, executor=executor)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def require(self, name: str) -> ToolSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"unknown tool: {name!r}")
        return spec

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def __len__(self) -> int:
        return len(self._specs)


# ------------------------------------------------------------------ 本日三条工具


def default_registry() -> ToolRegistry:
    """Day 6 的三条工具 spec。实现由各自 owner 在装配处 `bind()`。

    参数 schema 只写**用得到的那部分 JSON Schema**（type / properties / required /
    additionalProperties）；执行器只做这一层校验，不做完整 JSON Schema 实现——
    引入一个 jsonschema 依赖只为校验三个字段不值得。
    """
    return ToolRegistry(
        (
            ToolSpec(
                name="memory_recall",
                description="回忆这位长者此前说过的偏好与事实，用于个性化回应",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "category": {"type": "string"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                side_effect=False,
                idempotent=True,
            ),
            ToolSpec(
                name="get_weather",
                description="查询指定城市的实时天气（含数据时间），用于回答天气类问题",
                parameters_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string", "minLength": 1}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
                side_effect=False,
                idempotent=True,
                transport="mcp",  # N26：走 app/mcp_client，失败分类由 tool_error_class 定
            ),
            ToolSpec(
                name="medication_confirm",
                description="记录长者对服药提醒的回答（吃了 / 没吃 / 稍后），推进用药任务",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "answer": {"type": "string", "minLength": 1},
                        "plan_id": {"type": "string"},
                    },
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                side_effect=True,
                idempotent=True,
            ),
            # ---- Day 8 B4（dev-B）：mock-operations 两工具 + 戏曲点播 ----
            # `subject_elder_id` 不进 required：模型可以传（用于 AC-39 越权检测），
            # 也可以不传（执行链步骤 1.5 从运行时身份注入）。执行链对声明了该参数
            # 的工具统一做注入/越权拒绝，见 tool_executor.SUBJECT_ELDER_KEY。
            ToolSpec(
                name="create_work_order",
                description="为这位长者创建照护工单（Mock 落库），如上报异常、请求人工跟进",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "subject_elder_id": {"type": "string"},
                        "order_type": {"type": "string", "minLength": 1},
                        "severity": {"type": "string", "minLength": 1},
                        "note": {"type": "string"},
                    },
                    "required": ["order_type", "severity"],
                    "additionalProperties": False,
                },
                side_effect=True,
                idempotent=True,  # dedupe_token 幂等（工单服务口径）
                transport="mcp",  # mock-operations server（7102），失败分类归 mcp_client
            ),
            ToolSpec(
                name="send_family_notification",
                description="给这位长者的家属发送通知（Mock 落库），如健康异常通报、日常报平安",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "subject_elder_id": {"type": "string"},
                        "notification_type": {"type": "string", "minLength": 1},
                        "payload": {"type": "object"},
                        "channel": {"type": "string"},
                    },
                    "required": ["notification_type", "payload"],
                    "additionalProperties": False,
                },
                side_effect=True,
                idempotent=True,  # dedupe_key 幂等
                transport="mcp",
            ),
            ToolSpec(
                name="play_opera",
                description=(
                    "戏曲播放与控制。action=play 时按 query（剧目/剧种/歌手）检索并返回播放地址；"
                    "其余为播放控制动作，无需 query：pause/resume/stop/replay/interrupt/"
                    "volume_up/volume_down/next（换一段，服务端取下一首）"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "play",
                                "pause",
                                "resume",
                                "stop",
                                "next",
                                "replay",
                                "interrupt",
                                "volume_up",
                                "volume_down",
                            ],
                        },
                        "query": {"type": "string", "minLength": 1},
                    },
                    # query 只在 action=play 时必填（条件校验在执行器）；
                    # 规则直连只传 {"action": ...}，required 卡死会把七动作全炸掉
                    "additionalProperties": False,
                },
                side_effect=False,  # 只读检索；media_history 归 C4 media_service（X8 ②）
                idempotent=True,
            ),
        )
    )


__all__ = ["ToolExecutorFn", "ToolRegistry", "ToolSpec", "default_registry"]
