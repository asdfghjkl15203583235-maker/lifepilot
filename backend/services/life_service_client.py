"""External life-service clients (weather + map) with configurable providers.

Open-Meteo and Nominatim are used by default so the feature works without API keys;
OpenWeatherMap, Google Maps and Amap (高德) are optional key-based providers.

Amap (China) works in two steps: the weather endpoint needs an ``adcode`` (e.g.
Shanghai = 310000), so a city/address is first resolved through the geocoding
endpoint and the mapping is cached in-process to avoid repeat lookups.

Map lookups use **POI keyword search** (`place/text`), not geocoding: geocoding only
understands postal addresses and never resolves 景点 like 上海博物馆/豫园, while an
unscoped POI search matches same-name places nationwide (外滩 → 武汉/福州). Both the
POI search and the geocode fallback take a `city` and scope the request to it.

Route planning (`get_route_planning`) wraps Amap's walking / transit / driving
direction endpoints and returns distance + duration, so the agent can answer
"这两个景点之间怎么走、要多久".
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from config import Settings

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 30

# 高德天气的两个档次：base 只返回实况（lives），all 才返回逐日预报（forecasts）。
# 用户问"明天/后天"时只有 all 能给出那一天的数据，因此天气查询统一走 all。
AMAP_WEATHER_EXTENSIONS_ALL = "all"

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPENWEATHER_MAP_URL = "https://api.openweathermap.org/data/2.5/weather"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
GOOGLE_MAPS_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
AMAP_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
AMAP_REGEO_URL = "https://restapi.amap.com/v3/geocode/regeo"
AMAP_WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
# POI（景点/商场/车站）必须走 place/text，地理编码只认"地址"，拿不到「上海博物馆」
# 这类 POI 的坐标，还会因为全国同名把「外滩」匹配到武汉、福州。
AMAP_POI_TEXT_URL = "https://restapi.amap.com/v3/place/text"
AMAP_DIRECTION_WALKING_URL = "https://restapi.amap.com/v3/direction/walking"
AMAP_DIRECTION_DRIVING_URL = "https://restapi.amap.com/v3/direction/driving"
AMAP_DIRECTION_TRANSIT_URL = "https://restapi.amap.com/v3/direction/transit/integrated"

_AMAP_ADCODE_RE = re.compile(r"^\d{6}$")
_AMAP_ADCODE_CACHE_MAX = 256
# "city|address" -> list of normalized geocode rows. In-process only; Amap
# geocoding results are effectively static, so a small FIFO cache is enough.
# key 里必须带 city：同一句"人民广场"在上海和南京是两个地方，只按 address
# 缓存会把上一个城市的坐标复用给下一个城市。
_AMAP_GEOCODE_CACHE: dict[str, list[dict[str, Any]]] = {}

POI_DEFAULT_LIMIT = 5
# place/text 单页上限 25；再大也没有意义，agent 只需要前几个候选。
POI_MAX_LIMIT = 25
ROUTE_MODES = ("walking", "transit", "driving")
_ROUTE_MODE_LABEL = {"walking": "步行", "transit": "公交/地铁", "driving": "驾车"}
_MAX_ROUTE_STEPS = 8


class LifeServiceError(Exception):
    """Raised when an external life service returns a non-2xx response.

    ``fatal`` marks errors that mean "the user's target simply is not available"
    (unknown place, entity mismatch). Those must abort the whole DAG step instead
    of being retried or silently satisfied with another city's data.
    """

    def __init__(
        self,
        provider: str,
        status_code: int,
        detail: str,
        *,
        fatal: bool = False,
    ) -> None:
        self.provider = provider
        self.status_code = status_code
        self.detail = detail
        self.fatal = fatal
        super().__init__(f"{provider} API {status_code}: {detail}")


async def _amap_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET an Amap REST endpoint and unwrap its ``status`` envelope."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(url, params=params)
    if response.status_code != 200:
        raise LifeServiceError("amap", response.status_code, response.text)
    data = response.json()
    if str(data.get("status", "1")) != "1":
        raise LifeServiceError("amap", 0, str(data.get("info") or "amap request failed"))
    return data


def _amap_text(value: Any) -> str | None:
    """Amap returns empty lists for missing fields; normalize to text/None."""
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _amap_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _amap_int(value: Any) -> int | None:
    number = _amap_float(value)
    return int(number) if number is not None else None


def _parse_amap_location(location: Any) -> tuple[float | None, float | None]:
    """Amap ``location`` is "lon,lat"."""
    text = _amap_text(location)
    if not text or "," not in text:
        return None, None
    lon_text, _, lat_text = text.partition(",")
    return _amap_float(lon_text), _amap_float(lat_text)


def _normalize_amap_geocode(row: dict[str, Any]) -> dict[str, Any]:
    lon, lat = _parse_amap_location(row.get("location"))
    city = _amap_text(row.get("city"))
    province = _amap_text(row.get("province"))
    district = _amap_text(row.get("district"))
    label = _amap_text(row.get("formatted_address"))
    if not label:
        label = " ".join(part for part in (province, city, district) if part) or None
    return {
        "label": label,
        "formatted_address": label,
        "province": province,
        "city": city,
        "district": district,
        "adcode": _amap_text(row.get("adcode")),
        "latitude": lat,
        "longitude": lon,
    }


async def _amap_geocode(
    settings: Settings,
    address: str,
    *,
    city: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve an address/city name to Amap geocode rows (cached in-process).

    `city` 会作为高德地理编码的 `city` 参数，把结果限定在该城市内。真实 badcase：
    「外滩」不限城市时高德会返回武汉、福州、上海浦东的同名点，模型只能如实说
    "匹配到多个城市"，用户拿不到上海的坐标。
    """
    key = address.strip()
    if not key:
        return []
    city_scope = (city or "").strip() or None
    cache_key = f"{city_scope or ''}|{key}"
    cached = _AMAP_GEOCODE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    params: dict[str, Any] = {"address": key, "key": settings.amap_api_key}
    if city_scope:
        params["city"] = city_scope
    data = await _amap_get(AMAP_GEOCODE_URL, params)
    rows = [
        _normalize_amap_geocode(row)
        for row in (data.get("geocodes") or [])
        if isinstance(row, dict) and row.get("adcode")
    ]
    if rows:
        if len(_AMAP_GEOCODE_CACHE) >= _AMAP_ADCODE_CACHE_MAX:
            _AMAP_GEOCODE_CACHE.pop(next(iter(_AMAP_GEOCODE_CACHE)))
        _AMAP_GEOCODE_CACHE[cache_key] = rows
    return rows


