"""Day 9 P15（dev-C 需求，主理人授权 B 自行交付）：`metrics` 加 `llm_tokens` stage 与 token 列。

为什么需要这条迁移
------------------
B4 交付的 `record_llm_usage()`（LLM 每次成功调用的 token 用量观测）只有
「结构化日志 + 进程内计数」两层——进程内计数**非持久**，重启即丢、多 worker
不可见；家属端/console 的 LLM 成本统计要落库才能看。落点按既定契约是
`metrics` 表新增 stage 值 `llm_tokens`（一行 = 一次调用）。

与 0014 同款纪律：模型 CHECK、服务白名单、本迁移三处同步加值，只加不改。

列设计
------
- `prompt_tokens` / `completion_tokens` / `total_tokens`：`Integer` **可空**。
  NULL = 供应商未回 usage（「缺」与 0 语义不同，不补零——B4 的 LLMReply 口径）；
- `duration_ms` 语义复用：`llm_tokens` 行恒 0（它不是耗时打点，非空约束不变）；
- provider / model 不入库：`source` 列已有口径位（`'llm_usage'`），明细在
  `llm_usage` 结构化日志里，metrics 表只写指标不写内容（表注释红线）。

可回滚性
--------
downgrade 删三列、恢复 0020 后的八值白名单。若库中已写入 `llm_tokens` 行，
downgrade 会因 CHECK 拒绝而失败——需先清理含新值的行再降级（0014 同口径）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0021_day9_llm_tokens"
down_revision: str | None = "0020_day9_medication_enums"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

#: 0020 后的八值白名单（保留兼容已有行）。
_PREVIOUS: tuple[str, ...] = (
    "wakeup",
    "connection",
    "asr",
    "route",
    "llm_first_token",
    "tts_first_packet",
    "playback",
    "response_segment",
)
#: P15 新增：LLM 单次调用 token 用量（一行 = 一次成功调用）。
_LLM_TOKENS: tuple[str, ...] = ("llm_tokens",)


def _check_expr(values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"stage IN ({joined})"


def upgrade() -> None:
    op.drop_constraint("stage", "metrics", type_="check")
    op.create_check_constraint("stage", "metrics", _check_expr(_PREVIOUS + _LLM_TOKENS))
    op.add_column(
        "metrics",
        sa.Column(
            "prompt_tokens",
            sa.Integer(),
            nullable=True,
            comment="LLM 调用输入 token 数（NULL = 供应商未回 usage，不补零）",
        ),
    )
    op.add_column(
        "metrics",
        sa.Column(
            "completion_tokens",
            sa.Integer(),
            nullable=True,
            comment="LLM 调用输出 token 数（NULL = 供应商未回 usage，不补零）",
        ),
    )
    op.add_column(
        "metrics",
        sa.Column(
            "total_tokens",
            sa.Integer(),
            nullable=True,
            comment="LLM 调用总 token 数（仅两侧都已知时非 NULL）",
        ),
    )


def downgrade() -> None:
    op.drop_column("metrics", "total_tokens")
    op.drop_column("metrics", "completion_tokens")
    op.drop_column("metrics", "prompt_tokens")
    op.drop_constraint("stage", "metrics", type_="check")
    op.create_check_constraint("stage", "metrics", _check_expr(_PREVIOUS))
