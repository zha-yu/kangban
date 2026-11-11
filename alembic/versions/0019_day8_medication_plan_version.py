"""Day 8 dev-A H+1：medication_events 加 plan_version，唯一键升级为三列。

为什么今天改（G1 裁决，2026-11-11）
-----------------------------------
既有 `UNIQUE(plan_id, scheduled_at)` 唯一键不含 version 列。医生改药单的
drug_name / dose 但 schedule_cron 不变（同一分钟）时，scheduler 下次扫到
同一 `(plan_id, scheduled_at)` 但 `payload_hash` 不同 →
`MedicationConflict` → scheduler warning 后跳过提醒（静默丢失）。

改法（数据库唯一约束 + Service 层校验 + Scheduler 幂等性，三者组合）：

1. 本迁移：加 `plan_version INT NOT NULL DEFAULT 1` + 回填历史行 +
   删旧唯一约束 + 建新三列唯一约束 `UNIQUE(plan_id, scheduled_at, plan_version)`；
2. `medication_service.create_event` 加 `plan_version: int` 参数，两处唯一键
   查重（主查询 + IntegrityError 回查）和 INSERT 块都改用三列；
3. `medication_due_service.DuePlan` 加 `version: int`，
   `list_due_plans` 构造时多填一个字段；
4. `medication_jobs._trigger_plan` 传 `plan_version=plan.version`；
5. Service 层 update_plan 比对新旧 cron 是否变化，不变则审计打
   `cron_unchanged: true` 标记 + 响应对前端提示。

改完后 plan 改版 → version+1 → 新事件带新版本号 → 唯一键天然不冲突。
历史事件 drug_snapshot 是独立 JSONB 列，plan 改版不影响——AC-27 自动满足。

回填策略
--------
历史行的 plan_version 从 plan 表当前 version 查回来（
`UPDATE medication_events e SET plan_version = p.version FROM medication_plans p
WHERE e.plan_id = p.plan_id`）。Day 6 以后没改过 version 列，值恒为 1，
但 SQL 写法保险（未来某天有人手动改了 version，回填也能对齐）。

downgrade
---------
逆序：删新唯一约束 → 删 plan_version 列 → 还原旧唯一约束。downgrade
时历史行 plan_version 字段丢失，旧唯一键 `(plan_id, scheduled_at)` 自动
恢复原始约束——不需要二次回填（drop column 就没了）。

不碰历史表
----------
down_revision = "0018_day8_media"。0001–0018 一字不改。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_day8_medplan_version"
down_revision: str | None = "0018_day8_media"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ① 加 plan_version 列
    op.add_column(
        "medication_events",
        sa.Column(
            "plan_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
            comment="药单版本（与 medication_plans.version 联动；改药单后新事件自动升级到新版本）",
        ),
    )

    # ② 回填历史行（从 plan 表查当前 version，保险写法——即使将来 version 不等于 1 也能对齐）
    op.execute(
        sa.text(
            "UPDATE medication_events e "
            "SET plan_version = p.version "
            "FROM medication_plans p "
            "WHERE e.plan_id = p.plan_id"
        )
    )

    # ③ 删旧唯一约束（0010 迁移创建的）
    op.drop_index(
        "uq_medication_events_plan_id_scheduled_at",
        table_name="medication_events",
    )

    # ④ 建新三列唯一约束（plan_id + scheduled_at + plan_version）
    op.create_index(
        "uq_medication_events_plan_id_scheduled_at_plan_version",
        "medication_events",
        ["plan_id", "scheduled_at", "plan_version"],
        unique=True,
    )


def downgrade() -> None:
    # ④ 删新三列唯一约束
    op.drop_index(
        "uq_medication_events_plan_id_scheduled_at_plan_version",
        table_name="medication_events",
    )

    # ③ 还原旧两列唯一约束（0010 的原始约束名）
    op.create_index(
        "uq_medication_events_plan_id_scheduled_at",
        "medication_events",
        ["plan_id", "scheduled_at"],
        unique=True,
    )

    # ② 删 plan_version 列（历史数据随列消失，不需要额外清理）
    op.drop_column("medication_events", "plan_version")
