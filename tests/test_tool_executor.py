"""ToolExecutor 契约测试（N12 / 计划 V9）。

七条判据：
1. 模型自报身份（`args.elder_id`）被丢弃，且留 `tool_identity_override_attempt` 审计；
2. 未注册工具 → `denied`，不执行；
3. 四类失败各一条：`bad_args` / `not_found` / `transient`（含退避重试）/ `forbidden`；
4. 幂等：同键同参第二次 `idempotent_replay=True` 且执行器只跑一次；
5. 同键异参 → `denied` + `tool_idempotency_conflict` 审计（不允许静默重复副作用）；
6. 护栏预算：`Guardrails` 拒绝时返回 `denied` 并带可读原因；
7. 已注册未绑定 → `not_found`（不编造、不回退）。

DB 隔离沿用仓库写法（`db_engine` + savepoint），用例结束整体回滚。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents.guardrails import Guardrails
from app.agents.tool_registry import ToolRegistry, ToolSpec
from app.core.runtime_context import Identity
from app.mcp_client.client import MCPResult
from app.models import AuditLog
from app.services.tool_executor import (
    MESSAGES,
    ToolArgumentError,
    ToolExecutor,
    ToolForbiddenError,
    ToolNotFoundError,
    ToolTransientError,
)


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    try:
        conn = await db_engine.connect()
    except Exception as exc:
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")

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
        device_id="11111111-1111-1111-1111-111111111111",
        elder_id="22222222-2222-2222-2222-222222222222",
    )


def _spec(name: str = "probe", *, side_effect: bool = False) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="测试用工具",
        parameters_schema={
            "type": "object",
            "properties": {"city": {"type": "string", "minLength": 1}},
            "required": ["city"],
            "additionalProperties": False,
        },
        side_effect=side_effect,
        idempotent=True,
    )


async def _audits(session: AsyncSession, action: str) -> list[AuditLog]:
    rows = (await session.execute(select(AuditLog).where(AuditLog.action == action))).scalars()
    return list(rows)


# ------------------------------------------------------------------ 1. 身份消毒


async def test_identity_in_args_is_dropped_and_audited(
    session: AsyncSession, identity: Identity
) -> None:
    seen: dict[str, Any] = {}

    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        seen.update(args)
        seen["_runtime_elder"] = runtime.elder_id
        return {"ok": True}

    registry = ToolRegistry((_spec(),))
    registry.bind("probe", _executor)
    executor = ToolExecutor(session, registry=registry)

    result = await executor.execute(
        tool_name="probe",
        args={"city": "杭州", "elder_id": "99999999-9999-9999-9999-999999999999"},
        runtime=identity,
        turn_id=None,
        idempotency_key="k-1",
    )

    assert result.status == "succeeded"
    assert "elder_id" not in seen, "模型自报的 elder_id 不得进入工具实现"
    assert seen["_runtime_elder"] == identity.elder_id, "身份只能来自 runtime"
    audits = await _audits(session, "tool_identity_override_attempt")
    assert len(audits) == 1
    assert audits[0].detail is not None and audits[0].detail["dropped_keys"] == ["elder_id"]


# ------------------------------------------------------------------ 2. 白名单


async def test_unregistered_tool_is_denied(session: AsyncSession, identity: Identity) -> None:
    executor = ToolExecutor(session, registry=ToolRegistry())
    result = await executor.execute(
        tool_name="delete_everything",
        args={},
        runtime=identity,
        turn_id=None,
        idempotency_key="k-2",
    )

    assert result.status == "denied"
    assert result.message == MESSAGES["not_found"]
    assert len(await _audits(session, "tool_not_registered")) == 1


async def test_registered_but_unwired_tool_is_not_found(
    session: AsyncSession, identity: Identity
) -> None:
    from app.agents.tool_registry import default_registry

    registry = default_registry()  # get_weather 尚无实现（等 dev-C 的 N25）
    executor = ToolExecutor(session, registry=registry)
    result = await executor.execute(
        tool_name="get_weather",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="k-3",
    )

    assert result.status == "failed"
    assert result.error_class == "not_found"
    assert "查不到" in (result.message or "")


# ------------------------------------------------------------------ 3. 四类失败


async def test_bad_args_from_schema(session: AsyncSession, identity: Identity) -> None:
    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        return {"never": "called"}

    registry = ToolRegistry((_spec(),))
    registry.bind("probe", _executor)
    executor = ToolExecutor(session, registry=registry)

    missing = await executor.execute(
        tool_name="probe", args={}, runtime=identity, turn_id=None, idempotency_key="k-4"
    )
    extra = await executor.execute(
        tool_name="probe",
        args={"city": "杭州", "mood": "好"},
        runtime=identity,
        turn_id=None,
        idempotency_key="k-5",
    )
    wrong_type = await executor.execute(
        tool_name="probe", args={"city": 42}, runtime=identity, turn_id=None, idempotency_key="k-6"
    )

    for result in (missing, extra, wrong_type):
        assert result.status == "failed"
        assert result.error_class == "bad_args"
        assert result.message is not None and MESSAGES["bad_args"] in result.message


async def test_not_found_maps_to_message(session: AsyncSession, identity: Identity) -> None:
    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        raise ToolNotFoundError("no such plan")

    registry = ToolRegistry((_spec(side_effect=True),))
    registry.bind("probe", _executor)
    executor = ToolExecutor(session, registry=registry)

    result = await executor.execute(
        tool_name="probe",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="k-7",
    )
    assert result.status == "failed"
    assert result.error_class == "not_found"


async def test_transient_retries_then_succeeds(session: AsyncSession, identity: Identity) -> None:
    calls = {"n": 0}

    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ToolTransientError("upstream 502")
        return {"city": args["city"], "temp": 18}

    registry = ToolRegistry((_spec(),))
    registry.bind("probe", _executor)
    executor = ToolExecutor(session, registry=registry, retry_backoff_seconds=0.0)

    result = await executor.execute(
        tool_name="probe",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="k-8",
    )
    assert result.ok and calls["n"] == 3, "transient 必须退避重试（≤3 次尝试）"


async def test_transient_gives_up_with_readable_message(
    session: AsyncSession, identity: Identity
) -> None:
    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        raise ToolTransientError("upstream timeout")

    registry = ToolRegistry((_spec(),))
    registry.bind("probe", _executor)
    executor = ToolExecutor(session, registry=registry, retry_backoff_seconds=0.0)

    result = await executor.execute(
        tool_name="probe",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="k-9",
    )
    assert result.status == "failed"
    assert result.error_class == "transient"
    assert result.message == MESSAGES["transient"]


async def test_forbidden_is_denied_and_audited(session: AsyncSession, identity: Identity) -> None:
    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        raise ToolForbiddenError("other elder")

    registry = ToolRegistry((_spec(side_effect=True),))
    registry.bind("probe", _executor)
    executor = ToolExecutor(session, registry=registry)

    result = await executor.execute(
        tool_name="probe",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="k-10",
    )
    assert result.status == "denied"
    assert result.error_class == "forbidden"
    assert len(await _audits(session, "tool_failed")) == 1


async def test_argument_error_from_executor_is_bad_args(
    session: AsyncSession, identity: Identity
) -> None:
    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        raise ToolArgumentError("city unknown")

    registry = ToolRegistry((_spec(),))
    registry.bind("probe", _executor)
    executor = ToolExecutor(session, registry=registry)

    result = await executor.execute(
        tool_name="probe",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="k-11",
    )
    assert result.error_class == "bad_args"


# ------------------------------------------------------------------ 4–5. 幂等


async def test_same_key_same_args_replays(session: AsyncSession, identity: Identity) -> None:
    calls = {"n": 0}

    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        calls["n"] += 1
        return {"prescription_id": "rx-1"}

    registry = ToolRegistry((_spec(side_effect=True),))
    registry.bind("probe", _executor)
    executor = ToolExecutor(session, registry=registry)

    first = await executor.execute(
        tool_name="probe",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="same",
    )
    second = await executor.execute(
        tool_name="probe",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="same",
    )

    assert first.ok and first.idempotent_replay is False
    assert second.ok and second.idempotent_replay is True
    assert second.data == {"prescription_id": "rx-1"}
    assert calls["n"] == 1, "副作用工具同键同参只能真正执行一次"


async def test_same_key_different_args_is_denied(session: AsyncSession, identity: Identity) -> None:
    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        return {"ok": True}

    registry = ToolRegistry((_spec(side_effect=True),))
    registry.bind("probe", _executor)
    executor = ToolExecutor(session, registry=registry)

    await executor.execute(
        tool_name="probe",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="dup",
    )
    conflict = await executor.execute(
        tool_name="probe",
        args={"city": "苏州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="dup",
    )

    assert conflict.status == "denied"
    assert conflict.error_class == "forbidden"
    assert len(await _audits(session, "tool_idempotency_conflict")) == 1


# ------------------------------------------------------------------ 6. 护栏


async def test_guardrail_budget_denies(session: AsyncSession, identity: Identity) -> None:
    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        return {"ok": True}

    registry = ToolRegistry((_spec(name="a"), _spec(name="b")))
    registry.bind("a", _executor)
    registry.bind("b", _executor)
    guardrails = Guardrails(max_tool_calls=1, repeat_window_s=60, repeat_threshold=3)
    executor = ToolExecutor(session, registry=registry, guardrails=guardrails)

    first = await executor.execute(
        tool_name="a", args={"city": "杭州"}, runtime=identity, turn_id=None, idempotency_key="g1"
    )
    second = await executor.execute(
        tool_name="b", args={"city": "杭州"}, runtime=identity, turn_id=None, idempotency_key="g2"
    )

    assert first.ok
    assert second.status == "denied"
    assert second.message is not None and "上限" in second.message
    assert len(await _audits(session, "tool_guardrail_denied")) == 1


# ------------------------------------------------------------------ 7. 记账


async def test_side_effect_writes_execution_row(session: AsyncSession, identity: Identity) -> None:
    from app.models.conversation import ToolExecution

    async def _executor(args: dict[str, Any], runtime: Identity) -> dict[str, Any]:
        return {"done": True}

    registry = ToolRegistry((_spec(side_effect=True),))
    registry.bind("probe", _executor)
    executor = ToolExecutor(session, registry=registry)

    await executor.execute(
        tool_name="probe",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key=str(uuid.uuid4()),
    )

    row = (await session.execute(select(ToolExecution))).scalar_one()
    assert row.status == "succeeded"
    assert row.finished_at is not None
    assert row.result_json == {"done": True}


# ------------------------------------------------------------------ 8. MCP 分支（N26）


class _FakeMCP:
    """MCP 客户端替身：只实现 `call_tool`，并记录被调参数。"""

    def __init__(self, result: MCPResult | Exception) -> None:
        self._result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, args: dict[str, Any]) -> MCPResult:
        self.calls.append((tool_name, args))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _weather_executor(session: AsyncSession, client: _FakeMCP | None) -> ToolExecutor:
    from app.agents.tool_registry import default_registry

    return ToolExecutor(session, registry=default_registry(), mcp_client=client)


async def test_mcp_success_passes_through_cached_and_observed_at(
    session: AsyncSession, identity: Identity
) -> None:
    """成功时 `cached` / `observed_at` 必须原样透传——Agent 要据此播「数据时间」。"""
    payload = {
        "ok": True,
        "city": "杭州",
        "condition": "晴",
        "temperature_c": 18.5,
        "observed_at": "2026-11-09T08:12:00+00:00",
        "cached": True,
    }
    client = _FakeMCP(MCPResult(ok=True, data=payload, error=None))
    executor = _weather_executor(session, client)

    result = await executor.execute(
        tool_name="get_weather",
        args={"city": "杭州", "elder_id": "99999999-9999-9999-9999-999999999999"},
        runtime=identity,
        turn_id=None,
        idempotency_key="mcp-1",
    )

    assert result.ok
    assert result.data == payload, "成功载荷不得被改写（cached/observed_at 尤其）"
    assert client.calls == [("get_weather", {"city": "杭州"})], "身份字段应先被剥离再下发"


async def test_mcp_timeout_maps_to_transient(session: AsyncSession, identity: Identity) -> None:
    client = _FakeMCP(MCPResult(ok=False, data=None, error="timeout"))
    executor = _weather_executor(session, client)

    result = await executor.execute(
        tool_name="get_weather",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="mcp-2",
    )
    assert result.status == "failed"
    assert result.error_class == "transient"
    assert result.message == MESSAGES["transient"], "必须给可播报的降级话术，不许编造天气"


async def test_mcp_unauthorized_maps_to_forbidden(
    session: AsyncSession, identity: Identity
) -> None:
    client = _FakeMCP(MCPResult(ok=False, data=None, error="unauthorized"))
    executor = _weather_executor(session, client)

    result = await executor.execute(
        tool_name="get_weather",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="mcp-3",
    )
    assert result.status == "denied"
    assert result.error_class == "forbidden"


async def test_mcp_bad_request_refines_to_bad_args(
    session: AsyncSession, identity: Identity
) -> None:
    """`bad_request` 是参数错，应由 tool_error_class 细化为 bad_args，而不是 transient。"""
    client = _FakeMCP(
        MCPResult(
            ok=False,
            data={"ok": False, "error": "bad_request", "message": "城市无法解析"},
            error="upstream_error",
        )
    )
    executor = _weather_executor(session, client)

    result = await executor.execute(
        tool_name="get_weather",
        args={"city": "火星"},
        runtime=identity,
        turn_id=None,
        idempotency_key="mcp-4",
    )
    assert result.error_class == "bad_args"


async def test_mcp_client_crash_is_transient(session: AsyncSession, identity: Identity) -> None:
    client = _FakeMCP(ConnectionError("connection refused"))
    executor = _weather_executor(session, client)

    result = await executor.execute(
        tool_name="get_weather",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="mcp-5",
    )
    assert result.error_class == "transient"
    assert result.message == MESSAGES["transient"]


async def test_mcp_tool_without_client_is_not_found(
    session: AsyncSession, identity: Identity
) -> None:
    executor = _weather_executor(session, None)
    result = await executor.execute(
        tool_name="get_weather",
        args={"city": "杭州"},
        runtime=identity,
        turn_id=None,
        idempotency_key="mcp-6",
    )
    assert result.status == "failed"
    assert result.error_class == "not_found"


def test_mcp_tool_cannot_bind_local_executor() -> None:
    """MCP 工具不允许再绑本地实现：两条执行路径会让失败分类各说各话。"""
    from app.agents.tool_registry import default_registry

    registry = default_registry()

    async def _nope(args: dict[str, Any], runtime: Any) -> dict[str, Any]:
        return {}

    with pytest.raises(ValueError):
        registry.bind("get_weather", _nope)
