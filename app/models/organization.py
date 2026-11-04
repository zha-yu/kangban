"""Day 1 身份组织五表：personas / rooms / elders / beds / devices（ER §4.1.1–§4.1.5）。

与 `alembic/versions/0001_day1_*.py` 的 DDL 一一对应：CHECK 只给短名（name="status"），
由 `Base.naming_convention` 渲染成 ck_<table>_status（0003 迁移已把 0001 落库的
双前缀名 RENAME 成同名）；FK / UNIQUE / PK 不写名，全交约定推导。
devices 的部分唯一索引走显式名 uq_devices_elder_id（uq_ 前缀不经 ix 模板，必须显式给）。
列注释逐字对齐 0001——多写或少写 comment 都会让 autogenerate 产生漂移 diff。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Persona(Base):
    """数字人人设表：性格/开场白/口吻示例拼进 system prompt 固定前缀（保 prompt cache）。"""

    __tablename__ = "personas"
    __table_args__ = ({"comment": "数字人人设表"},)

    persona_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="人设名（小满 / 孙女 / 老同事…）"
    )
    personality: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="性格描述，拼进 system prompt 固定前缀"
    )
    greeting: Mapped[str] = mapped_column(Text(), nullable=False, comment="开场白")
    tone_examples: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(), nullable=True, comment="口吻示例 few-shot，≤3 条，保 prompt cache 稳定"
    )
    voice_id: Mapped[str] = mapped_column(Text(), nullable=False, comment="TTS 音色 ID")
    avatar_url: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="VRM 模型地址（nginx 静态托管）"
    )


class Room(Base):
    """房间表：status 由 beds 聚合得出，服务层维护，模型不做联动。"""

    __tablename__ = "rooms"
    __table_args__ = (
        CheckConstraint("status IN ('available', 'partial', 'full', 'maintenance')", name="status"),
        {"comment": "房间表"},
    )

    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    room_no: Mapped[str] = mapped_column(Text(), nullable=False, comment="房号（如 A-301）")
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'available'"),
        comment="由 beds 聚合，服务层维护",
    )


class Elder(Base):
    """长者表：session_seq 每新会话 +1，拼进 thread_id（LangGraph 断点恢复的根）。"""

    __tablename__ = "elders"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'away', 'transferred', 'archived')", name="status"),
        {"comment": "长者表"},
    )

    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text(), nullable=False, comment="姓名（演示数据，日志脱敏）")
    birth_date: Mapped[datetime.date | None] = mapped_column(
        Date(), nullable=True, comment="出生日期；用 date 不用 text（可算年龄）"
    )
    gender: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="性别，可选填")
    persona_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("personas.persona_id"), nullable=True, comment="数字人人设分配"
    )
    status: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'active'"))
    session_seq: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        server_default=text("0"),
        comment="会话计数器，每新会话 +1 → thread_id",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Bed(Base):
    """床位表：(room_id, bed_no) 唯一；elder_id 为当前入住长者，空床 NULL。"""

    __tablename__ = "beds"
    __table_args__ = (
        UniqueConstraint("room_id", "bed_no"),
        CheckConstraint("status IN ('available', 'occupied', 'maintenance')", name="status"),
        {"comment": "床位表"},
    )

    bed_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    room_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rooms.room_id"),
        nullable=False,
        comment="所属房间；双人间声纹候选集由设备房间推导",
    )
    bed_no: Mapped[str] = mapped_column(Text(), nullable=False, comment="床号（1 号床）")
    status: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'available'"))
    elder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("elders.elder_id"), nullable=True, comment="当前入住长者，空床为 NULL"
    )


class Device(Base):
    """模拟设备表：设备:长者 1:1（部分唯一索引，未绑定设备可先行入库）。

    token_hash 只存长期令牌哈希，解绑即轮换失效（裁决 D2）。
    """

    __tablename__ = "devices"
    __table_args__ = (
        Index(
            "uq_devices_elder_id",
            "elder_id",
            unique=True,
            postgresql_where=text("elder_id IS NOT NULL"),
        ),
        CheckConstraint("status IN ('online', 'offline', 'locked')", name="status"),
        {"comment": "模拟设备表"},
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    room_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rooms.room_id"), nullable=False, comment="所在房间 → 决定声纹候选范围"
    )
    elder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("elders.elder_id"), nullable=True, comment="绑定主使用者；设备:长者 1:1，可空"
    )
    token_hash: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="设备长期令牌哈希，解绑即失效"
    )
    status: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'offline'"))
    last_heartbeat: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="超 2 个心跳周期判掉线"
    )
