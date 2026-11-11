"""Day 8 Step 5（B4）工具注册与执行链测试。

必过用例（计划点名）：
- `test_opera_tool_executes_via_executor`：play_opera 经 ToolExecutor 统一执行链
  调通（X8 口径②：返回 {"found", "media"}，media_url 严格走稳定路径）；
- `test_work_order_tool_via_mcp`：create_work_order 走 mcp transport，
  经注入的 MCP 客户端调通，`subject_elder_id` 由执行链注入运行时身份；
- `test_tool_uses_context_elder_id_ignores_model_arg`（AC-39）：模型自报的
  `elder_id`（身份键）被剥离审计，服务对象取运行时身份；
- `test_cross_elder_call_rejected_audited`（AC-39）：模型试图指定别的长者
  → `forbidden` 拒绝 + `tool_cross_elder_attempt` 审计落库。

补充：注册表三条新 spec 的 transport/side_effect 口径、MultiServerMCPClient
按工具名路由（weather / mock-operations 双 server）、play_opera 未找到返回。

DB 用例连真实测试 PostgreSQL（savepoint 会话 + 种子长者，PG 不可达自动 skip）；
MCP 客户端用协议桩（记录 call_tool 入参），不打真实 server——真 server 联调
归 Step 6 集成段。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents.bind_tools import MultiServerMCPClient, bind_dev_b_tools
from app.agents.tool_registry import ToolRegistry, default_registry
from app.core.runtime_context import Identity
from app.mcp_client.client import MCPResult
from app.models.audit import AuditLog
from app.models.media import MediaLibrary
from app.models.organization import Elder
from app.services.tool_executor import MESSAGES, ToolExecutor

# ------------------------------------------------------------------ 夹具


@pytest_asyncio.fixture
async def session_factory(
    db_engine: AsyncEngine,
) -> AsyncIterator[Callable[[], AsyncSession]]:
    """同连接会话工厂：外层事务内开 savepoint 子会话。

    执行器内部 `async with session_factory()` 拿到的短会话与测试会话共享
    同一连接/事务——未提交的种子行对执行器可见，且回滚后零残留。
    """
    try:
        conn = await db_engine.connect()
    except Exception as exc:  # pragma: no cover - 本地无 PG 时跳过
        pytest.skip(f"PostgreSQL 不可达，跳过工具注册 DB 用例：{exc!r}")

    trans = await conn.begin()
    sf = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield sf
    finally:
        await trans.rollback()
        await conn.close()


@pytest_asyncio.fixture
async def session(session_factory: Callable[[], AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as sess:
        yield sess


@pytest_asyncio.fixture
async def elder_id(session: AsyncSession) -> AsyncIterator[str]:
    elder = Elder(name="孙德海", status="active")
    session.add(elder)
    await session.flush()
    yield str(elder.elder_id)


@pytest.fixture
def runtime(elder_id: str) -> Identity:
    """设备身份（长者端）：绑定当前长者——工具服务对象的唯一合法来源。"""
    device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "pad-test-001"))
    return Identity(kind="device", device_id=device_id, elder_id=elder_id)


class RecordingMCPClient:
    """MCPClient 协议桩：记录 call_tool 入参，返回预设结果。"""

    def __init__(self, result: MCPResult | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result or MCPResult(ok=True, data={"ok": True}, error=None)

    async def call_tool(self, tool_name: str, args: dict[str, Any] | None = None) -> MCPResult:
        self.calls.append((tool_name, dict(args or {})))
        return self._result


def _executor(
    session: AsyncSession, registry: ToolRegistry | None = None, **kwargs: Any
) -> ToolExecutor:
    return ToolExecutor(session, registry=registry or default_registry(), **kwargs)


# ------------------------------------------------------------------ 注册表口径


def test_registry_has_day8_specs() -> None:
    """三条新 spec 注册齐备，transport / side_effect 口径正确。"""
    registry: ToolRegistry = default_registry()
    names = set(registry.names)
    assert {"create_work_order", "send_family_notification", "play_opera"} <= names

    wo = registry.get("create_work_order")
    fn = registry.get("send_family_notification")
    po = registry.get("play_opera")
    assert wo is not None and wo.transport == "mcp" and wo.side_effect and wo.idempotent
    assert fn is not None and fn.transport == "mcp" and fn.side_effect and fn.idempotent
    assert po is not None and po.transport != "mcp" and not po.side_effect
    # X8 口径③（2026-11-11 主理人裁决）：schema 含 action 枚举，required 不卡 query
    # （规则直连只传 {"action": ...}；query 必填移到执行器 action=play 条件校验）
    actions = po.parameters_schema["properties"]["action"]["enum"]
    assert {"play", "pause", "resume", "stop", "next", "replay", "volume_up", "volume_down"} <= set(
        actions
    )
    assert "query" not in po.parameters_schema.get("required", [])


# ------------------------------------------------------------------ 必过用例


async def test_opera_tool_executes_via_executor(
    session: AsyncSession, elder_id: str, runtime: Identity, session_factory: Any
) -> None:
    """play_opera 经 ToolExecutor 统一执行链调通（X8 ②返回形态 + ①路径约定）。"""
    mid = uuid.uuid5(uuid.NAMESPACE_URL, "test-liangzhu")
    session.add(
        MediaLibrary(
            media_id=mid,
            type="opera",
            title="梁山伯与祝英台",
            genre="越剧",
            era="1950s",
            artist="袁雪芬",
            media_url=f"/static/media/opera/{mid}.wav",
            duration=42,
            copyright_status="public_domain",
        )
    )
    await session.flush()

    registry = default_registry()
    bind_dev_b_tools(registry, session_factory=session_factory)
    executor = _executor(session, registry=registry)

    result = await executor.execute(
        tool_name="play_opera",
        args={"query": "梁山伯"},
        runtime=runtime,
        turn_id=None,
        idempotency_key="ik-opera-hit",
    )

    assert result.ok, result.message
    data = result.data or {}
    assert data["found"] is True
    assert data["media"]["media_id"] == str(mid)
    assert data["media"]["media_url"] == f"/static/media/opera/{mid}.wav"  # X8 稳定路径①
    assert data["media"]["genre"] == "越剧"


async def test_opera_tool_not_found_returns_found_false(
    session: AsyncSession, runtime: Identity, session_factory: Any
) -> None:
    """查无此曲：X8 ② 明确返回 {"found": False, "media": None}，不算工具失败。"""
    registry = default_registry()
    bind_dev_b_tools(registry, session_factory=session_factory)
    executor = _executor(session, registry=registry)

    result = await executor.execute(
        tool_name="play_opera",
        args={"query": "不存在的剧目"},
        runtime=runtime,
        turn_id=None,
        idempotency_key="ik-opera-miss",
    )

    assert result.ok, result.message
    assert result.data == {"found": False, "media": None}


async def test_opera_tool_next_returns_random_media(
    session: AsyncSession, elder_id: str, runtime: Identity, session_factory: Any
) -> None:
    """换一段（X8 ③）：C4 锚点未交付，过渡期随机返回一首 type=opera 全集曲目。"""
    session.add(
        MediaLibrary(
            media_id=uuid.uuid5(uuid.NAMESPACE_URL, "test-next-a"),
            type="opera",
            title="换段测试A",
            genre="越剧",
            era="1950s",
            artist="测试",
            media_url="/static/media/opera/test-next-a.mp3",
            duration=30,
            copyright_status="public_domain",
        )
    )
    await session.flush()

    registry = default_registry()
    bind_dev_b_tools(registry, session_factory=session_factory)
    executor = _executor(session, registry=registry)

    result = await executor.execute(
        tool_name="play_opera",
        args={"action": "next"},
        runtime=runtime,
        turn_id=None,
        idempotency_key="ik-opera-next",
    )

    assert result.ok, result.message
    data = result.data or {}
    assert data["found"] is True
    assert data["media"]["media_url"].startswith("/static/media/opera/")
    assert data["media"]["title"]


async def test_opera_tool_control_actions_return_marker(
    session: AsyncSession, runtime: Identity, session_factory: Any
) -> None:
    """播放控制七动作：不查库，返回 {"control": action} 标记（下发层转 media_control 帧）。"""
    registry = default_registry()
    bind_dev_b_tools(registry, session_factory=session_factory)
    executor = _executor(session, registry=registry)

    for action in ("pause", "resume", "stop", "replay", "volume_up", "volume_down"):
        result = await executor.execute(
            tool_name="play_opera",
            args={"action": action},
            runtime=runtime,
            turn_id=None,
            idempotency_key=f"ik-opera-ctl-{action}",
        )
        assert result.ok, (action, result.message)
        assert result.data == {"control": action}, action


async def test_work_order_tool_via_mcp(
    session: AsyncSession, elder_id: str, runtime: Identity
) -> None:
    """create_work_order 走 mcp transport：subject 由执行链注入运行时身份。"""
    stub = RecordingMCPClient()
    executor = _executor(session, mcp_client=stub)

    result = await executor.execute(
        tool_name="create_work_order",
        args={"order_type": "health_alert", "severity": "yellow", "note": "血压偏高"},
        runtime=runtime,
        turn_id=None,
        idempotency_key="ik-wo-1",
    )

    assert result.ok, result.message
    assert len(stub.calls) == 1
    called_name, called_args = stub.calls[0]
    assert called_name == "create_work_order"
    assert called_args["subject_elder_id"] == elder_id  # 注入运行时身份（AC-39）
    assert called_args["order_type"] == "health_alert"


async def test_tool_uses_context_elder_id_ignores_model_arg(
    session: AsyncSession, elder_id: str, runtime: Identity
) -> None:
    """AC-39：模型自报 elder_id（身份键）被剥离并审计；subject 取运行时身份。"""
    stub = RecordingMCPClient()
    executor = _executor(session, mcp_client=stub)

    result = await executor.execute(
        tool_name="create_work_order",
        args={
            "elder_id": "forged-by-model",  # 身份键 → 剥离 + 审计
            "subject_elder_id": elder_id,  # 与上下文一致 → 放行（执行链仍会覆盖）
            "order_type": "family_visit",
            "severity": "green",
        },
        runtime=runtime,
        turn_id=None,
        idempotency_key="ik-wo-2",
    )

    assert result.ok, result.message
    _, called_args = stub.calls[0]
    assert called_args["subject_elder_id"] == elder_id  # 最终值来自运行时身份
    assert "elder_id" not in called_args  # 模型自报身份被剥离

    rows = (
        (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "tool_identity_override_attempt")
            )
        )
        .scalars()
        .all()
    )
    assert any("elder_id" in ((r.detail or {}).get("dropped_keys") or []) for r in rows)


async def test_cross_elder_call_rejected_audited(
    session: AsyncSession, elder_id: str, runtime: Identity
) -> None:
    """AC-39：模型试图指定别的长者 → forbidden 拒绝 + tool_cross_elder_attempt 审计。"""
    other_elder = str(uuid.uuid4())  # 上下文之外的另一长者：服务对象越权
    stub = RecordingMCPClient()
    executor = _executor(session, mcp_client=stub)

    result = await executor.execute(
        tool_name="send_family_notification",
        args={
            "subject_elder_id": other_elder,
            "notification_type": "health_alert",
            "payload": {"severity": "red"},
        },
        runtime=runtime,
        turn_id=None,
        idempotency_key="ik-wo-3",
    )

    assert not result.ok
    assert result.error_class == "forbidden"
    assert result.message == MESSAGES["forbidden"]  # 播报话术不泄漏细节
    assert stub.calls == []  # 请求根本没到 MCP server

    rows = (
        (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "tool_cross_elder_attempt")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].detail["tool_name"] == "send_family_notification"
    assert rows[0].detail["claimed"] == other_elder
    assert rows[0].detail["context"] == elder_id


# ------------------------------------------------------------------ 多 server 路由


async def test_multi_server_mcp_routes_by_tool() -> None:
    """MultiServerMCPClient：按工具名分发到 weather / mock-operations 客户端。"""
    weather = RecordingMCPClient()
    operations = RecordingMCPClient()
    router = MultiServerMCPClient(
        {
            "get_weather": weather,
            "create_work_order": operations,
            "send_family_notification": operations,
        },
    )

    await router.call_tool("create_work_order", {"order_type": "x"})
    await router.call_tool("get_weather", {"city": "上海"})

    assert [name for name, _ in operations.calls] == ["create_work_order"]
    assert [name for name, _ in weather.calls] == ["get_weather"]

    # 未登记的 MCP 工具 → bad_response 短路（「未接线」口径，不让模型编答案）
    result = await router.call_tool("unknown_tool", {})
    assert not result.ok and result.error == "bad_response"
