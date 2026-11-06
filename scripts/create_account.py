"""命令行建号工具：在没有管理员账号的新环境里创建第一个账号（T1）。

用法::

    # 建第一个管理员
    uv run python scripts/create_account.py --role admin --name 王管理 --phone 13700000000 --password 'Admin@2026'

    # 建护工 / 医生 / 家属
    uv run python scripts/create_account.py --role care_worker --name 李护工 --phone 13800000002 --password 'Care@2026'
    uv run python scripts/create_account.py --role guardian   --name 张女士 --phone 13900000002 --password 'Fam@2026'

**为什么需要它**：账号本应由管理员在后台创建（PRD FR-ADM-01），但空库里没有任何
管理员，形成「没有 admin 就建不出 admin」的引导死锁。本脚本是打破死锁的一次性入口，
不替代后台接口。

口径与登录侧一致：
- 手机号在 `staff_accounts` 与 `guardian_accounts` 之间**全局唯一**（登录按手机号查两表，
  两表同号会产生二义性）；
- 口令只存 bcrypt 哈希（`app.core.security.hash_password`），**绝不明文**；
- 幂等：同号已存在时不改动既有账号，直接打印已有 id（`--strict` 时改为退出码 2）。

退出码：0 成功或幂等命中；1 环境/数据库错误；2 参数非法或 `--strict` 下重号。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db import get_session  # noqa: E402
from app.models.guardian import GuardianAccount  # noqa: E402
from app.models.staff import StaffAccount  # noqa: E402

_PHONE_RE = re.compile(r"^1\d{10}$")
_MIN_PASSWORD_LEN = 8
_STAFF_ROLES = ("admin", "care_worker", "doctor")
_ALL_ROLES = (*_STAFF_ROLES, "guardian")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="命令行建号（打破「没有管理员就建不出管理员」的引导死锁）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：uv run python scripts/create_account.py --role admin --name 王管理 "
        "--phone 13700000000 --password 'Admin@2026'",
    )
    parser.add_argument("--role", required=True, choices=_ALL_ROLES, help="账号角色")
    parser.add_argument("--name", required=True, help="姓名")
    parser.add_argument("--phone", required=True, help="11 位手机号（跨员工/家属全局唯一）")
    parser.add_argument("--password", required=True, help="口令，至少 8 位（只存哈希）")
    parser.add_argument(
        "--status",
        default="active",
        choices=("active", "disabled"),
        help="账号状态，默认 active",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="手机号已存在时以退出码 2 结束（默认视为幂等命中，退出码 0）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只校验参数，不写库")
    return parser


def _validate(args: argparse.Namespace) -> str | None:
    """返回错误文案；None 表示通过。"""
    if not _PHONE_RE.match(args.phone):
        return f"手机号格式不对：{args.phone}（要 11 位，1 开头）"
    if len(args.password) < _MIN_PASSWORD_LEN:
        return f"口令至少 {_MIN_PASSWORD_LEN} 位（当前 {len(args.password)} 位）"
    if not args.name.strip():
        return "姓名不能为空"
    return None


async def _find_existing(phone: str) -> tuple[str, uuid.UUID] | None:
    """在两张账号表里查同号账号，返回 (kind, account_id)。"""
    async for session in get_session():
        staff_hit = (
            await session.execute(select(StaffAccount).where(StaffAccount.phone == phone))
        ).scalar_one_or_none()
        if staff_hit is not None:
            return "staff", staff_hit.staff_id
        guardian_hit = (
            await session.execute(select(GuardianAccount).where(GuardianAccount.phone == phone))
        ).scalar_one_or_none()
        if guardian_hit is not None:
            return "guardian", guardian_hit.guardian_id
    return None


async def _create(args: argparse.Namespace) -> int:
    existing = await _find_existing(args.phone)
    if existing is not None:
        kind, account_id = existing
        print(f"已存在 kind={kind} account_id={account_id} phone={args.phone}")
        if args.strict:
            print("--strict 指定了「重号即失败」，未做任何修改", file=sys.stderr)
            return 2
        return 0

    password_hash = hash_password(args.password)

    async for session in get_session():
        if args.role == "guardian":
            guardian = GuardianAccount(
                name=args.name,
                phone=args.phone,
                password_hash=password_hash,
                status=args.status,
            )
            session.add(guardian)
            await session.commit()
            print(f"CREATED role=guardian account_id={guardian.guardian_id} phone={args.phone}")
        else:
            staff = StaffAccount(
                name=args.name,
                role=args.role,
                phone=args.phone,
                password_hash=password_hash,
                status=args.status,
            )
            session.add(staff)
            await session.commit()
            print(f"CREATED role={args.role} account_id={staff.staff_id} phone={args.phone}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    problem = _validate(args)
    if problem is not None:
        print(f"参数错误：{problem}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"DRY-RUN ok role={args.role} phone={args.phone}（未写库）")
        return 0

    try:
        return asyncio.run(_create(args))
    except SQLAlchemyError as exc:
        print(
            "数据库不可达或写入失败，请确认 PostgreSQL 已启动且 DATABASE_URL 正确。"
            f"\n底层信息：{exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