async def _amap_regeo(
    settings: Settings,
    latitude: float,
    longitude: float,
) -> dict[str, Any] | None:
    """Reverse-geocode coordinates to an adcode (for coordinate-only weather)."""
    data = await _amap_get(
        AMAP_REGEO_URL,
        {"location": f"{longitude},{latitude}", "key": settings.amap_api_key},
    )
    component = ((data.get("regeocode") or {}).get("addressComponent") or {})
    adcode = _amap_text(component.get("adcode"))
    if not adcode:
        return None
    return {
        "label": _amap_text((data.get("regeocode") or {}).get("formatted_address")),
        "formatted_address": _amap_text((data.get("regeocode") or {}).get("formatted_address")),
        "province": _amap_text(component.get("province")),
        "city": _amap_text(component.get("city")),
        "district": _amap_text(component.get("district")),
        "adcode": adcode,
        "latitude": latitude,
        "longitude": longitude,
    }


_CITY_FRAGMENT_RE = re.compile(r"[市省区县州盟旗]+")
# 行政级别字段：只有省/市/区才算"这个实体"本身；formatted_address 里出现
# 目标词可能只是街道/村庄重名（"火星村"），不能据此认定是用户要的地方。
_ENTITY_LEVEL_KEYS = ("province", "city", "district")


