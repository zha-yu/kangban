"""Day 9 A3 · 生命周期流水线测试（退房 / 教学重置 / away）。

X4/X12 六钩子属 B/C 写域，骨架期以桩顶替（lifecycle_service._stub_purge_hook，
审计 detail 带 stub=True）；联调段（D10 16:00）换真钩子后本文件的桩相关断言
保持有效（钩子返回口径同为 PurgeRef）。

验收对齐（任务书 A3 §3.4 九行验收表）：
- 教学重置无确认令牌 → 409 零副作用（AC-19）；
- 退房五步顺序执行、每步 1 条 DB 审计、审计行数=步数；
- 旧设备 token → 401 TOKEN_REVOKED；原绑定家属再访问 → 403（AC-18/24）；
- 幂等：重跑不重复归档、审计行数不增长；
- 中途失败：已成功步骤保持，重跑按幂等跳过。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.security import sha256_hex, sign_staff_access
from app.db import get_session
from app.main import create_app
from app.models import (
    AuditLog,
    Device,
    Elder,
    ElderGuardian,
    ElderStaffAssignment,
    GuardianAccount,
    Room,
    StaffAccount,
    Voiceprint,
)
from app.services import voiceprint_service
from app.services.lifecycle_service import (
    CONFIRM_TOKEN,
    ElderNotFoundError,
    LifecycleConfirmError,
    LifecycleStepError,
    checkout_elder,
    set_away,
    teaching_reset,
)

ROOM_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
ELDER_A = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
DEVICE_A = uuid.UUID("cccccccc-0000-0000-0000-000000000001")
STAFF_ADMIN = uuid.UUID("dddddddd-0000-0000-0000-000000000001")
GUARDIAN_ID = uuid.UUID("eeeeeeee-0000-0000-0000-000000000001")

DEVICE_SECRET_A = "device-a-long-term-secret"
TEST_JWT_SECRET = "lifecycle-suite-secret-0123456789abcdef"


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
    """逐批 flush（无 relationship 时 UoW 表依赖排序不可靠，test_gates 踩坑留痕）。"""
    session.add_all(
        [
            Room(room_id=ROOM_ID, room_no="A-301"),
            Elder(elder_id=ELDER_A, name="张桂芬", status="active"),
            StaffAccount(
                staff_id=STAFF_ADMIN,
                name="管理员",
                role="admin",
                phone="13700000001",
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
    await session.flush()
    session.add_all(
        [
            ElderStaffAssignment(elder_id=ELDER_A, staff_id=STAFF_ADMIN, relation="primary_care"),
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
            Voiceprint(
                elder_id=ELDER_A,
                version=1,
                sample_index=0,
                embedding=b"\x00" * 16,
                status="active",
            ),
            Voiceprint(
                elder_id=ELDER_A,
                version=1,
                sample_index=1,
                embedding=b"\x01" * 16,
                status="active",
            ),
        ]
    )
    await session.commit()


# ------------------------------------------------------------------ helpers


def _admin_identity() -> object:  # Identity（避免顶部循环导入，测试内构造）
    from app.core.runtime_context import Identity

    return Identity(kind="staff", staff_id=STAFF_ADMIN, role="admin")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _admin_token() -> str:
    return sign_staff_access(kind="staff", staff_id=STAFF_ADMIN, role="admin", token_version=0)


async def _device_token(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/device-sessions",
        json={"device_id": str(DEVICE_A), "device_secret": DEVICE_SECRET_A},
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


async def _lifecycle_audits(session: AsyncSession, reason: str) -> list[AuditLog]:
    rows = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.resource_id == ELDER_A,
                AuditLog.detail["reason"].as_string() == reason,
            )
        )
    ).scalars()
    return list(rows)


async def _voiceprint_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Voiceprint).where(Voiceprint.elder_id == ELDER_A)
        )
    ).scalar_one()


# ------------------------------------------------------------------ 退房


async def test_checkout_runs_five_steps_with_per_step_audit(
    db_session: AsyncSession,
) -> None:
    """退房五步顺序执行；每步 1 条审计（行数=步数）；状态收口 transferred。"""
    report = await checkout_elder(
        db_session,
        operator=_admin_identity(),  # type: ignore[arg-type]
        elder_id=ELDER_A,
        confirm_token=CONFIRM_TOKEN,
    )

    assert report.success is True
    assert [s.step for s in report.steps] == [
        "revoke_tokens",
        "purge_voiceprints",
        "purge_memory",
        "archive_health",
        "unbind_devices",
    ]
    # 三步真实现：声纹 2 行删除、设备 1 台解绑；X4/X12 桩步 affected=0 且带 stub 标记
    assert report.steps[1].affected_rows == 2
    assert report.steps[4].affected_rows == 1
    assert report.steps[2].stub is True and report.steps[3].stub is True

    audits = await _lifecycle_audits(db_session, "checkout")
    assert len(audits) == 5, "审计行数必须等于步数"
    stub_rows = [a for a in audits if a.detail and a.detail.get("stub")]
    assert len(stub_rows) == 2, "桩步审计必须带 stub=True（联调段换真钩子后为 0）"

    elder = await db_session.get(Elder, ELDER_A)
    assert elder is not None and elder.status == "transferred"
    assert await _voiceprint_count(db_session) == 0

    device = await db_session.get(Device, DEVICE_A)
    assert device is not None
    assert device.elder_id is None and device.status == "offline"
    assert device.token_hash != sha256_hex(DEVICE_SECRET_A), "设备 token_hash 必须轮换"


async def test_checkout_invalidates_device_token_and_guardian_scope(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """退房后：旧设备 token → 401 TOKEN_REVOKED（AC-24）；原绑定家属失权（AC-18）。

    家属失权观测口径（A-01 裁决：家属读长者档案一律 403，退房前后都 403），
    故用 F0 绑定清单（`GET /api/family/elders`）观测：退房前含 A，退房后不含。
    """
    device_token = await _device_token(client)
    ok = await client.get("/api/sessions/current", headers=_bearer(device_token))
    assert ok.status_code == 200, ok.text

    guardian_token = sign_staff_access(
        kind="guardian", staff_id=GUARDIAN_ID, role="", token_version=0
    )
    bound_list = await client.get("/api/family/elders", headers=_bearer(guardian_token))
    assert bound_list.status_code == 200, bound_list.text
    assert str(ELDER_A) in bound_list.text, "退房前家属绑定清单必须含长者 A"

    await checkout_elder(
        db_session,
        operator=_admin_identity(),  # type: ignore[arg-type]
        elder_id=ELDER_A,
        confirm_token=CONFIRM_TOKEN,
    )

    replay = await client.get("/api/sessions/current", headers=_bearer(device_token))
    assert replay.status_code == 401, replay.text
    assert replay.json()["code"] == "TOKEN_REVOKED"

    unbound_list = await client.get("/api/family/elders", headers=_bearer(guardian_token))
    assert unbound_list.status_code == 200, unbound_list.text
    assert str(ELDER_A) not in unbound_list.text, "退房后家属绑定清单不得再含长者 A"

    # 档案端点按 A-01 对家属恒 403；绑定失权由 elder_guardians unbound 落库佐证
    binding = (
        await db_session.execute(
            select(ElderGuardian).where(
                ElderGuardian.elder_id == ELDER_A, ElderGuardian.guardian_id == GUARDIAN_ID
            )
        )
    ).scalar_one()
    assert binding.status == "unbound"


async def test_checkout_without_confirm_token_has_zero_side_effects(
    db_session: AsyncSession,
) -> None:
    """无确认令牌 → LifecycleConfirmError（REST 409），零副作用（AC-19 同型）。"""
    with pytest.raises(LifecycleConfirmError):
        await checkout_elder(
            db_session,
            operator=_admin_identity(),  # type: ignore[arg-type]
            elder_id=ELDER_A,
            confirm_token=None,
        )
    with pytest.raises(LifecycleConfirmError):
        await checkout_elder(
            db_session,
            operator=_admin_identity(),  # type: ignore[arg-type]
            elder_id=ELDER_A,
            confirm_token="confirm",  # 大小写不符也算无效
        )

    assert await _lifecycle_audits(db_session, "checkout") == []
    assert await _voiceprint_count(db_session) == 2
    elder = await db_session.get(Elder, ELDER_A)
    assert elder is not None and elder.status == "active"
    device = await db_session.get(Device, DEVICE_A)
    assert device is not None and device.elder_id == ELDER_A


async def test_checkout_is_idempotent_on_rerun(db_session: AsyncSession) -> None:
    """重跑退房：全部步骤按幂等跳过，不重复归档，审计行数不增长。"""
    operator = _admin_identity()  # type: ignore[arg-type]
    first = await checkout_elder(
        db_session,
        operator=operator,
        elder_id=ELDER_A,
        confirm_token=CONFIRM_TOKEN,  # type: ignore[arg-type]
    )
    assert first.success is True

    second = await checkout_elder(
        db_session,
        operator=operator,
        elder_id=ELDER_A,
        confirm_token=CONFIRM_TOKEN,  # type: ignore[arg-type]
    )
    assert all(s.skipped for s in second.steps), "重跑时所有已完成步骤必须跳过"
    assert len(await _lifecycle_audits(db_session, "checkout")) == 5, "重跑不得新增审计行"


async def test_checkout_step_failure_is_resumable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """步骤②失败：步骤①已保持（不回滚已吊销 token）；修复后重跑按幂等跳过①。"""
    operator = _admin_identity()  # type: ignore[arg-type]

    async def _boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("voiceprint backend down")

    monkeypatch.setattr(voiceprint_service, "delete_all_for_elder", _boom)
    with pytest.raises(LifecycleStepError) as exc_info:
        await checkout_elder(
            db_session,
            operator=operator,
            elder_id=ELDER_A,
            confirm_token=CONFIRM_TOKEN,  # type: ignore[arg-type]
        )
    assert exc_info.value.step == "purge_voiceprints"
    assert len(await _lifecycle_audits(db_session, "checkout")) == 1, "仅步骤①完成并有审计"

    monkeypatch.undo()
    report = await checkout_elder(
        db_session,
        operator=operator,
        elder_id=ELDER_A,
        confirm_token=CONFIRM_TOKEN,  # type: ignore[arg-type]
    )
    assert report.success is True
    assert report.steps[0].skipped is True, "已成功步骤重跑必须跳过"
    assert report.steps[1].skipped is False
    assert len(await _lifecycle_audits(db_session, "checkout")) == 5


async def test_checkout_unknown_elder_raises(db_session: AsyncSession) -> None:
    with pytest.raises(ElderNotFoundError):
        await checkout_elder(
            db_session,
            operator=_admin_identity(),  # type: ignore[arg-type]
            elder_id=uuid.uuid4(),
            confirm_token=CONFIRM_TOKEN,
        )


# ------------------------------------------------------------------ 教学重置


async def test_teaching_reset_without_confirm_token_has_zero_side_effects(
    db_session: AsyncSession,
) -> None:
    """教学重置无确认令牌 → 409 零副作用（AC-19）。"""
    with pytest.raises(LifecycleConfirmError):
        await teaching_reset(
            db_session,
            operator=_admin_identity(),  # type: ignore[arg-type]
            elder_id=ELDER_A,
            confirm_token=None,
        )
    assert await _lifecycle_audits(db_session, "teaching_reset") == []
    assert await _voiceprint_count(db_session) == 2, "教学重置不删声纹"


async def test_teaching_reset_runs_six_stub_steps_and_preserves_accounts(
    db_session: AsyncSession,
) -> None:
    """教学重置：六步（X4 + X12 五域）全部经桩执行并逐条审计；账号/声纹/审计保留。"""
    report = await teaching_reset(
        db_session,
        operator=_admin_identity(),  # type: ignore[arg-type]
        elder_id=ELDER_A,
        confirm_token=CONFIRM_TOKEN,
    )
    assert report.success is True
    assert [s.step for s in report.steps] == [
        "purge_memory",
        "purge_medication",
        "clear_health",
        "purge_notifications",
        "purge_reports",
        "purge_work_orders",
    ]
    assert all(s.stub for s in report.steps), "骨架期六步全为桩"

    audits = await _lifecycle_audits(db_session, "teaching_reset")
    assert len(audits) == 6, "审计行数=步数"

    staff_count = (
        await db_session.execute(select(func.count()).select_from(StaffAccount))
    ).scalar_one()
    assert staff_count == 1, "账号/角色保留（FR-ADM-02）"
    elder = await db_session.get(Elder, ELDER_A)
    assert elder is not None and elder.status == "active", "教学重置不改长者状态"
    assert await _voiceprint_count(db_session) == 2


# ------------------------------------------------------------------ away 暂离


async def test_away_round_trip_with_audit(db_session: AsyncSession) -> None:
    """暂离 → away＋1 条审计；恢复 → active＋1 条审计（药单未被修改——A3 只动状态）。"""
    operator = _admin_identity()  # type: ignore[arg-type]
    await set_away(db_session, operator=operator, elder_id=ELDER_A, away=True)  # type: ignore[arg-type]
    elder = await db_session.get(Elder, ELDER_A)
    assert elder is not None and elder.status == "away"

    await set_away(db_session, operator=operator, elder_id=ELDER_A, away=False)  # type: ignore[arg-type]
    elder = await db_session.get(Elder, ELDER_A)
    assert elder is not None and elder.status == "active"

    mark_audits = await _lifecycle_audits(db_session, "away")
    assert len(mark_audits) == 2, "暂离与恢复各 1 条审计（mark_away + resume）"


async def test_away_repeat_is_noop(db_session: AsyncSession) -> None:
    """重复暂离：幂等跳过，不写第二条审计。"""
    operator = _admin_identity()  # type: ignore[arg-type]
    first = await set_away(
        db_session,
        operator=operator,
        elder_id=ELDER_A,
        away=True,  # type: ignore[arg-type]
    )
    assert first.steps[0].skipped is False

    second = await set_away(
        db_session,
        operator=operator,
        elder_id=ELDER_A,
        away=True,  # type: ignore[arg-type]
    )
    assert second.steps[0].skipped is True
    assert len(await _lifecycle_audits(db_session, "away")) == 1
