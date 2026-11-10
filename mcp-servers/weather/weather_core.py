"""天气 MCP server 的核心逻辑（Day 6 · N24）。

边界（契约 §5.4 / 计划 C-2 / R-7）：

- 不做自由外网访问：唯一出站请求是天气上游 API（AGENTS §4.5：天气是唯一真实外部接口）；
- 无 `weather_api_key` / 上游失败 / 上游超时一律返回**结构化失败**，绝不编造天气数据；
- 错误信息不回显内部细节：完整 URL、key、上游响应体一律不出现在返回值里；
- 缓存为进程内 TTL 缓存，TTL 由装配层从 `settings.weather_cache_ttl_seconds` 传入，
  命中时返回**数据时间戳**（`observed_at` 是上游观测时间，不因命中而刷新）；
- 上游 HTTP 抽成可注入的 fetch 协程（`WeatherFetch`）：测试注入假 fetch，
  不依赖真实外网与真实 key。

失败返回结构（与成功同层，`ok` 字段区分）：

    {"ok": False, "error": "<机器码>", "message": "<长者可读话术>"}

机器码取值：`bad_request` / `missing_api_key` / `upstream_timeout` /
`upstream_unavailable` / `bad_upstream_response`。MCP 客户端把除 `bad_request`
之外的失败统一映射为 `upstream_error`（临时错误语义：退避重试 ≤2 次后降级话术）。

降级顺序（TD §6.4 临时错误处置 / AC-02）：上游失败时若有「曾成功过、已过 TTL」的
旧缓存，先回退旧缓存并保留原 `observed_at`（数据时间如实标注），完全没有缓存才明确
失败——任何路径都不编造天气数据。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

import httpx

from app.core.context import get_trace_id

logger = logging.getLogger("mcp_servers.weather")

# 入参强校验上限：城市名合理长度（防超长串塞日志/塞上游查询串）
MAX_CITY_LENGTH: Final[int] = 64
# 城市名允许的字符：中英文、数字、空格、间隔号（如 "St. Louis" / "乌兰察布"）
_CITY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[\w\u4e00-\u9fff .·-]+$")
# 上游自身的超时要小于客户端整体预算（mcp_call_timeout_seconds=5s），留出协议开销
UPSTREAM_TIMEOUT_SECONDS: Final[float] = 4.0
# 高德 REST 端点（挂在 base_url 根地址之后）
_AMAP_GEOCODE_PATH: Final[str] = "/v3/geocode/geo"
_AMAP_WEATHER_PATH: Final[str] = "/v3/weather/weatherInfo"
# 高德 reporttime 是北京时间字符串；存储口径 UTC（AGENTS §8），转成 UTC ISO 后下发
_AMAP_TZ: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")

# 长者可读的失败话术：不暴露内部细节（无 key / 上游问题统一「查不到」口径，契约 §5.4）
FAILURE_MESSAGES: Final[dict[str, str]] = {
    "bad_request": "城市名好像不太对，请再说一遍要查哪里的天气。",
    "missing_api_key": "这个我暂时查不到，请稍后再试，或者找护工帮帮忙。",
    "upstream_timeout": "这个我暂时查不到，请稍后再试，或者找护工帮帮忙。",
    "upstream_unavailable": "这个我暂时查不到，请稍后再试，或者找护工帮帮忙。",
    "bad_upstream_response": "这个我暂时查不到，请稍后再试，或者找护工帮帮忙。",
}

# 假设（2026-11-10 落实）：上游为高德开放平台 REST API（国内直连，OpenWeatherMap
# 大陆网络不可达已实测）。base_url 配 REST 根地址（如 https://restapi.amap.com），
# 默认 fetch 走两步：/v3/geocode/geo（城市名 → adcode）→ /v3/weather/weatherInfo
# （adcode → 实况）。换供应商只需替换本函数或注入自定义 fetch，核心逻辑零改动。


class WeatherFetchError(Exception):
    """上游临时故障（连接失败、非 2xx、非 JSON）。消息仅进日志，不外发。"""


class WeatherFetchTimeout(WeatherFetchError):
    """上游超时（区别于连接失败，方便日志分类，返回码同为 upstream_timeout）。"""


class WeatherCityUnknown(WeatherFetchError):
    """城市名无法在 geocoding 中解析（映射为 bad_request：回传模型换个说法重试）。"""


# 注入点：async (city, api_key, base_url) -> 上游 JSON dict；
# 失败抛 WeatherFetchError / WeatherFetchTimeout。签名不含任何内部细节。
WeatherFetch = Callable[[str, str, str], Awaitable[dict[str, Any]]]

# 时钟注入点：测试可替换为假时钟，避免真等 TTL
MonotonicClock = Callable[[], float]


async def httpx_fetch(city: str, api_key: str, base_url: str) -> dict[str, Any]:
    """默认上游 fetch：高德两步——geocoding（城市名 → adcode）→ 实况天气。

    两步共享同一个总超时预算（UPSTREAM_TIMEOUT_SECONDS），保证 fetch 整体仍小于
    客户端 5s 总预算；key 无效 / 配额超限（status != "1"）按临时故障处理，
    城市解析不出（geocodes 为空）抛 WeatherCityUnknown → bad_request。
    """
    root = base_url.strip().rstrip("/")
    if not root:
        raise WeatherFetchError("weather upstream base_url is not configured")
    try:
        async with asyncio.timeout(UPSTREAM_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
                adcode = await _amap_geocode(client, root, city, api_key)
                return await _amap_weather(client, root, adcode, api_key)
    except TimeoutError as exc:  # asyncio.timeout 预算耗尽（含两步合计超支）
        raise WeatherFetchTimeout("upstream timed out") from exc
    except httpx.TimeoutException as exc:
        raise WeatherFetchTimeout("upstream timed out") from exc
    except httpx.TransportError as exc:
        raise WeatherFetchError("upstream unreachable") from exc


async def _amap_get_json(
    client: httpx.AsyncClient, url: str, params: dict[str, str], what: str
) -> dict[str, Any]:
    """高德单次 GET：状态码 / JSON / 对象形态三重把关，异常消息不含内部细节。"""
    try:
        response = await client.get(url, params=params)
    except httpx.TimeoutException:
        raise
    except httpx.TransportError as exc:
        raise WeatherFetchError(f"amap {what} unreachable") from exc
    if response.status_code != 200:
        logger.warning(
            "weather_upstream_http_error",
            extra={
                "trace_id": get_trace_id() or "-",
                "status_code": response.status_code,
                "step": what,
            },
        )
        raise WeatherFetchError(f"amap {what} http error")
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise WeatherFetchError(f"amap {what} returned non-json") from exc
    if not isinstance(payload, dict):
        raise WeatherFetchError(f"amap {what} returned non-object json")
    return payload


async def _amap_geocode(client: httpx.AsyncClient, root: str, city: str, api_key: str) -> str:
    """城市名 → 行政区划 adcode；解析不出抛 WeatherCityUnknown。"""
    payload = await _amap_get_json(
        client, root + _AMAP_GEOCODE_PATH, {"address": city, "key": api_key}, "geocode"
    )
    if payload.get("status") != "1":
        # infocode 10001（key 无效）/ 1000x（配额）等统一按临时故障，细节只进日志
        logger.warning(
            "weather_amap_geocode_rejected",
            extra={"trace_id": get_trace_id() or "-", "infocode": payload.get("infocode")},
        )
        raise WeatherFetchError("amap geocode rejected")
    geocodes = payload.get("geocodes")
    first = geocodes[0] if isinstance(geocodes, list) and geocodes else None
    adcode = first.get("adcode") if isinstance(first, dict) else None
    if not isinstance(adcode, str) or not adcode:
        raise WeatherCityUnknown(f"city not resolvable: {city}")
    return adcode


async def _amap_weather(
    client: httpx.AsyncClient, root: str, adcode: str, api_key: str
) -> dict[str, Any]:
    """adcode → 实况天气 payload；status 非 "1" 按临时故障。"""
    payload = await _amap_get_json(
        client,
        root + _AMAP_WEATHER_PATH,
        {"city": adcode, "key": api_key, "extensions": "base"},
        "weather",
    )
    if payload.get("status") != "1":
        logger.warning(
            "weather_amap_weather_rejected",
            extra={"trace_id": get_trace_id() or "-", "infocode": payload.get("infocode")},
        )
        raise WeatherFetchError("amap weather rejected")
    return payload


def _failure(error: str) -> dict[str, Any]:
    """统一失败结构：机器码 + 长者可读话术，无任何内部细节。"""
    return {"ok": False, "error": error, "message": FAILURE_MESSAGES[error]}


def normalize_city(city: object) -> str | None:
    """入参强校验：必须是可打印的非空字符串；非法返回 None。"""
    if not isinstance(city, str):
        return None
    stripped = city.strip()
    if not stripped or len(stripped) > MAX_CITY_LENGTH:
        return None
    if not _CITY_PATTERN.match(stripped):
        return None
    return stripped


def _parse_temperature(raw: object) -> float | None:
    """高德温度字段（字符串或数值）→ float；形态不符返回 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            return float(raw.strip())
        except ValueError:
            return None
    return None


