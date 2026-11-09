"""MCP 客户端测试（Day 6 · N25）。

不连真实 MCP server：`ClientSession` 用假实现（monkeypatch 模块属性，
可编程返回 / 延迟 / 异常），`session_factory` 注入内存假流；audit_sink 用
进程内收集器断言审计字段；身份经 `identity_scope` 注入 runtime_context。
覆盖：正常调用 / 超时映射 / 越权拒绝且审计 / bad_response 映射 /
upstream_error 映射（含服务端业务失败载荷保留）/ elder_id 剥离 /
工具发现 / `tool_error_class` 四类失败映射全表 / `DbAuditSink` 失败不外抛。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

import app.mcp_client.client as client_module
from app.core.config import Settings
from app.core.context import get_trace_id, reset_trace_id, set_trace_id
from app.core.runtime_context import Identity, identity_scope
from app.core.security import KIND_DEVICE
from app.mcp_client import (
    ACTION_IDENTITY_OVERRIDE,
    ACTION_TOOL_CALL,
    AuditEntry,
    MCPClient,
    MCPResult,
    tool_error_class,
)

ELDER_A = str(uuid.uuid4())  # 当前会话绑定的长者
ELDER_B = str(uuid.uuid4())  # 他人长者（越权目标）
DEVICE_ID = str(uuid.uuid4())

WEATHER_PAYLOAD: dict[str, Any] = {
    "ok": True,
    "city": "北京",
    "condition": "多云",
    "temperature_c": 18.4,
    "observed_at": "2026-11-09T08:00:00+00:00",
    "cached": False,
}


# ---------------------------------------------------------------------------
# 假 SDK 会话 / 假审计 sink / 装配助手
# ---------------------------------------------------------------------------


class FakeClientSession:
    """可编程假 ClientSession：记录调用入参，按设定返回 / 延迟 / 抛异常。"""

    def __init__(
        self,
        *,
        tools: list[SimpleNamespace] | None = None,
        call_result: CallToolResult | None = None,
        delay_seconds: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.tools = tools or []
        self.call_result = call_result
        self.delay_seconds = delay_seconds
        self.error = error
        self.call_args: list[tuple[str, dict[str, Any] | None]] = []

    async def __aenter__(self) -> FakeClientSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def initialize(self) -> None:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=list(self.tools))

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: float | None = None,
    ) -> CallToolResult:
        self.call_args.append((name, arguments))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        assert self.call_result is not None, "test forgot to set call_result"
        return self.call_result


class AuditCollector:
    """假 audit sink：收集审计记录供断言（实现 AuditSink 协议）。"""

    def __init__(self) -> None:
        self.records: list[AuditEntry] = []

    async def write(self, entry: AuditEntry) -> None:
        self.records.append(entry)


def weather_tool_result(payload: Any) -> CallToolResult:
    """把业务载荷包成 SDK CallToolResult（无 structured_content，走 TextContent JSON）。"""
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        is_error=False,
        structured_content=None,
    )


@asynccontextmanager
async def fake_session_factory(_session: FakeClientSession):
    """假传输工厂：必须是 async 生成器——contextlib 的异步上下文管理器在
    __aenter__ 里对生成器调 anext()，同步生成器会抛 TypeError（被客户端
    归类为 upstream_error，所有走假流的用例都会因此误报）。"""
    yield (None, None)


def make_client(
    monkeypatch: pytest.MonkeyPatch,
    session: FakeClientSession,
    audit: AuditCollector | None = None,
    *,
    timeout_seconds: int = 5,
    probe: list[bool] | None = None,
) -> MCPClient:
    """装配被测客户端：假 session + 假流工厂 + 收集器 sink。"""
    settings = Settings(mcp_call_timeout_seconds=timeout_seconds)
    client = MCPClient(
        settings,
        server_url="http://localhost:7101",
        audit_sink=audit,
    )
    sentinel = probe if probe is not None else []

    def factory() -> Any:
        sentinel.append(True)
        return fake_session_factory(session)

    monkeypatch.setattr(client, "_session_factory", factory)
    monkeypatch.setattr(client_module, "ClientSession", lambda *args, **kwargs: session)
    return client


class TraceContext:
    """测试内设置/复位 trace_id 与身份，防止 contextvar 跨用例串号。"""

    def __enter__(self) -> str:
        self._trace_token = set_trace_id("trace" + uuid.uuid4().hex)
        return get_trace_id() or ""

    def __exit__(self, *exc_info: object) -> None:
        assert self._trace_token is not None
        reset_trace_id(self._trace_token)


def with_elder_identity() -> Identity:
    """device 身份（绑定 ELDER_A），配 identity_scope 使用。"""
    return Identity(kind=KIND_DEVICE, device_id=DEVICE_ID, elder_id=ELDER_A)


# ---------------------------------------------------------------------------
# 场景 1：正常调用 + 身份字段剥离
# ---------------------------------------------------------------------------


async def test_successful_call_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeClientSession(call_result=weather_tool_result(WEATHER_PAYLOAD))
    audit = AuditCollector()

    with TraceContext(), identity_scope(with_elder_identity()):
        client = make_client(monkeypatch, session, audit)
        result = await client.call_tool("get_weather", {"city": "北京"})

    assert result.ok is True
    assert result.error is None
    assert result.data == WEATHER_PAYLOAD
    assert session.call_args == [("get_weather", {"city": "北京"})]

    assert len(audit.records) == 1
    entry = audit.records[0]
    assert entry.action == ACTION_TOOL_CALL
    assert entry.tool_name == "get_weather"
    assert entry.outcome == "ok"
    assert entry.latency_ms >= 0


async def test_matching_elder_id_passes_but_is_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """与当前身份一致的 elder_id 放行，但仍不透传（服务端上下文注入）。"""
    session = FakeClientSession(call_result=weather_tool_result(WEATHER_PAYLOAD))
    audit = AuditCollector()

    with TraceContext(), identity_scope(with_elder_identity()):
        client = make_client(monkeypatch, session, audit)
        result = await client.call_tool("get_weather", {"city": "北京", "elder_id": ELDER_A})

    assert result.ok is True
    assert session.call_args == [("get_weather", {"city": "北京"})]
    assert audit.records[0].action == ACTION_TOOL_CALL


# ---------------------------------------------------------------------------
# 场景 2：越权拒绝 + 审计（闸门③）
# ---------------------------------------------------------------------------


async def test_other_elder_id_rejected_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe: list[bool] = []
    session = FakeClientSession(call_result=weather_tool_result(WEATHER_PAYLOAD))
    audit = AuditCollector()

    with TraceContext(), identity_scope(with_elder_identity()):
        client = make_client(monkeypatch, session, audit, probe=probe)
        result = await client.call_tool("get_weather", {"city": "北京", "elder_id": ELDER_B})

    assert result.ok is False
    assert result.data is None
    assert result.error == "unauthorized"
    # 越权在闸门③短路：根本不许建连、不许到达 server
    assert probe == []
    assert session.call_args == []

    assert len(audit.records) == 1
    entry = audit.records[0]
    assert entry.action == ACTION_IDENTITY_OVERRIDE
    assert entry.tool_name == "get_weather"
    assert entry.outcome == "unauthorized"
    assert entry.detail == {
        "claimed_elder_id": ELDER_B,
        "expected_elder_id": ELDER_A,
    }


async def test_elder_id_without_runtime_identity_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读不到身份时，对携带 elder_id 的调用一律拒绝（不得采信传入值）。"""
    session = FakeClientSession(call_result=weather_tool_result(WEATHER_PAYLOAD))
    audit = AuditCollector()

    with TraceContext():  # 故意不注入身份
        client = make_client(monkeypatch, session, audit)
        result = await client.call_tool("get_weather", {"city": "北京", "elder_id": ELDER_A})

    assert result.error == "unauthorized"
    assert session.call_args == []
    assert audit.records[0].action == ACTION_IDENTITY_OVERRIDE
    assert audit.records[0].detail == {
        "claimed_elder_id": ELDER_A,
        "expected_elder_id": None,
    }


