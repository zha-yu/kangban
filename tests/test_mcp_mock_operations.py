"""mock-operations MCP 提前件单测（计划
docs/plans/adhoc-20261110-mockoperations提前实现.md）。

覆盖五件事，对应「错了会静默出错」的点：

1. 两个工具的参数校验在触库前就拒掉（非法 UUID / 枚举 / token / payload）；
2. 建单真实落库且首条流转存在，`severity` 快照正确；
3. `dedupe_token` 幂等：第二次调用返回既有单（created=False），不重复派单；
4. 长者不存在 → `not_found` 如实失败，不编造；
5. 家属通知按绑定逐人落 `is_mock=true` 记录、幂等键 `<dedupe_key>:<guardian_id>`
   兜底、无绑定 → `not_found`。

校验用例用「触库即炸」的 session_factory 证明零 DB；落库用例复用 conftest 的
`db_engine`（PG 不可达时 skip），savepoint 隔离，服务层 commit 只释放 savepoint。
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.models.guardian import GuardianAccount
from app.models.medication import WorkOrder
from app.models.notifications import Notification
from app.models.organization import Elder
from app.models.relation import ElderGuardian
from app.services import notify_service, work_order_service

# 目录名含连字符不能作包名：把 mock-operations 目录与仓库根塞进 sys.path
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_MOCK_OPS = _REPO_ROOT / "mcp-servers" / "mock-operations"
for _path in (str(_REPO_ROOT), str(_MOCK_OPS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from operations_core import OperationsService  # noqa: E402

# ------------------------------------------------------------------ 零 DB 校验


class _BoomFactory:
    """触库即炸：证明参数校验在打开会话之前就返回了。"""

    def __call__(self) -> Any:
        raise AssertionError("参数校验失败不应触库")


@pytest.fixture
def boom_service() -> OperationsService:
    return OperationsService(session_factory=_BoomFactory())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"subject_elder_id": "not-a-uuid"},
        {"subject_elder_id": 12345},
        {
            "subject_elder_id": str(uuid.uuid4()),
            "order_type": "make_music",
            "severity": "red",
        },
        {
            "subject_elder_id": str(uuid.uuid4()),
            "order_type": "health_alert",
            "severity": "green",
        },
        {
            "subject_elder_id": str(uuid.uuid4()),
            "order_type": "health_alert",
            "severity": "red",
            "dedupe_token": "bad token with space",
        },
        {
            "subject_elder_id": str(uuid.uuid4()),
            "order_type": "health_alert",
            "severity": "red",
            "event_id": "nope",
        },
        {
            "subject_elder_id": str(uuid.uuid4()),
            "order_type": "health_alert",
            "severity": "red",
            "note": 42,
        },
        {
            "subject_elder_id": str(uuid.uuid4()),
            "order_type": "health_alert",
            "severity": "red",
            "note": "超" * 501,
        },
    ],
)
async def test_create_work_order_rejects_bad_params_without_db(
    boom_service: OperationsService, args: dict[str, Any]
) -> None:
    result = await boom_service.create_work_order(args)
    assert result["ok"] is False
    assert result["error"] == "bad_request"
    assert result["message"]


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"subject_elder_id": "x"},
        {
            "subject_elder_id": str(uuid.uuid4()),
            "notification_type": "报告就绪",
        },
        {
            "subject_elder_id": str(uuid.uuid4()),
            "notification_type": "REPORT_READY",
        },
        {
            "subject_elder_id": str(uuid.uuid4()),
            "notification_type": "report_ready",
            "payload": "不是对象",
        },
        {
            "subject_elder_id": str(uuid.uuid4()),
            "notification_type": "report_ready",
            "payload": None,
        },
        {
            "subject_elder_id": str(uuid.uuid4()),
            "notification_type": "report_ready",
            "payload": {"body": "x" * 5000},
        },
        {
            "subject_elder_id": str(uuid.uuid4()),
            "notification_type": "report_ready",
            "payload": {"body": "x"},
            "dedupe_key": "bad key!",
        },
        {
            "subject_elder_id": str(uuid.uuid4()),
            "notification_type": "report_ready",
            "payload": {"body": "x"},
            "channel": "sms",
        },
    ],
)
async def test_send_family_notification_rejects_bad_params_without_db(
    boom_service: OperationsService, args: dict[str, Any]
) -> None:
    result = await boom_service.send_family_notification(args)
    assert result["ok"] is False
    assert result["error"] == "bad_request"
    assert result["message"]


# ------------------------------------------------------------------ 真库用例


@pytest_asyncio.fixture
async def tx_session_factory(
    db_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[Any]]:
    """savepoint 会话工厂：服务层 commit 只释放 savepoint，外层事务整体回滚。"""
    try:
        conn = await db_engine.connect()
    except Exception as exc:
        pytest.skip(f"PostgreSQL 不可达，跳过数据库用例：{exc!r}")

    trans = await conn.begin()
    sf: async_sessionmaker[Any] = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    yield sf
    await trans.rollback()
    await conn.close()


@pytest_asyncio.fixture
async def service(tx_session_factory: async_sessionmaker[Any]) -> OperationsService:
    return OperationsService(session_factory=tx_session_factory)


@pytest_asyncio.fixture
async def seeded_family(tx_session_factory: async_sessionmaker[Any]) -> dict[str, Any]:
    async with tx_session_factory() as session:
        elder = Elder(name="测试长者", status="active")
        guardian = GuardianAccount(name="张小华", password_hash="x" * 32, status="active")
        session.add_all([elder, guardian])
        await session.flush()
        session.add(
            ElderGuardian(
                elder_id=elder.elder_id,
                guardian_id=guardian.guardian_id,
                relation="子女",
                status="active",
            )
        )
        await session.flush()
        # commit 只释放 savepoint：行对外可见（后续服务的独立会话在同一连接上），
        # 外层 trans.rollback() 仍保证用例间隔离
        await session.commit()
        return {"elder_id": elder.elder_id, "guardian_id": guardian.guardian_id}


# ---- create_work_order ----


async def test_create_work_order_persists_with_first_transition(
    service: OperationsService, seeded_family: dict[str, Any]
) -> None:
    result = await service.create_work_order(
        {
            "subject_elder_id": str(seeded_family["elder_id"]),
            "order_type": "health_alert",
            "severity": "red",
            "dedupe_token": "health_event:" + str(uuid.uuid4()),
            "note": "血压 195/122，建议就医",
        }
    )

    assert result["ok"] is True
    assert result["created"] is True
    assert result["status"] == "new"
    assert result["severity"] == "red"
    assert result["order_no"].startswith("WO-")


async def test_create_work_order_idempotent_on_same_token(
    service: OperationsService, seeded_family: dict[str, Any]
) -> None:
    token = "health_event:" + str(uuid.uuid4())
    args = {
        "subject_elder_id": str(seeded_family["elder_id"]),
        "order_type": "health_alert",
        "severity": "red",
        "dedupe_token": token,
    }
    first = await service.create_work_order(args)
    second = await service.create_work_order(args)

    assert first["ok"] is True and second["ok"] is True
    assert second["created"] is False, "同一事件重复调用必须返回既有单"
    assert second["order_id"] == first["order_id"]


async def test_create_work_order_unknown_elder_not_found(
    service: OperationsService, tx_session_factory: async_sessionmaker[Any]
) -> None:
    result = await service.create_work_order(
        {
            "subject_elder_id": str(uuid.uuid4()),
            "order_type": "health_alert",
            "severity": "red",
        }
    )
    assert result["ok"] is False
    assert result["error"] == "not_found"

    async with tx_session_factory() as session:
        orders = list((await session.execute(select(WorkOrder))).scalars())
    assert orders == [], "not_found 失败不得留下任何工单"


# ---- send_family_notification ----


async def test_send_family_notification_creates_mock_rows(
    service: OperationsService, seeded_family: dict[str, Any]
) -> None:
    result = await service.send_family_notification(
        {
            "subject_elder_id": str(seeded_family["elder_id"]),
            "notification_type": "report_ready",
            "payload": {"title": "今日日报已生成", "is_mock": True},
            "dedupe_key": "daily_report:2026-11-10",
        }
    )

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["created"] is True
    row = result["notifications"][0]
    assert row["guardian_id"] == str(seeded_family["guardian_id"])
    assert row["is_mock"] is True and row["status"] == "generated"


async def test_send_family_notification_dedupe_is_per_guardian(
    service: OperationsService,
    seeded_family: dict[str, Any],
    tx_session_factory: async_sessionmaker[Any],
) -> None:
    args = {
        "subject_elder_id": str(seeded_family["elder_id"]),
        "notification_type": "report_ready",
        "payload": {"title": "今日日报已生成"},
        "dedupe_key": "daily_report:2026-11-10",
    }
    first = await service.send_family_notification(args)
    second = await service.send_family_notification(args)

    assert first["created"] is True
    assert second["created"] is False, "同 dedupe_key 重复调用只落一条"
    assert (
        second["notifications"][0]["notification_id"]
        == (first["notifications"][0]["notification_id"])
    )

    async with tx_session_factory() as session:
        rows = list((await session.execute(select(Notification))).scalars())
    assert len(rows) == 1
    assert rows[0].dedupe_key == f"daily_report:2026-11-10:{seeded_family['guardian_id']}"


async def test_send_family_notification_without_binding_not_found(
    service: OperationsService, tx_session_factory: async_sessionmaker[Any]
) -> None:
    result = await service.send_family_notification(
        {
            "subject_elder_id": str(uuid.uuid4()),
            "notification_type": "report_ready",
            "payload": {"title": "今日日报已生成"},
        }
    )
    assert result["ok"] is False
    assert result["error"] == "not_found"

    async with tx_session_factory() as session:
        rows = list((await session.execute(select(Notification))).scalars())
    assert rows == [], "无绑定失败不得留下任何通知"


# ------------------------------------------------------------------ 失败映射（FR-TC-06 核心 Tool 100% 口径）


def _valid_order_args(elder_id: uuid.UUID) -> dict[str, Any]:
    return {
        "subject_elder_id": str(elder_id),
        "order_type": "health_alert",
        "severity": "red",
    }


async def test_create_work_order_event_not_found(
    service: OperationsService, seeded_family: dict[str, Any]
) -> None:
    """event_id 指向不存在的健康事件 → not_found，不建单。"""
    args = _valid_order_args(seeded_family["elder_id"]) | {"event_id": str(uuid.uuid4())}
    result = await service.create_work_order(args)

    assert result["ok"] is False
    assert result["error"] == "not_found"
    assert result["message"]


async def test_create_work_order_conflict_mapping(
    service: OperationsService,
    seeded_family: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WorkOrderConflict → conflict 话术（并发兜底路径的映射）。"""

    async def _raise(*args: Any, **kwargs: Any) -> None:
        raise work_order_service.WorkOrderConflict("并发抢单")

    monkeypatch.setattr(work_order_service, "open_work_order", _raise)
    result = await service.create_work_order(_valid_order_args(seeded_family["elder_id"]))

    assert result["ok"] is False
    assert result["error"] == "conflict"
    assert "已经建过工单" in result["message"]


