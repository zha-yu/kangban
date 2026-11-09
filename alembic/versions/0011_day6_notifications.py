"""Day 6 · notifications 通知表（0011）——N27 安全一票否决的通知落库（裁决 C-0-5/C-0-6）

**为什么提前到 Day 6**（计划 `docs/plans/day06/dev-c-Agent与统一出口.md` §0.1，
主理人 2026-11-09 裁决）：

原排 Day 8 与 mock-operations 一起建的 `notifications` 表提前——N27 安全一票否决
（AC-04）命中时要「红色事件 + 工单 + 护工通知」三行同事务落库，**落库即视为已通知**；
家属侧 `send_family_notification` 仍走 Day 8 mock-operations，范围不变。

字段逐列对齐 TD §3.5「notifications」（line 659-676）与 ER §4.4.4，不得增删：

1. `payload` 用 jsonb（TD 原文口径），不拆 title/content 两列；
2. `dedupe_key` 部分唯一 `WHERE dedupe_key IS NOT NULL`（裁决 P1：同一幂等键只发一次，
   空值不冲突）——TD line 729 已明文该条件，不按 status 过滤；
3. `target_id` / `acknowledged_by` 是多态目标（guardian/staff 两表），**故意不建 FK**；
4. 枚举 CHECK 按 ER §5.3 口径只落 target_type / channel / status 三个：`type` 是开放
   枚举（TD 原文「emergency_health/sensitive/missed_medication/report_ready/sos…」），
   ER §5.3 未给 `type` 设 CHECK，本迁移照抄不擅自收口；
5. 索引照 TD §3.6 必需清单：重试扫描 `(status, created_at)`、未读列表
   `(target_type, target_id, status)`、部分唯一 `uq_notifications_dedupe_key`；
6. 时间列全部 timestamptz（存储一律 UTC，展示层转 Asia/Shanghai，AGENTS §8）。

已合入迁移 0001–0010 一字不改。

人工审查要点（AGENTS §5）：
- downgrade 逆序删索引与表，可回滚到 0010；
- 不写死任何连接串；无 PG ENUM（裁决 C6）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_day6_notifications"
down_revision: str | None = "0010_day6_medication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGET_TYPE = ("guardian", "staff")
_CHANNEL = ("inapp", "webpush")
_STATUS = ("generated", "pending", "delivered", "acknowledged", "failed")


def _in_list(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column(
            "notification_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="通知编号",
        ),
        sa.Column(
            "target_type",
            sa.Text(),
            nullable=False,
            comment="guardian 监护人 / staff 员工（多态目标，不建 FK）",
        ),
        sa.Column(
            "target_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="目标对象 ID（多态目标，不建 FK）",
        ),
        sa.Column(
            "type",
            sa.Text(),
            nullable=False,
            comment="emergency_health/sensitive/missed_medication/report_ready/sos…"
            "（开放枚举，ER §5.3 未设 CHECK）",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="通知内容（脱敏）",
        ),
        sa.Column(
            "channel",
            sa.Text(),
            nullable=False,
            comment="inapp 站内（保底）/ webpush 浏览器推送（无微信、无邮件）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="generated/pending/delivered/acknowledged/failed（送达≠确认）",
        ),
        sa.Column(
            "is_mock",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="是否模拟通知（默认 false；不计入送达率分母）",
        ),
        sa.Column(
            "retry_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="重试次数（pending 每 30s 重试，3 次失败 → failed）",
        ),
        sa.Column(
            "dedupe_key",
            sa.Text(),
            nullable=True,
            comment="幂等键（部分唯一：同一键只发一次，空值不冲突，裁决 P1）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="生成时间（重试计时起点；与 sent_at 区分：生成后可能延迟发送）",
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True, comment="发送时间"),
        sa.Column(
            "acknowledged_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="确认人（staff/guardian_id，多态不建 FK；AC-23 三态严格区分）",
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True, comment="确认时间"),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True, comment="阅读时间"),
        sa.PrimaryKeyConstraint("notification_id", name="pk_notifications"),
        sa.CheckConstraint(_in_list("target_type", _TARGET_TYPE), name="target_type"),
        sa.CheckConstraint(_in_list("channel", _CHANNEL), name="channel"),
        sa.CheckConstraint(_in_list("status", _STATUS), name="status"),
        comment="通知表（所有外发通知统一落库；无微信，多态 target 不建 FK）",
    )
    # ---- TD §3.6 必需索引：重试扫描 / 未读列表 / 幂等键部分唯一 ----
    op.create_index("ix_notifications_status_created_at", "notifications", ["status", "created_at"])
    op.create_index(
        "ix_notifications_target_type_target_id_status",
        "notifications",
        ["target_type", "target_id", "status"],
    )
    op.create_index(
        "uq_notifications_dedupe_key",
        "notifications",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_notifications_dedupe_key", table_name="notifications")
    op.drop_index("ix_notifications_target_type_target_id_status", table_name="notifications")
    op.drop_index("ix_notifications_status_created_at", table_name="notifications")
    op.drop_table("notifications")