# ---------------------------------------------------------------------------
# 场景 3：超时 / 上游失败 / 协议形态异常
# ---------------------------------------------------------------------------


async def test_timeout_maps_to_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeClientSession(call_result=weather_tool_result(WEATHER_PAYLOAD), delay_seconds=0.2)
    audit = AuditCollector()
    # Settings 超时为 int；0 秒 + 假会话 0.2s 延迟 → 必超时（不真等 5s）
    with TraceContext():
        client = make_client(monkeypatch, session, audit, timeout_seconds=0)
        result = await client.call_tool("get_weather", {"city": "北京"})

    assert result.ok is False
    assert result.data is None
    assert result.error == "timeout"
    assert audit.records[0].outcome == "timeout"


async def test_server_business_failure_maps_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务端 ok:false（无 key / 上游失败）→ upstream_error，结构化载荷保留。"""
    payload = {"ok": False, "error": "missing_api_key", "message": "这个我暂时查不到"}
    session = FakeClientSession(call_result=weather_tool_result(payload))
    audit = AuditCollector()

    with TraceContext():
        client = make_client(monkeypatch, session, audit)
        result = await client.call_tool("get_weather", {"city": "北京"})

    assert result.ok is False
    assert result.error == "upstream_error"
    assert result.data == payload  # 供 tool_error_class 细化与话术拼接
    assert audit.records[0].outcome == "upstream_error"


async def test_server_iserror_without_payload_maps_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeClientSession(
        call_result=CallToolResult(content=[], is_error=True, structured_content=None)
    )

    with TraceContext():
        client = make_client(monkeypatch, session)
        result = await client.call_tool("get_weather", {"city": "北京"})

    assert result.ok is False
    assert result.data is None
    assert result.error == "upstream_error"


async def test_unresolvable_payload_maps_bad_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_error=False 且取不出任何载荷 → 协议形态异常 bad_response。"""
    session = FakeClientSession(
        call_result=CallToolResult(content=[], is_error=False, structured_content=None)
    )

    with TraceContext():
        client = make_client(monkeypatch, session)
        result = await client.call_tool("get_weather", {"city": "北京"})

    assert result.ok is False
    assert result.error == "bad_response"


