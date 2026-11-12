# owner=day08 · 依据 docs/team_tasks/day08/contracts.md §C-1（task_command kind=health）
"""健康采集任务下发服务层（pad 面板触达，H-3 补启动 / 调度补推共用唯一入口）。

职责边界：**只组帧 + 投递，不写库**。长者身份、`elder_id` 一律由调用方
（API 层从设备 Identity 解出 / 调度器遍历）注入，本层不猜身份（AGENTS §4.9）。

帧形状（§C-1，前端 protocol.ts `TaskCommandMsg` kind=health 分支）：

    {"type": "task_command", "task_id": "health:{elder_id}:{shanghai_day}",
     "kind": "health", "source": "scheduler"|"staff", "hint": ...,
     "payload": {"kind": "health", "startedAt": "HH:MM",
                 "source": "voice_scheduled"|"staff_scheduled"},
     "issued_at": <ms>}

- `task_id` 按当日固定：同一长者同一天重复下发不换键，pad 端按 taskId 幂等
  （FR-MED-04 同款口径）——设备离线时帧丢失，调度 tick 窗口内每分钟补推，
  设备上线后下一分钟自动触达，不重开已渲染面板。
- `payload.source` 与 `health_records.source` 的 voice_/staff_ 前缀对齐
  （技术设计文档 §3 健康来源枚举），前端仅作展示标签，不参与分级。

投递走 `app/audio/signaling.deliver`（进程内注册表）。services 层不在 import 期
依赖 audio 层，**延迟 import**（照 graphs 层 `_default_deliver` 先例，L6#6 同款
豁免登记）；单 worker 运行约束（注册表不跨进程）随契约注明。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import medication_service
from app.services.health_service import verify_staff_proxy

logger = logging.getLogger(__name__)

#: 大字幕提示文案（与前端 task-channel.ts `TASK_HINTS.health` 逐字一致）
HEALTH_TASK_HINT: str = "今天的健康采集开始了，请回答右边的问题"

DispatchStatus = Literal["voice_dispatched", "device_offline", "no_bound_device"]

_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

#: source（触发方）→ payload.source（health_records 前缀口径）映射
_PAYLOAD_SOURCE: dict[str, str] = {
    "scheduler": "voice_scheduled",
    "staff": "staff_scheduled",
}


async def push_health_collection_task(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    source: Literal["scheduler", "staff"],
    operator_id: uuid.UUID | None = None,
    operator_role: str | None = None,
    now: datetime.datetime | None = None,
) -> tuple[DispatchStatus, int]:
    """推送「打开健康采集面板」的 task_command 帧到长者绑定设备。

    - **越权保护（AC-32）**：`source="staff"` 且操作者是 care_worker 时，操作者
      必须是该长者的 primary_care 护工，否则 PermissionError（API 层 403 + 审计）；
      doctor / admin 不限名下范围（与 H-2 代填同口径）。
    - 返回 `(回执, 绑定设备数)`，回执三态照用药触达口径（TD-1）：至少一台设备
      收进发送队列 = `voice_dispatched`；有绑定但全离线 = `device_offline`；
      没绑设备 = `no_bound_device`（运维缺口，不算本轮失败）。
    - 重复下发同 taskId：调度补推与人工补启动同日同键，pad 端幂等不重启面板。
    """
    if source == "staff" and operator_role == "care_worker":
        assert operator_id is not None  # staff 路径 API 层必传
        await verify_staff_proxy(session, elder_id=elder_id, operator_id=operator_id)

    device_ids = await medication_service.list_bound_device_ids(session, elder_id=elder_id)
    if not device_ids:
        logger.info("health_task_no_device elder_id=%s source=%s", elder_id, source)
        return "no_bound_device", 0

    now = now or datetime.datetime.now(_TZ_SHANGHAI)
    now = now.replace(tzinfo=_TZ_SHANGHAI) if now.tzinfo is None else now.astimezone(_TZ_SHANGHAI)
    day = now.date()
    message: dict[str, Any] = {
        "type": "task_command",
        "task_id": f"health:{elder_id}:{day:%Y%m%d}",
        "kind": "health",
        "source": source,
        "hint": HEALTH_TASK_HINT,
        "payload": {
            "kind": "health",
            "startedAt": now.strftime("%H:%M"),
            "source": _PAYLOAD_SOURCE[source],
        },
        "issued_at": int(datetime.datetime.now(datetime.UTC).timestamp() * 1000),
    }

    # 延迟 import：services 层不在 import 期依赖 audio 层（graphs 层同款豁免）
    from app.audio.signaling import deliver

    dispatched = False
    for device_id in device_ids:
        if deliver(str(device_id), message):
            dispatched = True
    status: DispatchStatus = "voice_dispatched" if dispatched else "device_offline"
    logger.info(
        "health_task_dispatch elder_id=%s source=%s status=%s devices=%d task_id=%s",
        elder_id,
        source,
        status,
        len(device_ids),
        message["task_id"],
    )
    return status, len(device_ids)


__all__ = [
    "HEALTH_TASK_HINT",
    "push_health_collection_task",
]
