"""Alembic 运行环境。

约定（见 AGENTS.md §5 高风险清单）：
- 连接串统一走 `app.core.config.get_settings().database_url`，与 `app.db` / pytest 同源：
  Pydantic Settings 的优先级是「进程环境变量 > 仓库根 .env > 字段默认值」，因此
  `export DATABASE_URL=...` 依然可以覆盖 `.env`。**禁止**在这里另读一份 os.environ。
- 项目用 asyncpg 异步驱动，迁移走同步驱动 psycopg，故此处做 `+asyncpg` → `+psycopg` 替换。
- `app.models` 尚不存在时（1.4 建骨架之前）退化为 `target_metadata=None`：
  手写迁移照常执行，只是 autogenerate 不可用——这正是 Day 1 的状态。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    """取同步连接串（迁移用 psycopg，不用 asyncpg）。

    走 Settings 而非裸 os.getenv：此前未 export DATABASE_URL 时会退回
    localhost:5432 的硬编码默认值，本机被其他 Postgres 占用时就会连错库
    （表现为 `password authentication failed for user "silvermate"`）。
    现在与 app.db / tests 读同一个 `.env`，`export DATABASE_URL=...` 仍优先生效。
    """
    raw = get_settings().database_url
    return raw.replace("postgresql+asyncpg://", "postgresql+psycopg://")


try:  # 1.4 建好 app/models 之后自动生效，autogenerate 才需要
    from app.models import Base  # type: ignore[import-not-found]

    target_metadata = Base.metadata
except Exception:  # 骨架未建立时属预期（ImportError 等），保持迁移可独立运行
    target_metadata = None


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL（`alembic upgrade head --sql`），不连库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
