"""Day 7 C1 · dev-C 工具装配 hook。

**为什么独立一个模块**：

- companion.py 和 tool_registry.py 都是「描述层」（ToolSpec 定义 + ReAct prompt 协议）——
  不能在那里 import services 里的 executor 工厂（分层红线：agents → services 是单向依赖，
  但描述层应该保持纯描述，不知道底下怎么实现）。
- main.py 是 A3 独占的高风险文件。C1 不碰它——只提供一个函数让 A3 调一次。

调用时机：A3 在 lifespan 里已经拿到 session_factory + checkpointer（或
SchedulerRuntime 启动后 get 出来的 checkpointer）之后，调一次：

```python
from app.agents.bind_tools import bind_dev_c_tools

bind_dev_c_tools(
    registry=default_registry(),       # 或 ToolExecutor 内部用的那个
    session_factory=session_factory,
    checkpointer=medication_checkpointer_instance,
)
```

如果 main.py 用的是 `_SessionToolExecutor`（内部每次调用用 default_registry()），
那这里要传入 registry 实例——或者直接改 default_registry 让它返回已绑定的 spec。
后者更干：`default_registry()` 改接受可选工厂参数，没参数返回空 executor 的注册表，
传了工厂返回已绑定的。

**但 ToolRegistry 是 frozen dataclass（spec 里有 executor 字段），一旦构造就不可变**——
所以只能在 registry 实例上 bind。也就是说：A3 必须拿到同一个 registry 实例来 bind。

main.py 当前用 `_SessionToolExecutor(session_factory, mcp_client=...)`，内部每次调用
`default_registry()` 返回**新实例**，外部 bind 没用。

所以 A3 的装配顺序是：

```python
registry = default_registry()          # 一次构造，后续复用
bind_dev_c_tools(registry, session_factory, checkpointer)
executor = _SessionToolExecutor(..., registry=registry)
agent = CompanionAgent(..., tools=[registry.get(n) for n in registry.names])
```

C1 不改 main.py，只写这个 hook 让 A3 知道怎么接。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agents.tool_registry import ToolRegistry
from app.services.tool_impls import (
    make_medication_confirm_executor,
    make_memory_recall_executor,
    make_play_opera_executor,
)

logger = logging.getLogger(__name__)


def bind_dev_c_tools(
    registry: ToolRegistry,
    *,
    session_factory: Callable[[], Any],
    checkpointer: AsyncPostgresSaver,
) -> None:
    """把 Day 7 dev-C 实现的两个本地工具执行器绑定到 registry。

    :raises KeyError: registry 里没有 memory_recall / medication_confirm（
        default_registry 应该都有；如果调用方自己造了空注册表，这里会报错提醒漏 spec）
    :raises ValueError: 工具已绑过（重复装配 = 谁先 import 谁生效）
    """
    registry.bind(
        "memory_recall",
        make_memory_recall_executor(session_factory=session_factory),
    )
    registry.bind(
        "medication_confirm",
        make_medication_confirm_executor(
            session_factory=session_factory,
            checkpointer=checkpointer,
        ),
    )
    logger.info(
        "bind_dev_c_tools_bound names=%s",
        [n for n in ("memory_recall", "medication_confirm") if registry.get(n).wired],
    )


# ------------------------------------------------------------------ Day 8 B4（dev-B）


class MultiServerMCPClient:
    """按工具名路由到对应 MCP server 的客户端适配（Day 8 B4）。

    ToolExecutor 只持有**一个** mcp_client（`self._mcp.call_tool(...)`），而 Day 8
    有两个 MCP server：weather（7101，get_weather）与 mock-operations（7102，
    create_work_order / send_family_notification）。本类**不新增执行路径**——只是把
    同协议、不同基地址的两个 `MCPClient` 按工具名分发；失败分类仍由各自
    `MCPClient.call_tool` 内的 `tool_error_class()` 决定（G2：同一工具不得有两套
    失败语义）。A3 装配时传给 `_SessionToolExecutor(mcp_client=...)` 即可。

    路由必须显式登记（含 get_weather）；未登记的 MCP 工具 → `bad_response` 短路
    （「未接线」口径，绝不让模型自己编答案）。
    """

    def __init__(self, routes: Mapping[str, Any], *, default: Any | None = None) -> None:
        self._routes = dict(routes)
        self._default = default

    def client_for(self, tool_name: str) -> Any | None:
        return self._routes.get(tool_name, self._default)

    async def call_tool(self, tool_name: str, args: Mapping[str, Any] | None = None) -> Any:
        from app.mcp_client.client import MCPResult

        client = self.client_for(tool_name)
        if client is None:
            logger.warning("mcp_route_missing tool=%s", tool_name)
            return MCPResult(ok=False, data=None, error="bad_response")
        return await client.call_tool(tool_name, args)


def bind_dev_b_tools(registry: ToolRegistry, *, session_factory: Callable[[], Any]) -> None:
    """把 Day 8 dev-B 的本地工具执行器绑定到 registry（A3 装配时调用一次）。

    只绑 `play_opera`。MCP 两工具（create_work_order / send_family_notification）
    **不在本函数绑定**——`transport="mcp"` 的工具由 ToolExecutor 注入的 mcp_client
    执行；A3 装配时用 `MultiServerMCPClient` 包装 weather 与 operations 两个
    MCPClient 传给 `_SessionToolExecutor(mcp_client=...)`：

    ```python
    from app.mcp_client.client import MCPClient
    from app.agents.bind_tools import MultiServerMCPClient, bind_dev_b_tools

    ops_client = MCPClient(settings, server_url=settings.mcp_operations_server_url)
    weather_client = MCPClient(settings)  # Day 7 既有
    mcp = MultiServerMCPClient(
        {
            "get_weather": weather_client,
            "create_work_order": ops_client,
            "send_family_notification": ops_client,
        },
    )
    bind_dev_b_tools(tool_registry, session_factory=session_factory)
    session_tool_executor = _SessionToolExecutor(session_factory, registry=tool_registry, mcp_client=mcp)
    ```
    """
    registry.bind("play_opera", make_play_opera_executor(session_factory=session_factory))
    logger.info("bind_dev_b_tools_bound play_opera_wired=%s", registry.get("play_opera").wired)


__all__ = ["MultiServerMCPClient", "bind_dev_b_tools", "bind_dev_c_tools"]
