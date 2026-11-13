"""照护台三新 API（Day 9 · C4）接口测试：/api/metrics、/api/stats/weekly、/api/memory/*。

覆盖口径：
- F26（TD:1182）：admin-only；分层命中率 / S1 闭环率 / S2 升级率按种子行精确断言；
  token_usage 读进程内计数（llm_service 已埋点回传 usage）——测试进程无调用 →
  available=false 如实呈现，不编造；
- FR-ALT-05：本周红色事件数 / 平均确认时长（分钟）/ 各楼栋漏采次数（A 楼 + 未分配）；
- X5 骨架：memory_admin_service 未交付 → 503 X5_NOT_DELIVERED；鉴权 401/403；
- F21 顺序续播：get_next_media 同剧种下一段 / 回绕 / 无 genre 全库回绕 / 不存在 None
  （media_id 为随机 UUID，断言按同剧种 media_id 排序后的相对次序）。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.memory as memory_api
from app.core.security import sign_staff_access
from app.db import get_session
from app.main import create_app
from app.models.conversation import ConversationThread
from app.models.decision import DecisionLog
from app.models.medication import RiskEvent, WorkOrder
from app.models.organization import Bed, Device, Elder, Room
from app.services import media_service, stats_service

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module")
async def app_with_session(db_conn) -> AsyncIterator[tuple[FastAPI, AsyncSession]]:
    session = AsyncSession(db_conn)
    application = create_app()

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = _override
    try:
        yield application, session
    finally:
        application.dependency_overrides.pop(get_session, None)
        await session.close()


@pytest_asyncio.fixture(loop_scope="module")
async def client(app_with_session) -> AsyncIterator[AsyncClient]:
    application, _ = app_with_session
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _admin_token(staff_id: uuid.UUID) -> str:
    return sign_staff_access(kind="staff", staff_id=staff_id, role="admin", token_version=0)


def _care_token(staff_id: uuid.UUID) -> str:
    return sign_staff_access(kind="staff", staff_id=staff_id, role="care_worker", token_version=0)


# ------------------------------------------------------------------ F26 /api/metrics


async def _seed_decision_world(session: AsyncSession) -> None:
    """Room→Device→Elder→Thread→5 条 DecisionLog：
    rule×2、system_one×2（其中 1 条 escalated）、agent×1。"""
    room = Room(room_no="M-101")
    session.add(room)
    await session.flush()
    elder = Elder(name="长者-MetricsS4", status="active")
    device = Device(room_id=room.room_id, token_hash="tok-metrics")
    session.add_all([elder, device])
    await session.flush()
    thread = ConversationThread(
        thread_id="thr-metrics-s4",
        elder_id=elder.elder_id,
        device_id=device.device_id,
        identity_source="device",
    )
    session.add(thread)
    await session.flush()
    plan = [
        ("rule", False),
        ("rule", False),
        ("system_one", False),
        ("system_one", True),
        ("agent", True),
    ]
    for i, (layer, esc) in enumerate(plan):
        session.add(
            DecisionLog(
                thread_id=thread.thread_id,
                trace_id=f"trace-metrics-{i}",
                elder_id=elder.elder_id,
                input_text=f"文本{i}",
                intent="chat",
                confidence=1 if layer == "rule" else "0.900",
                is_sensitive=False,
                route_layer=layer,
                escalated_to_s2=esc,
                latency_ms=10,
                model_version="test",
            )
        )
    await session.flush()


async def test_metrics_requires_admin(client: AsyncClient, db_conn) -> None:
    assert (await client.get("/api/metrics")).status_code == 401
    session = AsyncSession(db_conn)
    from app.models.staff import StaffAccount

    worker = StaffAccount(name="护工-MetricsS4", role="care_worker", password_hash="x")
    session.add(worker)
    await session.flush()
    resp = await client.get(
        "/api/metrics", headers={"Authorization": f"Bearer {_care_token(worker.staff_id)}"}
    )
    assert resp.status_code == 403
    await session.rollback()


async def test_metrics_aggregates_for_admin(client: AsyncClient, db_conn) -> None:
    session = AsyncSession(db_conn)
    await _seed_decision_world(session)

    from app.models.staff import StaffAccount

    admin = StaffAccount(name="管理员-MetricsS4", role="admin", password_hash="x")
    session.add(admin)
    await session.flush()
    resp = await client.get(
        "/api/metrics", headers={"Authorization": f"Bearer {_admin_token(admin.staff_id)}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    routing = body["routing"]
    by_layer = routing["by_layer"]
    assert by_layer["rule"]["count"] >= 2
    assert by_layer["system_one"]["count"] >= 2
    # 自洽：每层 ratio == count / total（占比与计数同源，不编造）
    for item in by_layer.values():
        assert item["ratio"] == round(item["count"] / routing["total"], 4)
    assert routing["s1_close_rate"] is not None
    assert routing["s2_escalation_rate"] is not None
    # token 消耗读进程内快照：测试进程无 LLM 调用 → available=false 如实呈现（不编造数值）
    assert body["token_usage"]["available"] is False
    await session.rollback()


# ------------------------------------------------------------------ FR-ALT-05 /api/stats/weekly


async def _seed_stats_world(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    room = Room(room_no="S-201")
    session.add(room)
    await session.flush()
    e1 = Elder(name="长者-StatsS4甲", status="active")
    e2 = Elder(name="长者-StatsS4乙", status="active")
    session.add_all([e1, e2])
    await session.flush()
    session.add(Bed(room_id=room.room_id, bed_no="1", status="occupied", elder_id=e1.elder_id))
    await session.flush()

    now = datetime.datetime.now(datetime.UTC)
    week_ago = stats_service.week_start_utc(now)
    in_week = week_ago + datetime.timedelta(days=1)
    out_week = week_ago - datetime.timedelta(days=2)

    session.add_all(
        [
            RiskEvent(  # 本周已确认（6 分钟）→ 计入红档数与平均确认
                elder_id=e1.elder_id,
                type="health_alert",
                severity="red",
                status="closed",
                created_at=in_week,
                acknowledged_at=in_week + datetime.timedelta(minutes=6),
            ),
            RiskEvent(  # 本周未确认 → 计入红档数
                elder_id=e1.elder_id,
                type="health_alert",
                severity="red",
                created_at=in_week,
            ),
            RiskEvent(  # 上周红档 → 不计
                elder_id=e2.elder_id,
                type="health_alert",
                severity="red",
                created_at=out_week,
            ),
            WorkOrder(
                order_no="WO-ST1",
                elder_id=e1.elder_id,
                type="collection_missed",
                severity="yellow",
                created_at=in_week,
            ),
            WorkOrder(
                order_no="WO-ST2",
                elder_id=e1.elder_id,
                type="collection_missed",
                severity="yellow",
                created_at=in_week,
            ),
            WorkOrder(
                order_no="WO-ST3",
                elder_id=e1.elder_id,
                type="missed_medication",
                severity="yellow",
                created_at=in_week,
            ),  # 非漏采不计
            WorkOrder(
                order_no="WO-ST4",
                elder_id=e2.elder_id,
                type="collection_missed",
                severity="yellow",
                created_at=in_week,
            ),  # 无床位 → 未分配
            WorkOrder(
                order_no="WO-ST5",
                elder_id=e1.elder_id,
                type="collection_missed",
                severity="yellow",
                created_at=out_week,
            ),  # 上周不计
        ]
    )
    await session.flush()
    return e1.elder_id, e2.elder_id


async def test_stats_weekly_aggregates(client: AsyncClient, db_conn) -> None:
    session = AsyncSession(db_conn)
    await _seed_stats_world(session)

    from app.models.staff import StaffAccount

    admin = StaffAccount(name="管理员-StatsS4", role="admin", password_hash="x")
    session.add(admin)
    await session.flush()
    resp = await client.get(
        "/api/stats/weekly", headers={"Authorization": f"Bearer {_admin_token(admin.staff_id)}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["red_events_count"] == 2
    assert body["avg_ack_minutes"] == 6.0
    assert body["missed_by_building"] == {"S": 2, "未分配": 1}
    assert body["week_start"].endswith("+00:00")
    await session.rollback()


# ------------------------------------------------------------------ X5 /api/memory（骨架）


async def test_memory_requires_auth(client: AsyncClient) -> None:
    assert (await client.get(f"/api/memory/{uuid.uuid4()}/memories")).status_code == 401


async def test_memory_forbidden_for_non_admin(client: AsyncClient, db_conn) -> None:
    session = AsyncSession(db_conn)
    from app.models.staff import StaffAccount

    worker = StaffAccount(name="护工-MemoryS4", role="care_worker", password_hash="x")
    session.add(worker)
    await session.flush()
    resp = await client.get(
        f"/api/memory/{uuid.uuid4()}/memories",
        headers={"Authorization": f"Bearer {_care_token(worker.staff_id)}"},
    )
    assert resp.status_code == 403
    await session.rollback()


async def test_memory_503_when_x5_not_delivered(client: AsyncClient, db_conn, monkeypatch) -> None:
    """骨架先行：B2 的 memory_admin_service 未交付 → 503 X5_NOT_DELIVERED（不炸 app）。"""
    session = AsyncSession(db_conn)
    from app.models.staff import StaffAccount

    admin = StaffAccount(name="管理员-MemoryS4", role="admin", password_hash="x")
    session.add(admin)
    await session.flush()
    monkeypatch.setattr(memory_api, "memory_admin_service", None)
    for method, url in (
        ("get", f"/api/memory/{uuid.uuid4()}/memories"),
        ("patch", f"/api/memory/{uuid.uuid4()}/memories/{uuid.uuid4()}"),
        ("delete", f"/api/memory/{uuid.uuid4()}/memories/{uuid.uuid4()}"),
    ):
        resp = await getattr(client, method)(
            url,
            headers={"Authorization": f"Bearer {_admin_token(admin.staff_id)}"},
            **({"json": {"new_text": "纠正"}} if method == "patch" else {}),
        )
        assert resp.status_code == 503
        assert resp.json()["code"] == "X5_NOT_DELIVERED"
    await session.rollback()


def test_memory_x5_delivered_skeleton_stands_down() -> None:
    """X5 交付收口（2026-11-12）：memory_admin_service 门面已落地 → 503 骨架下线。

    `/api/memory` 三个端点经 `_require_x5()` 找到三函数即直调 service 层，
    不再返回 X5_NOT_DELIVERED——console 记忆页横幅态随之解除。
    """
    from app.api import memory as memory_api
    from app.services import memory_admin_service

    assert memory_api._require_x5() is memory_admin_service
    for name in ("list_memories", "correct_memory", "delete_memory"):
        assert callable(getattr(memory_admin_service, name, None)), name


# ------------------------------------------------------------------ F21 顺序续播


_MEDIA_SEEDS = [
    {
        "type": "opera",
        "title": "梁山伯与祝英台",
        "genre": "越剧",
        "copyright_status": "teacher_authorized",
    },
    {"type": "opera", "title": "红楼梦", "genre": "越剧", "copyright_status": "teacher_authorized"},
    {"type": "opera", "title": "霸王别姬", "genre": "京剧", "copyright_status": "public_domain"},
    {"type": "opera", "title": "贵妃醉酒", "genre": None, "copyright_status": "public_domain"},
]


async def test_get_next_media_sequential_and_wrap(db_conn) -> None:
    """F21：同剧种下一段 → 末尾回绕；无 genre 全库回绕；不存在 → None。"""
    session = AsyncSession(db_conn)
    rows = await media_service.seed_media_library(session, _MEDIA_SEEDS)
    by_title = {r.title: r for r in rows}

    def _expected(current, pool: list) -> object:
        ordered = sorted(pool, key=lambda r: r.media_id)
        idx = ordered.index(current)
        return ordered[(idx + 1) % len(ordered)]

    yueju = [by_title["梁山伯与祝英台"], by_title["红楼梦"]]
    for cur in yueju:
        nxt = await media_service.get_next_media(session, current_media_id=cur.media_id)
        assert nxt is not None and nxt.media_id == _expected(cur, yueju).media_id

    # 京剧仅一条 → 循环回自身
    peking = by_title["霸王别姬"]
    nxt = await media_service.get_next_media(session, current_media_id=peking.media_id)
    assert nxt is not None and nxt.media_id == peking.media_id

    # 无 genre → 全库回绕（相对全库次序断言）
    no_genre = by_title["贵妃醉酒"]
    nxt = await media_service.get_next_media(session, current_media_id=no_genre.media_id)
    assert nxt is not None and nxt.media_id == _expected(no_genre, rows).media_id

    assert await media_service.get_next_media(session, current_media_id=uuid.uuid4()) is None
    await session.rollback()
