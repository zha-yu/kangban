"""ORM 基类。本文件的存在会激活 `alembic/env.py` 里的 `target_metadata`。

约定（技术文档 §3）：表名复数 snake_case、主键 `<entity>_id`、时间列 `timestamptz`、
布尔列 `is_` 前缀、枚举值小写下划线 —— 这些都是模型作者的责任，Base 只统一**约束命名**。

为什么要 `naming_convention`：不写它，Alembic autogenerate 与 Postgres 默认命名会漂移，
后续每次 autogenerate 都冒出一堆重命名 diff。命名与 `alembic/versions/0001_day1_*.py`
里手写的 `ck_elders_status` / `fk_elders_persona_id_personas` / `uq_devices_elder_id` 一致。
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# 约束命名模板：%(constraint_name)s 由 CheckConstraint(name=...) 提供
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的父类。`Base.metadata` 即 Alembic 的 target_metadata。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
