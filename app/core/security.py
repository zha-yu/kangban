"""鉴权内核：密码哈希与后续四类 JWT 的签发/校验。

本模块是 AGENTS.md §5 的高风险文件（auth）。三条贯穿全模块的口径：

1. 密钥与有效期只从 `app.core.config.Settings` 读，`jwt_secret` 为空必须拒绝签发与校验；
2. 密码原文、完整 token、`jwt_secret` 一律不进日志，也不写进异常 message；
3. 凭证类型（access / refresh / device / staff_card）不可串用。

`canonical_uuid`、`STAFF_ROLES`、`normalize_scope` 是本模块公开的三个校验工具，
`runtime_context.py` 构造身份时复用它们：令牌与身份必须走同一套判定，
两处各写一份迟早会分叉（一边接受大写 UUID、另一边拒绝就是 401 玄学故障的源头）。

密码哈希采用「NFKC 规范化 → SHA-256 → 十六进制 → bcrypt」，原因见 `_prehash` 注释。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import bcrypt
import jwt as pyjwt

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 异常体系
# 全部继承 ValueError：数据库脏值、非法载荷属于数据/编程错误，
# 调用方（auth.py、seed 脚本）必须把它和「密码错误」区分开。


class SecurityError(ValueError):
    """本模块所有鉴权异常的基类。"""


class PasswordHashInvalidError(SecurityError):
    """存储的哈希不是合法 bcrypt 哈希。

    这属于数据损坏，不是「密码错」。message 只说明类型，不回显哈希内容，
    避免异常文本经日志或错误响应泄露口令材料。
    """

    def __init__(self) -> None:
        super().__init__("stored password hash is not a valid bcrypt hash")


class JwtSecretNotConfiguredError(SecurityError):
    """`jwt_secret` 缺失、纯空白或短于 HS256 最小长度。

    默认值 `Settings.jwt_secret = ""`（config.py:46）是可触达的真实状态，
    签发与校验两侧都必须拒绝，不能签出「空密钥签名」的令牌。
    message 不含密钥内容，只说明缺什么。
    """

    def __init__(self, message: str = "jwt_secret is not configured") -> None:
        super().__init__(f"{message}; refusing to sign or verify tokens")


class TokenInvalidError(SecurityError):
    """签名不符、算法不符、载荷缺必需字段、`typ` 不匹配、主体格式非法。

    对客户端统一表现为「凭证失效」（契约 §7 → 401），内部原因只在异常类型上区分，
    message 与日志都不带原始 token。
    """


class TokenExpiredError(TokenInvalidError):
    """`exp` 已过。与「签名不合法」分开，便于日志定位但不改变客户端语义（仍 401）。"""


class TokenPayloadError(SecurityError):
    """签发表述本身就自相矛盾（如 `kind=staff` 却给了家属主体前缀）。

    属调用方编程错误：宁可签发前当场报错，也不签出一个下游必然拒绝的令牌。
    """


# ------------------------------------------------------------------ 密码哈希

# bcrypt 的输入硬上限是 72 字节。bcrypt 5.0.0 实测（见 tests/test_security.py 的
# `test_prehash_shrinks_any_password_under_bcrypt_limit`）：超过 72 字节直接抛
# `ValueError: password cannot be longer than 72 bytes`，而不是静默截断。
# 后果是任何超过 24 个汉字的中文口令都会让哈希调用当场失败，
# 未捕获就是 500，登录链路根本走不通 —— 所以这里必须先定长预哈希。
# 旧版 libcrypt/bcrypt 会静默丢弃第 72 字节之后的内容，那种环境下同样的预哈希
# 还能避免「前 72 字节相同即视为同一口令」的隐性放宽，两条理由指向同一个做法。
_BCRYPT_MAX_INPUT_BYTES = 72

# 选十六进制而不是裸摘要或 base64：实测 bcrypt 5.0.0 能吃下含 NUL 的裸摘要（约 12.7%
# 的 SHA-256 摘要含 NUL）与 base64，所以这不是可用性门槛，而是可验证性取舍 ——
# 十六进制输出恒为 `[0-9a-f]`，不含 base64 的 `+` `/`，跨 bcrypt 版本与封装不会
# 再引入新的字节边界问题，且长度固定 64 < 72，永远走同一条分支。
_PREHASH_DIGEST_HEX_LENGTH = 64

# bcrypt 的盐由 OS CSPRNG 生成，自带随机盐，不需要额外加盐步骤。

# 口令先做 NFKC 规范化：同一个「看得见一样」的口令可能有多种码点表示
# （全角数字 １２３ / 半角 123、é 的组合序列 / 预组合序列），不规范化就会出现
# 「用户以为输的是同一个密码」的登录失败。
# 代价：规范化会把 NFKC 等价的不同口令折叠成同一个（全角 １２３ == 半角 123），
# 该取舍已在契约 §8 S-12 登记，翻案需先改契约。
_NORMALIZATION_FORM: Literal["NFKC"] = "NFKC"

# 工作因子。PRD/技术文档只规定了 Access 2h / Refresh 7d / 设备 24h 三类有效期，
# 未规定 bcrypt cost，此处按 OWASP 对 bcrypt 的推荐值取 12，已登记为契约 §8 S-11。
# 不提供环境变量覆盖：一是 AGENTS §8 要求配置不散读 `os.environ`，二是这种开关
# 会被用来「临时提速」而悄悄削弱离线爆破成本。cost 已编进哈希字符串本身，
# 将来要调高只影响新哈希，旧哈希仍能正常校验，不需要留活口。
_BCRYPT_COST = 12


def _normalize_password(password: str) -> bytes:
    """口令 → UTF-8 字节，规范化路径唯一。

    哈希与校验必须走这同一个函数：任何一侧多一步或少一步规范化，
    都会让「同一个口令」在换设备或换输入法后登不上。
    """
    return unicodedata.normalize(_NORMALIZATION_FORM, password).encode("utf-8")


def _prehash(password: str) -> bytes:
    """SHA-256 预哈希 → 定长纯 ASCII 摘要（64 字节），交给 bcrypt。"""
    digest = hashlib.sha256(_normalize_password(password)).digest()
    return digest.hex().encode("ascii")


def hash_password(password: str) -> str:
    """为口令生成 bcrypt 哈希。

    返回值可安全存进 `staff_accounts.password_hash` / `guardian_accounts.password_hash`，
    绝不返回给前端，也不写进日志。
    """
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=_BCRYPT_COST)).decode("ascii")


def is_password_hash_format_valid(password_hash: str) -> bool:
    """粗筛：字符串看起来像不像 `$2b$` bcrypt 哈希。

    只查前缀、cost 两位和 `$` 分隔结构，不做密码学校验。
    seed 脚本与 `verify_password` 共用它，保证两侧判定一致。
    """
    # isascii 必须先判：`verify_password` 会把哈希 encode("ascii")，
    # 一串「长度和前缀都对但含中文」的脏数据若放过，抛的就是
    # UnicodeEncodeError 而不是 PasswordHashInvalidError（未捕获即 500）。
    if not password_hash.isascii() or not password_hash.startswith("$2b$"):
        return False
    parts = password_hash.split("$")
    # $2b$<cost>$<22 字符盐><31 字符校验值> → split 后应为 ['', '2b', cost, rest]
    if len(parts) != 4:
        return False
    return parts[1] == "2b" and len(parts[3]) == 53


def verify_password(password: str, password_hash: str) -> bool:
    """校验口令是否匹配存储的哈希。

    返回 `False`：密码错误、哈希为空或格式明显不对（库里脏数据不该被当成「校验通过」）。
    抛 `PasswordHashInvalidError`：哈希看着像 bcrypt 但已损坏，属数据完整性问题，
    调用方按内部错误处理，不得当成「密码错误」放行或提示。
    """
    if not password_hash:
        return False
    if not is_password_hash_format_valid(password_hash):
        # 记 `hash_prefix`（`$2b$` + 两位 cost，共 7 个字符）不违背禁区 3：
        # 它不含盐与校验值，推不出口令；不记就无法定位是哪条 seed 数据写坏了。
        # 绝不记 password 本身，也不记完整哈希。
        logger.warning(
            "password verification rejected: stored hash is malformed",
            extra={"hash_prefix": password_hash[:7]},
        )
        raise PasswordHashInvalidError
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("ascii"))
    except ValueError:
        # 走到这里说明 bcrypt 自己解析不了这串哈希：格式过了粗筛但内容损坏。
        logger.warning(
            "password verification rejected: stored hash is unparseable",
            extra={"hash_prefix": password_hash[:7]},
        )
        raise PasswordHashInvalidError from None


# ------------------------------------------------------------------ 令牌摘要与有效期


def sha256_hex(value: str) -> str:
    """文本的 SHA-256 十六进制摘要。

    用途一：设备长期令牌与 `devices.token_hash` 比对（契约 §5.3，R3）。
    用途二：工牌原始 token 的 SHA-256 作为 `staff_tokens.token_hash` 主键查询（R9）。
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    """定长时间字符串比较，用于哈希/摘要类比对。

    摘要比对若用 `==`，短路位置会随相等前缀长度变化，理论上可被计时侧信道逐字节猜；
    `hmac.compare_digest` 不做短路。
    """
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


