"""Day 7 TD-2：`medication_attempts.receipt` 白名单加值 `played`（触达 played 闭环）。

为什么需要这条迁移
------------------
0012 的三态投递回执只证明「投递」（voice_dispatched = 收进发送队列），证明不了
「听到」。TD-2（day07/contracts.md §2）让 pad 播完触达语音后回 `touch_played`，
服务端幂等落库——`played` 表达「触达语音真被听到」，是四态之外的第七个白名单值。

裁决留痕（day07/contracts.md §3）
----------------------------------
「receipt 加新值 `played` vs 复用 `tts_finished`」是 TD-2 任务卡留下的裁决点：
`tts_finished` 是 0010 占位口径的历史值，复用会让「送达」与「听到」两件事共用
一个词。本迁移按**新增 `played`**落地（dev-C 自裁，登记待主理人复核；否决时只需
收窄白名单并改 `mark_attempt_played` 的落值，不动消息契约与前端）。

只做加值
--------
不动语义、不动已有行——前六值全部仍是合法值。

可回滚性
--------
downgrade 恢复 0012 的六值白名单。若库中已写入 `played`，downgrade 会因 CHECK
拒绝而失败——需先清理或归档含新值的行再降级。教学期演示数据可接受；生产化时
（Day 10 收口）应改为「置 NULL 再收窄」的两步降级。
"""

from __future__ import annotations

from alembic import op

revision: str = "0013_touch_played_receipt"
down_revision: str | None = "0012_day6_receipt_whitelist"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

#: 0012 的六值白名单（保留兼容已有行）。
_PREVIOUS: tuple[str, ...] = (
    "card_rendered",
    "tts_finished",
    "none",
    "voice_dispatched",
    "device_offline",
    "no_bound_device",
)
#: TD-2 新增：触达语音真被听到（只允许从 voice_dispatched 升级）。
_PLAYED: tuple[str, ...] = ("played",)


def _check_expr(values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"receipt IS NULL OR receipt IN ({joined})"


def upgrade() -> None:
    # ⚠️ 必须传**短名** "receipt"：naming_convention 会把 name 参数再展开一次，
    # 传全名会变成 ck_medication_attempts_ck_..._receipt（0007 踩过的坑，0012 已记录）。
    op.drop_constraint("receipt", "medication_attempts", type_="check")
    op.create_check_constraint("receipt", "medication_attempts", _check_expr(_PREVIOUS + _PLAYED))


def downgrade() -> None:
    op.drop_constraint("receipt", "medication_attempts", type_="check")
    op.create_check_constraint("receipt", "medication_attempts", _check_expr(_PREVIOUS))
