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

import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from app.core.context import get_trace_id

logger = logging.getLogger("mcp_servers.weather")

# 入参强校验上限：城市名合理长度（防超长串塞日志/塞上游查询串）
MAX_CITY_LENGTH: Final[int] = 64
# 城市名允许的字符：中英文、数字、空格、间隔号（如 "St. Louis" / "乌兰察布"）
_CITY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[\w\u4e00-\u9fff .·-]+$")
# 上游自身的超时要小于客户端整体预算（mcp_call_timeout_seconds=5s），留出协议开销
UPSTREAM_TIMEOUT_SECONDS: Final[float] = 4.0

# 长者可读的失败话术：不暴露内部细节（无 key / 上游问题统一「查不到」口径，契约 §5.4）
FAILURE_MESSAGES: Final[dict[str, str]] = {
    "bad_request": "城市名好像不太对，请再说一遍要查哪里的天气。",
    "missing_api_key": "这个我暂时查不到，请稍后再试，或者找护工帮帮忙。",
    "upstream_timeout": "这个我暂时查不到，请稍后再试，或者找护工帮帮忙。",
    "upstream_unavailable": "这个我暂时查不到，请稍后再试，或者找护工帮帮忙。",
    "bad_upstream_response": "这个我暂时查不到，请稍后再试，或者找护工帮帮忙。",
}

# 假设：上游为 OpenWeatherMap 兼容的 current-weather 接口（provider 未冻结，
# 默认 fetch 只按此形态解析；解析失败一律走 bad_upstream_response，不猜字段）。
# 上线接真实 provider 时只需换掉本函数或注入自定义 fetch，核心逻辑零改动。


class WeatherFetchError(Exception):
    """上游临时故障（连接失败、非 2xx、非 JSON）。消息仅进日志，不外发。"""


class WeatherFetchTimeout(WeatherFetchError):
    """上游超时（区别于连接失败，方便日志分类，返回码同为 upstream_timeout）。"""


class WeatherCityNotFound(Exception):
    """城市地理编码查不到（Open-Meteo 分支）；映射为 bad_request，不触达天气上游。"""


# 注入点：async (city, api_key, base_url) -> 上游 JSON dict；
# 失败抛 WeatherFetchError / WeatherFetchTimeout。签名不含任何内部细节。
WeatherFetch = Callable[[str, str, str], Awaitable[dict[str, Any]]]
# Open-Meteo 分支注入点：async (city, base_url) -> 归一化片段
# {"condition", "temperature_c", "observed_at"}；失败抛上面三类异常之一。
OpenMeteoFetch = Callable[[str, str], Awaitable[dict[str, Any]]]

# Open-Meteo：无需 key 的公开实时天气（provider=open-meteo 时启用）。
OPEN_METEO_PROVIDER: Final[str] = "open-meteo"
OPEN_METEO_GEOCODING_URL: Final[str] = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_DEFAULT_BASE_URL: Final[str] = "https://api.open-meteo.com/v1/forecast"

# WMO weather_code → 长者可读中文（Open-Meteo 官方 code 表，未列出的按「未知天气」）
_WMO_CONDITIONS: Final[dict[int, str]] = {
    0: "晴",
    1: "大部晴朗",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷暴伴冰雹",
}


async def open_meteo_fetch(city: str, base_url: str) -> dict[str, Any]:
    """Open-Meteo 默认实现：城市 → 经纬度 → 当前天气（无需 API key）。

    只发两次请求：地理编码（取第一条）与 current 天气；城市查不到抛
    ``WeatherCityNotFound``（映射 bad_request），上游故障/超时按既有两类异常上抛。
    """
    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
            geo = await client.get(
                OPEN_METEO_GEOCODING_URL,
                params={"name": city, "count": 1, "language": "zh", "format": "json"},
            )
            if geo.status_code != 200:
                raise WeatherFetchError("geocoding http error")
            try:
                geo_payload: Any = geo.json()
            except ValueError as exc:
                raise WeatherFetchError("geocoding returned non-json") from exc
            results = geo_payload.get("results") if isinstance(geo_payload, dict) else None
            if not isinstance(results, list) or not results:
                raise WeatherCityNotFound

            place = results[0]
            if not isinstance(place, dict):
                raise WeatherFetchError("geocoding returned unexpected shape")
            latitude = place.get("latitude")
            longitude = place.get("longitude")
            if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
                raise WeatherFetchError("geocoding returned no coordinates")

            weather = await client.get(
                (base_url or OPEN_METEO_DEFAULT_BASE_URL).strip(),
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": "temperature_2m,weather_code",
                    "timezone": "auto",
                },
            )
            if weather.status_code != 200:
                raise WeatherFetchError("weather http error")
            try:
                payload: Any = weather.json()
            except ValueError as exc:
                raise WeatherFetchError("weather returned non-json") from exc
    except httpx.TimeoutException as exc:
        raise WeatherFetchTimeout("open-meteo timed out") from exc
    except httpx.TransportError as exc:
        raise WeatherFetchError("open-meteo unreachable") from exc

    current = payload.get("current") if isinstance(payload, dict) else None
    if not isinstance(current, dict):
        raise WeatherFetchError("open-meteo returned no current block")
    temperature = current.get("temperature_2m")
    code = current.get("weather_code")
    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
        raise WeatherFetchError("open-meteo returned no temperature")
    try:
        condition = _WMO_CONDITIONS.get(int(code), "未知天气") if code is not None else "未知天气"
    except (TypeError, ValueError):
        condition = "未知天气"
    observed_at = current.get("time")
    if not isinstance(observed_at, str) or not observed_at:
        observed_at = datetime.now(tz=UTC).isoformat(timespec="seconds")
    return {
        "condition": condition,
        "temperature_c": round(float(temperature), 1),
        "observed_at": observed_at,
    }