def requested_entity_mismatch(requested: str, resolved: dict[str, Any]) -> bool:
    """请求的实体是否与解析出的行政区划不是同一个地方。

    真实 badcase：用户问"火星天气"，高德把"火星"模糊命中成贵州黔东南的
    「火星村」，返回的数据看着像成功，但城市根本不是火星。只要请求名没有
    落在省/市/区任何一级，就判定实体不一致——宁可查不到，也不能拿别处的
    数据冒充，否则下游步骤会基于错误数据继续执行。
    """
    needle = (requested or "").strip().rstrip("市省区县州盟旗")
    if not needle:
        return False
    fragments = [needle] if len(needle) <= 3 else [needle[:2], needle[-2:]]
    haystack = " ".join(
        str(resolved.get(key) or "") for key in _ENTITY_LEVEL_KEYS
    )
    return not all(fragment in haystack for fragment in fragments)


def _normalize_amap_casts(forecast_block: dict[str, Any]) -> list[dict[str, Any]]:
    """把高德 `forecasts[].casts[]` 归一成按日预报列表（含日期与昼/夜温差）。

    高德天气的 `extensions=all` 才返回 `forecasts`；`extensions=base`（默认）只
    返回 `lives` 实况。每条 cast 的字段：date / dayweather / nightweather /
    daytemp / nighttemp。这里只保留 agent 需要展示的最小字段集。

    返回的每一项形如：
        {"date": "2026-10-29", "day_weather": "晴", "night_weather": "多云",
         "day_temp_c": 23, "night_temp_c": 15}
    """
    casts = forecast_block.get("casts") or []
    normalized: list[dict[str, Any]] = []
    for cast in casts:
        if not isinstance(cast, dict):
            continue
        date = _amap_text(cast.get("date"))
        if not date:
            continue
        normalized.append(
            {
                "date": date,
                "week": _amap_text(cast.get("week")),
                "day_weather": _amap_text(cast.get("dayweather")),
                "night_weather": _amap_text(cast.get("nightweather")),
                "day_temp_c": _amap_int(cast.get("daytemp")),
                "night_temp_c": _amap_int(cast.get("nighttemp")),
                "day_wind": _amap_text(cast.get("daywind")),
                "day_power": _amap_text(cast.get("daypower")),
            }
        )
    return normalized


