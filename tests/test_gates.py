"""Day 9 A2 · 三道隔离闸门攻击用例（AC-18 / AC-24 / 闸门①③）。

仿照 tests/test_auth_attacks.py 的装配：真 JWT 密钥、真 create_app()、真 DB
（外层事务回滚隔离），只覆盖 `get_session`。mock 掉任何一层都会让
「拦下来了」变成「假拦」。

覆盖矩阵（任务书 A2.2 / 计划 §7.3）：

| 闸门 | 用例 | 断言 |
| --- | --- | --- |
| ① 会话 | 伪造 thread_id 形状 / 跨设备归属 / 正常归属 | 格式层抛错；DB 层 False；正常 True |
| ② 记忆 | （B2 修复中，M5.1 复验，本文件不覆盖） | — |
| ③ 工具 | 模型自报 elder_id / 无身份调用 | 丢弃＋审计＋runtime 注入；IdentityNotSetError |
| AC-18 | 护工/家属读范围外长者详情 | 403 SCOPE_FORBIDDEN ＋ audit_logs ＋ trace_id |
| AC-24 | 员工停用 / 设备解绑后旧凭证重放 | 401 TOKEN_REVOKED |
| 正常路径 | 护工读在管长者 / admin 全量 | 200 |

闸门①的「403 ＋ 审计」完整闭环依赖 REST/WS 入口接线（现状零入口携带
thread_id，identity_service.validate_thread_belongs_to_device 未被业务模块
调用）：接线由 X1 回执帧（dev-B/D）落地，届时本文件补入口级用例（登记
计划 §8 执行记录）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.context import reset_trace_id, set_trace_id
from app.core.runtime_context import (
    Identity,
    IdentityConstructionError,
    IdentityNotSetError,
    ThreadFormatError,
    is_generic_thread_id,
    parse_elder_thread_id,
    require_identity,
    thread_matches_device_identity,
)
from app.core.security import sha256_hex, sign_staff_access
from app.db import get_session
from app.main import create_app
from app.models import (
    AuditLog,
    ConversationThread,
    Device,
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    Room,
    StaffAccount,
)
from app.services.identity_service import validate_thread_belongs_to_device
from app.services.tool_executor import ToolExecutor, ToolRegistry, ToolSpec

ROOM_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
ELDER_A = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
ELDER_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
DEVICE_A = uuid.UUID("cccccccc-0000-0000-0000-000000000001")
DEVICE_B = uuid.UUID("cccccccc-0000-0000-0000-000000000002")
STAFF_ADMIN = uuid.UUID("dddddddd-0000-0000-0000-000000000001")
STAFF_CARE = uuid.UUID("dddddddd-0000-0000-0000-000000000002")
GUARDIAN_ID = uuid.UUID("eeeeeeee-0000-0000-0000-000000000001")

DEVICE_SECRET_A = "device-a-long-term-secret"
DEVICE_SECRET_B = "device-b-long-term-secret"

TEST_JWT_SECRET = "gates-suite-secret-0123456789abcdef"

THREAD_A = f"{ELDER_A}:1"  # 服务端 _open_thread() 拼接形状（identity_service.py:143）
THREAD_B = f"{ELDER_B}:1"


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with db_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            await _seed(session)
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    application: FastAPI = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as ac:
        yield ac
    application.dependency_overrides.clear()


async def _seed(session: AsyncSession) -> None:
    session.add_all(
        [
            Room(room_id=ROOM_ID, room_no="A-301"),
            Elder(elder_id=ELDER_A, name="张桂芬"),
            Elder(elder_id=ELDER_B, name="李国强"),
            StaffAccount(
                staff_id=STAFF_ADMIN,
                name="管理员",
                role="admin",
                phone="13700000001",
                password_hash="unused-placeholder",
                token_version=0,
                status="active",
            ),
            StaffAccount(
                staff_id=STAFF_CARE,
                name="护工",
                role="care_worker",
                phone="13700000002",
                password_hash="unused-placeholder",
                token_version=0,
                status="active",
            ),
            GuardianAccount(
                guardian_id=GUARDIAN_ID,
                name="家属",
                phone="13700000003",
                password_hash="unused-placeholder",
                token_version=0,
                status="active",
            ),
        ]
    )
    # 分批 flush（auth_attacks 实测教训：模型间无 relationship()，同一 flush 的
    # 表依赖排序不可靠——主体→关系→设备→线程逐批落库）
    await session.flush()
    session.add_all(
        [
            # AC-18 范围：护工只管 A；家属只绑 A
            ElderStaffAssignment(elder_id=ELDER_A, staff_id=STAFF_CARE, relation="primary_care"),
            ElderGuardian(
                elder_id=ELDER_A, guardian_id=GUARDIAN_ID, relation="女儿", status="active"
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            Device(
                device_id=DEVICE_A,
                room_id=ROOM_ID,
                elder_id=ELDER_A,
                token_hash=sha256_hex(DEVICE_SECRET_A),
                status="online",
            ),
            Device(
                device_id=DEVICE_B,
                room_id=ROOM_ID,
                elder_id=ELDER_B,
                token_hash=sha256_hex(DEVICE_SECRET_B),
                status="online",
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            # 闸门①：两台设备各有一条服务端生成的长者会话线程
            ConversationThread(
                thread_id=THREAD_A,
                elder_id=ELDER_A,
                device_id=DEVICE_A,
                identity_source="voice",
                identity_confidence=None,
                last_event_seq=0,
            ),
            ConversationThread(
                thread_id=THREAD_B,
                elder_id=ELDER_B,
                device_id=DEVICE_B,
                identity_source="voice",
                identity_confidence=None,
                last_event_seq=0,
            ),
        ]
    )
    await session.commit()


# ------------------------------------------------------------------ helpers


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _care_token() -> str:
    return sign_staff_access(kind="staff", staff_id=STAFF_CARE, role="care_worker", token_version=0)


def _admin_token() -> str:
    return sign_staff_access(kind="staff", staff_id=STAFF_ADMIN, role="admin", token_version=0)


def _guardian_token() -> str:
    return sign_staff_access(kind="guardian", staff_id=GUARDIAN_ID, role="", token_version=0)


def _device_identity(device_id: uuid.UUID, elder_id: uuid.UUID) -> Identity:
    """设备身份：elder_id 由服务端从 devices 表读出注入（契约 §5.3），测试直构。"""
    return Identity(kind="device", device_id=device_id, elder_id=elder_id)


async def _device_token(client: AsyncClient, device_id: uuid.UUID, secret: str) -> str:
    resp = await client.post(
        "/api/device-sessions",
        json={"device_id": str(device_id), "device_secret": secret},
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


async def _audit_rows(session: AsyncSession, action: str) -> list[AuditLog]:
    rows = (await session.execute(select(AuditLog).where(AuditLog.action == action))).scalars()
    return list(rows)


# ------------------------------------------------------------------ 闸门① 会话隔离


def test_gate1_format_layer_rejects_forged_shapes() -> None:
    """非服务端形状在纯代码层即死，不进 DB（第一道）。"""
    with pytest.raises(ThreadFormatError):
        parse_elder_thread_id("")
    with pytest.raises(ThreadFormatError):
        parse_elder_thread_id("completely-forged")  # 客户端随手编
    with pytest.raises(ThreadFormatError):
        parse_elder_thread_id("12345")  # 既不是 uuid:seq 也不是 32hex
    with pytest.raises(ThreadFormatError):
        parse_elder_thread_id(f"{ELDER_A}:not-a-seq")  # 冒号形但 seq 非 numeric
    with pytest.raises(ThreadFormatError):
        parse_elder_thread_id(f"{ELDER_A}:-1")  # 负序号
    # generic（未绑定长者）线程不允许声称「属于某长者会话」
    generic = uuid.uuid4().hex
    assert is_generic_thread_id(generic)
    with pytest.raises(ThreadFormatError):
        parse_elder_thread_id(generic)


def test_gate1_format_layer_parses_server_shape() -> None:
    elder, seq = parse_elder_thread_id(THREAD_A)
    assert elder == str(ELDER_A)
    assert seq == 1


def test_gate1_pure_identity_match_rejects_cross_elder_and_staff() -> None:
    """纯代码层：thread 的 elder 维度必须与身份携带的 elder_id 一致。"""
    device_a = _device_identity(DEVICE_A, ELDER_A)
    assert thread_matches_device_identity(THREAD_A, device_a) is True
    assert thread_matches_device_identity(THREAD_B, device_a) is False  # 跨长者
    assert thread_matches_device_identity("forged-thread", device_a) is False  # 伪造形状
    # staff / guardian 没有 elder 维度：一律 False（范围授权在服务层）
    staff = Identity(kind="staff", staff_id=STAFF_CARE, role="care_worker")
    assert thread_matches_device_identity(THREAD_A, staff) is False
    # 未绑定长者的设备身份没有 elder_id
    unbound = Identity(kind="device", device_id=DEVICE_A)
    assert thread_matches_device_identity(THREAD_A, unbound) is False


async def test_gate1_db_layer_ownership(client: AsyncClient, db_session: AsyncSession) -> None:
    """DB 级权威裁决：thread 归属按 (thread_id, device_id) 判。"""
    # 本人 thread：True
    assert await validate_thread_belongs_to_device(
        db_session, thread_id=THREAD_A, device_id=DEVICE_A
    )
    # 跨设备拿别人的 thread：False（REST 层映射 403）
    assert not await validate_thread_belongs_to_device(
        db_session, thread_id=THREAD_B, device_id=DEVICE_A
    )
    assert not await validate_thread_belongs_to_device(
        db_session, thread_id=THREAD_A, device_id=DEVICE_B
    )
    # 库里不存在的 thread（伪造但形状合法）：False
    forged = f"{ELDER_A}:999"
    assert not await validate_thread_belongs_to_device(
        db_session, thread_id=forged, device_id=DEVICE_A
    )


# ------------------------------------------------------------------ 闸门③ 工具身份


def test_gate3_no_identity_raises_identity_not_set() -> None:
    """无身份上下文读身份 → IdentityNotSetError（闸门③正向：无身份 == 无权限）。"""
    with pytest.raises(IdentityNotSetError):
        require_identity()
    # dict 冒充身份在 set_identity 处被硬拦
    from app.core.runtime_context import set_identity

    with pytest.raises(IdentityConstructionError):
        set_identity({"kind": "device"})  # type: ignore[arg-type]


async def test_gate3_model_supplied_elder_id_dropped_and_audited(
    db_session: AsyncSession,
) -> None:
    """模型自报 elder_id=B：丢弃＋审计＋runtime 注入（AC-39①，真 DB 审计落库）。"""
    token = set_trace_id("trace-gate3-x")
    try:
        seen: dict[str, object] = {}

        async def _executor(args: dict[str, object], runtime: Identity) -> dict[str, object]:
            seen.update(args)
            seen["_runtime_elder"] = runtime.elder_id
            return {"ok": True}

        spec = ToolSpec(
            name="probe",
            description="测试用工具",
            parameters_schema={
                "type": "object",
                "properties": {"city": {"type": "string", "minLength": 1}},
                "required": ["city"],
                "additionalProperties": False,
            },
            side_effect=False,
            idempotent=True,
        )
        registry = ToolRegistry((spec,))
        registry.bind("probe", _executor)
        executor = ToolExecutor(db_session, registry=registry)

        identity = _device_identity(DEVICE_A, ELDER_A)
        result = await executor.execute(
            tool_name="probe",
            args={"city": "杭州", "elder_id": str(ELDER_B)},  # 模型自报 B 的身份
            runtime=identity,
            turn_id=None,
            idempotency_key="gate3-k1",
        )

        assert result.status == "succeeded"
        assert "elder_id" not in seen, "模型自报的 elder_id 不得进入工具实现"
        assert seen["_runtime_elder"] == str(ELDER_A), "身份只能来自 runtime"
        audits = await _audit_rows(db_session, "tool_identity_override_attempt")
        assert len(audits) == 1
        assert audits[0].detail is not None
        assert audits[0].detail["dropped_keys"] == ["elder_id"]
        assert audits[0].detail["trace_id"] == "trace-gate3-x", "审计必须带 trace_id 串联"
    finally:
        reset_trace_id(token)


# ------------------------------------------------------------------ AC-18 范围授权


async def test_ac18_care_worker_out_of_scope_detail_403_and_audited(
    client: AsyncClient, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """护工读不在管长者详情：403 SCOPE_FORBIDDEN ＋ 审计。

    审计形态（elder_service._audit，A-02 口径）：范围外**读**走结构化日志审计
    （audit_action/actor/target/result/trace_id），DB 行审计用于写操作类。
    日志行必须可被 Logfire/ELK 检索，trace_id 与响应体一致可串联。
    """
    token = set_trace_id("trace-ac18-x")
    try:
        with caplog.at_level("INFO", logger="app.services.elder_service"):
            resp = await client.get(f"/api/elders/{ELDER_B}", headers=_bearer(_care_token()))
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "SCOPE_FORBIDDEN"

        audit_records = [r for r in caplog.records if r.getMessage() == "audit"]
        assert audit_records, "越权读必须落审计（结构化日志）"
        fields = audit_records[-1].__dict__
        assert fields["audit_action"] == "elder.read"
        assert fields["result"] == "forbidden"
        assert fields["actor"] == f"staff:{STAFF_CARE}"
        # 请求级 trace_id 由 trace 中间件生成（覆盖测试预置值是生产行为）；
        # 串联口径：审计日志 trace_id == 响应头 X-Trace-Id
        assert fields["trace_id"] == resp.headers["X-Trace-Id"]
    finally:
        reset_trace_id(token)


async def test_ac18_guardian_out_of_scope_detail_403(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """家属读未绑定长者详情：403（家属范围 = 绑定长者，AC-18）。"""
    with caplog.at_level("INFO", logger="app.services.elder_service"):
        resp = await client.get(f"/api/elders/{ELDER_B}", headers=_bearer(_guardian_token()))
    assert resp.status_code == 403, resp.text
    audit_records = [r for r in caplog.records if r.getMessage() == "audit"]
    assert any(r.__dict__["result"] == "forbidden" for r in audit_records)


async def test_ac18_normal_paths_still_work(client: AsyncClient) -> None:
    """正常路径不被误伤：护工读在管长者 200；admin 全量 200。"""
    resp = await client.get(f"/api/elders/{ELDER_A}", headers=_bearer(_care_token()))
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/api/elders/{ELDER_B}", headers=_bearer(_admin_token()))
    assert resp.status_code == 200, resp.text


# ------------------------------------------------------------------ AC-24 凭证撤销


async def test_ac24_staff_disable_invalidates_old_token(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """员工停用（token_version+1）后旧 access token 立即 401（FR-ADM-04）。"""
    token = _care_token()
    ok = await client.get(f"/api/elders/{ELDER_A}", headers=_bearer(token))
    assert ok.status_code == 200, ok.text

    # 走真实撤销链路：admin PATCH 停用 → 服务层 token_version+1
    resp = await client.patch(
        f"/api/staff/{STAFF_CARE}",
        json={"status": "disabled"},
        headers=_bearer(_admin_token()),
    )
    assert resp.status_code == 200, resp.text

    replay = await client.get(f"/api/elders/{ELDER_A}", headers=_bearer(token))
    assert replay.status_code == 401, replay.text
    assert replay.json()["code"] == "TOKEN_REVOKED"


async def test_ac24_device_unbind_invalidates_old_token(
    client: AsyncClient,
) -> None:
    """设备解绑（token_hash 轮换）后旧设备令牌立即 401（AC-24，auth.py:292 前缀判定）。"""
    device_token = await _device_token(client, DEVICE_A, DEVICE_SECRET_A)

    resp = await client.post(
        f"/api/devices/{DEVICE_A}/unbind",
        headers=_bearer(_admin_token()),
    )
    assert resp.status_code == 200, resp.text

    replay = await client.get("/api/sessions/current", headers=_bearer(device_token))
    assert replay.status_code == 401, replay.text
    assert replay.json()["code"] == "TOKEN_REVOKED"