# 时钟注入点：测试可替换为假时钟，避免真等 TTL
MonotonicClock = Callable[[], float]


async def httpx_fetch(city: str, api_key: str, base_url: str) -> dict[str, Any]:
    """默认上游 fetch：GET {base_url}（OpenWeather 兼容查询串）。"""
    if not base_url.strip():
        raise WeatherFetchError("weather upstream base_url is not configured")
    params = {"q": city, "appid": api_key, "units": "metric", "lang": "zh_cn"}
    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
            response = await client.get(base_url.strip(), params=params)
    except httpx.TimeoutException as exc:
        raise WeatherFetchTimeout("upstream timed out") from exc
    except httpx.TransportError as exc:
        raise WeatherFetchError("upstream unreachable") from exc
    # 状态码只进日志，不进异常消息（异常消息可能被上层透传，防内部细节外泄）
    if response.status_code != 200:
        logger.warning(
            "weather_upstream_http_error",
            extra={"trace_id": get_trace_id() or "-", "status_code": response.status_code},
        )
        raise WeatherFetchError("upstream http error")
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise WeatherFetchError("upstream returned non-json") from exc
    if not isinstance(payload, dict):
        raise WeatherFetchError("upstream returned non-object json")
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


def _normalize_upstream(payload: dict[str, Any], canonical_city: str) -> dict[str, Any] | None:
    """上游 JSON → 工具成功返回结构；形态不符返回 None（由调用方转为失败）。"""
    weather = payload.get("weather")
    main = payload.get("main")
    if not isinstance(weather, list) or not weather or not isinstance(main, dict):
        return None
    first = weather[0]
    if not isinstance(first, dict) or not isinstance(first.get("description"), str):
        return None
    temperature = main.get("temp")
    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
        return None
    observed_at: str
    dt = payload.get("dt")
    if isinstance(dt, (int, float)) and not isinstance(dt, bool) and dt > 0:
        observed_at = datetime.fromtimestamp(dt, tz=UTC).isoformat(timespec="seconds")
    else:
        # 上游不带观测时间时用本进程当前 UTC 时间兜底，仍如实标注数据时间
        observed_at = datetime.now(tz=UTC).isoformat(timespec="seconds")
    return {
        "ok": True,
        "city": canonical_city,
        "condition": first["description"],
        "temperature_c": round(float(temperature), 1),
        "observed_at": observed_at,
    }


class WeatherService:
    """天气查询：校验 → 无 key 短路 → 缓存 → 上游 → 归一化。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        cache_ttl_seconds: int,
        provider: str = "",
        fetch: WeatherFetch | None = None,
        open_meteo_fetch_fn: OpenMeteoFetch | None = None,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.strip()
        self._provider = provider.strip().lower()
        self._is_open_meteo = self._provider in {"open-meteo", "open_meteo", "openmeteo"}
        self._cache_ttl_seconds = max(0, cache_ttl_seconds)
        self._fetch = fetch if fetch is not None else httpx_fetch
        self._open_meteo_fetch: OpenMeteoFetch = (
            open_meteo_fetch_fn if open_meteo_fetch_fn is not None else open_meteo_fetch
        )
        self._clock = clock
        # key: 规范化城市名 -> (写入时刻 monotonic, 归一化成功载荷)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def fetch_city(self, city: object) -> dict[str, Any]:
        """工具入口：永不抛异常，失败一律结构化返回（上层不用 try 包它）。"""
        canonical = normalize_city(city)
        if canonical is None:
            return _failure("bad_request")
        if self._is_open_meteo:
            return await self._fetch_open_meteo(canonical)
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

    async def _fetch_open_meteo(self, canonical: str) -> dict[str, Any]:
        """Open-Meteo 分支：无需 key；城市查不到 → bad_request，上游故障 → 缓存兜底。"""
        cached = self._cache_get(canonical)
        if cached is not None:
            return {**cached, "cached": True}
        try:
            normalized = await self._open_meteo_fetch(canonical, self._base_url)
        except WeatherCityNotFound:
            logger.info("weather_city_not_found", extra={"trace_id": get_trace_id() or "-"})
            return _failure("bad_request")
        except WeatherFetchTimeout:
            logger.warning("weather_upstream_timeout", extra={"trace_id": get_trace_id() or "-"})
            return self._stale_or_failure(canonical, "upstream_timeout")
        except WeatherFetchError:
            logger.warning("weather_upstream_error", extra={"trace_id": get_trace_id() or "-"})
            return self._stale_or_failure(canonical, "upstream_unavailable")

        payload = {"ok": True, "city": canonical, **normalized}
        self._cache_put(canonical, payload)
        return {**payload, "cached": False}

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
    "OPEN_METEO_DEFAULT_BASE_URL",
    "OPEN_METEO_PROVIDER",
    "UPSTREAM_TIMEOUT_SECONDS",
    "MonotonicClock",
    "OpenMeteoFetch",
    "WeatherCityNotFound",
    "WeatherFetch",
    "WeatherFetchError",
    "WeatherFetchTimeout",
    "WeatherService",
    "httpx_fetch",
    "normalize_city",
    "open_meteo_fetch",
]
