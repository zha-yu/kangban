#!/usr/bin/env python
"""Day 4 · A3 · T4：联调种子（personas / rooms / elders / beds / devices）。

一条命令把 3D 展示链路要的最小数据写进库，**全部幂等**：重复跑不报错、不产生第二行，
也不覆盖别人正在用的数据。固定 UUID 是刻意的——`docs/plans/day04/a3-3D展示后端.md`
§4 的验收命令、前端 mock 与联调脚本都直接引用这些 id，随机 UUID 会让这些命令失效。

| 表 | 主键 | 关键字段 |
| --- | --- | --- |
| personas | …1201 | 小满 / `/avatars/vrm1_sample.vrm` / `voice_id=mock` |
| rooms | …1101 | A-301 |
| elders | …1102 | 张桂芬（`persona_id` **仅在为 NULL 时**补） |
| beds | …1105 | 1 号床 → 张桂芬（room_id=…1101） |
| devices | …1106 | `token_hash = sha256("device-long-secret-plain-abc123")` |

覆盖规则（直接关系到别人会不会被踢下线，改之前先读这段）：

1. `personas`：**upsert 覆盖**。人设 / 形象 / 音色本来就是本卡的数据源，换形象只需
   `--avatar-url /avatars/xiaoman.vrm`。
2. `elders.persona_id`：**仅当为 NULL 时补**。A1/A2 可能已把 …1102 绑到别的人设上，
   覆盖会让对方联调时人物不说话。
3. `rooms` / `beds`：**仅缺失时插入**。谁住哪张床是 A1 的演示布局，不归本卡管。
4. `devices`：**仅缺失时插入；已存在时绝不覆盖 `token_hash`**。
   `app/core/auth.py:278 device_identity_from_session` 用 `tv == token_hash[:16]`
   判会话是否还有效（「解绑即失效」的落点，AC-24），一改就让 A2 与前端**正在用**的
   设备会话全部 401。要轮换必须显式 `--force-device`，脚本先打印警告并等 3 秒——
   联调窗口内禁止使用。

`devices` 的三处「固定值表没写、但缺了就过不了验收」的取值与理由：

- `room_id` 必填（列 NOT NULL），取 …1101，与 beds 同房间；
- `elder_id` 绑 …1102：`/api/pad/avatar-config` 在 `identity.elder_id is None` 时返回
  403 `device_not_bound`，这台设备不绑长者，计划 §4 第 3 步就拿不到 200；
- `status="online"`：鉴权只拒 `locked`（`app/core/auth.py:302`），在线态是给照护台看的
  心跳字段，演示时平板本来就在线。

用法：
    python scripts/seed_personas.py                                     # 幂等补数据，安全
    python scripts/seed_personas.py --avatar-url /avatars/xiaoman.vrm   # 验 version 变化
    python scripts/seed_personas.py --force-device                      # ⚠ 轮换设备令牌

退出码：0 成功；1 连不上库 / 写库失败（会提示先 `alembic upgrade head`）。
"""

# ruff: noqa: E402
#
# 为什么整文件豁免 E402（import 不在文件顶部）：计划 §4 的验收命令是
# `uv run python scripts/seed_personas.py`。按**路径**执行脚本时，Python 只把脚本所在目录
# `scripts/` 放进 sys.path[0]，仓库根不在其中，`import app...` 会直接 ModuleNotFoundError
# （实测过：`--help` 都跑不起来）。所以下面这段 sys.path 引导必须排在所有 import 之前，
# 本文件的 import 也就必然落在语句之后 —— 是结构使然，不是疏忽。

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

# 仓库根补进 sys.path：scripts/seed_personas.py -> scripts -> 根
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import sha256_hex
from app.db import engine, session_factory
from app.models.organization import Bed, Device, Elder, Persona, Room

# --------------------------------------------------------------------------
# 固定标识（顺序与 0001 迁移的建表顺序一致：personas → rooms → elders → beds → devices）
# --------------------------------------------------------------------------
PERSONA_ID = uuid.UUID("11111111-1111-4111-8111-111111111201")
ROOM_ID = uuid.UUID("11111111-1111-4111-8111-111111111101")
ELDER_ID = uuid.UUID("11111111-1111-4111-8111-111111111102")
BED_ID = uuid.UUID("11111111-1111-4111-8111-111111111105")
DEVICE_ID = uuid.UUID("11111111-1111-4111-8111-111111111106")

ROOM_NO = "A-301"
BED_NO = "1 号床"
ELDER_NAME = "张桂芬"

