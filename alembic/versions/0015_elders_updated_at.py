"""Day 7 B3（P2-20）：`elders` 加 `updated_at` 列——E20 改绑乐观并发 CAS 基准。

为什么需要这条迁移
------------------
E20 全量覆盖式改绑的 stale 写风险：admin A 加载绑定列表后页面停留，admin B 完成一次
改绑，A 再提交会把 B 的变更静默冲掉（全量覆盖语义）。总表 §5.7 冻结契约要求
"乐观并发保护、后提交者 409"，但 `elders` 表没有版本/时间戳列可用，服务层无从比较。

口径（经主理人裁决，2026-11-10）
--------------------------------
- 新增 `updated_at`（timezone-aware，默认 `now()`）作为 CAS 基准；
- E20 请求体加**可选** `expected_updated_at`：不传 → 只走行锁（D2 不被阻塞）；
  传了 → 服务端比较涉改动长者的 `updated_at`，页面加载后有变更 → 409 `STALE_WRITE`；
- `updated_at` 由 `staff_service.reassign_elders` 成功路径**显式推进**（涉改动的长者行），
  不用 ORM `onupdate`：CAS 基准只在改绑成功时移动，其他长者行写入不产生误报。

回滚
----
drop_column 即可，无数据依赖。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015_elders_updated_at"
down_revision: str | None = "0014_metrics_response_segment"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "elders",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="乐观并发 CAS 基准（E20 改绑成功时由服务层显式推进）",
        ),
    )


def downgrade() -> None:
    op.drop_column("elders", "updated_at")