# ------------------------------------------------------------------ 有效期来源（Settings 唯一口径）


def access_ttl_seconds() -> int:
    """人员 Access 有效期（秒），来自 `Settings.access_token_expire_minutes`（默认 120 = 2h）。"""
    settings = get_settings()
    return _require_positive_ttl(
        settings.access_token_expire_minutes * 60, "access_token_expire_minutes"
    )


def refresh_ttl_seconds() -> int:
    """人员 Refresh 有效期（秒），来自 `Settings.refresh_token_expire_days`（默认 7d）。"""
    settings = get_settings()
    return _require_positive_ttl(
        settings.refresh_token_expire_days * 86400, "refresh_token_expire_days"
    )


def device_ttl_seconds() -> int:
    """设备会话有效期（秒），来自 `Settings.elder_session_expire_hours`（默认 24h）。"""
    settings = get_settings()
    return _require_positive_ttl(
        settings.elder_session_expire_hours * 3600, "elder_session_expire_hours"
    )


def _require_positive_ttl(ttl_seconds: int, label: str) -> int:
    """有效期必须是正整数秒。

    0 或负数会签出一个「天生已过期」的令牌，那种 bug 在运行期只表现为持续 401，
    很难往回查到配置层，所以在入口就拒。bool 也被拒：`True` 会通过 `isinstance(int)`，
    却意味着调用方把标志位当数字传了进来。
    """
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise TokenPayloadError(f"{label} must be an integer number of seconds")
    if ttl_seconds <= 0:
        raise TokenPayloadError(f"{label} must be a positive number of seconds")
    return ttl_seconds


