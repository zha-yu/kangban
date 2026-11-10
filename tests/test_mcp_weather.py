"""天气 MCP server 核心单测（N24 · 三场景 + 入参校验 + 缓存语义）。

不连真实外网、不用真实 key：上游 HTTP 抽成 `WeatherFetch` 注入点，测试塞假 fetch；
TTL 计时用 `clock` 注入假时钟，零 sleep、零 flaky。
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

# 目录名含连字符不能作包名：把 weather 目录插到 sys.path 后按文件名导入
_WEATHER_DIR = Path(__file__).resolve().parents[1] / "mcp-servers" / "weather"
if str(_WEATHER_DIR) not in sys.path:
    sys.path.insert(0, str(_WEATHER_DIR))

from weather_core import (  # noqa: E402  （sys.path 引导后才能 import）
    MAX_CITY_LENGTH,
    WeatherCityUnknown,
    WeatherFetchError,
    WeatherFetchTimeout,
    WeatherService,
)

# ---------------------------------------------------------------------------
# 夹具：假 fetch（高德实况形态，见 weather_core 假设）与假时钟
# ---------------------------------------------------------------------------

UPSTREAM_PAYLOAD: dict[str, Any] = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "lives": [
        {
            "province": "浙江省",
            "city": "杭州市",
            "weather": "小雨",
            "temperature": "18.4",
            "humidity": "72",
            "winddirection": "东",
            "windpower": "≤3",
            "reporttime": "2025-11-09 20:00:00",  # 北京时间 → UTC 2025-11-09T12:00:00+00:00
        }
    ],
}

FetchFn = Callable[[str, str, str], Awaitable[dict[str, Any]]]


def make_fetch(
    payload: dict[str, Any] | None = UPSTREAM_PAYLOAD,
    *,
    error: Exception | None = None,
) -> tuple[FetchFn, list[str]]:
    """造一个可观测调用的假 fetch：返回 (fetch, 调用过的城市列表)。"""
    calls: list[str] = []

    async def fetch(city: str, api_key: str, base_url: str) -> dict[str, Any]:
        calls.append(city)
        if error is not None:
            raise error
        return dict(payload) if payload is not None else {}

    return fetch, calls


@pytest.fixture
def service() -> WeatherService:
    fetch, _ = make_fetch()
    return WeatherService(
        api_key="test-key", base_url="https://weather.example", cache_ttl_seconds=600, fetch=fetch
    )


# ---------------------------------------------------------------------------
# 场景 1：正常返回（mock 上游）+ 缓存命中带数据时间
# ---------------------------------------------------------------------------


async def test_normal_fetch_and_cache_hit() -> None:
    fetch, calls = make_fetch()
    svc = WeatherService(
        api_key="k", base_url="https://w.example", cache_ttl_seconds=600, fetch=fetch
    )

    first = await svc.fetch_city("杭州")
    assert first["ok"] is True
    assert first["city"] == "杭州"
    assert first["condition"] == "小雨"
    assert first["temperature_c"] == 18.4
    assert first["cached"] is False
    assert first["observed_at"]  # 数据时间戳必带（AC-01）

    # 同城第二次：命中缓存，fetch 只被调用一次，observed_at（数据时间）不刷新
    second = await svc.fetch_city("杭州")
    assert second["cached"] is True
    assert second["observed_at"] == first["observed_at"]
    assert len(calls) == 1


async def test_ttl_expiry_triggers_refetch() -> None:
    fetch, calls = make_fetch()
    now = 1000.0
    svc = WeatherService(
        api_key="k",
        base_url="https://w.example",
        cache_ttl_seconds=600,
        fetch=fetch,
        clock=lambda: now,
    )

    await svc.fetch_city("杭州")
    now += 601  # TTL 过期
    third = await svc.fetch_city("杭州")
    assert third["cached"] is False
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# 场景 2：无 key 明确失败，不编造、不触上游
# ---------------------------------------------------------------------------


async def test_missing_api_key_fails_without_fabrication() -> None:
    fetch, calls = make_fetch()
    svc = WeatherService(
        api_key="   ", base_url="https://w.example", cache_ttl_seconds=600, fetch=fetch
    )

    result = await svc.fetch_city("杭州")
    assert result["ok"] is False
    assert result["error"] == "missing_api_key"
    assert "这个我暂时查不到" in result["message"]
    # 失败话术不回显内部细节（key / URL / 堆栈）
    assert "test" not in result["message"].lower()
    assert "w.example" not in result["message"]
    assert calls == []  # 根本没碰上游


# ---------------------------------------------------------------------------
# 场景 3：上游超时 / 失败 → 明确失败；有旧缓存则回退旧数据（TD §6.4）
# ---------------------------------------------------------------------------


async def test_upstream_timeout_fails_when_no_cache() -> None:
    fetch, calls = make_fetch(error=WeatherFetchTimeout("upstream timed out"))
    svc = WeatherService(
        api_key="k", base_url="https://w.example", cache_ttl_seconds=600, fetch=fetch
    )

    result = await svc.fetch_city("杭州")
    assert result["ok"] is False
    assert result["error"] == "upstream_timeout"
    assert "这个我暂时查不到" in result["message"]
    assert "timed out" not in result["message"]  # 异常细节不外泄
    assert len(calls) == 1


async def test_upstream_error_fails_when_no_cache() -> None:
    fetch, _ = make_fetch(error=WeatherFetchError("upstream unreachable"))
    svc = WeatherService(
        api_key="k", base_url="https://w.example", cache_ttl_seconds=600, fetch=fetch
    )

    result = await svc.fetch_city("杭州")
    assert result["ok"] is False
    assert result["error"] == "upstream_unavailable"


async def test_city_unknown_maps_to_bad_request() -> None:
    """geocoding 解析不出城市 → bad_request（回传模型重试），不当临时故障。"""
    fetch, _ = make_fetch(error=WeatherCityUnknown("city not resolvable: 不存在的城"))
    svc = WeatherService(
        api_key="k", base_url="https://w.example", cache_ttl_seconds=600, fetch=fetch
    )

    result = await svc.fetch_city("不存在的城")
    assert result["ok"] is False
    assert result["error"] == "bad_request"
    assert "城市名" in result["message"]
    assert "不存在的城" not in result["message"]  # 异常细节不外泄


async def test_upstream_timeout_falls_back_to_stale_cache() -> None:
    """TD §6.4：天气接口超时 → 最近缓存 + 标注缓存时间；无缓存才明确失败。"""
    fetch, calls = make_fetch()
    now = 1000.0
    svc = WeatherService(
        api_key="k",
        base_url="https://w.example",
        cache_ttl_seconds=600,
        fetch=fetch,
        clock=lambda: now,
    )

    fresh = await svc.fetch_city("杭州")
    assert fresh["ok"] is True

    # TTL 过期 + 上游开始超时：回退旧缓存，observed_at 保留原数据时间
    now += 601
    broken, _ = make_fetch(error=WeatherFetchTimeout("upstream timed out"))
    svc._fetch = broken
    stale = await svc.fetch_city("杭州")
    assert stale["ok"] is True
    assert stale["cached"] is True
    assert stale["observed_at"] == fresh["observed_at"]
    assert stale["condition"] == fresh["condition"]
    assert len(calls) == 1  # broken 的调用不计入第一个 fetch 的观测列表


async def test_bad_upstream_response_shape() -> None:
    """上游返回非预期形态 → 不猜字段，明确失败。"""
    fetch, _ = make_fetch(payload={"unexpected": "shape"})
    svc = WeatherService(
        api_key="k", base_url="https://w.example", cache_ttl_seconds=600, fetch=fetch
    )

    result = await svc.fetch_city("杭州")
    assert result["ok"] is False
    assert result["error"] == "bad_upstream_response"


# ---------------------------------------------------------------------------
# 入参强校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("city", "reason"),
    [
        (None, "非字符串"),
        (123, "非字符串"),
        ("", "空串"),
        ("   ", "纯空白"),
        ("杭" * (MAX_CITY_LENGTH + 1), "超长"),
        ("杭州<script>", "注入字符"),
        ("北京\n上海", "控制字符"),
    ],
)
async def test_invalid_city_rejected(city: object, reason: str) -> None:
    fetch, calls = make_fetch()
    svc = WeatherService(
        api_key="k", base_url="https://w.example", cache_ttl_seconds=600, fetch=fetch
    )

    result = await svc.fetch_city(city)
    assert result["ok"] is False, reason
    assert result["error"] == "bad_request"
    assert calls == []