def _parse_observed_at(reporttime: object) -> str:
    """高德 reporttime（北京时间字符串）→ UTC ISO；解析不出用当前 UTC 兜底。"""
    if isinstance(reporttime, str) and reporttime.strip():
        try:
            local = datetime.strptime(reporttime.strip(), "%Y-%m-%d %H:%M:%S")
            return local.replace(tzinfo=_AMAP_TZ).astimezone(UTC).isoformat(timespec="seconds")
        except ValueError:
            pass
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _normalize_upstream(payload: dict[str, Any], canonical_city: str) -> dict[str, Any] | None:
    """高德实况 JSON → 工具成功返回结构；形态不符返回 None（由调用方转为失败）。

    只认 `lives[0]` 里的 `weather` / `temperature`（或 `temperature_float`）/
    `reporttime`，不猜其他字段——status 与 infocode 已在 fetch 层把关。
    """
    lives = payload.get("lives")
    if not isinstance(lives, list) or not lives or not isinstance(lives[0], dict):
        return None
    first = lives[0]
    condition = first.get("weather")
    if not isinstance(condition, str) or not condition.strip():
        return None
    temperature = _parse_temperature(first.get("temperature"))
    if temperature is None:
        temperature = _parse_temperature(first.get("temperature_float"))
    if temperature is None:
        return None
    return {
        "ok": True,
        "city": canonical_city,
        "condition": condition.strip(),
        "temperature_c": round(temperature, 1),
        "observed_at": _parse_observed_at(first.get("reporttime")),
    }