# ------------------------------------------------------------------ JWT 签发与解码

# 契约 §5.1 的四类 `typ`。**注意不是任务卡里的 human_access / human_refresh**：
# 计划 §2.2 已裁决「冲突以冻结契约为准，且不兼容两套载荷」。
# 写成任务卡那套的后果是 task-5 签发的令牌 task-6 解不开，要到联调当天才暴露。
TYP_ACCESS = "access"
TYP_REFRESH = "refresh"
TYP_DEVICE = "device"
TYP_STAFF_CARD = "staff_card"

# 契约 §5.2 第 2 步：只有这三类能作为请求主体进入；refresh 只能由刷新流程解（task-5）。
PRINCIPAL_TYPE_LITERALS: tuple[str, ...] = (TYP_ACCESS, TYP_DEVICE, TYP_STAFF_CARD)

# `kind` 取值与 `sub` 前缀必须一致，来自契约 §5.1 与 §1 的主体模型。
KIND_STAFF = "staff"
KIND_GUARDIAN = "guardian"
KIND_DEVICE = "device"
KIND_STAFF_CARD = "staff_card"

# 员工角色（契约 R5）。家属不是员工 role，家属的 `kind=guardian` 令牌不带这个字段。
STAFF_ROLES = ("admin", "care_worker", "doctor")

