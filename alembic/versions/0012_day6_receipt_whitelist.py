"""Day 6 E2：`medication_attempts.receipt` 白名单扩展（主理人 2026-11-09 21:16 裁决）。

为什么需要这条迁移
------------------
0010 建 `receipt` CHECK 时按**占位实现**定了三值（`card_rendered` / `tts_finished` /
`none`）。E2 接线（信令层 `deliver()` 就绪）后，触达回执变成真实的**投递结果**，
三个新值不在白名单里：

- `voice_dispatched`：至少一台绑定设备把消息收进了发送队列；
- `device_offline`：有绑定设备但全部离线 / 未鉴权；
- `no_bound_device`：长者根本没绑设备（运维缺口）。

CHECK 拒绝 → `medication_attempts` 写不进去 → 四次触达全空 → `missed_unconfirmed`
链路整体断裂。更隐蔽的是，`record_attempt` 原先把所有 `IntegrityError` 当幂等冲突
吞掉，CHECK 违反会**静默消失**（触达记录凭空丢失，日志还挂着误导性的 duplicate 名）。
该吞异常语义已在本批修复（回查确认真有同键记录才算幂等）。

L11 破例说明
------------
L11（0011 归 dev-C，dev-A/B 本日不再新建迁移）的背景是三人并行写迁移怕双 head。
2026-11-09 21:16 时 A / B / C 的工作均已合入 develop、A 是唯一动代码的人，
主理人裁决授权本迁移（E2 与 receipt 白名单是绑定的，拆开没有意义）。
这是 L11 的唯一一次破例，后续迁移仍归路由日。

只做加值
--------
不动语义、不动已有行——旧三值全部仍是合法值（`card_rendered` 是占位期的产物，
历史数据里存在，保留兼容）。

可回滚性
--------
downgrade 恢复三值白名单。若库中已写入新值，downgrade 会因 CHECK 拒绝而失败——
需先清理或归档含新值的行再降级。教学期演示数据可接受；生产化时（Day 10 收口）
应改为「置 NULL 再收窄」的两步降级。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012_day6_receipt_whitelist"
down_revision: str | None = "0011_day6_notifications"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

#: 0010 的占位三值（保留兼容已有行）。
_ORIGINAL: tuple[str, ...] = ("card_rendered", "tts_finished", "none")
#: E2 接线后的投递回执三态（本迁移新增）。
_DISPATCH: tuple[str, ...] = ("voice_dispatched", "device_offline", "no_bound_device")


def _check_expr(values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"receipt IS NULL OR receipt IN ({joined})"


def upgrade() -> None:
    # ⚠️ 必须传**短名** "receipt"：op.drop_constraint / op.create_check_constraint 的
    # name 参数会被 naming_convention 再展开一次（ck_%(table_name)s_%(constraint_name)s）。
    # 传全名会变成 ck_medication_attempts_ck_..._receipt——0007 踩过同一个坑，
    # 0010 因此全程用短名。本迁移第一次验证就在这里炸过，记录在此以免重犯。
    op.drop_constraint("receipt", "medication_attempts", type_="check")
    op.create_check_constraint(
        "receipt", "medication_attempts", _check_expr(_ORIGINAL + _DISPATCH)
    )


def downgrade() -> None:
    op.drop_constraint("receipt", "medication_attempts", type_="check")
    op.create_check_constraint("receipt", "medication_attempts", _check_expr(_ORIGINAL))
