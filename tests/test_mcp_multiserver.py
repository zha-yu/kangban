"""Day 9 B1（F18/F19）双 server 路由 + MCP 幂等记账回归。

覆盖计划 §2.2 的点名判据：

1. 双 server 路由：`get_weather → 7101`、`create_work_order` / `send_family_notification
   → 7102`（假客户端各自记录 `server_url`，断言请求落在正确的 server 上）；
2. 同键重放：同 `dedupe_token` 同参数第二次调用返回原结果，MCP 只被调一次、
   `tool_executions` 只 1 行且第二次 `idempotent_replay=True`；
3. 同键异内容：工具层 `denied/forbidden` + `audit_logs` 1 行 `tool_idempotency_conflict`
   （**没有 HTTP 409**——409 是 REST 口径）；
4. MCP 记账：成功/失败都落 `tool_executions`（此前 MCP 分支在记账前 return）；
5. 跨长者越权：`denied/forbidden` + `tool_cross_elder_attempt` 审计（含 trace_id）；
6. 护栏回归：同轮第 9 次被拒、同工具 1 分钟第 3 次熔断对 MCP 路径同样成立。

DB 用例沿用仓库隔离写法（真实测试库 + savepoint，用例结束整体回滚）；PG 不可达自动
skip 并打印原因。MCP 客户端一律协议桩，不打真实 server。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents.bind_tools import MultiServerMCPClient
from app.agents.guardrails import Guardrails
from app.agents.tool_registry import ToolRegistry, ToolSpec, default_registry
from app.core.context import reset_trace_id, set_trace_id
from app.core.runtime_context import Identity
from app.mcp_client.client import MCPResult
from app.models import AuditLog
from app.models.conversation import ToolExecution
from app.services.tool_executor import MESSAGES, ToolExecutor

WEATHER_URL = "http://localhost:7101"
OPERATIONS_URL = "http://localhost:7102"


# ------------------------------------------------------------------ 夹具


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    try:
        conn = await db_engine.connect()
    except Exception as exc:
        pytest.skip(f"PostgreSQL 不可达，跳过 MCP 记账用例：{exc!r}")

    trans = await conn.begin()
    sf = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    async with sf() as sess:
        try:
            yield sess
        finally:
            await sess.rollback()
    await trans.rollback()


@pytest.fixture
def identity() -> Identity:
    return Identity(
        kind="device",
        device_id="33333333-3333-3333-3333-333333333333",
        elder_id="44444444-4444-4444-4444-444444444444",
    )


class RecordingClient:
    """MCPClient 协议桩：记录自己的 `server_url` 与每次 `call_tool` 入参。"""

    def __init__(self, server_url: str, result: MCPResult | None = None) -> None:
        self.server_url = server_url
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result or MCPResult(ok=True, data={"ok": True}, error=None)

    async def call_tool(self, tool_name: str, args: dict[str, Any] | None = None) -> MCPResult:
        self.calls.append((tool_name, dict(args or {})))
        return self._result


def _router(weather: RecordingClient, operations: RecordingClient) -> MultiServerMCPClient:
    return MultiServerMCPClient(
        {
            "get_weather": weather,
            "create_work_order": operations,
            "send_family_notification": operations,
        },
    )


def _executor(
    session: AsyncSession,
    *,
    router: MultiServerMCPClient,
    guardrails: Guardrails | None = None,
) -> ToolExecutor:
    return ToolExecutor(
        session,
        registry=default_registry(),
        guardrails=guardrails,
        mcp_client=router,
    )


async def _audits(session: AsyncSession, action: str) -> list[AuditLog]:
    rows = (await session.execute(select(AuditLog).where(AuditLog.action == action))).scalars()
    return list(rows)


async def _executions(session: AsyncSession, tool_name: str) -> list[ToolExecution]:
    rows = (
        await session.execute(select(ToolExecution).where(ToolExecution.tool_name == tool_name))
    ).scalars()
    return list(rows)


# ------------------------------------------------------------------ 1. 双 server 路由


async def test_dual_server_routes_weather_7101_and_ops_7102() -> None:
    weather = RecordingClient(WEATHER_URL, MCPResult(ok=True, data={"city": "杭州"}, error=None))
    operations = RecordingClient(
        OPERATIONS_URL, MCPResult(ok=True, data={"order_id": "wo-1"}, error=None)
    )
    router = _router(weather, operations)

    assert router.client_for("get_weather") is weather
    assert router.client_for("create_work_order") is operations
    assert router.client_for("send_family_notification") is operations
    assert router.client_for("get_weather").server_url == "http://localhost:7101"
    assert router.client_for("create_work_order").server_url == "http://localhost:7102"

    await router.call_tool("get_weather", {"city": "杭州"})
    await router.call_tool("create_work_order", {"order_type": "health_alert"})
    await router.call_tool("send_family_notification", {"notification_type": "health_alert"})

    assert [name for name, _ in weather.calls] == ["get_weather"]
    assert [name for name, _ in operations.calls] == [
        "create_work_order",
        "send_family_notification",
    ]

    # 未登记工具不落到默认 server，按 bad_response 短路
    missing = await router.call_tool("delete_everything", {})
    assert not missing.ok and missing.error == "bad_response"


# ------------------------------------------------------------------ 2. 同键重放


async def test_same_dedupe_token_replays_without_second_mcp_call(
    session: AsyncSession, identity: Identity
) -> None:
    operations = RecordingClient(
        OPERATIONS_URL,
        MCPResult(ok=True, data={"order_id": "wo-9", "created": True}, error=None),
    )
    executor = _executor(session, router=_router(RecordingClient(WEATHER_URL), operations))
    args = {
        "order_type": "health_alert",
        "severity": "yellow",
        "note": "血压偏高",
        "dedupe_token": "wo:health:2026-11-12:1",
    }

    first = await executor.execute(
        tool_name="create_work_order",
        args=args,
        runtime=identity,
        turn_id=None,
        idempotency_key="ik-wo-replay",
    )
    second = await executor.execute(
        tool_name="create_work_order",
        args=args,
        runtime=identity,
        turn_id=None,
        idempotency_key="ik-wo-replay",
    )

    assert first.ok and first.idempotent_replay is False
    assert second.ok and second.idempotent_replay is True
    # 重放的是首次落库的结果（_run_mcp 对未带标记的 server 补 cached=False）
    assert second.data == {"order_id": "wo-9", "created": True, "cached": False}
    assert len(operations.calls) == 1, "同键重放不得二次建单"
    assert len(await _executions(session, "create_work_order")) == 1

    # dedupe_token 原样下发，subject_elder_id 由运行时身份注入
    _, called_args = operations.calls[0]
    assert called_args["dedupe_token"] == "wo:health:2026-11-12:1"
    assert called_args["subject_elder_id"] == identity.elder_id


# ------------------------------------------------------------------ 3. 同键异内容


async def test_same_key_different_content_conflicts_with_audit(
    session: AsyncSession, identity: Identity
) -> None:
    operations = RecordingClient(
        OPERATIONS_URL, MCPResult(ok=True, data={"order_id": "wo-10"}, error=None)
    )
    executor = _executor(session, router=_router(RecordingClient(WEATHER_URL), operations))

    await executor.execute(
        tool_name="create_work_order",
        args={"order_type": "health_alert", "severity": "yellow", "dedupe_token": "wo-10"},
        runtime=identity,
        turn_id=None,
        idempotency_key="ik-wo-conflict",
    )
    conflict = await executor.execute(
        tool_name="create_work_order",
        args={"order_type": "health_alert", "severity": "red", "dedupe_token": "wo-10"},
        runtime=identity,
        turn_id=None,
        idempotency_key="ik-wo-conflict",
    )

    assert conflict.status == "denied"
    assert conflict.error_class == "forbidden"
    assert conflict.message == MESSAGES["forbidden"]
    assert len(operations.calls) == 1, "冲突不得产生第二次副作用"
    audits = await _audits(session, "tool_idempotency_conflict")
    assert len(audits) == 1
    assert audits[0].detail is not None
    assert audits[0].detail["idempotency_key"] == "ik-wo-conflict"


# ------------------------------------------------------------------ 4. MCP 记账


async def test_mcp_success_writes_execution_row(session: AsyncSession, identity: Identity) -> None:
    payload = {"order_id": "wo-11", "status": "pending", "created": True}
    operations = RecordingClient(OPERATIONS_URL, MCPResult(ok=True, data=payload, error=None))
    executor = _executor(session, router=_router(RecordingClient(WEATHER_URL), operations))

    result = await executor.execute(
        tool_name="send_family_notification",
        args={
            "notification_type": "health_alert",
            "payload": {"severity": "yellow"},
            "dedupe_key": "notice:1",
        },
        runtime=identity,
        turn_id=None,
        idempotency_key="ik-fn-1",
    )

    assert result.ok
    rows = await _executions(session, "send_family_notification")
    assert len(rows) == 1, "MCP 副作用调用必须有记账行（此前绕过）"
    assert rows[0].status == "succeeded"
    assert rows[0].finished_at is not None
    assert rows[0].result_json == {**payload, "cached": False}


async def test_mcp_failure_writes_failed_row_with_same_error_class(
    session: AsyncSession, identity: Identity
) -> None:
    operations = RecordingClient(OPERATIONS_URL, MCPResult(ok=False, data=None, error="timeout"))
    executor = _executor(session, router=_router(RecordingClient(WEATHER_URL), operations))

    result = await executor.execute(
        tool_name="create_work_order",
        args={"order_type": "health_alert", "severity": "red", "dedupe_token": "wo-12"},
        runtime=identity,
        turn_id=None,
        idempotency_key="ik-wo-fail",
    )

    assert result.status == "failed"
    assert result.error_class == "transient"
    rows = await _executions(session, "create_work_order")
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_class == "transient", "失败分类仍由 tool_error_class 唯一决定"


async def test_invalid_dedupe_token_rejected_before_mcp(
    session: AsyncSession, identity: Identity
) -> None:
    operations = RecordingClient(OPERATIONS_URL)
    executor = _executor(session, router=_router(RecordingClient(WEATHER_URL), operations))

    result = await executor.execute(
        tool_name="create_work_order",
        args={"order_type": "health_alert", "severity": "red", "dedupe_token": "bad token!"},
        runtime=identity,
        turn_id=None,
        idempotency_key="ik-wo-badtoken",
    )

    assert result.error_class == "bad_args"
    assert operations.calls == [], "参数不合法不得下发到 MCP server"


# ------------------------------------------------------------------ 5. 跨长者越权


async def test_cross_elder_mcp_call_denied_audited_with_trace_id(
    session: AsyncSession, identity: Identity
) -> None:
    other_elder = str(uuid.uuid4())
    operations = RecordingClient(OPERATIONS_URL)
    executor = _executor(session, router=_router(RecordingClient(WEATHER_URL), operations))

    token = set_trace_id("trace-b1-cross-elder")
    try:
        result = await executor.execute(
            tool_name="create_work_order",
            args={
                "subject_elder_id": other_elder,
                "order_type": "health_alert",
                "severity": "red",
                "dedupe_token": "wo-cross",
            },
            runtime=identity,
            turn_id=None,
            idempotency_key="ik-wo-cross",
        )
    finally:
        reset_trace_id(token)

    assert result.status == "denied"
    assert result.error_class == "forbidden"
    assert result.message == MESSAGES["forbidden"]
    assert operations.calls == [], "请求不得到达 MCP server"

    audits = await _audits(session, "tool_cross_elder_attempt")
    assert len(audits) == 1
    assert audits[0].detail is not None
    assert audits[0].detail["claimed"] == other_elder
    assert audits[0].detail["context"] == identity.elder_id
    assert audits[0].detail["trace_id"] == "trace-b1-cross-elder"


# ------------------------------------------------------------------ 6. 护栏回归（MCP 路径）


async def test_ninth_turn_mcp_call_denied_by_budget(
    session: AsyncSession, identity: Identity
) -> None:
    """同轮第 9 次工具调用被拒——9 个不同工具避开同 Tool 熔断，只测预算。"""
    names = [f"mcp_probe_{i}" for i in range(9)]
    registry = ToolRegistry(
        tuple(
            ToolSpec(
                name=name,
                description="测试用 MCP 工具",
                parameters_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                side_effect=True,
                idempotent=True,
                transport="mcp",
            )
            for name in names
        )
    )
    operations = RecordingClient(OPERATIONS_URL)
    guardrails = Guardrails(max_tool_calls=8, repeat_window_s=60, repeat_threshold=3)
    executor = ToolExecutor(
        session, registry=registry, guardrails=guardrails, mcp_client=operations
    )

    results = [
        await executor.execute(
            tool_name=name,
            args={},
            runtime=identity,
            turn_id=None,
            idempotency_key=f"ik-budget-{name}",
        )
        for name in names
    ]

    assert all(r.status == "succeeded" for r in results[:8])
    assert results[8].status == "denied"
    assert results[8].message is not None and "上限" in results[8].message
    assert len(operations.calls) == 8, "第 9 次不得到达 MCP server"
    assert len(await _audits(session, "tool_guardrail_denied")) == 1


async def test_third_same_mcp_tool_call_fused(session: AsyncSession, identity: Identity) -> None:
    """同工具 1 分钟内第 3 次熔断——MCP 路径不得绕过。"""
    operations = RecordingClient(
        OPERATIONS_URL, MCPResult(ok=True, data={"order_id": "wo-fuse"}, error=None)
    )
    guardrails = Guardrails(max_tool_calls=8, repeat_window_s=60, repeat_threshold=3)
    executor = _executor(
        session, router=_router(RecordingClient(WEATHER_URL), operations), guardrails=guardrails
    )
    args = {"order_type": "health_alert", "severity": "yellow", "dedupe_token": "wo-fuse"}

    results = [
        await executor.execute(
            tool_name="create_work_order",
            args=args,
            runtime=identity,
            turn_id=None,
            idempotency_key=f"ik-fuse-{i}",
        )
        for i in range(3)
    ]

    assert results[0].ok and results[1].ok
    assert results[2].status == "denied"
    assert results[2].message is not None and "熔断" in results[2].message
    assert len(operations.calls) == 2, "第 3 次不得到达 MCP server"