# 设备 `tv` 取 `token_hash` 前 16 位（契约 §5.1、计划 §5.2 表）。
_DEVICE_TOKEN_HASH_PREFIX_LENGTH = 16

# 工牌短时效上限。PRD/TD 未给数值，此处只钉「必须短时效且不超过 24h」这一约束，
# 具体 TTL 仍由 Day 9 的调用方显式传入（计划 §2.2 末段：本任务不新增配置字段）。
_STAFF_CARD_MAX_TTL_SECONDS = 86400

# 解码必需字段（计划 §5.2 第 3 条）。缺任何一个都会让「回查数据库」拿不到定位信息。
_CORE_REQUIRED_CLAIMS = ("sub", "typ", "iat", "exp")

# `iat` 允许的未来偏移（时钟提前量）。
#
# 这条是被实测逼出来的（pyjwt 2.15.1，见 tests/test_security.py 的
# `test_pyjwt_leeway_is_the_wrong_knob_for_iat_skew`）：默认 `verify_iat=True` 对
# 「iat 在未来」零宽容，签发机与校验机之间哪怕只有 1 秒时钟差 —— 多容器部署里这是常态 ——
# 令牌刚签出来就永远解不开，表现为「密码明明对却持续 401」。
# 为什么不直接用 `leeway=60`：实测 leeway 确实能救 iat，但它是**全局**旋钮，
# 同一个 60 会让「已过期 30 秒」的令牌也通过 —— 等于把四类令牌的有效期都往后推 60 秒。
# 做法：关掉 pyjwt 的 iat 校验、自己判一个有上限的提前量，`exp` 的宽容保持 0。
# 上限而非无限宽容：提前量太离谱说明时钟或配置坏了，且 `iat` 未来得越远，
# 一个被盗令牌的可用窗口就越长。
_IAT_MAX_FUTURE_SKEW_SECONDS = 60

# 每类令牌允许的**全部**载荷（含核心四字段）。这张表是签发与解码共用的唯一依据：
# 少一项 → 签发时当场报错（而不是签出个下游解不开的令牌）；多一项 → 解码时拒绝
# （设备令牌里塞 `role`、refresh 里塞 `scope` 这类伪装都进不来）。
_ALLOWED_CLAIMS_BY_TYP: dict[str, frozenset[str]] = {
    TYP_ACCESS: frozenset({"sub", "typ", "iat", "exp", "kind", "tv", "role"}),
    TYP_REFRESH: frozenset({"sub", "typ", "iat", "exp", "kind", "tv"}),
    TYP_DEVICE: frozenset({"sub", "typ", "iat", "exp", "kind", "dev", "tv"}),
    TYP_STAFF_CARD: frozenset({"sub", "typ", "iat", "exp", "kind", "dev", "scope"}),
}


# HS256 密钥的最小长度（字节）。pyjwt 5.x 对短密钥会打 `InsecureKeyLengthWarning`，
# 但**只警告不拦截** —— 一个 8 字符的 `JWT_SECRET=abc` 能正常签发出「可离线爆破」的令牌。
# 32 字节来自 RFC 7518 §3.2：HMAC-SHA256 的密钥不应短于摘要长度。
_MIN_SECRET_BYTES = 32


def _require_secret(secret: str) -> str:
    """签发与解码共用的密钥闸门。

    `Settings.jwt_secret` 默认是空串（config.py:46），没有 `.env` 时这是可触达的真实状态：
    空密钥签出来的 HMAC 任何人都能自行签发，等价于没有鉴权。

    注意这里**不做 `strip()`**：HMAC 把密钥当原始字节，首尾空格是会参与计算的，
    「校验时去掉空格、签名时保留」会让两侧算出不同结果。只判「全是空白」这一种情况。
    """
    if not secret or secret.isspace():
        raise JwtSecretNotConfiguredError
    if len(secret.encode("utf-8")) < _MIN_SECRET_BYTES:
        raise JwtSecretNotConfiguredError(
            f"jwt_secret is too short (need >= {_MIN_SECRET_BYTES} bytes for HS256)"
        )
    return secret


