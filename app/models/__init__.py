"""ORM 模型包。

`alembic/env.py` 通过 `from app.models import Base` 拿 metadata，因此这里必须导出 `Base`。

后续按技术文档 §3 的分组逐批补模型（Day1 五张已在 `alembic/versions/0001_day1_*.py`
手写入库，ORM 类在 Day2 的档案 CRUD 时补齐）：

- A 身份组织：rooms / beds / devices / elders / personas / staff_accounts / guardian_accounts
- B 关系绑定：elder_staff_assignments / elder_guardians / staff_tokens
- C 业务：voiceprints / medication_* / health_records / risk_events / work_orders* / ...
- D 内容日志：media_library / media_history / daily_reports / notifications / decision_logs / audit_logs

注意：模型类没写全之前**不要跑 `alembic revision --autogenerate`**，否则它会认为库里
已有的 5 张表是多余的，生成 `drop_table`。
"""

from app.models.base import NAMING_CONVENTION, Base

__all__ = ["NAMING_CONVENTION", "Base"]
