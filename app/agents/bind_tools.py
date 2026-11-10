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
from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agents.tool_registry import ToolRegistry
from app.services.tool_impls import (
    make_medication_confirm_executor,
    make_memory_recall_executor,
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


__all__ = ["bind_dev_c_tools"]