def _best_city_match(requested: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the geocode row that really is the requested place.

    Amap answers with its best fuzzy guess even for nonsense input, so a row is
    only accepted when the requested name appears in the resolved province /
    city / district / formatted address.
    """
    # 「山西榆次」会被解析成「山西省晋中市榆次区」——地名被补全了，仍然是对的；
    # 而「出行计划」被解析成「广西…龙州县出行」只是模糊撞词。用首尾片段判断：
    # 前者首「山西」尾「榆次」都在结果里，后者尾「计划」不在。
    needle = requested.strip().rstrip("市省区县州盟旗")
    if not needle:
        return None
    fragments = [needle] if len(needle) <= 3 else [needle[:2], needle[-2:]]
    for row in rows:
        haystack = " ".join(
            str(row.get(key) or "")
            for key in ("province", "city", "district", "formatted_address", "label")
        )
        if all(fragment in haystack for fragment in fragments):
            return row
    return None


async def _resolve_amap_location(
    settings: Settings,
    *,
    latitude: float | None,
    longitude: float | None,
    city: str | None,
) -> dict[str, Any] | None:
    """Get an adcode + display context for weather.

    Precedence: explicit 6-digit adcode → city name → coordinates → default city.

    A city the user actually named is *never* replaced by the default city. Amap's
    geocoder fuzzy-matches substrings ("出行计划" resolves to 广西崇左龙州县出行),
    so a malformed city string used to land on an unrelated place — and the old
    silent fallback then made that wrong place look like a successful answer.
    Better to fail loudly than to answer for the wrong city.
    """
    if city and _AMAP_ADCODE_RE.match(city.strip()):
        return {"adcode": city.strip(), "city": city.strip()}
    if city and city.strip():
        try:
            rows = await _amap_geocode(settings, city)
        except LifeServiceError:
            # Amap answers ENGINE_RESPONSE_DATA_ERROR for strings it cannot parse
            # at all, which is not something a user can act on.
            raise LifeServiceError(
                "amap", 0, f"无法识别城市「{city.strip()}」", fatal=True,
            ) from None
        if not rows:
            raise LifeServiceError(
                "amap", 0, f"无法识别城市「{city.strip()}」", fatal=True,
            )
        best = _best_city_match(city.strip(), rows)
        if best is None:
            resolved = rows[0].get("formatted_address") or rows[0].get("label")
            raise LifeServiceError(
                "amap", 0,
                f"「{city.strip()}」被解析成了「{resolved}」，已拒绝返回该地天气",
                fatal=True,
            )
        # 更深一层的实体校验：`_best_city_match` 认 formatted_address 里的子串，
        # 「火星」会被贵州「火星村」蒙混过关。只有省/市/区级别对得上，才算同一个
        # 地方；否则宁可报"查不到"，也不能拿别处的数据当成成功结果。
        if requested_entity_mismatch(city.strip(), best):
            resolved = best.get("formatted_address") or best.get("label") or best.get("city")
            raise LifeServiceError(
                "amap", 0,
                f"查不到「{city.strip()}」这个地方（高德只匹配到「{resolved}」）",
                fatal=True,
            )
        return best
    if latitude is not None and longitude is not None:
        regeo = await _amap_regeo(settings, latitude, longitude)
        if regeo is not None:
            return regeo
    if settings.weather_default_city:
        rows = await _amap_geocode(settings, settings.weather_default_city)
        if rows:
            return rows[0]
    return None


async def get_weather(
    settings: Settings,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    city: str | None = None,
) -> dict[str, Any]:
    """Return a normalized weather summary for the configured/default location."""
    provider = (settings.weather_provider or "open_meteo").lower()
    lat = latitude if latitude is not None else settings.weather_latitude
    lon = longitude if longitude is not None else settings.weather_longitude

    if provider == "amap":
        if not settings.amap_api_key:
            raise LifeServiceError("amap", 0, "需要配置 AMAP_API_KEY")
        resolved = await _resolve_amap_location(
            settings,
            latitude=latitude,
            longitude=longitude,
            city=city,
        )
        if resolved is None or not resolved.get("adcode"):
            raise LifeServiceError("amap", 0, f"无法解析城市/坐标对应的 adcode: {city or (lat, lon)}")
        # extensions=all 才返回预报（forecasts）；默认 base 只有实况（lives）。
        # 用户问"明天/后天"时若只请求 base，永远拿不到那一天的预报。
        data = await _amap_get(
            AMAP_WEATHER_URL,
            {
                "city": resolved["adcode"],
                "key": settings.amap_api_key,
                "extensions": AMAP_WEATHER_EXTENSIONS_ALL,
            },
        )
        lives = data.get("lives") or []
        forecasts = data.get("forecasts") or []
        casts: list[dict[str, Any]] = []
        if forecasts and isinstance(forecasts[0], dict):
            casts = _normalize_amap_casts(forecasts[0])
        # 实况与预报都缺失才算失败；只要有一边能给数据就返回。
        if not lives and not casts:
            raise LifeServiceError("amap", 0, str(data.get("info") or "未返回天气数据"))
        live = lives[0] if lives else {}
        return {
            "provider": "amap",
            "city": _amap_text(live.get("city")) or resolved.get("city"),
            "province": _amap_text(live.get("province")) or resolved.get("province"),
            "adcode": resolved.get("adcode"),
            "temperature_c": _amap_int(live.get("temperature")),
            "humidity": _amap_int(live.get("humidity")),
            "condition": _amap_text(live.get("weather")),
            "wind_direction": _amap_text(live.get("winddirection")),
            "wind_power": _amap_text(live.get("windpower")),
            "as_of_iso": _amap_text(live.get("reporttime")),
            # 逐日预报（可能为空——高德仅对未来若干天给预报）。
            "forecasts": casts,
            "raw": data,
        }

    if provider == "openweathermap":
        if not settings.openweather_api_key or lat is None or lon is None:
            raise LifeServiceError("openweathermap", 0, "需要配置 OPENWEATHER_API_KEY 和坐标")
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                OPENWEATHER_MAP_URL,
                params={
                    "lat": lat,
                    "lon": lon,
                    "appid": settings.openweather_api_key,
                    "units": "metric",
                },
            )
        if response.status_code != 200:
            raise LifeServiceError("openweathermap", response.status_code, response.text)
        data = response.json()
        return {
            "provider": "openweathermap",
            "city": data.get("name") or city or settings.weather_default_city,
            "temperature_c": data.get("main", {}).get("temp"),
            "feels_like_c": data.get("main", {}).get("feels_like"),
            "humidity": data.get("main", {}).get("humidity"),
            "condition": data.get("weather", [{}])[0].get("description"),
            "wind_speed_mps": data.get("wind", {}).get("speed"),
            "raw": data,
        }

    # Default: Open-Meteo (no key required)
    if lat is None or lon is None:
        return {
            "provider": "open_meteo",
            "city": city or settings.weather_default_city,
            "note": "未配置坐标，仅显示城市上下文。",
            "temperature_c": None,
            "condition": None,
        }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(
            OPEN_METEO_FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current_weather": "true",
                "timezone": "auto",
            },
        )
    if response.status_code != 200:
        raise LifeServiceError("open_meteo", response.status_code, response.text)
    data = response.json()
    current = data.get("current_weather", {}) or {}
    return {
        "provider": "open_meteo",
        "city": city or settings.weather_default_city,
        "temperature_c": current.get("temperature"),
        "wind_speed_kmh": current.get("windspeed"),
        "condition_code": current.get("weathercode"),
        "condition": _open_meteo_condition(current.get("weathercode")),
        "as_of_iso": current.get("time"),
        "raw": data,
    }


def _normalize_amap_poi(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize one `place/text` POI row into the map-context place shape.

    `extensions=all` 才会返回 `biz_ext`（评分 `rating` / 人均 `cost` / 营业时间）
    和 `photos`；`extensions=base` 下这些字段全缺，模型只能说"没有评分数据"。
    """
    longitude, latitude = _parse_amap_location(row.get("location"))
    name = _amap_text(row.get("name"))
    address = _amap_text(row.get("address"))
    city = _amap_text(row.get("cityname"))
    district = _amap_text(row.get("adname"))
    label = f"{name}（{address}）" if name and address else (name or address)

    biz_ext = row.get("biz_ext")
    if isinstance(biz_ext, list):
        biz_ext = biz_ext[0] if biz_ext else {}
    if not isinstance(biz_ext, dict):
        biz_ext = {}
    photos = row.get("photos")
    cover = None
    if isinstance(photos, list) and photos and isinstance(photos[0], dict):
        cover = _amap_text(photos[0].get("url"))

    return {
        "label": label,
        "name": name,
        "formatted_address": label,
        "address": address,
        "province": _amap_text(row.get("pname")),
        "city": city,
        "district": district,
        "adcode": _amap_text(row.get("adcode")),
        "latitude": latitude,
        "longitude": longitude,
        "type": _amap_text(row.get("type")),
        "typecode": _amap_text(row.get("typecode")),
        "tag": _amap_text(row.get("tag")),
        # 高德只对部分 POI（景点/餐饮为主）给点评数据；没有就返回 None，
        # 让模型如实说"暂无评分"，不要编 4.8 分。
        "rating": _amap_float(biz_ext.get("rating")),
        "cost": _amap_float(biz_ext.get("cost")),
        "open_time": _amap_text(biz_ext.get("open_time")),
        "cover_url": cover,
    }


def _city_matches(requested: str, resolved: str | None) -> bool:
    """请求的城市名是否与结果里的 cityname 同一个（"上海"vs"上海市"）。"""
    needle = requested.strip().rstrip("市省区县州盟旗")
    haystack = (resolved or "").strip().rstrip("市省区县州盟旗")
    if not needle or not haystack:
        return False
    return needle in haystack or haystack in needle


async def search_amap_places(
    settings: Settings,
    *,
    query: str,
    city: str | None = None,
    limit: int = POI_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """POI keyword search (`place/text`) scoped to a city.

    POI search is what resolves 景点/商场/车站 — geocoding only understands
    postal addresses, which is why 「上海博物馆」「豫园」 used to come back empty.
    With `city` + `citylimit=true` Amap no longer answers with 武汉/福州 的同名点。
    """
    keywords = (query or "").strip()
    if not keywords:
        return []
    params: dict[str, Any] = {
        "keywords": keywords,
        "key": settings.amap_api_key,
        "offset": max(1, min(limit, POI_MAX_LIMIT)),
        "page": 1,
        # all = 带上 biz_ext（评分/人均/营业时间）与 photos；base 会把这些全吞掉。
        "extensions": "all",
    }
    city_scope = (city or "").strip() or None
    if city_scope:
        # citylimit=true 是服务端硬限制：只返回该城市内的 POI。
        params["city"] = city_scope
        params["citylimit"] = "true"
    data = await _amap_get(AMAP_POI_TEXT_URL, params)
    places = [
        _normalize_amap_poi(row)
        for row in (data.get("pois") or [])
        if isinstance(row, dict)
    ]
    places = [place for place in places if place.get("latitude") is not None]
    if city_scope and not _AMAP_ADCODE_RE.match(city_scope):
        # 服务端已按城市过滤；这里只把同城结果排到前面，避免误删合法的跨区结果。
        places.sort(key=lambda place: 0 if _city_matches(city_scope, place.get("city")) else 1)
    return places


async def get_map_context(
    settings: Settings,
    *,
    query: str,
    latitude: float | None = None,
    longitude: float | None = None,
    city: str | None = None,
) -> dict[str, Any]:
    """Return a human-readable map context for a place/address.

    `city` 是会话已确认（或本轮点名）的城市；一旦给出，就作为硬范围：
    POI 搜索带 `citylimit=true`，地理编码带 `city`，不会再全国乱匹配。
    """
    provider = (settings.map_provider or "open_meteo").lower()
    if provider == "amap":
        if not settings.amap_api_key:
            raise LifeServiceError("amap", 0, "需要配置 AMAP_API_KEY")
        city_scope = (city or "").strip() or None
        # 先按 POI 搜（景点/场馆/商场），搜不到再退回地理编码（纯地址）。
        try:
            pois = await search_amap_places(settings, query=query, city=city_scope)
        except LifeServiceError as exc:
            # POI 接口偶发限流/参数错误时，地理编码仍可能拿到纯地址的坐标。
            logger.warning("Amap POI search failed for %r, falling back to geocode: %s", query, exc)
            pois = []
        if pois:
            return {
                "provider": "amap",
                "query": query,
                "city": city_scope,
                "scope": "poi",
                "places": pois,
            }
        rows = await _amap_geocode(settings, query, city=city_scope)
        return {
            "provider": "amap",
            "query": query,
            "city": city_scope,
            "scope": "geocode",
            "places": [
                {
                    "label": row.get("label"),
                    "formatted_address": row.get("formatted_address"),
                    "latitude": row.get("latitude"),
                    "longitude": row.get("longitude"),
                    "adcode": row.get("adcode"),
                    "city": row.get("city"),
                    "district": row.get("district"),
                }
                for row in rows[:3]
            ],
            # 明确告知模型"这次没找到"，避免它把空结果说成"没有这个景点"之外的东西。
            **({} if rows else {"note": f"高德未找到「{query}」的坐标"}),
        }

    if provider == "google_maps":
        if not settings.google_maps_api_key:
            raise LifeServiceError("google_maps", 0, "需要配置 GOOGLE_MAPS_API_KEY")
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                GOOGLE_MAPS_GEOCODE_URL,
                params={
                    "address": query,
                    "key": settings.google_maps_api_key,
                },
            )
        if response.status_code != 200:
            raise LifeServiceError("google_maps", response.status_code, response.text)
        data = response.json()
        rows = data.get("results", []) or []
        if not rows:
            return {"provider": "google_maps", "query": query, "places": []}
        first = rows[0]
        return {
            "provider": "google_maps",
            "query": query,
            "places": [
                {
                    "label": first.get("formatted_address"),
                    "latitude": first.get("geometry", {}).get("location", {}).get("lat"),
                    "longitude": first.get("geometry", {}).get("location", {}).get("lng"),
                }
            ],
        }

    if provider == "nominatim":
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                NOMINATIM_SEARCH_URL,
                params={"q": query, "format": "jsonv2", "limit": 3, "accept-language": "en"},
                headers={"User-Agent": "Chief-of-Staff-LifePilot/1.0 (local dev; personal assistant)"},
            )
        if response.status_code != 200:
            raise LifeServiceError("nominatim", response.status_code, response.text)
        rows = response.json() or []
        return {
            "provider": "nominatim",
            "query": query,
            "places": [
                {
                    "label": row.get("display_name"),
                    "latitude": row.get("lat"),
                    "longitude": row.get("lon"),
                    "type": row.get("type"),
                }
                for row in rows[:3]
            ],
        }

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(
            OPEN_METEO_GEOCODING_URL,
            params={
                "name": query,
                "count": 3,
                "language": "zh",
                "format": "json",
                "timezone": "auto",
            },
        )
    if response.status_code != 200:
        raise LifeServiceError("open_meteo", response.status_code, response.text)
    rows = (response.json() or {}).get("results") or []
    return {
        "provider": "open_meteo",
        "query": query,
        "places": [
            {
                "label": ", ".join(
                    part
                    for part in [row.get("name"), row.get("admin1"), row.get("country")]
                    if part
                ),
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
                "type": row.get("feature_code"),
            }
            for row in rows[:3]
        ],
    }


