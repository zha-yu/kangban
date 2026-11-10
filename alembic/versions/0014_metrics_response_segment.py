"""Day 7 B3（P1-12）：`metrics.stage` 白名单加值 `response_segment`。

为什么需要这条迁移
------------------
`response_service` 自 Day 5 起对每个分段打 `response_segment` 耗时点，但 0006 的
CHECK 约束（与 metrics_service 白名单同源）没有这个值——每一行都在服务层被白名单
丢弃并打 WARNING（每段一条，日志噪音 + 全部打点丢失）。审查 P1-12 指认。

只改白名单不改约束会让 INSERT 违反 CHECK、整批打点被丢——比现状更糟，因此
模型 CHECK、服务白名单、本迁移三处同步加值。

只做加值
--------
不动语义、不动已有行——原七值全部仍是合法值。

可回滚性
--------
downgrade 恢复 0006 的七值白名单。若库中已写入 `response_segment`，downgrade 会因
CHECK 拒绝而失败——需先清理含新值的行再降级（与 0013 同口径）。
"""

from __future__ import annotations

from alembic import op

revision: str = "0014_metrics_response_segment"
down_revision: str | None = "0013_touch_played_receipt"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

#: 0006 的七值白名单（保留兼容已有行）。
_PREVIOUS: tuple[str, ...] = (
    "wakeup",
    "connection",
    "asr",
    "route",
    "llm_first_token",
    "tts_first_packet",
    "playback",
)
#: B3 新增：分段下发→ack 的服务端耗时（response_service.record_stage）。
_SEGMENT: tuple[str, ...] = ("response_segment",)


def _check_expr(values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"stage IN ({joined})"


def upgrade() -> None:
    # ⚠️ 不能用 op.drop_constraint：0006 在 create_table 里显式传了全名
    # "ck_metrics_stage"，又被 naming_convention 套了一层 → 库里实际是
    # "ck_metrics_ck_metrics_stage"（双重展开脏名，测试库 pg_constraint 已核实）。
    # op.drop_constraint 的 name 参数会被 convention 再展开一次（0013 记录的坑），
    # 传短名/全名都对不上。这里用裸 SQL 删除脏名约束，再用短名 "stage" 重建——
    # 重建后落为规范名 "ck_metrics_stage"，与 ORM 模型（name="stage" 经 convention
    # 展开为 ck_metrics_stage）一致，顺带修正 0006 的历史脏名。
    op.execute("ALTER TABLE metrics DROP CONSTRAINT ck_metrics_ck_metrics_stage")
    op.create_check_constraint("stage", "metrics", _check_expr(_PREVIOUS + _SEGMENT))


def downgrade() -> None:
    op.execute("ALTER TABLE metrics DROP CONSTRAINT ck_metrics_stage")
    op.create_check_constraint("stage", "metrics", _check_expr(_PREVIOUS))
