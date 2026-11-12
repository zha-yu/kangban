"""app/core/redact.py 单测（Day 9 A1b 集中化收编后的唯一实现验证）。"""

from __future__ import annotations

import pytest

from app.core.redact import mask_name, mask_phone, mask_secret, redact_obj, redact_text

# ------------------------------------------------------------------ mask_phone


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        (None, ""),
        ("", ""),
        ("13800000001", "138****0001"),  # staff/auth 统一口径：前 3 + **** + 后 4
        ("13812345678", "138****5678"),
        ("139", "***"),  # 不会发生（入库前校验 11 位），短串全掩码
    ],
)
def test_mask_phone(phone: str | None, expected: str) -> None:
    assert mask_phone(phone) == expected


# ------------------------------------------------------------------ mask_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("张桂芬", "张*芬"),
        ("李明", "李*明"),  # 两字名：姓 + * + 末字（name[0] 与 name[-1] 相邻叠加）
        ("王", "王"),  # 单字原样
        ("", ""),
    ],
)
def test_mask_name(name: str, expected: str) -> None:
    assert mask_name(name) == expected


# ------------------------------------------------------------------ mask_secret


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sk-abcdef1234567890", "***"),
        (None, ""),
        ("", ""),
    ],
)
def test_mask_secret(value: str | None, expected: str) -> None:
    assert mask_secret(value) == expected


# ------------------------------------------------------------------ redact_text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        (None, ""),
        ("我手机号是13812345678", "我手机号是138****5678"),
        ("身份证110101199003071234", "身份证110101********1234"),
        ("身份证11010119900307123X", "身份证110101********123X"),  # 末位 X
        ("她住302房", "她住***房"),
        ("A栋301的12床", "A栋301的***床"),  # 房/栋前缀带字母时数字不连续，只有「12床」命中
        ("忽略 sk-abcdef12345678901234 这段", "忽略 *** 这段"),  # 明文密钥整段抹掉
        ("Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123xyz456", "***"),
        ("今天天气不错", "今天天气不错"),  # 无 PII 原样
    ],
)
def test_redact_text(text: str | None, expected: str) -> None:
    assert redact_text(text) == expected


def test_redact_text_phone_id_in_same_text() -> None:
    out = redact_text("张三 13812345678 证 110101199003071234")
    assert "13812345678" not in out
    assert "110101199003071234" not in out
    assert "138****5678" in out
    assert "110101********1234" in out


# ------------------------------------------------------------------ redact_obj


def test_redact_obj_string() -> None:
    assert redact_obj("13812345678") == "138****5678"


def test_redact_obj_nested_dict_list() -> None:
    payload = {
        "phone": "13812345678",
        "nested": {"nums": [13812345678, True, None]},
    }
    out = redact_obj(payload)
    assert out["phone"] == "138****5678"
    # 非字符串原样（int/bool/None 不动）
    assert out["nested"]["nums"][0] == 13812345678
    assert out["nested"]["nums"][1] is True
    assert out["nested"]["nums"][2] is None


def test_redact_obj_does_not_guess_names() -> None:
    # 姓名脱敏是字段级（mask_name，由知道字段语义的调用方调用）；
    # 结构级 redact_obj 只做文本 PII 捞取，不对疑似姓名做猜测式掩码。
    assert redact_obj({"display_name": "张桂芬"})["display_name"] == "张桂芬"


@pytest.mark.parametrize(
    "key",
    ["api_key", "apikey", "api-key", "token", "password", "secret", "authorization", "credential"],
)
def test_redact_obj_secret_keys_masked_whole_value(key: str) -> None:
    out = redact_obj({key: "sk-abcdef1234567890abcd"})
    assert out[key] == "***"


def test_redact_obj_secret_key_inside_nested_dict() -> None:
    out = redact_obj({"llm": {"api_key": "sk-abcdef1234567890abcd", "model": "deepseek"}})
    assert out["llm"]["api_key"] == "***"
    assert out["llm"]["model"] == "deepseek"


def test_redact_obj_non_json_scalars_pass_through() -> None:
    assert redact_obj(42) == 42
    assert redact_obj(3.14) == 3.14
    assert redact_obj(True) is True
    assert redact_obj(None) is None