def canonical_uuid(value: object) -> str:
    """把 UUID 或其文本规范成小写连字文本，非法就报错。

    `sub` 与 `dev` 里的 id 必须规范一致：同一个设备写成大写字母、花括号或无连字符形式，
    回查数据库时就成了查不到，故障表现是「令牌明明有效却持续 401」。
    """
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError):
        raise TokenPayloadError("id must be a UUID") from None


def _principal_sub(kind: str, principal_id: object) -> str:
    """拼 `sub`（`kind:canonical-uuid`）。

    `sub` 前缀是解码后分派到哪张表的唯一依据，所以宁可当场报错也不放过非法 UUID：
    前缀写错的令牌若能签出去，下游只会表现成「查不到记录 → 401」的玄学故障。
    """
    return f"{kind}:{canonical_uuid(principal_id)}"


def _encode_token(payload: Mapping[str, object]) -> str:
    """签发唯一出口：密钥与算法都只取 Settings，绝不由调用方传入。

    这是「空密钥拒签」和「算法白名单」两条要求在签发侧的落点，
    与 `decode_token` 共用 `_require_secret`，保证两侧不可能走偏。
    """
    settings = get_settings()
    return pyjwt.encode(
        dict(payload), _require_secret(settings.jwt_secret), algorithm=settings.jwt_algorithm
    )


def _build_payload(
    *,
    typ: str,
    sub: str,
    ttl_seconds: int,
    extra: Mapping[str, object],
    now: datetime | None,
    max_ttl_seconds: int | None = None,
) -> str:
    """按契约 §5.1 组装载荷，签发前先过与解码同一套载荷校验。

    `iat`/`exp` 用 UTC aware datetime 换算成 epoch 秒（AGENTS §8：存储一律 UTC），
    不用 `time.time()`，避免系统时钟与时区把过期判定带偏。

    「签发侧也跑一遍校验」是关键：否则一个矛盾载荷能顺利签出，
    要到 task-5 或 task-6 解码时才炸，报错点离写错的地方隔了几个模块。
    """
    if max_ttl_seconds is not None and ttl_seconds > max_ttl_seconds:
        raise TokenPayloadError(
            f"{typ} ttl must stay within {max_ttl_seconds}s; the real value is owned by Day 9"
        )
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise TokenPayloadError("now must be timezone-aware")
    iat = int(issued_at.timestamp())
    payload: dict[str, object] = {
        "sub": sub,
        "typ": typ,
        "kind": _expected_kind(typ=typ, sub_prefix=sub.partition(":")[0]),
        "iat": iat,
        "exp": iat + ttl_seconds,
        **extra,
    }
    _validate_payload(payload, expected_typ=typ)
    return _encode_token(payload)


def _expected_kind(typ: str, *, sub_prefix: str) -> str:
    """契约 §5.1 里 `kind` 的唯一决定方式：`typ` + `sub` 前缀。

    不能只用前缀推 —— 工牌令牌的 `sub` 是 `staff:<id>` 而 `kind` 是 `staff_card`，
    照前缀推会得出 `staff`，于是工牌在解码时被当成普通员工令牌，绕过范围限制。
    """
    if typ in (TYP_ACCESS, TYP_REFRESH):
        return sub_prefix
    if typ == TYP_DEVICE:
        return KIND_DEVICE
    if typ == TYP_STAFF_CARD:
        return KIND_STAFF_CARD
    raise TokenPayloadError("unknown token typ")