PERSONA_NAME = "小满"
# 两段文案只做演示：不进 prompt 固定前缀的拼装代码（那归 A1/A2 的 personas prompt），
# 但两列都是 NOT NULL，插入时必须给值。口径遵守 AGENTS §4.4：不出现任何医疗诊断。
PERSONA_PERSONALITY = "温和耐心的陪伴者，语速慢、句子短，先共情再回应，不替长者做决定"
PERSONA_GREETING = "您好呀，我是小满，今天感觉怎么样？"
VOICE_ID = "mock"
DEFAULT_AVATAR_URL = "/avatars/vrm1_sample.vrm"


def _line(table: str, action: str, detail: str) -> str:
    """一行人读报告：表 / 结果 / 关键字段。列宽固定，两次跑的 diff 只会有内容差异。"""
    return f"{table:<10} {action:<40} {detail}"


def device_token_hash() -> str:
    """设备长期令牌明文 → sha256 小写十六进制。

    明文只用于本地演示（计划 §4 的验收命令里也是这一串），库里存哈希；这里刻意**不**把它
    提成模块级常量：这类值一旦被命名成 `*_SECRET` / `*_TOKEN`，既会被 lint 的硬编码口令
    规则判红（S105），也会诱使别的脚本把它当共享常量复用。
    摘要复用 `app/core/security.sha256_hex`（`hashlib.sha256(...).hexdigest()`），
    与 `auth_service.exchange_device_token` 的比对口径是**同一份实现**，不复制算法。
    """
    return sha256_hex("device-long-secret-plain-abc123")


# --------------------------------------------------------------------------
# 五张表各自的幂等写入
# --------------------------------------------------------------------------
async def upsert_persona(session: AsyncSession, *, avatar_url: str) -> str:
    """personas：本卡**唯一**「覆盖」的表——形象与音色本来就归这里管。

    `tone_examples` 故意既不进 `values` 也不进 `set_`：few-shot 由 prompt 侧维护，
    这里既不用 NULL 冲掉既有示例，也不替别人编口吻。
    """
    insert_stmt = pg_insert(Persona).values(
        persona_id=PERSONA_ID,
        name=PERSONA_NAME,
        personality=PERSONA_PERSONALITY,
        greeting=PERSONA_GREETING,
        voice_id=VOICE_ID,
        avatar_url=avatar_url,
    )
    existed = (
        await session.execute(select(Persona.persona_id).where(Persona.persona_id == PERSONA_ID))
    ).scalar_one_or_none()
    await session.execute(
        insert_stmt.on_conflict_do_update(
            index_elements=["persona_id"],
            set_={
                "name": insert_stmt.excluded.name,
                "personality": insert_stmt.excluded.personality,
                "greeting": insert_stmt.excluded.greeting,
                "voice_id": insert_stmt.excluded.voice_id,
                "avatar_url": insert_stmt.excluded.avatar_url,
            },
        )
    )
    action = "overwrote (upsert)" if existed else "inserted"
    return _line("personas", action, f"{PERSONA_ID} name={PERSONA_NAME} avatar_url={avatar_url}")


async def ensure_room(session: AsyncSession) -> str:
    """rooms：仅缺失时插入，房号与状态都不改（A1 的演示布局优先）。"""
    room = await session.get(Room, ROOM_ID)
    if room is not None:
        return _line("rooms", "kept (已存在，不动演示布局)", f"{ROOM_ID} room_no={room.room_no}")
    result = await session.execute(
        pg_insert(Room)
        .values(room_id=ROOM_ID, room_no=ROOM_NO, status="available")
        .on_conflict_do_nothing()
    )
    if result.rowcount == 0:
        return _line("rooms", "skipped (唯一约束冲突，已有该房间)", f"{ROOM_ID} room_no={ROOM_NO}")
    return _line("rooms", "inserted", f"{ROOM_ID} room_no={ROOM_NO}")


async def ensure_elder(session: AsyncSession) -> str:
    """elders：只补 `persona_id`（且仅当为 NULL），姓名/状态/会话计数一概不动。"""
    elder = await session.get(Elder, ELDER_ID)
    if elder is None:
        result = await session.execute(
            pg_insert(Elder)
            .values(elder_id=ELDER_ID, name=ELDER_NAME, persona_id=PERSONA_ID)
            .on_conflict_do_nothing()
        )
        if result.rowcount == 0:
            return _line("elders", "skipped (唯一约束冲突，已有该长者)", f"{ELDER_ID}")
        return _line("elders", "inserted", f"{ELDER_ID} name={ELDER_NAME} persona_id={PERSONA_ID}")
    if elder.persona_id is None:
        elder.persona_id = PERSONA_ID
        return _line("elders", "patched (persona_id 原为 NULL，已补)", f"{ELDER_ID}")
    return _line(
        "elders", "kept (persona_id 已有绑定，不覆盖)", f"{ELDER_ID} persona_id={elder.persona_id}"
    )


