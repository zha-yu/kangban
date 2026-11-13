"""集中脱敏工具（Day 9 A1b，AGENTS §8「脱敏工具集中放 app/core/」）。

为什么集中：Day 8 收口时脱敏散落 4 处（auth_service._mask_phone /
staff_service.mask_phone / device_service._mask_display_name /
decision_log_service.redact_input），同一类 PII 有两套掩码口径，
日志 formatter 接不进去。本模块是**唯一实现**：

- 字段级（已知这是手机号/姓名）：`mask_phone` / `mask_name` / `mask_secret`；
- 文本级（自由文本里捞 PII）：`redact_text`（原 decision_log_service 正则引擎，原样迁入）；
- 结构级（日志 payload 深遍历）：`redact_obj`（logging formatter 用）。

掩码口径（唯一，不许再长出第二套）：

| 对象 | 输入 | 输出 |
| --- | --- | --- |
| 手机号 | `13800000001` | `138****0001`（auth_service 日志原为 `****0001`，统一为本格式，R-A1） |
| 姓名 | `张桂芬` | `张*芬`（裁决 S-06） |
| 文本内手机号 | `我手机号是13812345678` | `我手机号是138****5678` |
| 文本内身份证 | `110101199003071234` | `110101********1234` |
| 文本内房间/床位 | `302房` | `***房` |
| 密钥/令牌 | 任意值 | `***`（值不留任何位） |

`trace_id` 不在本模块的任何处理范围内——调用方（logging formatter）不得
把 trace_id 传进来，这是任务书红线。
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# 文本级正则（原样迁自 decision_log_service.py:39-44，语义一字不改）
# ---------------------------------------------------------------------------

#: 手机号：11 位，1 开头，第二位 3–9（保留前 3 后 4）
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")
#: 身份证：18 位（末位可为 X）或 15 位（保留前 6 后 4）
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{6})(\d{8})(\d{3}[\dXx])(?!\d)")
#: 房间/床位号：`302房`、`A栋301`、`12床`
_ROOM_RE = re.compile(r"([A-Za-z]?\d{1,4})\s*(房|房间|室|床|栋)")
#: 密钥/API Key（in-text 捞取）：sk- 前缀或Bearer 令牌，值 ≥12 位
_SECRET_RE = re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{12,}|bearer\s+[A-Za-z0-9._\-]{16,})\b")

#: extra 字段名命中即整值抹掉（值不留位）——字段级密钥防线。
#:
#: `token(?!s)`：放行 `*_tokens` **计数字段**（prompt_tokens / completion_tokens /
#: cache_hit_tokens / cache_miss_tokens / total_tokens 等 LLM usage 计数）。
#: 2026-11-13 B2 联调实测 bug：子串匹配曾把这类计数字段整值掩成 `***`（0 值变空串），
#: 波及日志轨（logging.py formatter）与 OTel span 属性（observability._scalarize）两轨。
#: 真令牌键（access_token / refresh_token / device_token / api_token 等，`token` 后
#: 不跟 `s`）仍整值掩码，防线不弱化。dev-A 写域，经主理人 2026-11-13 授权越域修复。
_SECRET_KEY_RE = re.compile(
    r"(?i)(secret|token(?!s)|password|passwd|api[_-]?key|authorization|credential)"
)


# ---------------------------------------------------------------------------
# 字段级
# ---------------------------------------------------------------------------


def mask_phone(phone: str | None) -> str:
    """手机号脱敏：`13800000001` → `138****0001`。空值返回空串。"""
    if not phone:
        return ""
    if len(phone) < 7:  # pragma: no cover - 入库前已校验 11 位
        return "*" * len(phone)
    return f"{phone[:3]}****{phone[-4:]}"


def mask_name(name: str) -> str:
    """候选姓名脱敏：姓 + `*` + 末字（裁决 S-06，例 张桂芬 → 张*芬）。"""
    if len(name) <= 1:
        return name
    return f"{name[0]}*{name[-1]}"


def mask_secret(value: Any) -> str:
    """密钥/令牌脱敏：值不留任何位，恒 `***`（与手机号留尾不同，密钥无安全尾位）。"""
    return "***" if value else ""


# ---------------------------------------------------------------------------
# 文本级
# ---------------------------------------------------------------------------


def redact_text(text: str | None) -> str:
    """自由文本 PII 捞取：身份证/手机号/房间号/明文密钥（保留可读性，便于复核 bad case）。"""
    if not text:
        return ""
    redacted = _ID_CARD_RE.sub(r"\1********\3", text)
    redacted = _PHONE_RE.sub(r"\1****\3", redacted)
    redacted = _SECRET_RE.sub("***", redacted)
    return _ROOM_RE.sub(lambda m: f"***{m.group(2)}", redacted)


# ---------------------------------------------------------------------------
# 结构级（logging formatter / JSON payload 用）
# ---------------------------------------------------------------------------


def redact_obj(value: Any) -> Any:
    """深遍历任意 JSON 形值并脱敏。

    规则：
    - `str` → `redact_text`；
    - dict → 键名命中 `_SECRET_KEY_RE` 时整值 `mask_secret`，否则递归值；
    - list/tuple → 逐项递归；
    - 其余（int/float/bool/None）原样——数字与布尔不构成明文 PII。
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: mask_secret(v)
            if isinstance(key, str) and _SECRET_KEY_RE.search(key)
            else redact_obj(v)
            for key, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_obj(item) for item in value]
    return value


__all__ = [
    "mask_name",
    "mask_phone",
    "mask_secret",
    "redact_obj",
    "redact_text",
]