def sign_staff_access(
    *,
    kind: Literal["staff", "guardian"],
    staff_id: str | UUID,
    role: str,
    token_version: int,
    now: datetime | None = None,
) -> str:
    """人员 Access（契约 §5.1）。TTL 固定取 Settings，不接受调用方覆盖。

    `role` 只给员工；家属令牌不带 role（家属不是员工角色，契约 §1）。
    密钥不在这里检查：签发与解码都经 `_encode_token` / `decode_token` 的同一道闸门，
    多传一个 `secret` 参数只会造出「绕开 Settings 签错密钥」的第二条路。
    """
    if kind == KIND_STAFF:
        if role not in STAFF_ROLES:
            raise TokenPayloadError("staff role must be one of admin/care_worker/doctor")
        extra: dict[str, object] = {"role": role, "tv": token_version}
    else:
        if role:
            raise TokenPayloadError("guardian access token must not carry a staff role")
        extra = {"tv": token_version}
    return _build_payload(
        typ=TYP_ACCESS,
        sub=_principal_sub(kind, staff_id),
        ttl_seconds=access_ttl_seconds(),
        extra=extra,
        now=now,
    )


def sign_staff_refresh(
    *,
    kind: Literal["staff", "guardian"],
    staff_id: str | UUID,
    token_version: int,
    now: datetime | None = None,
) -> str:
    """人员 Refresh（契约 §5.1）。不带 `role`，角色只以 Access 与库为准。"""
    return _build_payload(
        typ=TYP_REFRESH,
        sub=_principal_sub(kind, staff_id),
        ttl_seconds=refresh_ttl_seconds(),
        extra={"tv": token_version},
        now=now,
    )


def sign_device_session(
    *,
    device_id: str | UUID,
    token_hash_prefix: str,
    now: datetime | None = None,
) -> str:
    """设备会话（契约 §5.1）：`tv` 为 `devices.token_hash` 前 16 位。

    这是「解绑即失效」的落点（契约 §5.3、AC-24）：解绑轮换 `token_hash` 后，
    旧会话的 `tv` 必然对不上，无需黑名单。
    """
    prefix = token_hash_prefix.lower()
    if len(prefix) != _DEVICE_TOKEN_HASH_PREFIX_LENGTH or any(
        c not in "0123456789abcdef" for c in prefix
    ):
        raise TokenPayloadError("token_hash_prefix must be the first 16 hex chars of token_hash")
    identifier = canonical_uuid(device_id)
    return _build_payload(
        typ=TYP_DEVICE,
        sub=f"{KIND_DEVICE}:{identifier}",
        ttl_seconds=device_ttl_seconds(),
        extra={"dev": identifier, "tv": prefix},
        now=now,
    )


