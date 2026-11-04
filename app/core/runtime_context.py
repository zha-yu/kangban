"""服务端构造的身份上下文 —— 三道隔离闸门的第③道（PRD FR-ADM-06 ③）。

口径来源（逐条可回查）：

- 技术设计 §1.3：`app/core` 的产出是「鉴权主体、上下文」；
- 契约 §5.2 第 7 步：验签 + 回查库**都通过**之后才组装主体并注入上下文；
- PRD FR-ADM-06 ③：工具只从 runtime_context 读身份，不接受模型自行传入的 `elder_id`；
- AGENTS §4.9：`elder_id` / `thread_id` 只能由服务端注入，客户端与模型传入的值一律丢弃。

因此本模块只有两类东西：一个**构造时就自证合法**的 `Identity`，和一个**只能由
`auth.py` 写入**的 contextvar。这里故意不提供任何「按 elder_id 查身份」「从请求体
取身份」的入口 —— 少一个口子，就少一条能让模型自报身份的旁路。

`canonical_uuid` / `STAFF_ROLES` / `normalize_scope` 直接复用 `app.core.security`：
令牌校验与身份构造必须同源，两份实现迟早分叉（一边收大写 UUID、一边不收，
表现就是「令牌明明有效却持续 401」）。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass

from app.core.security import (
    KIND_DEVICE,
    KIND_GUARDIAN,
    KIND_STAFF,
    KIND_STAFF_CARD,
    STAFF_ROLES,
    TokenPayloadError,
    canonical_uuid,
    normalize_scope,
)

# ------------------------------------------------------------------ 异常体系


class RuntimeContextError(ValueError):
    """身份上下文相关异常的基类（属数据/编程错误，不是「密码错」那一类）。"""


class IdentityConstructionError(RuntimeContextError):
    """构造出的 `Identity` 自相矛盾：主体 ID 与 `kind` 不匹配、缺字段、UUID 非法。

    这类异常只在**服务端代码写错**时出现，绝不该由外部输入触发到 500：
    `auth.py` 必须先把解码结果按 `typ` 分派、回查过库，再来构造身份。
    """


class IdentityNotSetError(RuntimeContextError):
    """当前上下文里没有身份，却有人想读身份。

    这是闸门③的正向表现：无身份 == 无权限。工具层拿到它应当**拒绝执行并记审计**，
    绝不允许「读不到就退回用模型传来的 elder_id」—— 那等于把闸门③拆了。
    """


# ------------------------------------------------------------------ Identity


# `kind` 取值与令牌 `kind`（契约 §5.1）同名同义，不再发明第五种写法。
IDENTITY_KINDS: frozenset[str] = frozenset(
    {KIND_STAFF, KIND_GUARDIAN, KIND_DEVICE, KIND_STAFF_CARD}
)


def _canon(value: object, field_name: str) -> str:
    """必填主体 ID → 规范小写文本；不是 UUID 就报构造错误。

    允许传 `UUID` 或文本：`auth.py` 从 ORM 拿到的是 `UUID`，测试里手写的是文本，
    两种都是正当来源；但存进身份后只有一种文本形式，日志与回查不会再出现两种写法。
    """
    try:
        return canonical_uuid(value)
    except TokenPayloadError:
        raise IdentityConstructionError(f"{field_name} must be a UUID") from None


def _canon_optional(value: object | None, field_name: str) -> str | None:
    return None if value is None else _canon(value, field_name)


def _must_be_absent(value: object, field_name: str, kind: str) -> None:
    """`kind` 不该出现的字段必须真是 `None`，不许用空串/空元组「近似空值」蒙过去。"""
    if value is not None:
        raise IdentityConstructionError(f"{field_name} must be None when kind={kind!r}")


def _must_be_present(value: object | None, field_name: str, kind: str) -> str:
    if value is None:
        raise IdentityConstructionError(f"{field_name} is required when kind={kind!r}")
    return _canon(value, field_name)


@dataclass(frozen=True, slots=True)
class Identity:
    """一次请求的服务端身份。**构造即合法**，之后不可变。

    字段口径（计划 §5.3.1/§5.3.2）：只保留当前主体相关 ID，其余为 `None`。
    这条不变式由 `__post_init__` 强制，不是靠调用方自觉 —— 因为 `dataclasses.replace()`
    会重跑 `__post_init__`，想「复制一个身份顺手塞 elder_id」同样会被拒。

    各 `kind` 允许的字段：

    | kind | 必填 | 可填 | 一律为 None |
    | --- | --- | --- | --- |
    | `staff` | `staff_id`、`role` | — | `guardian_id`/`device_id`/`elder_id`/`scope` |
    | `guardian` | `guardian_id` | — | `staff_id`/`device_id`/`elder_id`/`role`/`scope` |
    | `device` | `device_id` | `elder_id`（未绑定则 None） | `staff_id`/`guardian_id`/`role`/`scope` |
    | `staff_card` | `staff_id`、`device_id`、非空 `scope` | `elder_id`（设备绑定的长者） | `guardian_id`/`role` |

    `elder_id` 为什么只允许挂在带设备上下文的两类上：长者端与工牌场景里，
    设备与长者的绑定关系是**服务端从 `devices` 表读出来的**（契约 §5.3）；
    照护台/家属端选哪个长者是**请求参数**，要走服务层的范围授权（计划 §8.2：
    范围过滤不在鉴权内核里）。若允许 `kind=staff` 直接携带 `elder_id`，
    就等于在身份结构上开了一个「绕过绑定表直接指定服务对象」的口子。
    """

    kind: str
    staff_id: str | None = None
    guardian_id: str | None = None
    device_id: str | None = None
    elder_id: str | None = None
    role: str | None = None
    scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in IDENTITY_KINDS:
            raise IdentityConstructionError(
                f"unknown identity kind {self.kind!r}; expected one of {sorted(IDENTITY_KINDS)}"
            )
        # frozen dataclass 里改字段只能走 object.__setattr__：这是「把调用方传进来的
        # UUID 对象规范成文本」的唯一办法，且只在本函数里用一次，不构成可变身份。
        write = object.__setattr__

        write(self, "elder_id", _canon_optional(self.elder_id, "elder_id"))

        if self.kind == KIND_STAFF:
            write(self, "staff_id", _must_be_present(self.staff_id, "staff_id", self.kind))
            write(self, "role", _require_role(self.role, self.kind))
            _must_be_absent(self.guardian_id, "guardian_id", self.kind)
            _must_be_absent(self.device_id, "device_id", self.kind)
            _must_be_absent(self.elder_id, "elder_id", self.kind)
            _must_be_absent(self.scope or None, "scope", self.kind)
        elif self.kind == KIND_GUARDIAN:
            write(
                self,
                "guardian_id",
                _must_be_present(self.guardian_id, "guardian_id", self.kind),
            )
            # 家属不是员工角色（契约 §1）：带 role 的家属身份是在伪造权限。
            _must_be_absent(self.staff_id, "staff_id", self.kind)
            _must_be_absent(self.device_id, "device_id", self.kind)
            _must_be_absent(self.elder_id, "elder_id", self.kind)
            _must_be_absent(self.role, "role", self.kind)
            _must_be_absent(self.scope or None, "scope", self.kind)
        elif self.kind == KIND_DEVICE:
            write(self, "device_id", _must_be_present(self.device_id, "device_id", self.kind))
            _must_be_absent(self.staff_id, "staff_id", self.kind)
            _must_be_absent(self.guardian_id, "guardian_id", self.kind)
            _must_be_absent(self.role, "role", self.kind)
            _must_be_absent(self.scope or None, "scope", self.kind)
        else:
            write(self, "staff_id", _must_be_present(self.staff_id, "staff_id", self.kind))
            write(self, "device_id", _must_be_present(self.device_id, "device_id", self.kind))
            _must_be_absent(self.guardian_id, "guardian_id", self.kind)
            _must_be_absent(self.role, "role", self.kind)
            # 工牌必带非空 scope：空范围的令牌「看不出能干什么」，签发侧已拒（S-16），
            # 身份侧再放行就等于承认存在「什么都能干的工牌」。
            if not self.scope:
                raise IdentityConstructionError("staff_card identity requires a non-empty scope")
            try:
                write(self, "scope", normalize_scope(self.scope))
            except TokenPayloadError:
                # 换成身份侧的异常类型：调用方不必知道 scope 规则其实来自令牌模块。
                raise IdentityConstructionError("staff_card scope is invalid") from None

    @property
    def principal_id(self) -> str:
        """当前主体的唯一 ID（`staff`/`staff_card` 取 `staff_id`）。

        审计日志与「查得到人」的判定只需要这一个值，给调用方少一处
        `if kind == ... : 取某字段` 的分支 —— 分支越少，漏一种 kind 的概率越低。
        """
        if self.kind in (KIND_STAFF, KIND_STAFF_CARD):
            return _assert_not_none(self.staff_id, "staff_id")
        if self.kind == KIND_GUARDIAN:
            return _assert_not_none(self.guardian_id, "guardian_id")
        return _assert_not_none(self.device_id, "device_id")

    @property
    def is_staff_principal(self) -> bool:
        """能否按员工身份走角色/权限判定（工牌也是员工，但范围受限）。"""
        return self.kind in (KIND_STAFF, KIND_STAFF_CARD)


def _require_role(role: str | None, kind: str) -> str:
    if role is None:
        raise IdentityConstructionError(f"role is required when kind={kind!r}")
    if role not in STAFF_ROLES:
        raise IdentityConstructionError("role must be one of admin/care_worker/doctor")
    return role


def _assert_not_none(value: str | None, field_name: str) -> str:
    """`principal_id` 的兜底断言：`__post_init__` 已保证非 None，走到这里说明代码写坏了。"""
    if value is None:
        raise IdentityConstructionError(f"{field_name} is missing; identity is malformed")
    return value


# ------------------------------------------------------------------ 上下文载体

# 默认 None：「没有身份」是本模块的合法初态，也是闸门判定要的语义。
# 不用 `...` 之类的哨兵值 —— 那会让每个读取方都得记一套「什么算空」。
_identity_ctx: ContextVar[Identity | None] = ContextVar("silvermate_identity", default=None)


def set_identity(identity: Identity) -> Token[Identity | None]:
    """写入当前上下文，返回用于精确回滚的 token。

    只允许 `auth.py`（以及测试）调用。这里做 `isinstance` 硬拦：如果放过一个
    `dict`，读取方拿到的就不是带校验的 `Identity`，闸门③当场名存实亡。
    """
    if not isinstance(identity, Identity):
        raise IdentityConstructionError("identity must be an Identity instance")
    return _identity_ctx.set(identity)


def get_identity() -> Identity | None:
    """读当前身份；无身份返回 `None`，不抛异常。

    给「可以匿名走」的分支用（如 E1 登录前的埋点）。需要身份的代码请用
    `require_identity()`，别在这里自己写 `if x is None: 用请求体里的值`。
    """
    return _identity_ctx.get()


def require_identity() -> Identity:
    """读当前身份，无身份即抛 `IdentityNotSetError`。

    工具层与 Agent 只应使用这个函数取 `elder_id`（PRD FR-ADM-06 ③）。
    """
    identity = _identity_ctx.get()
    if identity is None:
        raise IdentityNotSetError("no identity in runtime context")
    return identity


def reset_identity(token: Token[Identity | None]) -> None:
    """按 token 精确回滚到写入前的状态。"""
    _identity_ctx.reset(token)


@contextmanager
def identity_scope(identity: Identity) -> Generator[Identity, None, None]:
    """在块内绑定身份，退出（含异常）必然回滚。

    为什么必须是 `try/finally`：uvicorn 复用 worker 进程，同一条上下文里残留的上一请求
    身份会让下一个请求「带着别人的 elder_id」跑完全程 —— 这类串号在测试里最难复现，
    因为单请求用例永远看不到上一个请求留下的东西。
    """
    token = set_identity(identity)
    try:
        yield identity
    finally:
        reset_identity(token)


def as_mapping(identity: Identity) -> dict[str, object]:
    """身份 → 纯数据映射，只用于审计日志与测试断言。

    只做展开，不加任何判定：一旦这里出现「按 kind 补默认值」，同一个规则就有两份口径。
    """
    return {
        "kind": identity.kind,
        "staff_id": identity.staff_id,
        "guardian_id": identity.guardian_id,
        "device_id": identity.device_id,
        "elder_id": identity.elder_id,
        "role": identity.role,
        "scope": list(identity.scope),
    }


__all__ = [
    "IDENTITY_KINDS",
    "Identity",
    "IdentityConstructionError",
    "IdentityNotSetError",
    "RuntimeContextError",
    "as_mapping",
    "get_identity",
    "identity_scope",
    "require_identity",
    "reset_identity",
    "set_identity",
]
