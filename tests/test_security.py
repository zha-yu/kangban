"""task-4 T1+T2+T3+T4a · 密码、JWT、身份上下文与鉴权判定的单元测试（不依赖数据库）。

T1 覆盖计划 §5.1 第 5 条要求的五类输入：普通、错误、超 72 字节、中文/emoji、互不可验，
外加「长口令为什么必须预哈希」这一组依据实测的回归用例。

T2 覆盖四类令牌的签发/解码、`typ` 隔离、载荷一致性、TTL 来源与空密钥拒签。

T3 覆盖 `Identity` 的字段矩阵（谁能带 `elder_id`）、构造即合法、冻结与 `replace`
绕不过不变式，以及 contextvar 的初态、嵌套回滚、异常回滚和 asyncio 任务间不串号。

T4a 覆盖 `app/core/auth.py` 的纯判定层：Bearer 头语法、三类主体令牌的解码分派、
`refresh` 拒入、库里事实（状态 / `token_version` / 角色 / 设备哈希 / 工牌到期）与载荷的
逐项比对、身份构造入口、`check_roles` 只认员工会话。**这些用例把库里的值当参数直接喂给
判定函数**，因此不需要数据库；真正的 `SELECT` 与 FastAPI 依赖接线属 T4b，本文件不含。

bcrypt cost 取默认 12（约 0.3s/次），单文件耗时可接受；不为了提速把 cost 压到
低于允许下限，那样测出来的就不是生产路径的行为。
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import unicodedata
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from app.core.auth import (
    ROLE_FORBIDDEN,
    TOKEN_EXPIRED,
    TOKEN_INVALID,
    TOKEN_REVOKED,
    CredentialExpiredError,
    CredentialInvalidError,
    CredentialMissingError,
    CredentialRevokedError,
    RoleForbiddenError,
    bearer_token_from_header,
    check_roles,
    decode_principal_token,
    decode_refresh_token,
    device_hash_prefix_matches,
    device_identity_from_session,
    guardian_identity_from_access,
    staff_card_identity_from_card,
    staff_card_lookup_key,
    staff_identity_from_access,
)
from app.core.config import Settings, get_settings
from app.core.runtime_context import (
    Identity,
    IdentityConstructionError,
    IdentityNotSetError,
    as_mapping,
    get_identity,
    identity_scope,
    require_identity,
    reset_identity,
    set_identity,
)
from app.core.security import (
    _BCRYPT_MAX_INPUT_BYTES,
    _PREHASH_DIGEST_HEX_LENGTH,
    KIND_DEVICE,
    KIND_GUARDIAN,
    KIND_STAFF,
    KIND_STAFF_CARD,
    TYP_ACCESS,
    TYP_DEVICE,
    TYP_REFRESH,
    TYP_STAFF_CARD,
    JwtSecretNotConfiguredError,
    PasswordHashInvalidError,
    TokenExpiredError,
    TokenInvalidError,
    TokenPayloadError,
    access_ttl_seconds,
    constant_time_equals,
    decode_token,
    device_ttl_seconds,
    hash_password,
    is_password_hash_format_valid,
    refresh_ttl_seconds,
    sha256_hex,
    sign_device_session,
    sign_staff_access,
    sign_staff_card,
    sign_staff_refresh,
    verify_password,
)

NORMAL_PASSWORD = "Passw0rd-测试"
OTHER_PASSWORD = "Passw0rd-测试!"
# 中文口令：25 字 × 3 字节 = 75 字节，越过 bcrypt 的 72 字节线
# （24 个汉字恰好 72 字节、仍被接受，所以必须写到 25 字才是「超限」）
CHINESE_LONG = "我的密码很长很长很长很长很长很长很长很长很长很长了"
EMOJI_PASSWORD = "🍎🔐长口令🐉"
# 与 CHINESE_LONG 前 72 字节完全相同（第 73 字节的「了」被留在窗口外），
# 差异只出现在 bcrypt 上限之外
SAME_PREFIX_DIFFERENT_TAIL = CHINESE_LONG + "Z"

# ---------------------------------------------------------------- 夹具


def _over_bcrypt_limit(password: str) -> bool:
    """不走预哈希、直接喂 bcrypt 是否会因为超过 72 字节而失败。

    用来证明预哈希这一步不是可选装饰：短口令两种路径都可行，
    长中文/emoji 口令只有预哈希路径可行。
    """
    import bcrypt

    raw = unicodedata.normalize("NFKC", password).encode("utf-8")
    try:
        bcrypt.hashpw(raw, bcrypt.gensalt(rounds=4))
    except ValueError:
        return True
    return False


# ---------------------------------------------------------------- 正常链路


def test_hash_then_verify_normal_password() -> None:
    assert verify_password(NORMAL_PASSWORD, hash_password(NORMAL_PASSWORD)) is True


def test_hash_is_salted_random() -> None:
    """同一口令两次哈希结果不同（bcrypt 自带随机盐），但都能验过。"""
    first = hash_password(NORMAL_PASSWORD)
    second = hash_password(NORMAL_PASSWORD)
    assert first != second
    assert verify_password(NORMAL_PASSWORD, first) is True
    assert verify_password(NORMAL_PASSWORD, second) is True


def test_hash_is_textual_bcrypt_format() -> None:
    """库里与 ORM 列都是 text，哈希必须是可入库的 ASCII 文本且能被 bcrypt 解析。"""
    password_hash = hash_password(NORMAL_PASSWORD)
    assert isinstance(password_hash, str)
    assert password_hash.isascii()
    assert password_hash.startswith("$2b$")
    assert is_password_hash_format_valid(password_hash) is True


# ---------------------------------------------------------------- 密码错误


def test_verify_rejects_wrong_password() -> None:
    assert verify_password(OTHER_PASSWORD, hash_password(NORMAL_PASSWORD)) is False


def test_different_passwords_cannot_cross_verify() -> None:
    hash_a = hash_password(NORMAL_PASSWORD)
    hash_b = hash_password(OTHER_PASSWORD)
    assert verify_password(NORMAL_PASSWORD, hash_b) is False
    assert verify_password(OTHER_PASSWORD, hash_a) is False


def test_verify_empty_hash_is_false_not_error() -> None:
    """空哈希按「验不过」处理：宁可锁死账号，也绝不返回 True。"""
    assert verify_password(NORMAL_PASSWORD, "") is False


# ---------------------------------------------------------------- 超 72 字节上限


def test_password_over_72_bytes_verifies() -> None:
    long_ascii = "a" * 100
    assert len(long_ascii.encode("utf-8")) > 72
    assert verify_password(long_ascii, hash_password(long_ascii)) is True


def test_chinese_long_password_verifies() -> None:
    """25 个汉字 = 75 字节，超过 bcrypt 上限：中文口令整条链路必须可用。"""
    assert len(CHINESE_LONG.encode("utf-8")) == 75
    assert verify_password(CHINESE_LONG, hash_password(CHINESE_LONG)) is True


def test_emoji_long_password_verifies() -> None:
    """emoji 每字 4 字节，是越界最快的口令类型之一。"""
    long_emoji = EMOJI_PASSWORD * 10
    assert len(long_emoji.encode("utf-8")) > 72
    assert verify_password(long_emoji, hash_password(long_emoji)) is True


def test_prehash_shrinks_any_password_under_bcrypt_limit() -> None:
    """预哈希是「长口令可用」的唯一原因：同一口令走裸 bcrypt 会当场抛错。

    这条同时说明为什么 `_prehash` 不能被删掉：删了之后不是「变弱」，
    而是超过 72 字节的中文/emoji 口令在登录接口里直接 500。
    """
    from app.core.security import _prehash

    assert len(_prehash("x" * 4096)) == _PREHASH_DIGEST_HEX_LENGTH
    assert _over_bcrypt_limit(CHINESE_LONG) is True
    assert _over_bcrypt_limit(NORMAL_PASSWORD) is False
    assert _over_bcrypt_limit("🍎" * 30) is True


def test_tail_difference_beyond_72_bytes_still_matters() -> None:
    """第 72 字节之后的差异仍然改变口令摘要。

    bcrypt 5.0.0 用「抛错」而不是「截断」来守上限，所以这条不是防截断放宽的回归，
    而是钉住一个事实：整条口令都参与派生，尾部一个字符的差别不会被视为同一个口令。
    """
    import hashlib

    def digest(password: str) -> str:
        return hashlib.sha256(unicodedata.normalize("NFKC", password).encode()).hexdigest()

    assert digest(CHINESE_LONG) != digest(CHINESE_LONG + "Z")
    assert verify_password(SAME_PREFIX_DIFFERENT_TAIL, hash_password(CHINESE_LONG)) is False
    assert verify_password(CHINESE_LONG, hash_password(SAME_PREFIX_DIFFERENT_TAIL)) is False


def test_bcrypt_rejects_raw_input_over_72_bytes() -> None:
    """记录本版本 bcrypt 的真实行为，作为上面两条用例的依据。

    若将来换版本变成「静默截断」，本用例失败，就该重新确认预哈希的理由是否仍然成立。
    """
    import bcrypt

    raw = unicodedata.normalize("NFKC", CHINESE_LONG).encode("utf-8")
    assert len(raw) > 72
    with pytest.raises(ValueError, match="72 bytes"):
        bcrypt.hashpw(raw, bcrypt.gensalt(rounds=4))
    # 恰好 72 字节则被接受：上限是「不超过 72」，不是「小于 72」
    exactly_72 = "我" * 24
    assert len(exactly_72.encode("utf-8")) == 72
    assert _over_bcrypt_limit(exactly_72) is False


def test_prehash_output_fits_bcrypt_limit() -> None:
    """定长摘要保证任何口令都不会再触发截断分支。"""
    from app.core.security import _prehash

    for password in (CHINESE_LONG, "x" * 4096, EMOJI_PASSWORD):
        assert len(_prehash(password)) == _PREHASH_DIGEST_HEX_LENGTH
        assert len(_prehash(password)) <= _BCRYPT_MAX_INPUT_BYTES


# ---------------------------------------------------------------- Unicode / 规范化


@pytest.mark.parametrize(
    "password",
    [
        "张桂芬的密码",
        "🍎🔐长口令🐉",
        "Ｐａｓｓｗｏｒｄ１２３",  # 全角拉丁 + 全角数字
        "café密码",
        "口令\t换行\n都可以",
    ],
)
def test_unicode_variants_round_trip(password: str) -> None:
    assert verify_password(password, hash_password(password)) is True


def test_nfc_and_nfd_of_same_word_are_equivalent() -> None:
    """组合序列与预组合序列是同一个口令（NFC/NFD 属 NFKC 等价类）。"""
    nfc = "café"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    assert verify_password(nfd, hash_password(nfc)) is True


def test_fullwidth_and_halfwidth_collapse_is_intended() -> None:
    """钉住一个有意识的取舍：NFKC 会把全角数字折叠成半角。

    收益是同一口令的多种输入法表示可互通；代价是 NFKC 等价的不同口令撞成同一个，
    属行业通行做法。改动此断言等于改动口令语义，需要重新裁决。
    """
    fullwidth = "pass１２３"
    halfwidth = "pass123"
    assert fullwidth != halfwidth
    assert verify_password(halfwidth, hash_password(fullwidth)) is True


def test_normalization_is_applied_before_hashing() -> None:
    """规范化路径唯一：摘要应等于「NFKC 后 UTF-8 再 SHA-256」的十六进制。"""
    from app.core.security import _prehash

    expected = hashlib.sha256(unicodedata.normalize("NFKC", EMOJI_PASSWORD).encode()).hexdigest()
    assert _prehash(EMOJI_PASSWORD).decode("ascii") == expected


# ---------------------------------------------------------------- 损坏哈希


@pytest.mark.parametrize(
    "broken_hash",
    [
        "not-a-bcrypt-hash",
        "$2a$12$" + "x" * 53,  # 前缀版本不对（只允许 $2b$）
        "$2b$12$short",  # 长度不够
        "$2b$04$" + "x" * 31,  # cost 位数后只有一段，缺盐+校验值
        "$2b$12$脏数据不是ascii" + "x" * 20,  # 前缀与长度都像，但含非 ASCII
    ],
)
def test_malformed_hash_raises_without_leaking_content(
    broken_hash: str, caplog: pytest.LogCaptureFixture
) -> None:
    """格式非法/损坏 → 抛明确异常，且日志与异常文本都不含哈希内容与口令。

    最后一条（非 ASCII）是真实边界：`verify_password` 会对哈希做
    `encode("ascii")`，脏数据若放过就是未捕获的 UnicodeEncodeError（500），
    而不是可预期的 PasswordHashInvalidError。
    """
    with pytest.raises(PasswordHashInvalidError) as excinfo:
        verify_password(NORMAL_PASSWORD, broken_hash)

    assert broken_hash not in str(excinfo.value)
    assert NORMAL_PASSWORD not in str(excinfo.value)
    assert broken_hash not in caplog.text
    assert NORMAL_PASSWORD not in caplog.text


def test_hash_format_prefilter_rejects_non_bcrypt() -> None:
    assert is_password_hash_format_valid("$2b$12$" + "x" * 31) is False
    assert is_password_hash_format_valid("sha256:abcdef") is False
    assert is_password_hash_format_valid("") is False


# ---------------------------------------------------------------- 摘要比对工具


def test_sha256_hex_matches_known_vector() -> None:
    assert sha256_hex("device-secret") == hashlib.sha256(b"device-secret").hexdigest()
    assert len(sha256_hex("")) == 64


def test_constant_time_equals_compares_by_value() -> None:
    assert constant_time_equals("abc", "abc") is True
    assert constant_time_equals("abc", "abd") is False
    assert constant_time_equals("abc", "abcd") is False
    # 非 ASCII 也要能比：设备哈希前缀比对可能遇到被截断的多字节串
    assert constant_time_equals("口令", "口令") is True


# ---------------------------------------------------------------- 工作因子固定


def test_cost_is_pinned_at_12_with_no_override() -> None:
    """钉住「不给 bcrypt cost 留可削弱开关」这个决定。

    曾经有过 `SILVERMATE_BCRYPT_COST` 环境变量覆盖，两个理由去掉它：
    与 AGENTS §8「配置不散读 os.environ」冲突；且它能被用来悄悄降低口令派生成本。
    若将来有人再加回 env 覆盖，本用例失败。
    """
    from app.core.security import _BCRYPT_COST

    assert _BCRYPT_COST == 12
    assert hash_password(NORMAL_PASSWORD).startswith("$2b$12$")


# ================================================================== T2 · JWT
#
# 夹具说明：`get_settings()` 是 `lru_cache` 单例，测试注入密钥必须成对做
# 「setenv → cache_clear」与「清 cache 复原」，否则会把这个密钥泄漏给同一进程里
# 后续的文件（例如 test_health.py），造成「别处悄悄有密钥」的假绿或假红。

# 48 字节：满足内核的 HS256 最小密钥长度（32 字节），也不会触发 pyjwt 的短密钥警告。
JWT_SECRET = "unit-test-secret-not-a-real-key-0123456789abcdef"
STAFF_ID = "11111111-1111-4111-8111-111111111111"
GUARDIAN_ID = "22222222-2222-4222-8222-222222222222"
DEVICE_ID = "33333333-3333-4333-8333-333333333333"
ELDER_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
TOKEN_HASH_PREFIX = "a1b2c3d4e5f60718"

# 大写规范化用例必须用这个而不是上面三个：它们全是数字，`.upper()` 是空操作，
# 拿它们写「大写也能规范化」的断言等于什么都没测（这三条曾经全绿而无声失效）。
LETTERED_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0123456789ab"


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """注入测试密钥并返回生效的 Settings。

    用 `yield` 而不是 `return`：`cache_clear()` 必须在测试**之后**也执行一次，
    否则带密钥的 Settings 会留在 `lru_cache` 里，泄漏给同进程后续文件。
    """
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _now() -> datetime:
    """真实当前时间。

    曾经写成固定值 `datetime(2026, 11, 4, 12, tzinfo=UTC)`：本机时钟是 11:04 UTC，
    于是所有令牌的 `iat` 都落在「未来 56 分钟」，被 `_IAT_MAX_FUTURE_SKEW_SECONDS`
    正确拦下。**写死基准时间的测试会随真实时钟腐烂**，跨零点或跨时区就整批变红。
    过期用例用 `_now() - timedelta(days=1)`，不依赖固定日期。
    """
    return datetime.now(UTC)


def _sign_staff_access(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "kind": KIND_STAFF,
        "staff_id": STAFF_ID,
        "role": "care_worker",
        "token_version": 3,
        "now": _now(),
    }
    kwargs.update(overrides)
    return sign_staff_access(**kwargs)  # type: ignore[arg-type]


def _sign_device(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "device_id": DEVICE_ID,
        "token_hash_prefix": TOKEN_HASH_PREFIX,
        "now": _now(),
    }
    kwargs.update(overrides)
    return sign_device_session(**kwargs)  # type: ignore[arg-type]


def _sign_staff_card(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "staff_id": STAFF_ID,
        "device_id": DEVICE_ID,
        "scope": ["workorder:handle"],
        "ttl_seconds": 600,
        "now": _now(),
    }
    kwargs.update(overrides)
    return sign_staff_card(**kwargs)  # type: ignore[arg-type]


def _unsigned_token(payload: dict[str, object]) -> str:
    """手工拼一个 `alg: none` 的令牌（不签名），经典绕过尝试。"""

    def b64(obj: object) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )

    header = {"alg": "none", "typ": "JWT"}
    return f"{b64(header)}.{b64(payload)}."


def _tamper(token: str, **claim_changes: object) -> str:
    """改载荷后重新 base64，但**不重签** —— 模拟拿到令牌的人篡改内容。"""
    header_b64, payload_b64, signature = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    payload.update(claim_changes)
    new_payload = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    return f"{header_b64}.{new_payload}.{signature}"


# ---------------------------------------------------------------- 正常链路


def test_staff_access_round_trip(jwt_env: Settings) -> None:
    del jwt_env
    token = _sign_staff_access()
    payload = decode_token(token, expected_typ=TYP_ACCESS)
    assert payload["sub"] == f"staff:{STAFF_ID}"
    assert payload["kind"] == KIND_STAFF
    assert payload["role"] == "care_worker"
    assert payload["tv"] == 3
    assert payload["typ"] == TYP_ACCESS
    assert payload["exp"] - payload["iat"] == access_ttl_seconds()


def test_guardian_access_round_trip_has_no_role(jwt_env: Settings) -> None:
    del jwt_env
    token = sign_staff_access(
        kind=KIND_GUARDIAN, staff_id=GUARDIAN_ID, role="", token_version=1, now=_now()
    )
    payload = decode_token(token, expected_typ=TYP_ACCESS)
    assert payload["sub"] == f"guardian:{GUARDIAN_ID}"
    assert payload["kind"] == KIND_GUARDIAN
    assert "role" not in payload


def test_refresh_round_trip(jwt_env: Settings) -> None:
    del jwt_env
    token = sign_staff_refresh(kind=KIND_STAFF, staff_id=STAFF_ID, token_version=3, now=_now())
    payload = decode_token(token, expected_typ=TYP_REFRESH)
    assert payload["typ"] == TYP_REFRESH
    assert payload["tv"] == 3
    assert "role" not in payload


def test_device_round_trip(jwt_env: Settings) -> None:
    del jwt_env
    token = _sign_device()
    payload = decode_token(token, expected_typ=TYP_DEVICE)
    assert payload["sub"] == f"device:{DEVICE_ID}"
    assert payload["kind"] == KIND_DEVICE
    assert payload["dev"] == DEVICE_ID
    assert payload["tv"] == TOKEN_HASH_PREFIX


def test_staff_card_round_trip(jwt_env: Settings) -> None:
    del jwt_env
    token = _sign_staff_card(scope=["workorder:handle", "health:staff_form"])
    payload = decode_token(token, expected_typ=TYP_STAFF_CARD)
    assert payload["sub"] == f"staff:{STAFF_ID}"
    assert payload["kind"] == KIND_STAFF_CARD
    assert payload["scope"] == ["workorder:handle", "health:staff_form"]
    assert payload["exp"] - payload["iat"] == 600


# ---------------------------------------------------------------- TTL 来源 Settings


def test_ttls_come_from_settings_not_literals(jwt_env: Settings) -> None:
    """改 Settings 字段必须改变令牌有效期；写死的 TTL 会在这里暴露。"""
    assert access_ttl_seconds() == 120 * 60
    assert refresh_ttl_seconds() == 7 * 86400
    assert device_ttl_seconds() == 24 * 3600

    monkeypatched = Settings(jwt_secret=JWT_SECRET, access_token_expire_minutes=7)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.core.security.get_settings", lambda: monkeypatched)
        assert access_ttl_seconds() == 7 * 60
        token = _sign_staff_access()
        payload = decode_token(token, expected_typ=TYP_ACCESS)
        assert payload["exp"] - payload["iat"] == 420


def test_default_settings_ship_an_empty_secret() -> None:
    """钉住 AGENTS 禁区 2 的真实起点：不配 `.env` 时 `jwt_secret` 默认就是空串。

    这条是「空密钥拒签」能被真实触达的前提 —— 如果默认值是个写死的密钥，
    忘记配 `.env` 反而会静默用着一个公开密钥签名。
    """
    assert Settings.model_fields["jwt_secret"].default == ""


# ---------------------------------------------------------------- 空密钥拒签


@pytest.mark.parametrize("bad_secret", ["", "   ", "\t\n"])
def test_empty_secret_refuses_both_signing_and_decoding(
    monkeypatch: pytest.MonkeyPatch, bad_secret: str
) -> None:
    """签发与校验两侧都要拒（计划 §5.2 第 2 条）。

    只拒签发不够：若解码侧放行，任何人都能用空密钥自签一个「有效」令牌。
    """
    monkeypatch.setenv("JWT_SECRET", bad_secret)
    get_settings.cache_clear()
    try:
        with pytest.raises(JwtSecretNotConfiguredError):
            _sign_staff_access()
        with pytest.raises(JwtSecretNotConfiguredError):
            decode_token("anything", expected_typ=TYP_ACCESS)
    finally:
        get_settings.cache_clear()


def test_short_secret_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """pyjwt 对短密钥只**警告不拦截**，内核必须拦。

    8 个字符的 `JWT_SECRET` 能正常签发出可离线爆破的令牌，那种「跑起来了但有洞」
    比直接报错危险得多。顺带钉住一条实现细节：判定不能 `strip()` 后再用原值签名，
    否则首尾空格参与不参与计算会两侧不一致。
    """
    monkeypatch.setenv("JWT_SECRET", "short123")
    get_settings.cache_clear()
    try:
        with pytest.raises(JwtSecretNotConfiguredError):
            _sign_staff_access()
        with pytest.raises(JwtSecretNotConfiguredError):
            decode_token("anything", expected_typ=TYP_ACCESS)
    finally:
        get_settings.cache_clear()


def test_secret_with_surrounding_spaces_is_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """长度达标、首尾带空格的密钥应当可用（空格是密钥的一部分，不是空白噪声）。

    如果哪天有人「顺手加个 strip()」，这条会红：签名侧剥空格、校验侧不剥，
    令牌就会在自己系统里解不开。
    """
    padded = f"  {JWT_SECRET}  "
    monkeypatch.setenv("JWT_SECRET", padded)
    get_settings.cache_clear()
    try:
        token = _sign_staff_access()
        assert decode_token(token, expected_typ=TYP_ACCESS)["typ"] == TYP_ACCESS
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------- 五类失败之一：伪造签名


def test_forged_signature_is_rejected(jwt_env: Settings) -> None:
    """同一份载荷换个密钥签出来，必须解不开。"""
    import jwt as pyjwt

    forged = pyjwt.encode(
        {
            "sub": f"staff:{STAFF_ID}",
            "typ": TYP_ACCESS,
            "kind": KIND_STAFF,
            "role": "admin",
            "tv": 3,
            "iat": 1,
            "exp": 2**31,
        },
        "attacker-secret-long-enough-to-pass-length-check",
        algorithm="HS256",
    )
    with pytest.raises(TokenInvalidError):
        decode_token(forged, expected_typ=TYP_ACCESS)


_TAMPER_CASES = [
    ("role 提权", {"role": "admin"}),
    ("tv 改版本", {"tv": 99}),
    ("换主体", {"sub": f"staff:{GUARDIAN_ID}"}),
]


@pytest.mark.parametrize(("label", "changes"), _TAMPER_CASES)
def test_tampered_claims_are_rejected(jwt_env: Settings, label: str, changes: dict) -> None:
    """改载荷不重签：签名立刻对不上。label 只用于失败时可读。"""
    del label
    token = _tamper(_sign_staff_access(), **changes)
    with pytest.raises(TokenInvalidError):
        decode_token(token, expected_typ=TYP_ACCESS)


def test_alg_none_token_is_rejected(jwt_env: Settings) -> None:
    """`alg: none` 的经典绕过：算法白名单里只有 Settings 的 HS256。"""
    forged = _unsigned_token(
        {
            "sub": f"staff:{STAFF_ID}",
            "typ": TYP_ACCESS,
            "kind": KIND_STAFF,
            "role": "admin",
            "tv": 3,
            "iat": int(_now().timestamp()),
            "exp": int((_now() + timedelta(hours=2)).timestamp()),
        }
    )
    with pytest.raises(TokenInvalidError):
        decode_token(forged, expected_typ=TYP_ACCESS)


def test_other_hmac_algorithm_token_is_rejected(jwt_env: Settings) -> None:
    """同密钥换算法（HS512）也不行：算法固定取 Settings，不按 header 自选。"""
    import jwt as pyjwt

    token = pyjwt.encode(
        {
            "sub": f"staff:{STAFF_ID}",
            "typ": TYP_ACCESS,
            "kind": KIND_STAFF,
            "role": "admin",
            "tv": 3,
            "iat": 1,
            "exp": 2**31,
        },
        JWT_SECRET,
        algorithm="HS512",
    )
    with pytest.raises(TokenInvalidError):
        decode_token(token, expected_typ=TYP_ACCESS)


# ---------------------------------------------------------------- 五类失败之二：过期


def test_expired_token_raises_dedicated_error(jwt_env: Settings) -> None:
    """计划 §5.5 第 5 条：用过去时间构造 `exp`，不引入时间冻结依赖。"""
    past = _now() - timedelta(days=1)
    token = _sign_staff_access(now=past)
    with pytest.raises(TokenExpiredError):
        decode_token(token, expected_typ=TYP_ACCESS)


def test_expired_is_still_a_token_invalid_subclass(jwt_env: Settings) -> None:
    """`TokenExpiredError` 必须能被当 `TokenInvalidError` 捕获 → auth 层一条 except 出 401。"""
    token = _sign_staff_access(now=_now() - timedelta(days=1))
    with pytest.raises(TokenInvalidError):
        decode_token(token, expected_typ=TYP_ACCESS)


def test_zero_ttl_is_refused_at_signing(jwt_env: Settings) -> None:
    """TTL 传 0 会签出「天生已过期」的令牌，签发入口就该拒。"""
    with pytest.raises(TokenPayloadError):
        _sign_staff_card(ttl_seconds=0)
    with pytest.raises(TokenPayloadError):
        _sign_staff_card(ttl_seconds=-1)


# ---------------------------------------------------------------- 五类失败之三：typ 混用


_ALL_TYPE_LITERALS = (TYP_ACCESS, TYP_REFRESH, TYP_DEVICE, TYP_STAFF_CARD)


@pytest.mark.parametrize("signer", ["access", "refresh", "device", "staff_card"])
@pytest.mark.parametrize("expected", _ALL_TYPE_LITERALS)
def test_typ_cannot_be_cross_used(jwt_env: Settings, signer: str, expected: str) -> None:
    """四类令牌两两交叉：只有 `signer == expected` 那一格能通过。

    这一条同时覆盖「refresh 不能当 access 用」「人 token 不能当设备 token 用」，
    是任务卡硬约束 3 的机器证明。
    """
    signed = {
        "access": _sign_staff_access,
        "refresh": lambda: sign_staff_refresh(
            kind=KIND_STAFF, staff_id=STAFF_ID, token_version=3, now=_now()
        ),
        "device": _sign_device,
        "staff_card": _sign_staff_card,
    }[signer]()
    if signer == expected:
        assert decode_token(signed, expected_typ=expected)["typ"] == expected
    else:
        with pytest.raises(TokenInvalidError):
            decode_token(signed, expected_typ=expected)


def test_expected_typ_is_required_keyword_argument(jwt_env: Settings) -> None:
    """漏传 `expected_typ` 必须是 TypeError，而不是「默认接受任何类型」。

    防串用的闸口如果有个默认值，忘记传参的代码会静默放行所有类型。
    """
    with pytest.raises(TypeError):
        decode_token(_sign_staff_access())  # type: ignore[call-arg]


# ---------------------------------------------------------------- 缺少必需字段 / 非法载荷


def test_missing_core_claim_is_rejected(jwt_env: Settings) -> None:
    """`sub`/`typ`/`iat`/`exp` 缺任何一个都拒（计划 §5.2 第 3 条）。

    直接用底层库签一个缺字段的令牌，模拟「非本内核签出」的东西。
    """
    import jwt as pyjwt

    complete = {
        "sub": f"staff:{STAFF_ID}",
        "typ": TYP_ACCESS,
        "kind": KIND_STAFF,
        "role": "care_worker",
        "tv": 3,
        "iat": int(_now().timestamp()),
        "exp": int((_now() + timedelta(hours=2)).timestamp()),
    }
    for missing in ("sub", "typ", "iat", "exp"):
        broken = {k: v for k, v in complete.items() if k != missing}
        token = pyjwt.encode(broken, JWT_SECRET, algorithm="HS256")
        # 不断言具体文案：本层要保的是「缺必需字段一定变成 TokenInvalidError（→ 401）」，
        # 绑死 pyjwt 的措辞只会让升级变成测试噪音。
        with pytest.raises(TokenInvalidError):
            decode_token(token, expected_typ=TYP_ACCESS)


def test_unknown_claim_is_rejected(jwt_env: Settings) -> None:
    """签名正确但带白名单外的载荷（这里加一个 `aud`）→ 拒。

    白名单双向生效：`sign_staff_access` 根本不接受 `aud`，解码侧也不放过它。
    """
    import jwt as pyjwt

    token = pyjwt.encode(
        {
            "sub": f"staff:{STAFF_ID}",
            "typ": TYP_ACCESS,
            "kind": KIND_STAFF,
            "role": "admin",
            "tv": 3,
            "iat": 1,
            "exp": 2**31,
            "aud": "someone-else",
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenInvalidError):
        decode_token(token, expected_typ=TYP_ACCESS)


def test_device_token_cannot_carry_role(jwt_env: Settings) -> None:
    """设备令牌塞 `role` 是「拿低层令牌伪装成有权主体」的尝试，白名单直接挡掉。"""
    import jwt as pyjwt

    token = pyjwt.encode(
        {
            "sub": f"device:{DEVICE_ID}",
            "typ": TYP_DEVICE,
            "kind": KIND_DEVICE,
            "dev": DEVICE_ID,
            "tv": TOKEN_HASH_PREFIX,
            "role": "admin",
            "iat": 1,
            "exp": 2**31,
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenInvalidError):
        decode_token(token, expected_typ=TYP_DEVICE)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"kind": KIND_STAFF, "role": "super_user"}, "非法员工角色"),
        ({"kind": KIND_GUARDIAN, "role": "admin"}, "家属带 role"),
        ({"staff_id": "not-a-uuid"}, "非法 UUID 主体"),
        ({"staff_id": "11111111-1111-4111-8111"}, "残缺 UUID"),
    ],
)
def test_inconsistent_subject_is_refused_at_signing(
    jwt_env: Settings, overrides: dict, reason: str
) -> None:
    """矛盾载荷在**签发时**就报错，而不是签出一个下游必然 401 的令牌。"""
    del reason
    kwargs = {
        "kind": KIND_STAFF,
        "staff_id": STAFF_ID,
        "role": "care_worker",
        "token_version": 3,
        "now": _now(),
    }
    kwargs.update(overrides)
    with pytest.raises(TokenPayloadError):
        sign_staff_access(**kwargs)  # type: ignore[arg-type]


def test_uppercase_uuid_is_canonicalised_not_rejected(jwt_env: Settings) -> None:
    """大写输入规范化成小写 `sub`，而不是报错。

    同一台设备/同一个人存在多种 UUID 写法是常态；若拒绝大写，调用方就得自己
    记得先规范化，忘记的表现是「库里同一记录两种主键文本」。
    规范化只发生在**签发侧**，解码侧要求 `sub` 已经是规范形式（见 _validate_payload），
    否则旧令牌里的非规范文本会被静默接受。
    """
    assert LETTERED_ID.upper() != LETTERED_ID, "用例常量变了，这条会退化成空测试"
    token = sign_staff_access(
        kind=KIND_STAFF,
        staff_id=LETTERED_ID.upper(),
        role="care_worker",
        token_version=1,
        now=_now(),
    )
    payload = decode_token(token, expected_typ=TYP_ACCESS)
    assert payload["sub"] == f"staff:{LETTERED_ID}"


def test_non_canonical_sub_in_a_validly_signed_token_is_rejected(jwt_env: Settings) -> None:
    """签名对、但 `sub` 不是规范形式（大写）→ 拒。

    这条守住上一条评论说的边界：规范化是「我签发时做好」，不是「别人塞什么我都认」。
    """
    import jwt as pyjwt

    token = pyjwt.encode(
        {
            "sub": f"staff:{LETTERED_ID.upper()}",
            "typ": TYP_ACCESS,
            "kind": KIND_STAFF,
            "role": "admin",
            "tv": 1,
            "iat": int(_now().timestamp()),
            "exp": int((_now() + timedelta(hours=2)).timestamp()),
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenInvalidError):
        decode_token(token, expected_typ=TYP_ACCESS)


# ---------------------------------------------------------------- 时钟提前量


def test_slightly_future_iat_is_tolerated(jwt_env: Settings) -> None:
    """签发机时钟快 30 秒必须能用。

    多容器部署里机器间差一秒是常态，不容忍的表现是「刚签的令牌立刻 401」。
    pyjwt 默认对 `iat` 零宽容（见下一条实测），所以我们关掉它的 iat 校验、
    自己判一个有上限的提前量。
    """
    token = _sign_staff_access(now=_now() + timedelta(seconds=30))
    assert decode_token(token, expected_typ=TYP_ACCESS)["typ"] == TYP_ACCESS


def test_grossly_future_iat_is_rejected(jwt_env: Settings) -> None:
    """提前 10 分钟不是时钟漂移，是调用错误或人为造的长效令牌 → 拒。

    两侧都要拦：签发侧 `sign_staff_access` 当场报错（写错 `now` 的人立刻知道），
    解码侧对「别人用同一密钥签出来的」远未来 `iat` 也拒（不能只靠签发侧自觉）。
    """
    with pytest.raises(TokenPayloadError):
        _sign_staff_access(now=_now() + timedelta(minutes=10))

    import jwt as pyjwt

    iat = int((_now() + timedelta(minutes=10)).timestamp())
    forged = pyjwt.encode(
        {
            "sub": f"staff:{STAFF_ID}",
            "typ": TYP_ACCESS,
            "kind": KIND_STAFF,
            "role": "admin",
            "tv": 1,
            "iat": iat,
            "exp": iat + 7200,
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenInvalidError):
        decode_token(forged, expected_typ=TYP_ACCESS)


def test_pyjwt_leeway_is_the_wrong_knob_for_iat_skew(jwt_env: Settings) -> None:
    """记录事实依据（pyjwt 2.15.1 实测）：不用 `leeway` 而自判 `iat` 的理由。

    四条实测结论，前两条推翻「pyjwt 不管 iat 未来」的想当然：
    1. 默认配置下 `iat` 超前 30 秒 **会** 报 `ImmatureSignatureError` —— 零宽容；
    2. `leeway=60` **能** 救 `iat`（作用于 iat，不只是 exp）；
    3. 但 `leeway` 是全局的：同一个 60 也让「已过期 30 秒」的 `exp` 通过；
    4. 所以只能用 `verify_iat=False` + 自判提前量，才能既容忍时钟差、
       又保持 `exp` 零宽容（宽容一秒等于有效期往后推一秒）。
    """
    import jwt as pyjwt

    def _encode(iat: int, exp: int) -> str:
        return pyjwt.encode(
            {"sub": f"staff:{STAFF_ID}", "typ": TYP_ACCESS, "iat": iat, "exp": exp},
            JWT_SECRET,
            algorithm="HS256",
        )

    now = int(_now().timestamp())

    future_iat = _encode(now + 30, now + 7200)
    with pytest.raises(pyjwt.ImmatureSignatureError):
        pyjwt.decode(future_iat, JWT_SECRET, algorithms=["HS256"])
    # 结论 2：leeway 确实能救 iat —— 所以「关不掉」不是理由，下面才是理由。
    assert pyjwt.decode(future_iat, JWT_SECRET, algorithms=["HS256"], leeway=60)["iat"]

    expired_30s = _encode(now - 3600, now - 30)
    with pytest.raises(pyjwt.ExpiredSignatureError):
        pyjwt.decode(expired_30s, JWT_SECRET, algorithms=["HS256"])
    # 结论 3：同一个 leeway 把过期令牌也放行了，这正是我们的 decode 不许发生的。
    assert pyjwt.decode(expired_30s, JWT_SECRET, algorithms=["HS256"], leeway=60)["exp"]
    # 我们的 decode_token 对同一串已过期令牌仍然拒绝（exp 宽容为 0）。
    with pytest.raises(TokenExpiredError):
        decode_token(expired_30s, expected_typ=TYP_ACCESS)


def test_device_tv_must_be_16_hex_chars(jwt_env: Settings) -> None:
    for bad in ("a1b2c3d4", "a1b2c3d4e5f6071899", "zzzzzzzzzzzzzzzz"):
        with pytest.raises(TokenPayloadError):
            _sign_device(token_hash_prefix=bad)


def test_staff_card_scope_rules(jwt_env: Settings) -> None:
    """空 scope 看不出能干什么；传字符串会被逐字符拆开 —— 两种都在签发入口拒。"""
    with pytest.raises(TokenPayloadError):
        _sign_staff_card(scope=[])
    with pytest.raises(TokenPayloadError):
        _sign_staff_card(scope="workorder:handle")
    with pytest.raises(TokenPayloadError):
        _sign_staff_card(scope=["a", "a"])


def test_staff_card_ttl_must_be_explicit_and_short(jwt_env: Settings) -> None:
    """工牌 TTL 必须显式传入（Settings 无此字段），且不许超过 24h 上限。"""
    with pytest.raises(TypeError):
        sign_staff_card(staff_id=STAFF_ID, device_id=DEVICE_ID, scope=["a"], now=_now())  # type: ignore[call-arg]
    with pytest.raises(TokenPayloadError):
        _sign_staff_card(ttl_seconds=90000)


# ---------------------------------------------------------------- 身份不外泄


def test_failures_do_not_log_token_or_secret(
    jwt_env: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """日志与异常文本都不许出现原始令牌或密钥（AGENTS 禁区 3）。"""
    import jwt as pyjwt

    forged = pyjwt.encode(
        {
            "sub": f"staff:{STAFF_ID}",
            "typ": TYP_ACCESS,
            "kind": KIND_STAFF,
            "role": "admin",
            "tv": 3,
            "iat": 1,
            "exp": 2**31,
        },
        "attacker-secret-long-enough-to-pass-length-check",
        algorithm="HS256",
    )
    caplog.clear()
    with pytest.raises(TokenInvalidError) as excinfo:
        decode_token(forged, expected_typ=TYP_ACCESS)

    assert forged not in str(excinfo.value)
    assert JWT_SECRET not in str(excinfo.value)
    assert forged not in caplog.text
    assert JWT_SECRET not in caplog.text


def test_unknown_expected_typ_is_rejected(jwt_env: Settings) -> None:
    with pytest.raises(TokenPayloadError):
        decode_token(_sign_staff_access(), expected_typ="human_access")


def test_task_card_naming_is_not_accepted(jwt_env: Settings) -> None:
    """任务卡旧口径（`human_access` / `ver`）必须整体不可用。

    冻结契约优先（计划 §2.2）。若将来有人「顺手兼容两套载荷」，
    这条会挡住：兼容旧名等于允许 `typ=human_access` 的令牌进系统。
    """
    import jwt as pyjwt

    legacy = pyjwt.encode(
        {
            "sub": f"staff:{STAFF_ID}",
            "typ": "human_access",
            "kind": KIND_STAFF,
            "role": "admin",
            "ver": 3,
            "iat": 1,
            "exp": 2**31,
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenInvalidError):
        decode_token(legacy, expected_typ=TYP_ACCESS)
    with pytest.raises(TokenPayloadError):
        decode_token(legacy, expected_typ="human_access")


def test_uuid_objects_and_text_produce_the_same_token(jwt_env: Settings) -> None:
    """`UUID` 对象与等价文本必须签出可互解的载荷 —— auth 层两种都会传。"""
    token = sign_staff_access(
        kind=KIND_STAFF, staff_id=uuid.UUID(STAFF_ID), role="doctor", token_version=1, now=_now()
    )
    assert decode_token(token, expected_typ=TYP_ACCESS)["sub"] == f"staff:{STAFF_ID}"


def test_naive_datetime_is_refused(jwt_env: Settings) -> None:
    """不带时区的 `now` 会被按本地时区解释，直接把 `exp` 推偏几小时 → 拒。"""
    with pytest.raises(TokenPayloadError):
        _sign_staff_access(now=datetime(2026, 11, 4, 12, 0))


# ==================================================================== T3 · Identity


# ------------------------------------------------------------------ 构造与字段矩阵


def _staff_identity(**overrides: object) -> Identity:
    kwargs: dict[str, object] = {"kind": KIND_STAFF, "staff_id": STAFF_ID, "role": "care_worker"}
    kwargs.update(overrides)
    return Identity(**kwargs)  # type: ignore[arg-type]


def _guardian_identity(**overrides: object) -> Identity:
    kwargs: dict[str, object] = {"kind": KIND_GUARDIAN, "guardian_id": GUARDIAN_ID}
    kwargs.update(overrides)
    return Identity(**kwargs)  # type: ignore[arg-type]


def _device_identity(**overrides: object) -> Identity:
    kwargs: dict[str, object] = {"kind": KIND_DEVICE, "device_id": DEVICE_ID, "elder_id": ELDER_ID}
    kwargs.update(overrides)
    return Identity(**kwargs)  # type: ignore[arg-type]


def _card_identity(**overrides: object) -> Identity:
    kwargs: dict[str, object] = {
        "kind": KIND_STAFF_CARD,
        "staff_id": STAFF_ID,
        "device_id": DEVICE_ID,
        "scope": ("workorder:handle",),
    }
    kwargs.update(overrides)
    return Identity(**kwargs)  # type: ignore[arg-type]


def test_staff_identity_keeps_only_staff_fields() -> None:
    """员工身份只带 `staff_id` + `role`，其余主体 ID 必须是 None（计划 §5.3.2）。"""
    identity = _staff_identity()
    assert identity.staff_id == STAFF_ID
    assert identity.role == "care_worker"
    assert identity.guardian_id is None
    assert identity.device_id is None
    assert identity.elder_id is None
    assert identity.principal_id == STAFF_ID
    assert identity.is_staff_principal is True


def test_guardian_identity_never_carries_a_staff_role() -> None:
    """家属不是员工角色（契约 §1）：带 `role` 的家属身份是在伪造权限，构造即拒。"""
    assert _guardian_identity().role is None
    for role in ("admin", "care_worker", "doctor"):
        with pytest.raises(IdentityConstructionError):
            _guardian_identity(role=role)


def test_device_identity_may_be_unbound_and_is_not_a_staff_principal() -> None:
    """未绑定长者的设备（先建设备后绑定 / 退房解绑）身份仍然成立（TD：devices.elder_id 可空）。"""
    unbound = _device_identity(elder_id=None)
    assert unbound.device_id == DEVICE_ID
    assert unbound.elder_id is None
    assert unbound.is_staff_principal is False
    assert unbound.principal_id == DEVICE_ID


def test_staff_identity_cannot_carry_elder_id() -> None:
    """闸门③的结构化落点：照护台/家属端身份**不允许**携带 `elder_id`。

    这不是字段洁癖。选哪个长者是请求参数，必须走服务层按绑定表做范围授权
    （计划 §8.2）；一旦身份结构本身容得下 `elder_id`，
    「绕过绑定表直接指定服务对象」就成了一句没有代码保证的话。
    """
    with pytest.raises(IdentityConstructionError):
        _staff_identity(elder_id=ELDER_ID)
    with pytest.raises(IdentityConstructionError):
        _guardian_identity(elder_id=ELDER_ID)


def test_device_bound_elder_id_must_be_a_uuid() -> None:
    """设备身份可以带服务端解析出的 `elder_id`，但它必须是合法 UUID。

    带设备上下文的身份是 `elder_id` 唯一的合法载体（契约 §5.3：绑定关系读自 `devices`），
    所以这里的宽松必须配上「文本合法」这条硬约束——脏值会一路传到记忆检索的过滤条件里。
    """
    assert _device_identity(elder_id=ELDER_ID.upper()).elder_id == ELDER_ID
    for bad in ("", "1", "not-a-uuid", ELDER_ID[:8]):
        with pytest.raises(IdentityConstructionError):
            _device_identity(elder_id=bad)


def test_identity_rejects_cross_filled_principal_ids() -> None:
    """`kind` 与主体 ID 必须自洽：设备身份塞 `staff_id` 就是把两种凭证混成一个主体。"""
    with pytest.raises(IdentityConstructionError):
        _device_identity(staff_id=STAFF_ID)
    with pytest.raises(IdentityConstructionError):
        _staff_identity(guardian_id=GUARDIAN_ID)
    with pytest.raises(IdentityConstructionError):
        _staff_identity(device_id=DEVICE_ID)


@pytest.mark.parametrize("kind", ["human", "super_user", "", "Staff"])
def test_identity_rejects_unknown_kind(kind: str) -> None:
    """`kind` 只认契约 §5.1 的四种，大小写敏感。

    放过一个「看着像」的写法（`Staff`、`human`）等于承认第五种主体，
    下游每个按 kind 分派的分支都得重新数一遍。
    """
    with pytest.raises(IdentityConstructionError):
        Identity(kind=kind, staff_id=STAFF_ID, role="admin")


def test_identity_rejects_non_uuid_principal_id() -> None:
    for bad in ("", "1", "not-a-uuid", "abc"):
        with pytest.raises(IdentityConstructionError):
            _staff_identity(staff_id=bad)


def test_identity_requires_role_for_staff() -> None:
    """员工身份缺 `role` 直接拒：缺角色就没法做 `require_roles`，静默当「无角色」等于放行。"""
    with pytest.raises(IdentityConstructionError):
        _staff_identity(role=None)
    with pytest.raises(IdentityConstructionError):
        _staff_identity(role="super_user")


def test_identity_normalises_uuid_objects_and_uppercase_text() -> None:
    """ORM 给的 `UUID` 对象与手写的大写文本，落到身份里必须是同一种小写文本。

    否则同一个人/设备在库里出现两种主键文本，回查与审计都会「查不到这条记录」。
    """
    assert LETTERED_ID.upper() != LETTERED_ID, "用例常量变了，这条会退化成空测试"
    from_text = _staff_identity(staff_id=LETTERED_ID.upper())
    from_object = _staff_identity(staff_id=uuid.UUID(LETTERED_ID))
    assert from_text.staff_id == from_object.staff_id == LETTERED_ID


def test_empty_string_is_not_accepted_as_absent() -> None:
    """不该出现的字段只认 `None`：`""` 会让「这个主体没有设备」和「设备 ID 是空串」分不清。"""
    with pytest.raises(IdentityConstructionError):
        _staff_identity(device_id="")
    with pytest.raises(IdentityConstructionError):
        _guardian_identity(role="")


def test_staff_card_identity_requires_non_empty_scope() -> None:
    """工牌身份必带非空 `scope`：空范围工牌 == 「什么都能干的工牌」，签发侧已拒（S-16）。"""
    with pytest.raises(IdentityConstructionError):
        _card_identity(scope=())
    with pytest.raises(IdentityConstructionError):
        _card_identity(scope=("workorder:handle", "workorder:handle"))
    with pytest.raises(IdentityConstructionError):
        _card_identity(scope=("口令",))
    card = _card_identity(scope=("workorder:handle", "notification:send"))
    assert card.scope == ("workorder:handle", "notification:send")
    assert card.elder_id is None
    assert card.is_staff_principal is True


def test_staff_card_identity_may_carry_device_bound_elder() -> None:
    """工牌是「员工 + 设备」的复合主体：`elder_id` 来自设备绑定，仍由服务端填入。"""
    card = _card_identity(elder_id=ELDER_ID)
    assert (card.staff_id, card.device_id, card.elder_id) == (STAFF_ID, DEVICE_ID, ELDER_ID)
    with pytest.raises(IdentityConstructionError):
        _card_identity(guardian_id=GUARDIAN_ID)


# ------------------------------------------------------------------ 不可变性


def test_identity_is_frozen_and_uses_slots() -> None:
    """冻结 + slots：身份一旦被注入上下文，任何下游代码都改不动它。

    可变的身份等于「工具层可以临时把自己切成别的长者」，那是闸门③的反面。
    """
    identity = _staff_identity()
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.role = "admin"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.elder_id = ELDER_ID  # type: ignore[misc]
    assert not hasattr(identity, "__dict__")


def test_replace_cannot_inject_elder_id() -> None:
    """`dataclasses.replace()` 也绕不过不变式——它同样会重跑 `__post_init__`。

    这条不是重复上一条：frozen 只挡赋值，挡不住「复制一份带 elder_id 的」。
    """
    identity = _staff_identity()
    with pytest.raises(IdentityConstructionError):
        dataclasses.replace(identity, elder_id=ELDER_ID)


def test_as_mapping_has_exactly_the_identity_fields() -> None:
    """审计与响应用的展开：键集合固定为七个，`scope` 出成 list。

    这里只展开不判定，保证「什么算合法身份」的规则只有 `__post_init__` 一处。
    """
    mapping = as_mapping(_card_identity(elder_id=ELDER_ID))
    assert set(mapping) == {
        "kind",
        "staff_id",
        "guardian_id",
        "device_id",
        "elder_id",
        "role",
        "scope",
    }
    assert mapping["scope"] == ["workorder:handle"]


# ------------------------------------------------------------------ contextvar 载体


def test_context_starts_empty_and_require_raises() -> None:
    """无身份是合法初态：读取返回 None，`require_identity()` 抛明确异常。

    绝不能返回一个「空的 Identity」或默认长者 ID —— 那会让漏检变成越权。
    """
    assert get_identity() is None
    with pytest.raises(IdentityNotSetError):
        require_identity()


def test_set_and_reset_round_trip() -> None:
    identity = _staff_identity()
    token = set_identity(identity)
    try:
        assert require_identity() is identity
    finally:
        reset_identity(token)
    assert get_identity() is None


def test_identity_scope_restores_previous_identity() -> None:
    """嵌套作用域退出后回到上一层身份，最外层退出后回到 None。"""
    outer = _staff_identity(role="admin")
    inner = _device_identity()
    with identity_scope(outer):
        assert require_identity() is outer
        with identity_scope(inner):
            assert require_identity() is inner
        assert require_identity() is outer
    assert get_identity() is None


def test_identity_scope_resets_when_body_raises() -> None:
    """异常路径必须照样回滚：uvicorn 复用 worker，残留身份会带进下一个请求。"""
    with pytest.raises(IdentityConstructionError), identity_scope(_staff_identity()):
        raise IdentityConstructionError("boom")
    assert get_identity() is None


def test_set_identity_refuses_a_plain_mapping() -> None:
    """只接受 `Identity` 实例：放过 dict 等于给「模型自报身份」留一条最省事的旁路。

    dict 是最危险的输入形态——客户端与模型的输出天然是 dict，
    一旦 `set_identity` 能吃 dict，就会有人写 `set_identity(request.json())`。
    """
    with pytest.raises(IdentityConstructionError):
        set_identity({"kind": KIND_DEVICE, "elder_id": ELDER_ID})  # type: ignore[arg-type]
    assert get_identity() is None


def test_identity_does_not_leak_between_asyncio_tasks() -> None:
    """并发请求之间不串身份：一个 task 里 `set` 不影响兄弟 task。

    uvicorn 每个请求跑在独立 asyncio Task 里，Task 创建时复制上下文、写回只影响自己；
    若把身份存成模块级全局变量，`sleep` 一让出控制权就会把身份串给别的请求。
    """

    async def bound_request() -> str | None:
        with identity_scope(_device_identity()):
            await asyncio.sleep(0)  # 让出控制权，逼出交错执行
            return require_identity().device_id

    async def unbound_request() -> Identity | None:
        await asyncio.sleep(0)
        return get_identity()

    async def main() -> tuple[object, object]:
        return await asyncio.gather(bound_request(), unbound_request())

    bound, seen = asyncio.run(main())
    assert bound == DEVICE_ID
    assert seen is None


# ==================================================================== T4a · 凭证解析与失效判定


# 库里读出的事实用普通字符串表达：这一层的职责是「拿到事实怎么判」，
# ORM 接线（T4b）只负责把这些事实查出来，判定规则不许再写一遍。
DEVICE_LONG_TOKEN = "unit-test-device-long-token"
DEVICE_TOKEN_HASH = sha256_hex(DEVICE_LONG_TOKEN)


def _access_claims(**overrides: object) -> dict:
    """签一张员工 Access 再解回来，拿到**与真实解码同形**的载荷（list/str/int 都是 JSON 形）。"""
    token = _sign_staff_access(**overrides)
    return decode_token(token, expected_typ=TYP_ACCESS)


def _guardian_claims(**overrides: object) -> dict:
    kwargs: dict[str, object] = {
        "kind": KIND_GUARDIAN,
        "staff_id": GUARDIAN_ID,
        "role": "",
        "token_version": 1,
        "now": _now(),
    }
    kwargs.update(overrides)
    return decode_token(sign_staff_access(**kwargs), expected_typ=TYP_ACCESS)  # type: ignore[arg-type]


def _device_claims(**overrides: object) -> dict:
    kwargs: dict[str, object] = {
        "device_id": DEVICE_ID,
        "token_hash_prefix": DEVICE_TOKEN_HASH[:16],
        "now": _now(),
    }
    kwargs.update(overrides)
    return decode_token(sign_device_session(**kwargs), expected_typ=TYP_DEVICE)  # type: ignore[arg-type]


def _card_claims(**overrides: object) -> dict:
    kwargs: dict[str, object] = {
        "staff_id": STAFF_ID,
        "device_id": DEVICE_ID,
        "scope": ["workorder:handle"],
        "ttl_seconds": 600,
        "now": _now(),
    }
    kwargs.update(overrides)
    return decode_token(sign_staff_card(**kwargs), expected_typ=TYP_STAFF_CARD)  # type: ignore[arg-type]


# ------------------------------------------------------------------ Bearer 头


def test_missing_authorization_header_is_a_credential_missing_error() -> None:
    for header in (None, "", "   "):
        with pytest.raises(CredentialMissingError) as excinfo:
            bearer_token_from_header(header)
        assert excinfo.value.code == TOKEN_INVALID


@pytest.mark.parametrize(
    "header",
    [
        "Basic dXNlcjpwYXNz",  # 第二种凭证通道：放过就等于开了 Basic Auth
        "Token abcdef",
        "Bearer",  # 只有 scheme 没有令牌
        "abcdef",  # 只有令牌没有 scheme
        "Bearer a b",  # 多段：JWT 里不可能有空格
        "Bearer  ",  # 空令牌
        "Bearer 口令",  # 非 ASCII
    ],
)
def test_malformed_authorization_header_is_rejected(header: str) -> None:
    with pytest.raises(CredentialInvalidError):
        bearer_token_from_header(header)


def test_bearer_scheme_is_case_insensitive(jwt_env: Settings) -> None:
    """`bearer` / `BEARER` 必须收：RFC 6750 只规定写法，代理与老客户端大小写不一。

    大小写敏感的后果不是安全问题，是「同一份令牌换个客户端就 401」的难查故障。
    """
    token = _sign_staff_access()
    for prefix in ("Bearer", "bearer", "BEARER"):
        assert bearer_token_from_header(f"{prefix} {token}") == token


def test_rejected_headers_do_not_echo_the_token() -> None:
    """失败信息不回显收到的值：错误 message 会进日志，令牌是敏感材料。"""
    secretish = "Bearer super.secret-looking-token-value"
    with pytest.raises(CredentialInvalidError) as excinfo:
        bearer_token_from_header("Basic " + "super.secret-looking-token-value")
    assert "super.secret-looking-token-value" not in str(excinfo.value)
    del secretish


# ------------------------------------------------------------------ 主体令牌解码


def test_all_three_principal_types_decode_but_refresh_does_not(jwt_env: Settings) -> None:
    """`access` / `device` / `staff_card` 可作为请求主体；`refresh` 一律不行（契约 §5.2 第 2 步）。

    refresh 若被接受，任何拿到 refresh 的客户端都能直接当 Access 打业务接口，
    「Access 2h / Refresh 7d」的有效期分层当场失效。
    """
    assert decode_principal_token(_sign_staff_access())["typ"] == TYP_ACCESS
    assert decode_principal_token(_sign_device())["typ"] == TYP_DEVICE
    assert decode_principal_token(_sign_staff_card())["typ"] == TYP_STAFF_CARD
    refresh = sign_staff_refresh(kind=KIND_STAFF, staff_id=STAFF_ID, token_version=3, now=_now())
    with pytest.raises(CredentialInvalidError):
        decode_principal_token(refresh)


def test_refresh_has_its_own_entry_point(jwt_env: Settings) -> None:
    """task-5 的刷新流程用 `decode_refresh_token`；失败原因照 §7 分类。"""
    refresh = sign_staff_refresh(kind=KIND_STAFF, staff_id=STAFF_ID, token_version=3, now=_now())
    assert decode_refresh_token(refresh)["typ"] == TYP_REFRESH
    with pytest.raises(CredentialInvalidError):
        decode_refresh_token(_sign_staff_access())
    expired = sign_staff_refresh(
        kind=KIND_STAFF, staff_id=STAFF_ID, token_version=3, now=_now() - timedelta(days=30)
    )
    with pytest.raises(CredentialExpiredError):
        decode_refresh_token(expired)


def test_forged_signature_is_rejected_without_leaking(jwt_env: Settings) -> None:
    """换密钥签一张：三类主体全试一遍都过不了，且不把原始令牌写进 message。"""
    forged = sign_staff_access(
        kind=KIND_STAFF, staff_id=STAFF_ID, role="admin", token_version=3, now=_now()
    )
    import jwt as pyjwt

    wrong_key = pyjwt.encode(
        {
            "sub": f"staff:{STAFF_ID}",
            "typ": TYP_ACCESS,
            "iat": int(_now().timestamp()),
            "exp": int((_now() + timedelta(hours=1)).timestamp()),
        },
        "attacker-key-that-is-long-enough-32bytes!",
        algorithm="HS256",
    )
    del forged
    with pytest.raises(CredentialInvalidError) as excinfo:
        decode_principal_token(wrong_key)
    assert wrong_key[:20] not in str(excinfo.value)
    assert excinfo.value.code == TOKEN_INVALID


def test_expired_principal_token_maps_to_token_expired_code(jwt_env: Settings) -> None:
    """过期与「签名不合法」给不同的码：客户端才知道该刷新而不是重新登录。"""
    token = _sign_staff_access(now=_now() - timedelta(hours=3))
    with pytest.raises(CredentialExpiredError) as excinfo:
        decode_principal_token(token)
    assert excinfo.value.code == TOKEN_EXPIRED


# ------------------------------------------------------------------ 员工 / 家属身份


def test_staff_identity_uses_the_database_role(jwt_env: Settings) -> None:
    """角色以库为准（计划 §5.4.3）：令牌里写着旧角色也不能继续生效。

    这里 `db_role` 与载荷一致才放行；不一致的分支由下一条覆盖。
    """
    claims = _access_claims(role="care_worker", token_version=3)
    identity = staff_identity_from_access(
        claims, db_role="care_worker", db_status="active", db_token_version=3
    )
    assert (identity.kind, identity.staff_id, identity.role) == (
        KIND_STAFF,
        STAFF_ID,
        "care_worker",
    )
    assert identity.elder_id is None


def test_staff_role_drift_is_revoked(jwt_env: Settings) -> None:
    """库里角色变了但版本号没提 → 判撤销，不判「继续用旧角色」。

    按 O10，改 `role` 必须同时 `token_version + 1`；对不上说明撤销漏做或库被手改。
    放行的后果是「降职的医生仍然按 doctor 拿数据」，属于权限残留。
    """
    claims = _access_claims(role="doctor", token_version=3)
    with pytest.raises(CredentialRevokedError):
        staff_identity_from_access(
            claims, db_role="care_worker", db_status="active", db_token_version=3
        )


@pytest.mark.parametrize(
    "db_status,db_token_version",
    [("disabled", 3), ("active", 4), ("disabled", 4)],
)
def test_disabled_account_and_version_drift_are_revoked(
    jwt_env: Settings, db_status: str, db_token_version: int
) -> None:
    """停用或 `token_version` 不等 → `TOKEN_REVOKED`（FR-ADM-04、AC-24）。"""
    claims = _access_claims(token_version=3)
    with pytest.raises(CredentialRevokedError) as excinfo:
        staff_identity_from_access(
            claims, db_role="care_worker", db_status=db_status, db_token_version=db_token_version
        )
    assert excinfo.value.code == TOKEN_REVOKED


def test_database_role_out_of_range_never_reaches_identity(jwt_env: Settings) -> None:
    """库里 `role` 是越界值时，绝不会被打进身份——两条路都堵着。

    载荷角色受 S-16 白名单约束（越界值在签发时就拒签），所以库里一个 `super_user`
    必然与载荷不等，先命中 drift 判定按 `TOKEN_REVOKED` 拒。
    考点是「在库里改角色却不动版本号」这条悄悄提权的路：它得到的是 401，不是新身份。
    """
    claims = _access_claims(role="care_worker", token_version=1)
    with pytest.raises(CredentialRevokedError):
        staff_identity_from_access(
            claims, db_role="super_user", db_status="active", db_token_version=1
        )


def test_wrong_builder_for_wrong_token_type_is_rejected(jwt_env: Settings) -> None:
    """把 device 载荷交给员工构造函数 → 当场拒，而不是产出一个错身份。"""
    with pytest.raises(CredentialInvalidError):
        staff_identity_from_access(
            _device_claims(), db_role="admin", db_status="active", db_token_version=1
        )


def test_guardian_identity_has_no_role_and_no_elder(jwt_env: Settings) -> None:
    """家属身份：无角色、无 `elder_id`；能看哪位长者由服务层查 `elder_guardians`。"""
    claims = _guardian_claims(token_version=2)
    identity = guardian_identity_from_access(claims, db_status="active", db_token_version=2)
    assert identity.kind == KIND_GUARDIAN
    assert identity.guardian_id == GUARDIAN_ID
    assert identity.role is None
    assert identity.elder_id is None
    with pytest.raises(CredentialRevokedError):
        guardian_identity_from_access(claims, db_status="disabled", db_token_version=2)


# ------------------------------------------------------------------ 设备会话


def test_device_identity_takes_elder_from_the_database(jwt_env: Settings) -> None:
    """设备身份的 `elder_id` 只来自库（契约 §5.3）；未绑定设备得到 None。"""
    claims = _device_claims()
    bound = device_identity_from_session(
        claims,
        db_device_id=DEVICE_ID,
        db_token_hash=DEVICE_TOKEN_HASH,
        db_status="online",
        db_elder_id=ELDER_ID,
    )
    assert (bound.kind, bound.device_id, bound.elder_id) == (KIND_DEVICE, DEVICE_ID, ELDER_ID)
    unbound = device_identity_from_session(
        claims,
        db_device_id=uuid.UUID(DEVICE_ID),
        db_token_hash=DEVICE_TOKEN_HASH,
        db_status="offline",
    )
    assert unbound.elder_id is None


def test_unbound_device_session_is_revoked(jwt_env: Settings) -> None:
    """解绑轮换 `token_hash` → 旧会话 `tv` 对不上 → 401（AC-24，不需要黑名单）。"""
    claims = _device_claims()
    with pytest.raises(CredentialRevokedError) as excinfo:
        device_identity_from_session(
            claims,
            db_device_id=DEVICE_ID,
            db_token_hash=sha256_hex("rotated-long-token"),
            db_status="online",
            db_elder_id=ELDER_ID,
        )
    assert excinfo.value.code == TOKEN_REVOKED


def test_offline_device_is_not_treated_as_revoked(jwt_env: Settings) -> None:
    """`offline` 是心跳问题（FR-ADM-10），不是凭证问题：判 401 会让平板在抖动时被踢出登录。"""
    claims = _device_claims()
    identity = device_identity_from_session(
        claims, db_device_id=DEVICE_ID, db_token_hash=DEVICE_TOKEN_HASH, db_status="offline"
    )
    assert identity.device_id == DEVICE_ID
    with pytest.raises(CredentialRevokedError):
        device_identity_from_session(
            claims, db_device_id=DEVICE_ID, db_token_hash=DEVICE_TOKEN_HASH, db_status="locked"
        )


def test_device_id_mismatch_is_revoked(jwt_env: Settings) -> None:
    """载荷 `dev` 与查出来的行不是同一台设备 → 拒。这是给 T4b 接线写错的兜底。"""
    claims = _device_claims()
    with pytest.raises(CredentialRevokedError):
        device_identity_from_session(
            claims,
            db_device_id=LETTERED_ID,
            db_token_hash=DEVICE_TOKEN_HASH,
            db_status="online",
        )


def test_hash_prefix_compare_is_case_insensitive_and_constant_time(jwt_env: Settings) -> None:
    """库里哈希文本若写成大写，不该被解释成「已解绑」。"""
    claims = _device_claims()
    assert device_hash_prefix_matches(claims, db_token_hash=DEVICE_TOKEN_HASH.upper()) is True
    assert device_hash_prefix_matches(claims, db_token_hash=DEVICE_TOKEN_HASH) is True
    assert device_hash_prefix_matches(claims, db_token_hash="short") is False
    assert device_hash_prefix_matches({"tv": 1}, db_token_hash=DEVICE_TOKEN_HASH) is False


# ------------------------------------------------------------------ 工牌


def test_staff_card_lookup_key_is_a_hash_not_the_raw_token(jwt_env: Settings) -> None:
    """回查用 SHA-256 当主键（R9）：库里绝不存原始令牌。"""
    token = _sign_staff_card()
    key = staff_card_lookup_key(token)
    assert key == sha256_hex(token)
    assert len(key) == 64
    assert token not in key


def test_staff_card_identity_carries_scope_and_bound_elder(jwt_env: Settings) -> None:
    claims = _card_claims(scope=["workorder:handle", "health:staff_form"])
    expires = _now() + timedelta(minutes=10)
    identity = staff_card_identity_from_card(
        claims,
        db_staff_id=STAFF_ID,
        db_device_id=uuid.UUID(DEVICE_ID),
        db_staff_status="active",
        db_expires_at=expires,
        db_elder_id=ELDER_ID,
    )
    assert identity.kind == KIND_STAFF_CARD
    assert identity.scope == ("workorder:handle", "health:staff_form")
    assert identity.elder_id == ELDER_ID
    assert identity.role is None


def test_staff_card_expiry_and_account_status(jwt_env: Settings) -> None:
    """到期、停用、naive 时间三种情况一律拒；时间必须带时区（AGENTS §8）。"""
    claims = _card_claims()
    with pytest.raises(CredentialRevokedError):
        staff_card_identity_from_card(
            claims,
            db_staff_id=STAFF_ID,
            db_device_id=DEVICE_ID,
            db_staff_status="active",
            db_expires_at=_now() - timedelta(seconds=1),
        )
    with pytest.raises(CredentialRevokedError):
        staff_card_identity_from_card(
            claims,
            db_staff_id=STAFF_ID,
            db_device_id=DEVICE_ID,
            db_staff_status="active",
            db_expires_at=datetime(2030, 1, 1),  # naive：按本地时区解释会把有效期推偏
        )
    with pytest.raises(CredentialRevokedError):
        staff_card_identity_from_card(
            claims,
            db_staff_id=STAFF_ID,
            db_device_id=DEVICE_ID,
            db_staff_status="disabled",
            db_expires_at=_now() + timedelta(minutes=5),
        )


def test_staff_card_subject_mismatch_is_revoked(jwt_env: Settings) -> None:
    """工牌行的 staff/device 与载荷不符 → 拒。

    令牌哈希当主键查，理论上不会拿到别人的行；这条防的是「库里把令牌挂错了人」，
    那种情况下放行等于让 A 的工牌拿到 B 的身份。
    """
    claims = _card_claims()
    expires = _now() + timedelta(minutes=5)
    with pytest.raises(CredentialRevokedError):
        staff_card_identity_from_card(
            claims,
            db_staff_id=LETTERED_ID,
            db_device_id=DEVICE_ID,
            db_staff_status="active",
            db_expires_at=expires,
        )
    with pytest.raises(CredentialRevokedError):
        staff_card_identity_from_card(
            claims,
            db_staff_id=STAFF_ID,
            db_device_id=LETTERED_ID,
            db_staff_status="active",
            db_expires_at=expires,
        )


# ------------------------------------------------------------------ 角色判定


def test_check_roles_accepts_only_staff_sessions(jwt_env: Settings) -> None:
    """角色判定只认完整员工会话；家属、设备、工牌一律 403。

    工牌那一条最容易被写错：工牌也是员工（`is_staff_principal` 为真），
    但它的授权来自 `scope` 而不是 `role`，载荷里根本没有 `role`（S-16 白名单）。
    若这里放过工牌，刷一张卡就等于拿到该员工的全部角色权限。
    """
    admin = staff_identity_from_access(
        _access_claims(role="admin", token_version=1),
        db_role="admin",
        db_status="active",
        db_token_version=1,
    )
    check_roles(admin, ("admin", "doctor"))
    with pytest.raises(RoleForbiddenError) as excinfo:
        check_roles(admin, ("doctor",))
    assert excinfo.value.code == ROLE_FORBIDDEN

    others = [
        guardian_identity_from_access(
            _guardian_claims(token_version=1), db_status="active", db_token_version=1
        ),
        device_identity_from_session(
            _device_claims(),
            db_device_id=DEVICE_ID,
            db_token_hash=DEVICE_TOKEN_HASH,
            db_status="online",
        ),
        staff_card_identity_from_card(
            _card_claims(),
            db_staff_id=STAFF_ID,
            db_device_id=DEVICE_ID,
            db_staff_status="active",
            db_expires_at=_now() + timedelta(minutes=5),
        ),
    ]
    for identity in others:
        with pytest.raises(RoleForbiddenError):
            check_roles(identity, ("admin", "care_worker", "doctor"))
