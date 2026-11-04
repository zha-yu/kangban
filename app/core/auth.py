"""鉴权主体的凭证解析与失效判定 —— 契约 §5.2 的实现口径。

分工（刻意只做到「不需要数据库连接就能判定」这一层）：

- `security.py`：验签、算法白名单、载荷一致性（纯密码学，失败原因按内部语义分类）；
- **本文件**：Bearer 头解析 → 三类主体令牌解码 → 把「从库里读出的事实」与载荷逐项比对
  → 组装 `Identity`；失败原因按契约 §7 的对外 `code` 分类；
- 接线层（T4b-1 人员/工牌、T4b-2 设备）：文件末尾的 `resolve_principal` /
  `get_current_principal` 与三个主体依赖，只负责 `SELECT` 与 `get_session`，
  把读到的事实原样传进上面的判定函数。
  **判定规则不许在依赖层再写一遍**，否则「护工改角色」这类失效判定会出现两套口径。
  设备会话与工牌身份的 `elder_id` 都读自 `devices`（契约 §5.3、S-17）。
  仍未做的一条：工牌不比对 `staff_tokens.scope` 与载荷 `scope`，原因与翻案条件见契约 S-22。

本文件不生成 HTTP 响应：异常只携带契约 §7 的 `code`，异常 → 响应的映射属
`app/core/errors.py`（§11 落点，B 负责，但不在 task-4 写作用域内）。

对应验收：FR-ADM-03（读当前身份）、FR-ADM-04（改角色/停用即时失效）、
FR-ADM-06 ③（身份只从 runtime_context 走）、AC-24（解绑/停用立即 401）、AC-39。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.runtime_context import Identity, reset_identity, set_identity
from app.core.security import (
    KIND_DEVICE,
    KIND_GUARDIAN,
    KIND_STAFF,
    KIND_STAFF_CARD,
    PRINCIPAL_TYPE_LITERALS,
    STAFF_ROLES,
    TYP_ACCESS,
    TYP_DEVICE,
    TYP_REFRESH,
    TYP_STAFF_CARD,
    TokenExpiredError,
    TokenInvalidError,
    canonical_uuid,
    constant_time_equals,
    decode_token,
    sha256_hex,
)
from app.db import get_session
from app.models import Device, GuardianAccount, StaffAccount, StaffToken

# ------------------------------------------------------------------ 对外错误码（契约 §7）

# 401 的三种成因在响应里必须可分：排障时「没带凭证」和「凭证被撤销」是完全不同的动作。
# 但三者对客户端语义相同（凭证失效，去重新登录），所以只有码，没有分支状态。
# `noqa: S105`：ruff 见到 `*_TOKEN = "..."` 就报硬编码口令，而这 3 个是响应码字面量，不是密钥。
TOKEN_INVALID = "TOKEN_INVALID"  # noqa: S105
TOKEN_EXPIRED = "TOKEN_EXPIRED"  # noqa: S105
TOKEN_REVOKED = "TOKEN_REVOKED"  # noqa: S105
ROLE_FORBIDDEN = "ROLE_FORBIDDEN"

# 账号状态取值（契约 R5/R6）与设备锁定状态（R3）。写成常量而不是散在比较式里，
# 是为了让「库里到底认哪几个值」这件事只有一处定义。
ACCOUNT_STATUS_ACTIVE = "active"
DEVICE_STATUS_LOCKED = "locked"

# 设备 `tv` 取 `devices.token_hash` 前 16 位（契约 §5.1、§5.3）；
# 与 security.py 的签发侧长度必须一致，故从那里借语义、这里只做切片。
_DEVICE_TOKEN_HASH_PREFIX_LENGTH = 16


class AuthError(Exception):
    """鉴权失败的基类：`code` 是契约 §7 的对外码，HTTP 状态由 `errors.py` 决定。

    刻意继承 `Exception` 而不是 `ValueError`：T2 那一系（`SecurityError`）表达的是
    「数据/编程坏了」，本系表达的是「这次请求被拒」。两者混成一类，
    `errors.py` 就没法区分「该回 401」和「该报 500 并查库」。
    """

    code: str = TOKEN_INVALID

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class CredentialMissingError(AuthError):
    """没带 `Authorization` 头，或头里是空的 `Bearer`。"""

    code = TOKEN_INVALID


class CredentialInvalidError(AuthError):
    """签名不符、算法不符、载荷非法、`typ` 不是可进入请求的三类、`refresh` 冒充 Access。"""

    code = TOKEN_INVALID


class CredentialExpiredError(AuthError):
    """`exp` 已过。与「签名不合法」分开给码，客户端才知道该走刷新而不是重登。"""

    code = TOKEN_EXPIRED


class CredentialRevokedError(AuthError):
    """凭证本身有效，但库里已经不再承认它。

    覆盖四种：账号 `status != active`；`token_version` 与载荷 `tv` 不等（FR-ADM-04、O10）；
    令牌里记的角色/设备与库里当前值不一致；设备 `token_hash` 前缀对不上（解绑，AC-24）；
    工牌 `expires_at` 已过或所属员工已停用（O12）。
    """

    code = TOKEN_REVOKED


class RoleForbiddenError(AuthError):
    """凭证有效、主体合法，但角色不够 —— 403，不是 401（契约 §3 的四类拒绝）。"""

    code = ROLE_FORBIDDEN


# ------------------------------------------------------------------ Bearer 头解析


def bearer_token_from_header(authorization: str | None) -> str:
    """从 `Authorization` 头取原始令牌。缺失/格式错一律 `CredentialMissingError`。

    三条不显眼但必须有的规则：

    1. scheme 大小写不敏感（RFC 6750 只规定 `Bearer` 的写法，历史客户端与代理会发
       `bearer`/`BEARER`），但**只接受这一个 scheme**：放过 `Basic` 就等于开了第二条
       凭证通道，与「四套载荷不可串用」的口径冲突。
    2. 不做 `strip()` 后交给下游：JWT 的三段 base64url 里不可能有空格，
       含空格就是格式坏，报 `TOKEN_INVALID` 比签个解不开的令牌好查。
    3. 异常 message 里**绝不带令牌内容**（AGENTS §4.3）：日志与错误响应都可能原样落盘。
    """
    if not authorization or not authorization.strip():
        raise CredentialMissingError("missing Authorization header")
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        # 只说结构不合法，不回显收到的值
        raise CredentialInvalidError("Authorization header must be 'Bearer <token>'")
    _, token = parts
    if not token or not token.isascii():
        raise CredentialInvalidError("bearer token is empty or not ASCII")
    return token


# ------------------------------------------------------------------ 主体令牌解码


def decode_principal_token(token: str) -> dict[str, Any]:
    """解出可作为请求主体的令牌，返回已校验的载荷。

    按 `access` → `device` → `staff_card` 依次尝试，**每次都做完整验签**。
    为什么不先看一眼 `typ` 再决定用哪套规则：`typ` 在未验签的载荷里是攻击者可控字段，
    拿它来选校验分支就是 T2 里禁掉「按 header 自选算法」的同一类错误。
    三次 HMAC 验证的成本是微秒级；猜错类型时不会误放行，因为载荷一致性检查
    （`_validate_payload` 核对 `typ`）在返回载荷之前就会拒绝，唯一提前离开循环的是
    「签名有效且已过期」——那本来就该报 401 过期。

    `refresh` 不在此列（契约 §5.2 第 2 步：只能由刷新流程解），它走 `decode_refresh_token`。
    """
    for expected_typ in PRINCIPAL_TYPE_LITERALS:
        try:
            return decode_token(token, expected_typ=expected_typ)
        except TokenExpiredError as exc:
            # 过期校验发生在验签之后、我们的 `typ` 核对之前：能走到这里说明签名已通过。
            # 若这张令牌其实是别的类型却已过期，报「过期」与报「无效」对客户端是同一语义
            # （都是 401 去重新登录），而「过期」对排障更有用，所以直接返回不再试其余类型。
            raise CredentialExpiredError("token has expired") from exc
        except TokenInvalidError:
            continue  # 换下一类再试；三类都不通过才判「凭证失效」，不外泄底层原因
    raise CredentialInvalidError("token is not a valid principal credential")


def decode_refresh_token(token: str) -> dict[str, Any]:
    """Refresh 专用解码入口（O2，供 task-5 的刷新流程使用）。

    单独开一个函数而不是给上面那个加 `allow_refresh=True`：参数化的口子迟早被
    「顺手在业务路由里解 refresh」用掉，而 refresh 只能由刷新流程解是硬口径。
    账号状态与 `tv` 的回查由 task-5 复用本模块的判定函数，刷新不许绕过失效检查。
    """
    try:
        return decode_token(token, expected_typ=TYP_REFRESH)
    except TokenExpiredError as exc:
        raise CredentialExpiredError("refresh token has expired") from exc
    except TokenInvalidError as exc:
        raise CredentialInvalidError("refresh token is invalid") from exc


# ------------------------------------------------------------------ 库里事实的通用核对


def _require_active_and_current_version(
    claims: dict[str, Any],
    *,
    db_status: str,
    db_token_version: int,
    label: str,
) -> None:
    """人员账号的两条硬判定：状态必须 active，`tv` 必须等于库里的 `token_version`。

    两条都归 `CredentialRevokedError`：签名已经过了，是库里不再承认这张凭证。
    停用/改角色/解绑都会 `token_version + 1`（FR-ADM-04、O10），所以「不等」的
    正确解释是「被撤销」，不是「数据坏了」—— 后者会让运维以为要修库。
    """
    if db_status != ACCOUNT_STATUS_ACTIVE:
        raise CredentialRevokedError(f"{label} account is not active")
    if claims["tv"] != db_token_version:
        raise CredentialRevokedError(f"{label} token_version no longer matches")


def _require_expected_claim(
    claims: dict[str, Any], *, expected_typ: str, expected_kind: str
) -> None:
    """调用方必须把令牌交给**与 `typ`/`kind` 匹配**的构造函数。

    这是给 T4b 的接线兜底：分派写错（把 device 载荷传给人员构造函数）不该
    悄悄产出一个错身份，而该在当场报「凭证与处理器不符」。
    """
    if claims.get("typ") != expected_typ or claims.get("kind") != expected_kind:
        raise CredentialInvalidError("token type does not match the identity builder used")


# ------------------------------------------------------------------ 身份构造


def staff_identity_from_access(
    claims: dict[str, Any],
    *,
    db_role: str,
    db_status: str,
    db_token_version: int,
) -> Identity:
    """员工 Access → `Identity`（`kind=staff`）。**角色以数据库当前值为准**。

    载荷里的 `role` 只用来发现「角色变过但没提版本号」这种不一致：
    按 O10，改 `role` 必须同时 `token_version + 1`，两者对不上说明要么撤销漏做、
    要么库里数据被手改 —— 两种都不该继续放行，判撤销并让调用方重新登录。
    身份里的 `role` 取 `db_role`，不取载荷：否则旧令牌里的旧角色会一直生效到过期。
    """
    _require_expected_claim(claims, expected_typ=TYP_ACCESS, expected_kind=KIND_STAFF)
    if claims.get("role") != db_role:
        raise CredentialRevokedError("staff role changed since the token was issued")
    _require_active_and_current_version(
        claims, db_status=db_status, db_token_version=db_token_version, label="staff"
    )
    staff_id = _id_from_sub(claims, prefix=KIND_STAFF)
    # 走到这里 `db_role` 必然与载荷角色相等，而载荷角色已被 S-16 的白名单校验过，
    # 所以库里塞进越界角色（`super_user`）不会被构造进身份：要么 drift 先命中判撤销，
    # 要么在登录签发侧就被 `security` 拒签。这里不需要再兜一层角色白名单。
    return Identity(kind=KIND_STAFF, staff_id=staff_id, role=db_role)


def guardian_identity_from_access(
    claims: dict[str, Any],
    *,
    db_status: str,
    db_token_version: int,
) -> Identity:
    """家属 Access → `Identity`（`kind=guardian`）。家属没有角色字段（契约 §1）。

    能看哪些长者由服务层查 `elder_guardians`（`status='active'`）决定，
    身份里**不放** `elder_id`（S-17），否则「绕过绑定表直接指定长者」没有代码拦阻。
    """
    _require_expected_claim(claims, expected_typ=TYP_ACCESS, expected_kind=KIND_GUARDIAN)
    _require_active_and_current_version(
        claims, db_status=db_status, db_token_version=db_token_version, label="guardian"
    )
    return Identity(kind=KIND_GUARDIAN, guardian_id=_id_from_sub(claims, prefix=KIND_GUARDIAN))


def device_identity_from_session(
    claims: dict[str, Any],
    *,
    db_device_id: object,
    db_token_hash: str,
    db_status: str,
    db_elder_id: object | None = None,
) -> Identity:
    """设备会话 → `Identity`（`kind=device`），`elder_id` 只从库里读（契约 §5.3）。

    三条判定各有其所以必须有的理由：

    1. `dev == db_device_id`：载荷与查询结果必须是同一台设备。T4b 用 `sub` 里的 id 查行，
       这一条防的是接线写错（拿房间/心跳查出一行却当成主体）。
    2. `tv == token_hash` 前 16 位：这是「解绑即失效」的落点（AC-24）。解绑会轮换
       `token_hash`，旧会话的 `tv` 必然对不上，因此不需要黑名单。
       比对走 `constant_time_equals`，不用 `==`（摘要类比对的短路会泄露相等前缀长度）。
    3. `status == locked` 才拒：`offline` 不拒 —— 掉线是心跳问题（FR-ADM-10），
       不是凭证问题；把掉线判成 401 会让平板在心跳抖动时被踢出登录。
    """
    _require_expected_claim(claims, expected_typ=TYP_DEVICE, expected_kind=KIND_DEVICE)
    identifier = canonical_uuid(db_device_id)
    if claims.get("dev") != identifier:
        raise CredentialRevokedError("device id does not match the token")
    if db_status == DEVICE_STATUS_LOCKED:
        raise CredentialRevokedError("device is locked")
    if not device_hash_prefix_matches(claims, db_token_hash=db_token_hash):
        raise CredentialRevokedError("device token hash prefix no longer matches (unbound)")
    return Identity(
        kind=KIND_DEVICE, device_id=identifier, elder_id=_optional_uuid_text(db_elder_id)
    )


def device_hash_prefix_matches(claims: dict[str, Any], *, db_token_hash: str) -> bool:
    """载荷 `tv` 是否等于 `devices.token_hash` 的前 16 位。

    单独成函数是因为 T4b 之外还有用：`task-5` 的设备换会话（O4）也要拿长期令牌算出
    同一个前缀再签新会话，两处口径必须一模一样。
    库里存的是 `sha256_hex` 的结果（小写十六进制，S-14 的写入侧），
    比对前两侧都 `lower()`：大小写差异不该被解释成「解绑」。
    """
    tv = claims.get("tv")
    if not isinstance(tv, str) or not isinstance(db_token_hash, str):
        return False
    prefix = db_token_hash[:_DEVICE_TOKEN_HASH_PREFIX_LENGTH].lower()
    if len(prefix) != _DEVICE_TOKEN_HASH_PREFIX_LENGTH:
        return False
    return constant_time_equals(tv.lower(), prefix)


def staff_card_lookup_key(token: str) -> str:
    """工牌原始令牌的 SHA-256 —— `staff_tokens.token_hash` 的主键查询值（R9、O12）。

    库里不存明文令牌（AGENTS §4.2），所以回查只能用哈希值当键；
    这也意味着**一旦返回 401，重放同一个令牌仍然查得到同一行**，
    失效靠的是 `expires_at`，不是「换个哈希」。
    """
    return sha256_hex(token)


def staff_card_identity_from_card(
    claims: dict[str, Any],
    *,
    db_staff_id: object,
    db_device_id: object,
    db_staff_status: str,
    db_expires_at: datetime,
    now: datetime | None = None,
    db_elder_id: object | None = None,
) -> Identity:
    """工牌受限令牌 → `Identity`（`kind=staff_card`）。

    与设备会话不同的两条判定：

    - 工牌**没有版本号**（R9 只有 `expires_at`），失效完全靠到期时间，所以
      `expires_at > now` 这条必须在这里判，不能让 T4b 的 SQL `WHERE` 独吞 ——
      SQL 侧过滤 + 本函数二次确认，是为了接线时漏写条件也能兜住。
    - 工牌主体是「员工 + 设备」，所以员工必须仍 active（人停用了，卡也就废了）。

    `db_elder_id` 由 T4b 从设备绑定读出后传入；载荷里**没有** `elder_id`
    （S-16 的白名单不含它），所以这里不存在「相信客户端说的长者」这条路径。

    **口径未定、本函数故意不做的事**：不比对 `staff_tokens.scope` 与载荷 `scope`。
    R9 的 `scope` 是单个 text 字段，令牌载荷却是列表，一行的 scope 文本怎么切成多项
    在 PRD/TD/契约里都没有出处（Day 9 建 E14 时必须先定切分口径，再补这条比对）。
    宁可留一条显式的未做项，也不在这里私自选一个分隔符当成口径 ——
    选了就等于把「`workorder:handle,health:staff_form` 按逗号切」写进了权限判定。
    """
    _require_expected_claim(claims, expected_typ=TYP_STAFF_CARD, expected_kind=KIND_STAFF_CARD)
    if canonical_uuid(db_staff_id) != _id_from_sub(claims, prefix=KIND_STAFF):
        raise CredentialRevokedError("staff token belongs to another staff account")
    if canonical_uuid(db_device_id) != claims.get("dev"):
        raise CredentialRevokedError("staff token belongs to another device")
    if db_staff_status != ACCOUNT_STATUS_ACTIVE:
        raise CredentialRevokedError("staff account is not active")
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or db_expires_at.tzinfo is None:
        # AGENTS §8：存储一律 UTC。naive 时间按本地时区解释，会把有效期整段推偏几小时。
        raise CredentialRevokedError("staff token expiry must be a timezone-aware UTC instant")
    if db_expires_at <= moment:
        raise CredentialRevokedError("staff token has expired")
    scope = claims.get("scope")
    return Identity(
        kind=KIND_STAFF_CARD,
        staff_id=_id_from_sub(claims, prefix=KIND_STAFF),
        device_id=canonical_uuid(db_device_id),
        elder_id=_optional_uuid_text(db_elder_id),
        # 解码后的 JSON 数组是 list，`Identity` 需要不可变元组 —— 转换只在这里做一次。
        scope=tuple(scope or ()),
    )


# ------------------------------------------------------------------ 角色判定


def check_roles(identity: Identity, allowed_roles: Sequence[str]) -> None:
    """在**员工身份**上做角色判定；不够就抛 `RoleForbiddenError`（403）。

    三类主体在这里都会被拒，而且都必须被拒：

    - `guardian` / `device`：不是员工，谈不上员工角色（契约 §1）。
    - `staff_card`：工牌按 `scope` 授权，载荷白名单里就没有 `role`（S-16），
      所以它永远过不了角色判定。要按范围放行是服务层与 Day 9 工牌接口的事，
      不能靠「工牌也是员工」把它当全权会话用。

    角色取的是 `identity.role`，而 `identity.role` 来自数据库（见
    `staff_identity_from_access`）—— 这里不再回库，也不读令牌。
    """
    if identity.kind != KIND_STAFF:
        raise RoleForbiddenError("principal is not a staff session")
    if identity.role not in allowed_roles:
        raise RoleForbiddenError("staff role is not allowed for this operation")


def _id_from_sub(claims: dict[str, Any], *, prefix: str) -> str:
    """取 `sub` 的 id 段并核对前缀（T2 已保证它是规范 UUID，这里只防接线拿错载荷）。"""
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.startswith(f"{prefix}:"):
        raise CredentialInvalidError("subject prefix does not match the identity kind")
    return canonical_uuid(sub.partition(":")[2])


def _optional_uuid_text(value: object | None) -> str | None:
    """库里读出的可空 UUID（`devices.elder_id` 未绑定为 NULL）→ 规范文本或 None。

    在这里就规范化而不是丢给 `Identity`：`Identity` 收到什么就存什么，
    传进 `UUID` 对象会得到 `str(UUID)` 的等价文本，但类型口径会一路带到日志与响应里。
    """
    return None if value is None else canonical_uuid(value)


# ------------------------------------------------------------------ T4b：库里回查的接线


def _uuid_bind(value: str) -> uuid.UUID:
    """规范 UUID 文本 → 绑定参数（`UUID(as_uuid=True)` 的列要 `UUID` 对象）。

    S-16 已保证进来的是小写连字规范形式，所以这里失败只可能是接线拿错了载荷，
    按「凭证与处理器不符」处理而不是当成数据坏了。
    """
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise CredentialInvalidError("subject id does not match an account") from exc


async def _load_staff_row(session: AsyncSession, staff_id: str) -> Sequence[Any]:
    """按主键读 `staff_accounts` 的三列判定值；缺行即判撤销。"""
    row = (
        await session.execute(
            select(
                StaffAccount.role,
                StaffAccount.status,
                StaffAccount.token_version,
            ).where(StaffAccount.staff_id == _uuid_bind(staff_id))
        )
    ).one_or_none()
    if row is None:
        # 不回 404：账号是否存在不是凭证层该告诉客户端的信息（契约 §7 的 404 只给范围内资源）
        raise CredentialRevokedError("staff account row is gone")
    return row


async def _load_guardian_row(session: AsyncSession, guardian_id: str) -> Sequence[Any]:
    """按主键读 `guardian_accounts` 的两列判定值；缺行即判撤销。"""
    row = (
        await session.execute(
            select(
                GuardianAccount.status,
                GuardianAccount.token_version,
            ).where(GuardianAccount.guardian_id == _uuid_bind(guardian_id))
        )
    ).one_or_none()
    if row is None:
        raise CredentialRevokedError("guardian account row is gone")
    return row


async def _load_device_row(session: AsyncSession, device_id: str) -> Sequence[Any]:
    """按主键读 `devices` 的判定列；缺行即判撤销。

    只取 `device_id / token_hash / status / elder_id`，心跳、房间、绑定关系一概不读：
    这里产出的是**凭证事实**，设备在不在网属 FR-ADM-10 的心跳判定，不属鉴权。
    """
    row = (
        await session.execute(
            select(
                Device.device_id,
                Device.token_hash,
                Device.status,
                Device.elder_id,
            ).where(Device.device_id == _uuid_bind(device_id))
        )
    ).one_or_none()
    if row is None:
        # 设备被删（解绑后回收）与「令牌对不上任何设备」在客户端看来是同一件事：重新配对。
        raise CredentialRevokedError("device row is gone")
    return row


async def _load_staff_token_row(session: AsyncSession, lookup_key: str) -> Sequence[Any]:
    """以哈希为键读 `staff_tokens`，并带出所属员工的当前状态与所在设备的绑定长者。

    两处 JOIN 都是必须的：

    - `staff_accounts`：R9 只有 `expires_at`，员工停用不会写进工牌行；不 JOIN 就会出现
      「人被 disabled 了，卡还能刷到过期」。
    - `devices`：工牌的可见长者范围由设备绑定决定（契约 §5.3、S-17）。载荷白名单里没有
      `elder_id`（S-16），所以这个值**只有**库里一条来源；漏了 JOIN 就等于把工牌当全权会话。
    """
    row = (
        await session.execute(
            select(
                StaffToken.staff_id,
                StaffToken.device_id,
                StaffToken.expires_at,
                StaffAccount.status,
                Device.elder_id,
            )
            .join(StaffAccount, StaffAccount.staff_id == StaffToken.staff_id)
            .join(Device, Device.device_id == StaffToken.device_id)
            .where(StaffToken.token_hash == lookup_key)
        )
    ).one_or_none()
    if row is None:
        raise CredentialRevokedError("staff token record is gone")
    return row


async def resolve_principal(authorization: str | None, session: AsyncSession) -> Identity:
    """请求头 → 已通过库内校验的 `Identity`。**不碰 contextvar**，所以可以直接单测。

    分派只依据**已验签的载荷**（`typ` + `kind`），一条令牌只查一张表：跨表「试着找找」
    会让「哪张表负责这个主体」这个问题无法回答，也会把漏查变成误命中。

    工牌的 `elder_id` 与设备会话同源，都读自 `devices`（契约 §5.3、S-17）：
    工牌行只记「哪张卡在哪个设备上激活」，绑定长者是设备的属性，不是卡的属性。
    """
    token = bearer_token_from_header(authorization)
    claims = decode_principal_token(token)
    typ = claims["typ"]
    if typ == TYP_DEVICE:
        db_device_id, db_token_hash, db_status, db_elder_id = await _load_device_row(
            session, _id_from_sub(claims, prefix=KIND_DEVICE)
        )
        return device_identity_from_session(
            claims,
            db_device_id=db_device_id,
            db_token_hash=db_token_hash,
            db_status=db_status,
            db_elder_id=db_elder_id,
        )
    kind = claims["kind"]
    if typ == TYP_ACCESS and kind == KIND_STAFF:
        staff_id = _id_from_sub(claims, prefix=KIND_STAFF)
        db_role, db_status, db_token_version = await _load_staff_row(session, staff_id)
        return staff_identity_from_access(
            claims,
            db_role=db_role,
            db_status=db_status,
            db_token_version=db_token_version,
        )
    if typ == TYP_ACCESS and kind == KIND_GUARDIAN:
        guardian_id = _id_from_sub(claims, prefix=KIND_GUARDIAN)
        db_status, db_token_version = await _load_guardian_row(session, guardian_id)
        return guardian_identity_from_access(
            claims, db_status=db_status, db_token_version=db_token_version
        )
    if typ == TYP_STAFF_CARD:
        (
            db_staff_id,
            db_device_id,
            db_expires_at,
            db_staff_status,
            db_elder_id,
        ) = await _load_staff_token_row(session, staff_card_lookup_key(token))
        return staff_card_identity_from_card(
            claims,
            db_staff_id=db_staff_id,
            db_device_id=db_device_id,
            db_staff_status=db_staff_status,
            db_expires_at=db_expires_at,
            db_elder_id=db_elder_id,
        )
    # 解码层已按 S-16 白名单核过 typ/kind，走到这里只能是接线把新类型加进了允许列表
    raise CredentialInvalidError("token type is not accepted as a request principal")


async def get_current_principal(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> AsyncIterator[Identity]:
    """FastAPI 依赖：解析主体并把 `Identity` 注入上下文，请求结束或异常都回滚。

    用生成器依赖而不是普通依赖，是因为回滚点必须在**响应结束之后**：
    普通依赖里 `set_identity` 之后没有落点可 `reset`，uvicorn 复用同一进程处理下一个请求时
    就会带着上一个人的身份跑完全程（契约 S-18 ③）。

    `authorization` 的 `= None` 不是写法偏好，是口径：只写 `Annotated[str | None, Header()]`
    时 FastAPI 把 `Authorization` 当**必填头**，缺头直接 422 `{"type":"missing"}`，我们的解析器
    根本不会被调用 —— 而契约 §7 要求「没带凭证」是 401 `TOKEN_INVALID`（实测：去掉 `= None`
    本文件的 `test_asgi_chain_surfaces_...` 立刻变红）。
    有默认值就得排在最后，所以参数顺序与 `resolve_principal` 相反：框架按注解解析依赖，顺序无关。
    """
    identity = await resolve_principal(authorization, session)
    marker = set_identity(identity)
    try:
        yield identity
    finally:
        reset_identity(marker)


def _require_kind(identity: Identity, *, kind: str) -> Identity:
    """主体种类不符 → 401（`TOKEN_INVALID`），不是 403。

    403 会误导客户端「换个更高权限的账号就能过」，而真实情况是拿错了凭证体系，
    该重登的是当前这个（契约 §7 的四类拒绝里，凭证问题一律走 401）。
    """
    if identity.kind != kind:
        raise CredentialInvalidError("principal kind does not match the endpoint")
    return identity


async def current_staff(identity: Annotated[Identity, Depends(get_current_principal)]) -> Identity:
    """员工全权会话主体（`kind=staff`）。工牌主体不走这里，它按 `scope` 授权。"""
    return _require_kind(identity, kind=KIND_STAFF)


async def current_guardian(
    identity: Annotated[Identity, Depends(get_current_principal)],
) -> Identity:
    """家属主体（`kind=guardian`）。能看哪些长者由服务层查 `elder_guardians`。"""
    return _require_kind(identity, kind=KIND_GUARDIAN)


async def current_device(
    identity: Annotated[Identity, Depends(get_current_principal)],
) -> Identity:
    """设备主体（`kind=device`）。`elder_id` 是设备当前绑定的长者，未绑定为 `None`。"""
    return _require_kind(identity, kind=KIND_DEVICE)


def require_roles(*roles: str) -> Callable[[Identity], Awaitable[Identity]]:
    """角色门禁工厂：`Depends(require_roles("admin"))`。只管角色，不做长者范围过滤。

    在工厂调用处就把越界角色名挡掉：写错一个角色名（`super_user`）如果拖到运行期，
    表现是「所有人都 403」，看起来像权限配窄了而不是代码写错了。
    """
    unknown = sorted({role for role in roles if role not in STAFF_ROLES})
    if unknown:
        raise ValueError(f"unknown staff roles in require_roles: {', '.join(unknown)}")

    async def dependency(identity: Annotated[Identity, Depends(current_staff)]) -> Identity:
        check_roles(identity, roles)
        return identity

    return dependency


__all__ = [
    "ACCOUNT_STATUS_ACTIVE",
    "DEVICE_STATUS_LOCKED",
    "ROLE_FORBIDDEN",
    "TOKEN_EXPIRED",
    "TOKEN_INVALID",
    "TOKEN_REVOKED",
    "AuthError",
    "CredentialExpiredError",
    "CredentialInvalidError",
    "CredentialMissingError",
    "CredentialRevokedError",
    "RoleForbiddenError",
    "bearer_token_from_header",
    "check_roles",
    "current_device",
    "current_guardian",
    "current_staff",
    "decode_principal_token",
    "decode_refresh_token",
    "device_hash_prefix_matches",
    "device_identity_from_session",
    "get_current_principal",
    "guardian_identity_from_access",
    "require_roles",
    "resolve_principal",
    "staff_card_identity_from_card",
    "staff_card_lookup_key",
    "staff_identity_from_access",
]
