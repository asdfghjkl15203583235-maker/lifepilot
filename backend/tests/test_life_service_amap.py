"""Amap (高德) provider tests — city → adcode resolution, caching, map geocoding.

No network: `_amap_get` is patched with canned Amap envelopes.
"""

import unittest
from unittest.mock import patch

from config import Settings
from services import life_service_client as client


GEO_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "geocodes": [
        {
            "formatted_address": "上海市黄浦区人民广场",
            "province": "上海市",
            "city": [],
            "district": "黄浦区",
            "adcode": "310000",
            "location": "121.473701,31.230416",
        },
        {
            "formatted_address": "上海市浦东新区",
            "province": "上海市",
            "city": [],
            "district": "浦东新区",
            "adcode": "310115",
            "location": "121.544379,31.221517",
        },
    ],
}

WEATHER_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "lives": [
        {
            "province": "上海",
            "city": "上海市",
            "adcode": "310000",
            "weather": "晴",
            "temperature": "23",
            "humidity": "45",
            "winddirection": "东",
            "windpower": "≤3",
            "reporttime": "2026-10-26 22:00:00",
        }
    ],
}

REGEO_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "regeocode": {
        "formatted_address": "上海市徐汇区某路",
        "addressComponent": {
            "province": "上海市",
            "city": [],
            "district": "徐汇区",
            "adcode": "310104",
        },
    },
}


def _settings(**overrides) -> Settings:
    # 必须用小写字段名：pydantic-settings 的 init kwargs 大小写不敏感只对 env 生效，
    # 传 AMAP_API_KEY 会被当成未知字段静默忽略（曾经靠 .env 里的真实 key 掩盖了这个坑）。
    values = {
        "weather_provider": "amap",
        "map_provider": "amap",
        "amap_api_key": "test-key",
        "weather_default_city": "上海",
    }
    values.update(overrides)
    return Settings(**values)


class AmapProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        client._AMAP_GEOCODE_CACHE.clear()

    def _patch_amap(self, calls: list[tuple[str, dict]]) -> None:
        async def fake_amap_get(url: str, params: dict) -> dict:
            calls.append((url, dict(params)))
            if url == client.AMAP_GEOCODE_URL:
                return GEO_PAYLOAD
            if url == client.AMAP_REGEO_URL:
                return REGEO_PAYLOAD
            return WEATHER_PAYLOAD

        patcher = patch.object(client, "_amap_get", new=fake_amap_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_city_name_resolves_to_adcode_and_is_cached(self) -> None:
        calls: list[tuple[str, dict]] = []
        self._patch_amap(calls)

        first = await client.get_weather(_settings(), city="上海")
        second = await client.get_weather(_settings(), city="上海")

        self.assertEqual(first["provider"], "amap")
        self.assertEqual(first["adcode"], "310000")
        self.assertEqual(first["temperature_c"], 23)
        self.assertEqual(first["humidity"], 45)
        self.assertEqual(first["condition"], "晴")
        self.assertEqual(second["adcode"], "310000")

        geocode_calls = [c for c in calls if c[0] == client.AMAP_GEOCODE_URL]
        weather_calls = [c for c in calls if c[0] == client.AMAP_WEATHER_URL]
        self.assertEqual(len(geocode_calls), 1, "adcode 应命中缓存，只做一次地理编码")
        self.assertEqual(weather_calls[0][1]["city"], "310000")
        self.assertEqual(len(weather_calls), 2)

    async def test_adcode_input_skips_geocoding(self) -> None:
        calls: list[tuple[str, dict]] = []
        self._patch_amap(calls)

        result = await client.get_weather(_settings(), city="310000")

        self.assertEqual(result["adcode"], "310000")
        self.assertEqual(
            [c for c in calls if c[0] == client.AMAP_GEOCODE_URL],
            [],
            "传入 adcode 时不应再调用地理编码",
        )

    async def test_coordinates_use_regeo(self) -> None:
        calls: list[tuple[str, dict]] = []
        self._patch_amap(calls)

        result = await client.get_weather(_settings(), latitude=31.19, longitude=121.40)

        self.assertEqual(result["adcode"], "310104")
        regeo_calls = [c for c in calls if c[0] == client.AMAP_REGEO_URL]
        self.assertEqual(len(regeo_calls), 1)
        self.assertEqual(regeo_calls[0][1]["location"], "121.4,31.19")

    async def test_map_context_returns_geocoded_places(self) -> None:
        calls: list[tuple[str, dict]] = []
        self._patch_amap(calls)

        result = await client.get_map_context(_settings(), query="人民广场")

        self.assertEqual(result["provider"], "amap")
        self.assertEqual(len(result["places"]), 2)
        place = result["places"][0]
        self.assertEqual(place["adcode"], "310000")
        self.assertAlmostEqual(place["longitude"], 121.473701)
        self.assertAlmostEqual(place["latitude"], 31.230416)
        self.assertIn("上海", place["label"])

    async def test_missing_api_key_raises(self) -> None:
        settings = _settings(amap_api_key=None)
        with self.assertRaises(client.LifeServiceError):
            await client.get_weather(settings, city="上海")

    async def test_unresolvable_city_raises(self) -> None:
        async def fake_amap_get(url: str, params: dict) -> dict:
            if url == client.AMAP_GEOCODE_URL:
                return {"status": "1", "info": "OK", "geocodes": []}
            return WEATHER_PAYLOAD

        with patch.object(client, "_amap_get", new=fake_amap_get):
            with self.assertRaises(client.LifeServiceError):
                # 明确指定了城市但解析不出来 → 必须报错，不能回落到默认城市。
                await client.get_weather(_settings(), city="不存在的地方")


if __name__ == "__main__":
    unittest.main()