def _route_coord(latitude: float, longitude: float) -> str:
    """Amap expects "lon,lat" (note the order) for every direction endpoint."""
    return f"{longitude:.6f},{latitude:.6f}"


def humanize_duration(seconds: int | None) -> str:
    if seconds is None:
        return "未知"
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"约 {minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"约 {hours} 小时" + (f" {rest} 分钟" if rest else "")


def _route_steps_walking_or_driving(path: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for step in (path.get("steps") or [])[:_MAX_ROUTE_STEPS]:
        if not isinstance(step, dict):
            continue
        steps.append(
            {
                "instruction": _amap_text(step.get("instruction")) or _amap_text(step.get("road")),
                "road": _amap_text(step.get("road")),
                "distance_m": _amap_int(step.get("distance")),
                "duration_s": _amap_int(step.get("duration")),
            }
        )
    return steps


def _route_steps_transit(transit: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for segment in (transit.get("segments") or [])[:_MAX_ROUTE_STEPS]:
        if not isinstance(segment, dict):
            continue
        walking = segment.get("walking") or {}
        walk_distance = _amap_int(walking.get("distance"))
        if walk_distance:
            steps.append(
                {
                    "instruction": f"步行约 {walk_distance} 米",
                    "distance_m": walk_distance,
                    "duration_s": _amap_int(walking.get("duration")),
                }
            )
        bus_lines = (segment.get("bus") or {}).get("buslines") or []
        if bus_lines and isinstance(bus_lines[0], dict):
            line = bus_lines[0]
            line_name = _amap_text(line.get("name")) or "公交"
            departure = line.get("departure_stop")
            departure_name = _amap_text(departure.get("name")) if isinstance(departure, dict) else None
            steps.append(
                {
                    "instruction": f"乘坐 {line_name}"
                    + (f"（{departure_name} 上车）" if departure_name else ""),
                    "distance_m": _amap_int(line.get("distance")),
                    "duration_s": _amap_int(line.get("duration")),
                    "line": line_name,
                }
            )
    return steps


async def get_route_planning(
    settings: Settings,
    *,
    origin_latitude: float,
    origin_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    mode: str = "walking",
    city: str | None = None,
) -> dict[str, Any]:
    """Plan a route between two coordinates and return distance + duration.

    `mode`: walking / transit / driving. 公交/地铁路线必须带城市，缺省时用起点
    逆地理编码得到的城市补上——否则高德会报 INSUFFICIENT_PRIVILEGES 或给出
    其它城市的线路。
    """
    provider = (settings.map_provider or "open_meteo").lower()
    if provider != "amap":
        raise LifeServiceError("amap", 0, "路径规划目前仅支持高德地图（MAP_PROVIDER=amap）")
    if not settings.amap_api_key:
        raise LifeServiceError("amap", 0, "需要配置 AMAP_API_KEY")

    route_mode = (mode or "walking").strip().lower()
    if route_mode not in ROUTE_MODES:
        raise LifeServiceError(
            "amap", 0, f"不支持的出行方式「{mode}」，可选：{'/'.join(ROUTE_MODES)}", fatal=True,
        )

    origin_text = _route_coord(origin_latitude, origin_longitude)
    destination_text = _route_coord(destination_latitude, destination_longitude)
    params: dict[str, Any] = {
        "origin": origin_text,
        "destination": destination_text,
        "key": settings.amap_api_key,
    }
    if route_mode == "walking":
        url = AMAP_DIRECTION_WALKING_URL
    elif route_mode == "driving":
        url = AMAP_DIRECTION_DRIVING_URL
    else:
        url = AMAP_DIRECTION_TRANSIT_URL
        city_scope = (city or "").strip()
        if not city_scope:
            regeo = await _amap_regeo(settings, origin_latitude, origin_longitude)
            city_scope = (regeo or {}).get("city") or (regeo or {}).get("province") or ""
        if not city_scope:
            raise LifeServiceError(
                "amap", 0, "公交/地铁路线需要城市信息，请传入 city（如「上海」）", fatal=True,
            )
        params["city"] = city_scope
        params["cityd"] = city_scope

    data = await _amap_get(url, params)
    route = data.get("route") or {}
    if route_mode == "transit":
        transits = route.get("transits") or []
        if not transits or not isinstance(transits[0], dict):
            raise LifeServiceError("amap", 0, "未找到公交/地铁线路")
        primary = transits[0]
        distance_m = _amap_int(primary.get("distance"))
        duration_s = _amap_int(primary.get("duration"))
        steps = _route_steps_transit(primary)
    else:
        paths = route.get("paths") or []
        if not paths or not isinstance(paths[0], dict):
            raise LifeServiceError("amap", 0, "未找到可用路线")
        primary = paths[0]
        distance_m = _amap_int(primary.get("distance"))
        duration_s = _amap_int(primary.get("duration"))
        steps = _route_steps_walking_or_driving(primary)

    label = _ROUTE_MODE_LABEL[route_mode]
    distance_text = (
        f"{distance_m / 1000:.1f} 公里" if distance_m and distance_m >= 1000
        else (f"{distance_m} 米" if distance_m is not None else "未知距离")
    )
    return {
        "provider": "amap",
        "mode": route_mode,
        "origin": {"latitude": origin_latitude, "longitude": origin_longitude},
        "destination": {"latitude": destination_latitude, "longitude": destination_longitude},
        "distance_m": distance_m,
        "duration_s": duration_s,
        "duration_text": humanize_duration(duration_s),
        "summary": f"{label}{distance_text}，{humanize_duration(duration_s)}",
        "steps": steps,
    }


def _open_meteo_condition(code: Any) -> str:
    mapping = {
        0: "晴",
        1: "大部晴朗",
        2: "局部多云",
        3: "阴",
        45: "雾",
        48: "冻雾",
        51: "小毛毛雨",
        53: "毛毛雨",
        55: "浓毛毛雨",
        61: "小雨",
        63: "雨",
        65: "大雨",
        71: "小雪",
        73: "雪",
        75: "大雪",
        80: "小阵雨",
        81: "阵雨",
        82: "强烈阵雨",
        95: "雷暴",
        96: "伴冰雹雷暴",
        99: "伴强冰雹雷暴",
    }
    try:
        return mapping.get(int(code), "未知")
    except (TypeError, ValueError):
        return "未知"