async def test_non_dict_payload_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 dict 成功载荷包一层，保持 data 恒为 dict 的调用方假设。"""
    session = FakeClientSession(call_result=weather_tool_result("plain text"))

    with TraceContext():
        client = make_client(monkeypatch, session)
        result = await client.call_tool("get_weather", {"city": "北京"})

    assert result.ok is True
    assert result.data == {"result": "plain text"}


async def test_unexpected_result_type_maps_bad_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 CallToolResult 返回（HITL elicitation 等）→ bad_response。"""
    session = FakeClientSession()

    def factory() -> Any:
        return fake_session_factory(session)

    settings = Settings(mcp_call_timeout_seconds=5)
    client = MCPClient(
        settings, server_url="http://localhost:7101", audit_sink=client_module._NullAuditSink()
    )
    monkeypatch.setattr(client, "_session_factory", factory)
    monkeypatch.setattr(client_module, "ClientSession", lambda *a, **kw: session)
    # 让 _invoke 拿到非 CallToolResult：假会话的 call_tool 返回值直接换成 dict
    monkeypatch.setattr(session, "call_tool", _fake_call_tool_plain)

    with TraceContext():
        result = await client.call_tool("get_weather", {"city": "北京"})

    assert result.ok is False
    assert result.error == "bad_response"


async def _fake_call_tool_plain(*args: Any, **kwargs: Any) -> Any:
    """返回普通 dict（非 CallToolResult），触发 _invoke 的类型防线。"""
    return {"unexpected": "shape"}


# ---------------------------------------------------------------------------
# 场景 4：工具发现
# ---------------------------------------------------------------------------


async def test_list_tools_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeClientSession(
        tools=[SimpleNamespace(name="get_weather", description="查询城市当前天气")]
    )

    with TraceContext():
        client = make_client(monkeypatch, session)
        result = await client.list_tools()

    assert result.ok is True
    assert result.error is None
    assert result.data == {"tools": [{"name": "get_weather", "description": "查询城市当前天气"}]}


async def test_list_tools_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeClientSession(tools=[], delay_seconds=0.2)

    with TraceContext():
        client = make_client(monkeypatch, session, timeout_seconds=0)
        result = await client.list_tools()

    assert result.ok is False
    assert result.error == "timeout"


async def test_list_tools_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeClientSession(tools=[], error=RuntimeError("connection refused"))

    with TraceContext():
        client = make_client(monkeypatch, session)
        result = await client.list_tools()

    assert result.ok is False
    assert result.error == "upstream_error"


# ---------------------------------------------------------------------------
# 场景 5：四类失败映射（tool_error_class 唯一口径）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (MCPResult(ok=True, data=WEATHER_PAYLOAD, error=None), None),
        (MCPResult(ok=False, data=None, error="timeout"), "transient"),
        (MCPResult(ok=False, data=None, error="bad_response"), "transient"),
        (MCPResult(ok=False, data=None, error="unauthorized"), "forbidden"),
        # 服务端业务失败：bad_request 细化为 bad_args，其余临时错误
        (
            MCPResult(
                ok=False,
                data={"ok": False, "error": "bad_request", "message": "城市名不太对"},
                error="upstream_error",
            ),
            "bad_args",
        ),
        (
            MCPResult(
                ok=False,
                data={"ok": False, "error": "upstream_timeout", "message": "查不到"},
                error="upstream_error",
            ),
            "transient",
        ),
    ],
)
def test_tool_error_class_mapping(result: MCPResult, expected: str | None) -> None:
    assert tool_error_class(result) == expected


# ---------------------------------------------------------------------------
# 场景 6：默认审计 sink（DbAuditSink）失败不外抛；审计可关闭
# ---------------------------------------------------------------------------


async def test_db_audit_sink_swallows_write_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """record_audit 链路任何失败都只告警，不抛给工具调用方。"""

    @asynccontextmanager
    async def broken_factory() -> Any:
        raise RuntimeError("db down")
        yield  # pragma: no cover - 使其成为 async 上下文管理器

    monkeypatch.setattr(client_module, "session_factory", broken_factory)
    sink = client_module.DbAuditSink()

    await sink.write(
        AuditEntry(
            action=ACTION_TOOL_CALL,
            tool_name="get_weather",
            latency_ms=1,
            outcome="ok",
        )
    )  # 不抛即通过


async def test_default_sink_is_db_and_injection_replaces_it() -> None:
    settings = Settings(mcp_call_timeout_seconds=5)
    client = MCPClient(settings, server_url="http://localhost:7101")
    assert isinstance(client._audit_sink, client_module.DbAuditSink)

    injected = client_module._NullAuditSink()
    replaced = MCPClient(settings, server_url="http://localhost:7101", audit_sink=injected)
    assert replaced._audit_sink is injected
