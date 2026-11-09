"""建号领域服务（`app/services/staff_service.py`）的单元级测试。

与 `tests/test_staff_api.py` / `tests/test_guardians_api.py` 的分工：
- 那两个文件走 HTTP，验证「契约 → 状态码」；
- 本文件直接调 service 函数，验证**每个领域异常分支**与边界（去重、同值不升版本、只数 active）。

这样做的理由：接口层用例为了覆盖 4 个端点已经很长，再往里塞分支会让失败定位变得困难；
而 service 是唯一写库层，它的每条 `raise` 都对应一个外部可见的错误码，值得逐条钉住。

无 PostgreSQL 时 `pytest.skip` 并打印原因（与其余测试文件一致，不静默通过）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
)

from app.core.security import STAFF_ROLES
from app.models.guardian import GuardianAccount
from app.models.organization import Elder
from app.models.relation import ElderGuardian, ElderStaffAssignment
from app.models.staff import StaffAccount
from app.services import staff_service
from app.services.staff_service import (
    MAX_ACTIVE_GUARDIANS_PER_ELDER,
    ElderNotFoundError,
    GuardianAlreadyBoundError,
    GuardianLimitExceededError,
    GuardianNotFoundError,
    PrimaryCareGapError,
    StaffConflictError,
    StaffNotFoundError,
    StaffRoleConflictError,
    bind_guardian,
    create_guardian,
    create_staff,
    mask_phone,
    reassign_elders,
    update_staff,
)

ELDER_A = uuid.UUID("aaaa1111-1111-4111-8111-111111111101")
ELDER_B = uuid.UUID("aaaa1111-1111-4111-8111-111111111102")
EXISTING_STAFF_ID = uuid.UUID("aaaa1111-1111-4111-8111-111111111201")
EXISTING_GUARDIAN_ID = uuid.UUID("aaaa1111-1111-4111-8111-111111111301")
EXISTING_STAFF_PHONE = "13800000010"
EXISTING_GUARDIAN_PHONE = "13900000010"


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """db_engine 为 conftest 的 module 级共享引擎（阶段 B 夹具治理，P-03 根治）。"""
    try:
        conn: AsyncConnection = await db_engine.connect()
    except Exception as exc:
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")

    trans = await conn.begin()
    sess = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        sess.add_all(
            [
                Elder(elder_id=ELDER_A, name="长者甲", status="active"),
                Elder(elder_id=ELDER_B, name="长者乙", status="active"),
                StaffAccount(
                    staff_id=EXISTING_STAFF_ID,
                    name="既有员工",
                    role="care_worker",
                    phone=EXISTING_STAFF_PHONE,
                    password_hash="$2b$12$placeholderplaceholderplaceholderplaceholder",
                    status="active",
                    token_version=0,
                ),
                GuardianAccount(
                    guardian_id=EXISTING_GUARDIAN_ID,
                    name="既有家属",
                    phone=EXISTING_GUARDIAN_PHONE,
                    password_hash="$2b$12$placeholderplaceholderplaceholderplaceholder",
                    status="active",
                ),
            ]
        )
        await sess.commit()
        yield sess
    finally:
        await sess.close()
        await trans.rollback()
        await conn.close()


# ------------------------------------------------------------------ mask_phone


def test_mask_phone_empty_returns_empty_string() -> None:
    assert mask_phone(None) == ""
    assert mask_phone("") == ""


def test_mask_phone_keeps_head_and_tail() -> None:
    assert mask_phone("13800000001") == "138****0001"


# ------------------------------------------------------------------ create_staff


async def test_create_staff_rejects_unknown_role(session: AsyncSession) -> None:
    with pytest.raises(StaffRoleConflictError, match="未知员工角色"):
        await create_staff(
            session, name="张三", role="nurse", phone="13700000001", password="Passw0rd!"
        )


async def test_create_staff_admin_with_elders_is_rejected(session: AsyncSession) -> None:
    """裁决 D-01：管理员不参与长者绑定。"""
    with pytest.raises(StaffRoleConflictError, match="管理员账号不参与长者绑定"):
        await create_staff(
            session,
            name="王管理",
            role="admin",
            phone="13700000002",
            password="Passw0rd!",
            elder_ids=[ELDER_A],
        )


async def test_create_staff_conflicts_with_existing_staff_phone(session: AsyncSession) -> None:
    with pytest.raises(StaffConflictError):
        await create_staff(
            session,
            name="撞号员工",
            role="doctor",
            phone=EXISTING_STAFF_PHONE,
            password="Passw0rd!",
        )


async def test_create_staff_conflicts_with_existing_guardian_phone(session: AsyncSession) -> None:
    """跨表查重：家属占用的号码不能再建员工，否则登录侧会出现二义性。"""
    with pytest.raises(StaffConflictError):
        await create_staff(
            session,
            name="撞号医生",
            role="doctor",
            phone=EXISTING_GUARDIAN_PHONE,
            password="Passw0rd!",
        )


async def test_create_staff_missing_elder_raises_before_writing(session: AsyncSession) -> None:
    ghost = uuid.uuid4()
    with pytest.raises(ElderNotFoundError, match="长者不存在"):
        await create_staff(
            session,
            name="李医生",
            role="doctor",
            phone="13700000003",
            password="Passw0rd!",
            elder_ids=[ELDER_A, ghost],
        )

    # 校验前置于写库：异常路径不应留下任何员工行
    count = (
        await session.execute(
            select(func.count())
            .select_from(StaffAccount)
            .where(StaffAccount.phone == "13700000003")
        )
    ).scalar_one()
    assert count == 0


async def test_create_staff_admin_without_elders_has_no_assignment(session: AsyncSession) -> None:
    staff = await create_staff(
        session, name="王管理", role="admin", phone="13700000004", password="Passw0rd!"
    )
    assert staff.role == "admin"
    assert staff.status == "active"
    assert staff.token_version == 0

    assignments = (
        await session.execute(
            select(func.count())
            .select_from(ElderStaffAssignment)
            .where(ElderStaffAssignment.staff_id == staff.staff_id)
        )
    ).scalar_one()
    assert assignments == 0


async def test_create_staff_doctor_assignments_dedupe(session: AsyncSession) -> None:
    """同一 elder_id 传两遍只落一行，否则会撞 (elder_id, staff_id, relation) 唯一约束。"""
    staff = await create_staff(
        session,
        name="李医生",
        role="doctor",
        phone="13700000005",
        password="Passw0rd!",
        elder_ids=[ELDER_A, ELDER_A, ELDER_B],
    )

    rows = (
        (
            await session.execute(
                select(ElderStaffAssignment).where(ElderStaffAssignment.staff_id == staff.staff_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert {row.relation for row in rows} == {"doctor"}
    assert {row.elder_id for row in rows} == {ELDER_A, ELDER_B}


async def test_create_staff_care_worker_maps_to_primary_care(session: AsyncSession) -> None:
    staff = await create_staff(
        session,
        name="赵护工",
        role="care_worker",
        phone="13700000006",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )
    row = (
        await session.execute(
            select(ElderStaffAssignment).where(ElderStaffAssignment.staff_id == staff.staff_id)
        )
    ).scalar_one()
    assert row.relation == "primary_care"


async def test_staff_roles_constant_is_source_of_truth() -> None:
    """角色白名单必须来自 `app.core.security.STAFF_ROLES`，不另起一份。"""
    assert set(staff_service._ROLE_TO_RELATION) <= set(STAFF_ROLES)
    assert "admin" not in staff_service._ROLE_TO_RELATION


# ------------------------------------------------------------------ update_staff


async def test_update_staff_not_found(session: AsyncSession) -> None:
    with pytest.raises(StaffNotFoundError):
        await update_staff(session, staff_id=uuid.uuid4(), role="doctor")


async def test_update_staff_same_values_do_not_bump_token_version(session: AsyncSession) -> None:
    """空 PATCH 与同值 PATCH 都不能误伤在线会话。"""
    same = await update_staff(
        session, staff_id=EXISTING_STAFF_ID, role="care_worker", status="active"
    )
    assert same.token_version == 0

    empty = await update_staff(session, staff_id=EXISTING_STAFF_ID)
    assert empty.token_version == 0


async def test_update_staff_role_change_bumps_token_version(session: AsyncSession) -> None:
    updated = await update_staff(session, staff_id=EXISTING_STAFF_ID, role="doctor")
    assert updated.role == "doctor"
    assert updated.token_version == 1


async def test_update_staff_status_change_bumps_token_version(session: AsyncSession) -> None:
    updated = await update_staff(session, staff_id=EXISTING_STAFF_ID, status="disabled")
    assert updated.status == "disabled"
    assert updated.token_version == 1


async def test_update_staff_both_fields_change_bumps_once(session: AsyncSession) -> None:
    """同一次请求改两个字段只加 1，不是加 2。"""
    updated = await update_staff(
        session, staff_id=EXISTING_STAFF_ID, role="doctor", status="disabled"
    )
    assert (updated.role, updated.status, updated.token_version) == ("doctor", "disabled", 1)


# ------------------------------------------------------------------ create_guardian


async def test_create_guardian_conflicts_with_staff_phone(session: AsyncSession) -> None:
    with pytest.raises(StaffConflictError):
        await create_guardian(
            session, name="撞号家属", phone=EXISTING_STAFF_PHONE, password="Passw0rd!"
        )


async def test_create_guardian_success(session: AsyncSession) -> None:
    guardian = await create_guardian(
        session, name="张女士", phone="13900000020", password="Passw0rd!"
    )
    assert guardian.status == "active"
    assert guardian.phone == "13900000020"
    assert guardian.password_hash.startswith("$2b$")


# ------------------------------------------------------------------ bind_guardian


async def test_bind_guardian_elder_not_found(session: AsyncSession) -> None:
    with pytest.raises(ElderNotFoundError):
        await bind_guardian(
            session, elder_id=uuid.uuid4(), guardian_id=EXISTING_GUARDIAN_ID, relation="女儿"
        )


async def test_bind_guardian_guardian_not_found(session: AsyncSession) -> None:
    with pytest.raises(GuardianNotFoundError):
        await bind_guardian(session, elder_id=ELDER_A, guardian_id=uuid.uuid4(), relation="女儿")


async def test_bind_guardian_already_bound(session: AsyncSession) -> None:
    await bind_guardian(
        session, elder_id=ELDER_A, guardian_id=EXISTING_GUARDIAN_ID, relation="女儿"
    )
    with pytest.raises(GuardianAlreadyBoundError):
        await bind_guardian(
            session, elder_id=ELDER_A, guardian_id=EXISTING_GUARDIAN_ID, relation="女儿"
        )


async def test_bind_guardian_limit_exceeded(session: AsyncSession) -> None:
    extra = []
    for index in range(MAX_ACTIVE_GUARDIANS_PER_ELDER + 1):
        extra.append(
            GuardianAccount(
                guardian_id=uuid.uuid4(),
                name=f"家属{index}",
                phone=f"1390000003{index}",
                password_hash="$2b$12$placeholderplaceholderplaceholderplaceholder",
                status="active",
            )
        )
    session.add_all(extra)
    await session.flush()

    for index in range(MAX_ACTIVE_GUARDIANS_PER_ELDER):
        await bind_guardian(
            session,
            elder_id=ELDER_B,
            guardian_id=extra[index].guardian_id,
            relation="儿子",
        )

    with pytest.raises(GuardianLimitExceededError):
        await bind_guardian(
            session,
            elder_id=ELDER_B,
            guardian_id=extra[-1].guardian_id,
            relation="儿子",
        )


async def test_bind_guardian_unbound_does_not_consume_quota(session: AsyncSession) -> None:
    """`unbound` 是解绑保痕（AC-24），不该继续占 2 个名额。"""
    first = await bind_guardian(
        session, elder_id=ELDER_A, guardian_id=EXISTING_GUARDIAN_ID, relation="女儿"
    )
    second = GuardianAccount(
        guardian_id=uuid.uuid4(),
        name="家属乙",
        phone="13900000040",
        password_hash="$2b$12$placeholderplaceholderplaceholderplaceholder",
        status="active",
    )
    session.add(second)
    await session.flush()
    await bind_guardian(session, elder_id=ELDER_A, guardian_id=second.guardian_id, relation="儿子")

    # 满员：第三位被拒
    third = GuardianAccount(
        guardian_id=uuid.uuid4(),
        name="家属丙",
        phone="13900000041",
        password_hash="$2b$12$placeholderplaceholderplaceholderplaceholder",
        status="active",
    )
    session.add(third)
    await session.flush()
    with pytest.raises(GuardianLimitExceededError):
        await bind_guardian(
            session, elder_id=ELDER_A, guardian_id=third.guardian_id, relation="侄子"
        )

    # 解绑第一位后名额释放
    first.status = "unbound"
    await session.flush()
    await bind_guardian(session, elder_id=ELDER_A, guardian_id=third.guardian_id, relation="侄子")


async def test_bind_guardian_writes_active_status(session: AsyncSession) -> None:
    binding = await bind_guardian(
        session, elder_id=ELDER_A, guardian_id=EXISTING_GUARDIAN_ID, relation="女儿"
    )
    assert binding.status == "active"

    stored = (
        await session.execute(select(ElderGuardian.status).where(ElderGuardian.id == binding.id))
    ).scalar_one()
    assert stored == "active"


# ------------------------------------------------------------------ reassign_elders（N19 / E20）


async def test_reassign_elders_staff_not_found(session: AsyncSession) -> None:
    with pytest.raises(StaffNotFoundError):
        await reassign_elders(
            session, staff_id=uuid.uuid4(), elder_ids=[], operator_id=uuid.uuid4()
        )


async def test_reassign_elders_admin_target_is_rejected(session: AsyncSession) -> None:
    """目标 staff 是 admin → 403（裁决 D-01：admin 不参与绑定）。"""
    admin = await create_staff(
        session, name="王管理", role="admin", phone="13700000010", password="Passw0rd!"
    )
    with pytest.raises(StaffRoleConflictError, match="管理员账号不参与长者绑定"):
        await reassign_elders(
            session, staff_id=admin.staff_id, elder_ids=[ELDER_A], operator_id=uuid.uuid4()
        )


async def test_reassign_elders_missing_elder_rejected(session: AsyncSession) -> None:
    ghost = uuid.uuid4()
    staff = await create_staff(
        session,
        name="赵护工",
        role="care_worker",
        phone="13700000011",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )
    with pytest.raises(ElderNotFoundError, match="长者不存在"):
        await reassign_elders(
            session,
            staff_id=staff.staff_id,
            elder_ids=[ELDER_A, ghost],
            operator_id=uuid.uuid4(),
        )


async def test_reassign_elders_normal_diff(session: AsyncSession) -> None:
    """care_worker：已有 [A] → 请求 [A, B]；added=[B], removed=[]。"""
    staff = await create_staff(
        session,
        name="赵护工",
        role="care_worker",
        phone="13700000012",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )

    result = await reassign_elders(
        session,
        staff_id=staff.staff_id,
        elder_ids=[ELDER_A, ELDER_B],
        operator_id=uuid.uuid4(),
    )

    assert result["staff_id"] == staff.staff_id
    assert result["added"] == [ELDER_B]
    assert result["removed"] == []

    # DB 验证
    rows = (
        (
            await session.execute(
                select(ElderStaffAssignment).where(ElderStaffAssignment.staff_id == staff.staff_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    for row in rows:
        assert row.relation == "primary_care"


async def test_reassign_elders_no_change_writes_audit(session: AsyncSession) -> None:
    """请求与现有绑定完全一致 → 返回 added=[], removed=[]；仍写审计（便于管理员操作追踪）。"""
    staff = await create_staff(
        session,
        name="赵护工",
        role="care_worker",
        phone="13700000013",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )

    result = await reassign_elders(
        session,
        staff_id=staff.staff_id,
        elder_ids=[ELDER_A],
        operator_id=uuid.uuid4(),
    )

    assert result == {"staff_id": staff.staff_id, "added": [], "removed": []}


async def test_reassign_elders_primary_care_gap_rejected(session: AsyncSession) -> None:
    """care_worker 是 ELDER_A 唯一的 primary_care → 移除 A 触发 409。"""
    staff = await create_staff(
        session,
        name="赵护工",
        role="care_worker",
        phone="13700000014",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )

    # 清空 ELDER_A（唯一责任护工） → 409
    with pytest.raises(PrimaryCareGapError, match=r"长者 .* 移除后将失去全部责任护工"):
        await reassign_elders(
            session, staff_id=staff.staff_id, elder_ids=[], operator_id=uuid.uuid4()
        )

    # 原绑定不应受影响（409 判定失败时 flush 没执行）
    count = (
        await session.execute(
            select(func.count())
            .select_from(ElderStaffAssignment)
            .where(ElderStaffAssignment.staff_id == staff.staff_id)
        )
    ).scalar_one()
    assert count == 1


async def test_reassign_elders_remove_non_last_care_worker_ok(session: AsyncSession) -> None:
    """care_worker 是 ELDER_A 的其中一位（还有另一个护工）→ 移除 A 不触发 409。"""
    staff_a = await create_staff(
        session,
        name="护工A",
        role="care_worker",
        phone="13700000015",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )
    staff_b = await create_staff(
        session,
        name="护工B",
        role="care_worker",
        phone="13700000016",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )

    # staff_a 清空自己的绑定 → ELDER_A 还有 staff_b 管着 → OK
    result = await reassign_elders(
        session, staff_id=staff_a.staff_id, elder_ids=[], operator_id=uuid.uuid4()
    )
    assert result["removed"] == [ELDER_A]
    assert result["added"] == []

    # staff_b 的绑定不受影响
    count_b = (
        await session.execute(
            select(func.count())
            .select_from(ElderStaffAssignment)
            .where(ElderStaffAssignment.staff_id == staff_b.staff_id)
        )
    ).scalar_one()
    assert count_b == 1


async def test_reassign_elders_doctor_change_no_primary_care_check(session: AsyncSession) -> None:
    """doctor 改绑不影响 primary_care 责任护工数量 → 移除任何 elder 都不触发 409。"""
    # ELDER_A 有一位 care_worker 管着 primary_care
    await create_staff(
        session,
        name="赵护工",
        role="care_worker",
        phone="13700000017",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )
    # doctor 先绑 ELDER_A
    doctor = await create_staff(
        session,
        name="李医生",
        role="doctor",
        phone="13700000018",
        password="Passw0rd!",
        elder_ids=[ELDER_A],
    )

    # doctor 移除 ELDER_A → OK（doctor 不承担 primary_care 责任）
    result = await reassign_elders(
        session, staff_id=doctor.staff_id, elder_ids=[], operator_id=uuid.uuid4()
    )
    assert result["removed"] == [ELDER_A]
    assert result["added"] == []
