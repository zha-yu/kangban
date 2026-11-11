"""ORM 模型包。

`alembic/env.py` 通过 `from app.models import Base` 拿 metadata，因此这里必须导出 `Base`。

按技术文档 §3 的分组逐批补模型（已建 10 张表全部有 ORM）：

- A 身份组织：rooms / beds / devices / elders / personas ✚ staff_accounts / guardian_accounts ✅
- B 关系绑定：elder_staff_assignments / elder_guardians / staff_tokens ✅（Day 2）
- C 业务：voiceprints / medication_* / health_records / risk_events / work_orders* / ...
- D 内容日志：media_library / media_history / daily_reports / notifications / decision_logs / audit_logs

注意：后续 C/D 组建表时同样「先加模型类，再 autogenerate」，避免同类漂移。
"""

from app.models.audit import AuditLog
from app.models.base import NAMING_CONVENTION, Base
from app.models.conversation import (
    ConversationEvent,
    ConversationThread,
    ConversationTurn,
    ToolExecution,
)
from app.models.decision import DecisionLog
from app.models.guardian import GuardianAccount
from app.models.health import HealthRecord
from app.models.media import MediaHistory, MediaLibrary
from app.models.metrics import Metric
from app.models.notifications import Notification
from app.models.organization import Bed, Device, Elder, Persona, Room
from app.models.relation import ElderGuardian, ElderStaffAssignment
from app.models.report import DailyReport
from app.models.staff import StaffAccount, StaffToken
from app.models.voiceprint import Voiceprint

__all__ = [
    "NAMING_CONVENTION",
    "AuditLog",
    "Base",
    "Bed",
    "ConversationEvent",
    "ConversationThread",
    "ConversationTurn",
    "DailyReport",
    "DecisionLog",
    "Device",
    "Elder",
    "ElderGuardian",
    "ElderStaffAssignment",
    "GuardianAccount",
    "HealthRecord",
    "MediaHistory",
    "MediaLibrary",
    "Metric",
    "Notification",
    "Persona",
    "Room",
    "StaffAccount",
    "StaffToken",
    "ToolExecution",
    "Voiceprint",
]
