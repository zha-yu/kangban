"""Day 2 · 修正 Day 1 CHECK 约束命名（双前缀 → 约定产出名）

**背景**：0001 书写时给 CHECK 传了显式全名（如 `ck_rooms_status`），但 env.py 的
`target_metadata`（`app/models/base.py` 的 naming_convention，ck 模板含
`%(constraint_name)s`）会把显式名再代入模板一次，落库成了 `ck_rooms_ck_rooms_status`。
0002 起已改用短名（`name="status"`）规避；本迁移把 0001 落库的四个旧名改成与约定
模板产出一致的最终名，保证 task-6 补齐 Day 1 五张表的 ORM 后 autogenerate 不漂移。

**为什么不动 0001 本体**：AGENTS.md §4.6 —— 已合入的 Alembic 历史迁移不改，只新增。
RENAME 方案对所有已执行过 0001 的库（含队友本机）同样生效。

**人工审查要点**（AGENTS.md §5）：仅 4 条 `ALTER TABLE ... RENAME CONSTRAINT`，
可逆（downgrade 逆改名）；不触碰任何数据行。

Revision ID: 0003_fix_day1_ck_names
Revises: 0002_day2
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_fix_day1_ck_names"
down_revision: str | None = "0002_day2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (表名, 错误名, 正确名)——正确名 = ck 模板 ck_%(table_name)s_%(constraint_name)s 代入短名
_RENAMES: list[tuple[str, str, str]] = [
    ("rooms", "ck_rooms_ck_rooms_status", "ck_rooms_status"),
    ("elders", "ck_elders_ck_elders_status", "ck_elders_status"),
    ("beds", "ck_beds_ck_beds_status", "ck_beds_status"),
    ("devices", "ck_devices_ck_devices_status", "ck_devices_status"),
]


def upgrade() -> None:
    for table, old, new in _RENAMES:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new}")


def downgrade() -> None:
    for table, old, new in _RENAMES:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {new} TO {old}")
