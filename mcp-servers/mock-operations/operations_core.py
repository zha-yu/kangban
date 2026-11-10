"""mock-operations MCP 的工具核心逻辑（Day 8 范围提前件，计划
docs/plans/adhoc-20261110-mockoperations提前实现.md）。

两个工具把 Agent 的对外副作用收敛为「Mock 落库」（技术设计文档 §1.1 副作用收敛）：

- `create_work_order`：红色事件 / 漏服 → `work_orders` + `work_order_transitions`
  首条历史（委托 `work_order_service`）；
- `send_family_notification`：按长者家属绑定逐人落 `notifications`
  （`is_mock=true`，委托 `notify_service`）。

关键口径：

- **身份注入**：入参 `subject_elder_id` 由后端 ToolExecutor 从 runtime_context
  注入（Day 8 接线），模型传入无效；args 里出现的 `elder_id` 键已被
  `MCPClient._resolve_args_identity` 在客户端丢弃（闸门③）；
- **失败四分类**：全部返回结构化结果、永不抛异常——参数错→`bad_request`，
  目标不存在→`not_found`，临时故障→`conflict`/`transient`；
  `mcp_client.tool_error_class()` 按 `bad_request → bad_args` 映射，零改动；
- **不指定 assignee**：谁处理工单是护工的职责，模型不得分派，建单留未分派态；
- **本层不写 SQL**：委托 `app/services/**`（唯一写库层），每工具一个独立事务。

运行方式见 `server.py` 模块 docstring；单测见 `tests/test_mcp_mock_operations.py`。
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_trace_id
from app.models.medication import RiskEvent
from app.models.organization import Elder
from app.services import notify_service, work_order_service
from app.services.notify_service import NotificationTargetNotFound
from app.services.work_order_service import (
    ORDER_TYPES,
    SEVERITIES,
    WorkOrderConflict,
    WorkOrderError,
)

logger = logging.getLogger("mcp_servers.mock_operations")

MAX_NOTE_LENGTH: Final[int] = 500
MAX_TOKEN_LENGTH: Final[int] = 128
# 幂等键/去重令牌：字母数字与 : . - _，防止把换行、SQL 片段塞进键
_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[\w:.-]{1,128}$")
# notifications.type 是开放枚举（ER §5.3 口径），只做形态校验防注入，不设死白名单
_NOTIFICATION_TYPE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z_]{1,63}$")
CHANNELS: Final[frozenset[str]] = frozenset({"inapp", "webpush"})

# message 一律给长者可读话术，不回显内部细节（MCP server 红线）
FAILURE_MESSAGES: Final[dict[str, str]] = {
    "bad_request": "参数好像不太对，麻烦您再说一遍。",
    "not_found": "没有找到对应的长者或家属，无法继续。",
    "conflict": "这一步刚刚执行过了，请稍等。",
    "transient": "系统暂时不可用，请稍后再试，或找护工帮帮忙。",
}


def _failure(error_code: str, message: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error_code,
        "message": message or FAILURE_MESSAGES[error_code],
    }


def _parse_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class OperationsService:
    """两个 mock 工具的核心逻辑；`session_factory` 可注入以便单测。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def create_work_order(self, args: dict[str, Any]) -> dict[str, Any]:
        """建工单（Mock 落库）。参数错/not_found 不触库前返回，永不抛异常。"""
        # ---- 参数校验（零 DB）----
        elder_id = _parse_uuid(args.get("subject_elder_id"))
        if elder_id is None:
            return _failure("bad_request", "subject_elder_id 必须是合法 UUID")
        order_type = args.get("order_type")
        if order_type not in ORDER_TYPES:
            return _failure("bad_request", f"order_type 必须是 {sorted(ORDER_TYPES)} 之一")
        severity = args.get("severity")
        if severity not in SEVERITIES:
            return _failure("bad_request", f"severity 必须是 {sorted(SEVERITIES)} 之一")
        dedupe_token = args.get("dedupe_token")
        if dedupe_token is not None and not (
            isinstance(dedupe_token, str) and _TOKEN_PATTERN.fullmatch(dedupe_token)
        ):
            return _failure(
                "bad_request",
                f"dedupe_token 必须是不超过 {MAX_TOKEN_LENGTH} 位的字母数字（含 : . - _）",
            )
        event_id: uuid.UUID | None = None
        if args.get("event_id") is not None:
            event_id = _parse_uuid(args.get("event_id"))
            if event_id is None:
                return _failure("bad_request", "event_id 必须是合法 UUID")
        note = args.get("note")
        if note is not None:
            if not isinstance(note, str):
                return _failure("bad_request", "note 必须是字符串")
            note = note.strip() or None
            if note is not None and len(note) > MAX_NOTE_LENGTH:
                return _failure("bad_request", f"note 不超过 {MAX_NOTE_LENGTH} 字")

        # ---- 落库（独立事务，委托唯一写库层）----
        try:
            async with self._session_factory() as session:
                elder_exists = (
                    await session.execute(select(Elder.elder_id).where(Elder.elder_id == elder_id))
                ).scalar_one_or_none()
                if elder_exists is None:
                    return _failure("not_found", "没有找到这位长者")
                if event_id is not None:
                    event_exists = (
                        await session.execute(
                            select(RiskEvent.event_id).where(RiskEvent.event_id == event_id)
                        )
                    ).scalar_one_or_none()
                    if event_exists is None:
                        return _failure("not_found", "没有找到对应的健康事件")
                ref = await work_order_service.open_work_order(
                    session,
                    elder_id=elder_id,
                    order_type=order_type,
                    severity=severity,
                    event_id=event_id,
                    dedupe_token=dedupe_token,
                )
                await session.commit()
        except WorkOrderConflict as exc:
            logger.warning(
                "mock_create_work_order_conflict: %s",
                exc,
                extra={"trace_id": get_trace_id() or "-"},
            )
            return _failure("conflict", "该事件已经建过工单，请勿重复派单。")
        except WorkOrderError as exc:
            return _failure("bad_request", str(exc) or None)
        except Exception:
            logger.exception(
                "mock_create_work_order_transient_failure",
                extra={"trace_id": get_trace_id() or "-"},
            )
            return _failure("transient")

        return {
            "ok": True,
            "order_id": str(ref.order_id),
            "order_no": ref.order_no,
            "status": ref.status,
            "severity": ref.severity,
            "created": ref.created,
        }

    async def send_family_notification(self, args: dict[str, Any]) -> dict[str, Any]:
        """通知家属（Mock 落库）。目标由家属绑定解析，无绑定如实失败。"""
        # ---- 参数校验（零 DB）----
        elder_id = _parse_uuid(args.get("subject_elder_id"))
        if elder_id is None:
            return _failure("bad_request", "subject_elder_id 必须是合法 UUID")
        ntype = args.get("notification_type")
        if not isinstance(ntype, str) or not _NOTIFICATION_TYPE_PATTERN.fullmatch(ntype):
            return _failure(
                "bad_request",
                "notification_type 必须是小写字母开头的 snake_case（如 report_ready）",
            )
        try:
            payload = notify_service.validate_payload(args.get("payload"))
        except notify_service.NotificationPayloadError as exc:
            return _failure("bad_request", str(exc))
        dedupe_key = args.get("dedupe_key")
        if dedupe_key is not None and not (
            isinstance(dedupe_key, str) and _TOKEN_PATTERN.fullmatch(dedupe_key)
        ):
            return _failure(
                "bad_request",
                f"dedupe_key 必须是不超过 {MAX_TOKEN_LENGTH} 位的字母数字（含 : . - _）",
            )
        channel = args.get("channel")
        if channel is not None and (not isinstance(channel, str) or channel not in CHANNELS):
            return _failure("bad_request", f"channel 必须是 {sorted(CHANNELS)} 之一")

        # ---- 落库（独立事务，委托唯一写库层）----
        try:
            async with self._session_factory() as session:
                refs = await notify_service.send_guardian_notifications(
                    session,
                    elder_id=elder_id,
                    ntype=ntype,
                    payload=payload,
                    dedupe_key=dedupe_key,
                    channel=channel,
                )
                await session.commit()
        except NotificationTargetNotFound:
            return _failure("not_found", "这位长者还没有绑定的家属，无法通知。")
        except Exception:
            logger.exception(
                "mock_send_family_notification_transient_failure",
                extra={"trace_id": get_trace_id() or "-"},
            )
            return _failure("transient")

        return {
            "ok": True,
            "count": len(refs),
            "created": any(r.created for r in refs),
            "notifications": [
                {
                    "notification_id": str(r.notification_id),
                    "guardian_id": str(r.target_id),
                    "type": r.type,
                    "channel": r.channel,
                    "status": r.status,
                    "is_mock": r.is_mock,
                    "created": r.created,
                }
                for r in refs
            ],
        }
