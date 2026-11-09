"""Day 6 · admin 房间与床位管理：audit_logs 建表 + rooms.room_no 唯一 + beds.status 扩 disabled（0007）

**为什么这一个迁移**（计划 `docs/plans/adhoc-20261108-房间床位管理.md` §4）：
房间/床位管理是第一批要求「操作留痕可查询」的 admin 写功能，而 ER §4.4.6 的
`audit_logs` 原排 Day 9。三处 schema 变更必须原子落地，否则管理端写接口无处记审计：

1. `audit_logs`——ER §4.4.6 全字段 + 两条查询索引；只增不改（本迁移不提供任何更新语义）；
2. `rooms.room_no` 全局唯一——房号是人工识别键，重复会让「按房号筛选/删除」失去确定性；
   升级前先预检存量重复，有重复则中止并提示人工清理，**不自动删数据**；
3. `beds.status` 增加 `disabled`——管理端需要「停用」区别于 `maintenance`（维修中）；
   重建 CHECK，downgrade 时先把 disabled 床位回落为 maintenance，保证约束可逆。

已合入迁移 0001–0006 一字不改（AGENTS §4.6）。

人工审查要点（AGENTS §5）：
- 预检只读，不写库；发现重复直接 RuntimeError 中止 upgrade；
- `detail` 为 jsonb 可空，不含任何敏感字段默认值；
- downgrade 可逆：先降级数据（disabled → maintenance）再收紧 CHECK，最后删表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_admin_org_audit"
down_revision: str | None = "0006_day5_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 旧 CHECK（0001 建表 · 0003 改名后的最终名 ck_beds_status）
_BEDS_STATUS_OLD = "status IN ('available', 'occupied', 'maintenance')"
_BEDS_STATUS_NEW = "status IN ('available', 'occupied', 'maintenance', 'disabled')"


def _assert_no_duplicate_room_no() -> None:
    """room_no 唯一前预检：有重复就中止，把清理决定权留给人工。"""
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT room_no, count(*) AS cnt FROM rooms "
                "GROUP BY room_no HAVING count(*) > 1 ORDER BY room_no"
            )
        )
        .all()
    )
    if rows:
        detail = "、".join(f"{room_no}×{cnt}" for room_no, cnt in rows)
        raise RuntimeError(
            f"rooms.room_no 存在重复，无法建立唯一约束；请先人工清理后再 upgrade：{detail}"
        )


def upgrade() -> None:
    # ---------------- 1. audit_logs ----------------
    op.create_table(
        "audit_logs",
        sa.Column(
            "audit_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="审计编号（不叫 log_id，与 decision_logs.log_id 区分）",
        ),
        sa.Column(
            "actor_type",
            sa.Text(),
            nullable=False,
            comment="操作者类型：staff / guardian / system",
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="操作者 ID（系统动作可空）",
        ),
        sa.Column(
            "action",
            sa.Text(),
            nullable=False,
            comment="动作（create_room / update_bed / batch_delete_beds / unbind_device …）",
        ),
        sa.Column(
            "resource_type",
            sa.Text(),
            nullable=False,
            comment="操作对象类型（room / bed / device / elder …）",
        ),
        sa.Column(
            "resource_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="操作对象 ID（批量操作可空，明细见 detail）",
        ),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="操作细节（before/after、trace_id；越权拒绝也记）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="操作发生时间（UTC 存储）",
        ),
        sa.PrimaryKeyConstraint("audit_id", name="pk_audit_logs"),
        sa.CheckConstraint(
            "actor_type IN ('staff', 'guardian', 'system')",
            name="ck_audit_logs_actor_type",
        ),
        comment="审计日志表（只增不改；写库唯一入口 audit_log_service）",
    )
    op.create_index(
        "ix_audit_logs_resource_type_resource_id_created_at",
        "audit_logs",
        ["resource_type", "resource_id", "created_at"],
    )
    op.create_index(
        "ix_audit_logs_actor_type_actor_id_created_at",
        "audit_logs",
        ["actor_type", "actor_id", "created_at"],
    )

    # ---------------- 2. rooms.room_no 全局唯一 ----------------
    _assert_no_duplicate_room_no()
    op.create_unique_constraint("uq_rooms_room_no", "rooms", ["room_no"])

    # ---------------- 3. beds.status 扩 disabled ----------------
    op.drop_constraint("ck_beds_status", "beds", type_="check")
    op.create_check_constraint("ck_beds_status", "beds", _BEDS_STATUS_NEW)


def downgrade() -> None:
    # 先降级数据再收紧 CHECK，否则存量 disabled 床会让 downgrade 直接失败
    op.execute("UPDATE beds SET status = 'maintenance' WHERE status = 'disabled'")
    op.drop_constraint("ck_beds_status", "beds", type_="check")
    op.create_check_constraint("ck_beds_status", "beds", _BEDS_STATUS_OLD)

    op.drop_constraint("uq_rooms_room_no", "rooms", type_="unique")

    op.drop_index("ix_audit_logs_actor_type_actor_id_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_resource_type_resource_id_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