async def test_create_work_order_service_error_to_bad_request(
    service: OperationsService,
    seeded_family: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WorkOrderError → bad_request，message 透传服务层原因。"""

    async def _raise(*args: Any, **kwargs: Any) -> None:
        raise work_order_service.WorkOrderError("药单不存在")

    monkeypatch.setattr(work_order_service, "open_work_order", _raise)
    result = await service.create_work_order(_valid_order_args(seeded_family["elder_id"]))

    assert result == {
        "ok": False,
        "error": "bad_request",
        "message": "药单不存在",
    }


async def test_create_work_order_unexpected_failure_is_transient(
    service: OperationsService,
    seeded_family: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """裸异常 → transient 长者话术，不回显内部细节、不抛协议异常。"""

    async def _raise(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db connection lost")

    monkeypatch.setattr(work_order_service, "open_work_order", _raise)
    result = await service.create_work_order(_valid_order_args(seeded_family["elder_id"]))

    assert result["ok"] is False
    assert result["error"] == "transient"
    assert "db connection lost" not in result["message"], "内部细节不得外泄"


async def test_send_family_notification_unexpected_failure_is_transient(
    service: OperationsService,
    seeded_family: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通知链路裸异常 → transient，永不抛异常（MCP 红线）。"""

    async def _raise(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(notify_service, "send_guardian_notifications", _raise)
    result = await service.send_family_notification(
        {
            "subject_elder_id": str(seeded_family["elder_id"]),
            "notification_type": "report_ready",
            "payload": {"title": "今日日报已生成"},
        }
    )

    assert result["ok"] is False
    assert result["error"] == "transient"
    assert result["message"]
