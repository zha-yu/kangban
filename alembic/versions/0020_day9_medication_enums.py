"""Day 9 dev-A1a（X8 契约）：medication_events 两枚举 CHECK 各扩至 5 值。

为什么今天改
------------
Day 8 审查缺口 F10 / F17 的枚举部分（day09 总览 §1.3 映射）：

1. `terminal_state` 缺 `late_taken_reported` —— PRD line 320-325 / AC-08：
   「已生成 missed_unconfirmed / notification_failed 工单后，长者随后补报
   '已经吃了'」需要**追加终态而不覆盖原终态**，但 CHECK 只有 4 值，
   写入会被数据库拒绝 → `late_taken_reported` 在现库不可达（F10）。
2. `failure_reason` 缺 `rtc_failed` —— PRD line 336 / FR-MED-06 / AC-41：
   反向 WebRTC 外呼建连失败降级为页面播报时，须记
   `failure_reason='rtc_failed'`（F17，PRD line 590 不得裁减项）。

改法（契约 docs/team_tasks/day09/contracts.md §X8）：

1. 本迁移：`terminal_state` 4→5 值（+`late_taken_reported`）、
   `failure_reason` 4→5 值（+`rtc_failed`）；
2. `app/models/medication.py` 两处 ORM CheckConstraint 同步（Q-A2 裁决：
   DDL 与 ORM 一致，否则 autogenerate 会再生成一条冗余迁移）；
3. TD line 778「Day 9 迁移 0 张」由本次提交在 TD 文末修订记录 v2.2 留痕
   （AGENTS §4.7，总览 §10 Q3）。

约束名
------
`Base.naming_convention`（app/models/base.py:20，`ck_%(table_name)s_%(constraint_name)s`）
会被 alembic env.py 套用到 op.drop_constraint / op.create_check_constraint 的
给定名上——**必须传短名**（`terminal_state` / `failure_reason`），由约定渲染成
`ck_medication_events_<short>`；传全名会被二次加前缀（实测踩坑：
`ck_medication_events_ck_medication_events_…` 不存在报错）。

downgrade
---------
先断言无新值行：若已存在 `late_taken_reported` / `rtc_failed` 行，
回退到 4 值 CHECK 会使这些行违反约束，故 downgrade 直接拒绝并提示
先清理数据——不静默删数据。改既有行数据在本迁移中一律不做。

不碰历史迁移
------------
down_revision = "0019_day8_medplan_version"。0001–0019 一字不改。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_day9_medication_enums"
down_revision: str | None = "0019_day8_medplan_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 5 值（目标态）：4 值原集 + 各追加 1 个新终态（契约 X8）
_TERMINAL_STATE_5 = (
    "taken_reported",
    "refused",
    "missed_unconfirmed",
    "notification_failed",
    "late_taken_reported",
)
_FAILURE_REASON_5 = ("no_answer", "denied", "tts_failed", "offline", "rtc_failed")

# 4 值（回退态）：0010 原集，一字不改
_TERMINAL_STATE_4 = _TERMINAL_STATE_5[:4]
_FAILURE_REASON_4 = _FAILURE_REASON_5[:4]

_TABLE = "medication_events"
# 短名：由 Base.naming_convention 渲染为 ck_medication_events_<short>（勿传全名，见上）
_CK_TERMINAL = "terminal_state"
_CK_FAILURE = "failure_reason"


def _in_list(column: str, values: tuple[str, ...]) -> str:
    """与 0010 同款的表达式生成：NULL 直通 + 枚举白名单。"""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IS NULL OR {column} IN ({quoted})"


def upgrade() -> None:
    # ① terminal_state 4→5 值（+late_taken_reported，PRD line 320-325）
    op.drop_constraint(_CK_TERMINAL, _TABLE, type_="check")
    op.create_check_constraint(_CK_TERMINAL, _TABLE, _in_list("terminal_state", _TERMINAL_STATE_5))

    # ② failure_reason 4→5 值（+rtc_failed，PRD line 336 / FR-MED-06）
    op.drop_constraint(_CK_FAILURE, _TABLE, type_="check")
    op.create_check_constraint(_CK_FAILURE, _TABLE, _in_list("failure_reason", _FAILURE_REASON_5))


def downgrade() -> None:
    # 回退保护：已有新值行时拒绝降级（回到 4 值 CHECK 会让这些行违反约束）
    conn = op.get_bind()
    stuck = conn.execute(
        sa.text(
            "SELECT count(*) FROM medication_events "
            "WHERE terminal_state = 'late_taken_reported' OR failure_reason = 'rtc_failed'"
        )
    ).scalar_one()
    if stuck:
        raise RuntimeError(
            f"downgrade 0020 refused: {stuck} row(s) use 'late_taken_reported'/'rtc_failed'; "
            "clean them up first (data-preserving, no silent delete)"
        )

    # ② failure_reason 还原 4 值
    op.drop_constraint(_CK_FAILURE, _TABLE, type_="check")
    op.create_check_constraint(_CK_FAILURE, _TABLE, _in_list("failure_reason", _FAILURE_REASON_4))

    # ① terminal_state 还原 4 值
    op.drop_constraint(_CK_TERMINAL, _TABLE, type_="check")
    op.create_check_constraint(_CK_TERMINAL, _TABLE, _in_list("terminal_state", _TERMINAL_STATE_4))