def sign_staff_card(
    *,
    staff_id: str | UUID,
    device_id: str | UUID,
    scope: Sequence[str],
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    """工牌受限令牌（契约 §5.1、R9）。

    TTL 必须由调用方显式传入：Settings 里没有工牌有效期字段，本任务不新增配置项，
    也不写死时长（计划 §2.2 末段）。上限 `_STAFF_CARD_MAX_TTL_SECONDS` 只是把
    「短时效」这个语义钉住，防止传进一个 30 天当成 refresh 用。
    """
    items = normalize_scope(scope)
    return _build_payload(
        typ=TYP_STAFF_CARD,
        sub=_principal_sub(KIND_STAFF, staff_id),
        ttl_seconds=_require_positive_ttl(ttl_seconds, "staff card ttl_seconds"),
        extra={"dev": canonical_uuid(device_id), "scope": list(items)},
        now=now,
        max_ttl_seconds=_STAFF_CARD_MAX_TTL_SECONDS,
    )


def normalize_scope(scope: Sequence[str]) -> tuple[str, ...]:
    """工牌范围：非空、不重复、纯 ASCII。

    空 scope 的令牌等于「看不出能干什么」，直接拒。传字符串也会被拒 ——
    `Sequence[str]` 对 `str` 同样成立，不挡住就会把 `"workorder:handle"` 逐字符拆开。
    """
    if isinstance(scope, str) or not scope:
        raise TokenPayloadError("staff card scope must be a non-empty sequence of strings")
    items = tuple(str(item) for item in scope)
    if any(not item or not item.isascii() for item in items):
        raise TokenPayloadError("staff card scope items must be non-empty ASCII")
    if len(set(items)) != len(items):
        raise TokenPayloadError("staff card scope items must be unique")
    return items


def _validate_payload(payload: Mapping[str, object], *, expected_typ: str) -> None:
    """载荷内部一致性核对（计划 §5.2 第 5 条）。

    **同一套规则双向使用**：`_build_payload` 在签发前跑一遍，`decode_token` 在验签后
    跑一遍。只在解码侧校验的话，一个矛盾载荷能顺利签出，故障点会漂到 task-5 / task-6；
    只在签发侧校验的话，伪造或旧版本签出的令牌就没人拦。

    抛 `TokenPayloadError`（数据不合法）；解码侧把它统一转成 `TokenInvalidError`（401）。
    """
    allowed = _ALLOWED_CLAIMS_BY_TYP[expected_typ]
    keys = set(payload)
    if not keys <= allowed:
        raise TokenPayloadError("token carries claims not allowed for its typ")

    sub = payload.get("sub")
    if not isinstance(sub, str) or ":" not in sub:
        raise TokenPayloadError("subject must look like '<prefix>:<uuid>'")
    prefix, _, identifier = sub.partition(":")
    if prefix not in (KIND_STAFF, KIND_GUARDIAN, KIND_DEVICE) or not identifier:
        raise TokenPayloadError("unknown subject prefix")
    if canonical_uuid(identifier) != identifier:
        raise TokenPayloadError("subject id must be a canonical UUID")

    # `typ` 只能出现在它自己那一类里：refresh 令牌冒充 access 就是这么拦下的。
    if payload.get("typ") != expected_typ:
        raise TokenPayloadError("token typ does not match the expected typ")
    if payload.get("kind") != _expected_kind(expected_typ, sub_prefix=prefix):
        raise TokenPayloadError("token kind does not match its typ and subject")

    # 时间字段：必须是 int，且 `exp` 晚于 `iat`。倒挂的令牌签出来就是永不过期或立即过期。
    iat, exp = payload.get("iat"), payload.get("exp")
    if isinstance(iat, bool) or not isinstance(iat, int):
        raise TokenPayloadError("iat must be an integer epoch second")
    if isinstance(exp, bool) or not isinstance(exp, int):
        raise TokenPayloadError("exp must be an integer epoch second")
    if exp <= iat:
        raise TokenPayloadError("exp must be later than iat")
    # 有上限的时钟提前量。签发侧也跑这条：拿一个未来很远的 `now` 签令牌
    # 本身就是调用错误，不该等到解码时才发现。
    if iat > int(datetime.now(UTC).timestamp()) + _IAT_MAX_FUTURE_SKEW_SECONDS:
        raise TokenPayloadError("iat is too far in the future")

    if expected_typ in (TYP_ACCESS, TYP_REFRESH):
        tv = payload.get("tv")
        if isinstance(tv, bool) or not isinstance(tv, int):
            raise TokenPayloadError("staff/guardian tv must be an integer token_version")
        if expected_typ == TYP_ACCESS:
            role = payload.get("role")
            if prefix == KIND_STAFF:
                if role not in STAFF_ROLES:
                    raise TokenPayloadError("staff access token must carry a valid role")
            elif role is not None:
                # 家属不是员工角色（契约 §1）；带 role 的「家属」令牌是在伪造权限。
                raise TokenPayloadError("guardian access token must not carry a role")
    elif expected_typ == TYP_DEVICE:
        tv = payload.get("tv")
        if (
            not isinstance(tv, str)
            or len(tv) != _DEVICE_TOKEN_HASH_PREFIX_LENGTH
            or any(c not in "0123456789abcdef" for c in tv.lower())
        ):
            raise TokenPayloadError("device tv must be the first 16 hex chars of token_hash")
        dev = payload.get("dev")
        if not isinstance(dev, str) or canonical_uuid(dev) != dev:
            raise TokenPayloadError("device dev must be a canonical UUID")
        # `dev` 与 `sub` 必须是同一台设备：两个字段不一致就没法确定回查哪条记录。
        if dev != identifier:
            raise TokenPayloadError("device dev must equal the id part of sub")
        if prefix != KIND_DEVICE:
            raise TokenPayloadError("device token subject must use the device prefix")
    else:
        dev = payload.get("dev")
        if not isinstance(dev, str) or canonical_uuid(dev) != dev:
            raise TokenPayloadError("staff card dev must be a canonical UUID")
        if prefix != KIND_STAFF:
            raise TokenPayloadError("staff card subject must use the staff prefix")
        scope = payload.get("scope")
        if not isinstance(scope, list):
            raise TokenPayloadError("staff card scope must be a list")
        normalize_scope(scope)


def decode_token(
    token: str,
    *,
    expected_typ: str,
) -> dict[str, object]:
    """解码并强校验一类令牌。**`expected_typ` 必填且必填到调用方无法省略**。

    校验顺序（契约 §5.2）：验签与 `exp` → 核对 `typ` → 核对载荷一致性 → 交回调用方回查库。
    算法固定取 Settings，绝不按 token header 自选，否则 `alg: none` 与算法替换可乘。
    密钥同样只取 Settings：留一个 `secret` 参数就等于留一条「用弱密钥验别人的令牌」的路。
    """
    settings = get_settings()
    secret = _require_secret(settings.jwt_secret)
    if expected_typ not in _ALLOWED_CLAIMS_BY_TYP:
        raise TokenPayloadError("expected_typ must be one of access/refresh/device/staff_card")
    try:
        payload = pyjwt.decode(
            token,
            secret,
            algorithms=[settings.jwt_algorithm],
            options={
                "require": list(_CORE_REQUIRED_CLAIMS),
                "verify_exp": True,
                # iat 交给 _validate_payload 判（带 60s 上限的提前量），
                # 因为 pyjwt 只有全局 leeway，放宽 iat 会连带放宽 exp —— 见常量注释。
                "verify_iat": False,
            },
            # exp 的宽容保持 0：宽容一秒等于把有效期往后推一秒。
            # 关掉 verify_iat 不影响 exp 判定，两者在 pyjwt 里是分开的代码路径。
            leeway=0,
        )
    except pyjwt.ExpiredSignatureError:
        raise TokenExpiredError from None
    except pyjwt.InvalidTokenError as exc:
        _reject_if_leaks(exc, secret=secret, token=token)
        raise TokenInvalidError from None
    except Exception as exc:  # 底层加密库的意外异常一律不外泄内容
        _reject_if_leaks(exc, secret=secret, token=token)
        raise TokenInvalidError from None

    try:
        _validate_payload(payload, expected_typ=expected_typ)
    except TokenPayloadError:
        # 签名有效但载荷自相矛盾 → 对客户端仍是「凭证失效」401，不是 500。
        raise TokenInvalidError from None
    return dict(payload)


def _reject_if_leaks(exc: BaseException, *, secret: str, token: str) -> None:
    """底层异常文案里若含密钥或原始令牌，就地拦掉并只记事实。

    HMAC 相关的库异常有把 key 写进 message 的先例；`raise ... from exc` 会把整条
    message 带进日志。这里不重述内容，只把它换成不含信息的异常。
    """
    message = str(exc)
    if (secret and secret in message) or (token and token in message):
        logger.error(
            "jwt decoding raised an error whose message appears to contain the secret or raw token"
        )
        raise TokenInvalidError from None


__all__ = [
    "KIND_DEVICE",
    "KIND_GUARDIAN",
    "KIND_STAFF",
    "KIND_STAFF_CARD",
    "PRINCIPAL_TYPE_LITERALS",
    "STAFF_ROLES",
    "TYP_ACCESS",
    "TYP_DEVICE",
    "TYP_REFRESH",
    "TYP_STAFF_CARD",
    "JwtSecretNotConfiguredError",
    "PasswordHashInvalidError",
    "SecurityError",
    "TokenExpiredError",
    "TokenInvalidError",
    "TokenPayloadError",
    "access_ttl_seconds",
    "canonical_uuid",
    "constant_time_equals",
    "decode_token",
    "device_ttl_seconds",
    "hash_password",
    "is_password_hash_format_valid",
    "normalize_scope",
    "refresh_ttl_seconds",
    "sha256_hex",
    "sign_device_session",
    "sign_staff_access",
    "sign_staff_card",
    "sign_staff_refresh",
    "verify_password",
]