class WeatherService:
    """天气查询：校验 → 无 key 短路 → 缓存 → 上游 → 归一化。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        cache_ttl_seconds: int,
        fetch: WeatherFetch | None = None,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.strip()
        self._cache_ttl_seconds = max(0, cache_ttl_seconds)
        self._fetch = fetch if fetch is not None else httpx_fetch
        self._clock = clock
        # key: 规范化城市名 -> (写入时刻 monotonic, 归一化成功载荷)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def fetch_city(self, city: object) -> dict[str, Any]:
        """工具入口：永不抛异常，失败一律结构化返回（上层不用 try 包它）。"""
        canonical = normalize_city(city)
        if canonical is None:
            return _failure("bad_request")
        if not self._api_key:
            logger.warning("weather_missing_api_key", extra={"trace_id": get_trace_id() or "-"})
            return _failure("missing_api_key")

        cached = self._cache_get(canonical)
        if cached is not None:
            return {**cached, "cached": True}

        try:
            upstream = await self._fetch(canonical, self._api_key, self._base_url)
        except WeatherFetchTimeout:
            logger.warning("weather_upstream_timeout", extra={"trace_id": get_trace_id() or "-"})
            return self._stale_or_failure(canonical, "upstream_timeout")
        except WeatherCityUnknown:
            # 城市解析不出是参数问题：回传模型换个说法重试（计入预算），不当临时故障
            logger.info("weather_city_unknown", extra={"trace_id": get_trace_id() or "-"})
            return _failure("bad_request")
        except WeatherFetchError:
            logger.warning("weather_upstream_error", extra={"trace_id": get_trace_id() or "-"})
            return self._stale_or_failure(canonical, "upstream_unavailable")

        normalized = _normalize_upstream(upstream, canonical)
        if normalized is None:
            logger.warning(
                "weather_bad_upstream_response", extra={"trace_id": get_trace_id() or "-"}
            )
            return _failure("bad_upstream_response")

        self._cache_put(canonical, normalized)
        return {**normalized, "cached": False}

    def _cache_get(self, canonical: str) -> dict[str, Any] | None:
        entry = self._cache.get(canonical)
        if entry is None:
            return None
        stored_at, payload = entry
        if self._clock() - stored_at > self._cache_ttl_seconds:
            # 过期**不清行**：上游随后失败时 _stale_or_failure 还要靠这行回退旧数据（TD §6.4）
            return None
        return payload

    def _stale_or_failure(self, canonical: str, error: str) -> dict[str, Any]:
        """上游失败时的降级：有曾成功过的旧缓存就回退（cached=True + 原 observed_at），
        没有才返回明确失败；两条路都绝不编造天气数据。"""
        entry = self._cache.get(canonical)
        if entry is not None:
            return {**entry[1], "cached": True}
        return _failure(error)

    def _cache_put(self, canonical: str, payload: dict[str, Any]) -> None:
        self._cache[canonical] = (self._clock(), payload)


__all__ = [
    "FAILURE_MESSAGES",
    "MAX_CITY_LENGTH",
    "UPSTREAM_TIMEOUT_SECONDS",
    "MonotonicClock",
    "WeatherCityUnknown",
    "WeatherFetch",
    "WeatherFetchError",
    "WeatherFetchTimeout",
    "WeatherService",
    "httpx_fetch",
    "normalize_city",
]