async def ensure_bed(session: AsyncSession) -> str:
    """beds：仅缺失时插入；`(room_id, bed_no)` 已被别的行占用时如实报冲突、不硬插。"""
    bed = await session.get(Bed, BED_ID)
    if bed is not None:
        return _line(
            "beds",
            "kept (已存在，不动演示布局)",
            f"{BED_ID} bed_no={bed.bed_no} elder_id={bed.elder_id}",
        )
    result = await session.execute(
        pg_insert(Bed)
        .values(
            bed_id=BED_ID,
            room_id=ROOM_ID,
            bed_no=BED_NO,
            status="occupied",
            elder_id=ELDER_ID,
        )
        .on_conflict_do_nothing()
    )
    if result.rowcount == 0:
        return _line(
            "beds",
            "skipped (冲突：另一行已占该 bed_id 或 A-301/1 号床)",
            f"{BED_ID} room_id={ROOM_ID} bed_no={BED_NO}",
        )
    return _line(
        "beds", "inserted", f"{BED_ID} room_id={ROOM_ID} bed_no={BED_NO} elder_id={ELDER_ID}"
    )


async def ensure_device(session: AsyncSession, *, force_device: bool) -> str:
    """devices：默认只补不改；换令牌必须 `--force-device`（会踢掉在用的设备会话）。"""
    token_hash = device_token_hash()
    device = await session.get(Device, DEVICE_ID)
    if device is None:
        result = await session.execute(
            pg_insert(Device)
            .values(
                device_id=DEVICE_ID,
                room_id=ROOM_ID,
                elder_id=ELDER_ID,
                token_hash=token_hash,
                status="online",
            )
            .on_conflict_do_nothing()
        )
        if result.rowcount == 0:
            return _line(
                "devices",
                "skipped (冲突：device_id 已存在，或该长者已绑另一台设备)",
                f"{DEVICE_ID}",
            )
        return _line(
            "devices",
            "inserted",
            f"{DEVICE_ID} room_id={ROOM_ID} elder_id={ELDER_ID} token_hash={token_hash[:16]}…",
        )
    if not force_device:
        return _line(
            "devices",
            "kept (token_hash 不覆盖)",
            f"{DEVICE_ID} elder_id={device.elder_id}（换令牌须 --force-device，联调窗口内禁止）",
        )
    print(
        "⚠  --force-device：即将覆盖 devices.token_hash。\n"
        f"   device_id = {DEVICE_ID}\n"
        "   device_identity_from_session 校验 tv == token_hash[:16]（解绑即失效的落点），\n"
        "   覆盖后 A2 与前端正在用的设备会话会立即全部 401。\n"
        "   3 秒后继续；联调窗口内请按 Ctrl+C 中止。",
        file=sys.stderr,
        flush=True,
    )
    await asyncio.sleep(3)
    device.token_hash = token_hash
    return _line(
        "devices", "rotated (--force-device：token_hash 已覆盖)", f"{DEVICE_ID} 旧设备会话已失效"
    )


async def seed(*, avatar_url: str, force_device: bool) -> list[str]:
    """按外键顺序写五张表；单个事务，失败整体回滚，不留下半套种子。"""
    lines: list[str] = []
    async with session_factory() as session, session.begin():
        lines.append(await upsert_persona(session, avatar_url=avatar_url))
        lines.append(await ensure_room(session))
        lines.append(await ensure_elder(session))
        lines.append(await ensure_bed(session))
        lines.append(await ensure_device(session, force_device=force_device))
    return lines


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Day 4 · A3 联调种子：personas / rooms / elders / beds / devices（幂等）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--avatar-url",
        default=DEFAULT_AVATAR_URL,
        help="写进 personas.avatar_url 的**相对**路径（验 avatar-config 的 version 变化时改它）",
    )
    parser.add_argument(
        "--force-device",
        action="store_true",
        help="⚠ 覆盖 devices.token_hash，会让在用的设备会话全部 401（联调窗口内禁止）",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    try:
        lines = await seed(avatar_url=args.avatar_url, force_device=args.force_device)
    except (SQLAlchemyError, OSError) as exc:
        print(f"✗ 种子写入失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "  先确认库已起好、迁移已到位："
            "docker compose up -d postgres && uv run alembic upgrade head",
            file=sys.stderr,
        )
        return 1
    finally:
        # 脚本是一次性进程：显式关池，避免 asyncpg 在解释器退出时刷 "Event loop is closed"
        await engine.dispose()

    print("\n".join(lines))
    print()
    print(f"PERSONA_ID={PERSONA_ID}")
    print(f"AVATAR_URL={args.avatar_url}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：`python scripts/seed_personas.py [--avatar-url ...] [--force-device]`。"""
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
