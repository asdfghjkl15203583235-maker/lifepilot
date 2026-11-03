"""任务一：高德天气预报（extensions=all + 按日期匹配 + 缺失兜底）。

真实 badcase：用户问"明天天气怎么样"，后端只请求 `extensions=base`（仅实况），
永远拿不到明天的预报，模型便把此刻实况说成明天的天气。

修复三点：
1. 天气查询统一 `extensions=all`（实况 + 逐日预报）；
2. 从 `forecasts` 中按用户询问的日期匹配，取 daytemp/nighttemp/dayweather；
3. `forecasts` 无对应日期时明确"暂无该日预报"，实况只能作为参考且必须标注。
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from config import Settings
from services import life_service_client as client


GEO_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "geocodes": [
        {
            "formatted_address": "上海市黄浦区",
            "province": "上海市",
            "city": "上海市",
            "district": "黄浦区",
            "adcode": "310000",
            "location": "121.473701,31.230416",
        }
    ],
}

LIVE_BLOCK = {
    "province": "上海",
    "city": "上海市",
    "adcode": "310000",
    "weather": "晴",
    "temperature": "23",
    "humidity": "45",
    "winddirection": "东",
    "windpower": "≤3",
    "reporttime": "2026-10-30 22:00:00",
}


def _forecast_payload() -> dict:
    """高德 extensions=all 的真实返回形状：forecasts[0].casts[]。"""
    tz = ZoneInfo("Asia/Shanghai")
    today = datetime.now(tz).date()
    casts = [
        {
            "date": (today + timedelta(days=offset)).strftime("%Y-%m-%d"),
            "week": str((today + timedelta(days=offset)).weekday() + 1),
            "dayweather": weather_day,
            "nightweather": weather_night,
            "daytemp": day_temp,
            "nighttemp": night_temp,
            "daywind": "东风",
            "daypower": "≤3",
        }
        for offset, (weather_day, weather_night, day_temp, night_temp) in enumerate(
            [
                ("多云", "晴", "24", "16"),
                ("大雨", "中雨", "19", "12"),
                ("晴", "晴", "26", "17"),
            ]
        )
    ]
    return {
        "status": "1",
        "info": "OK",
        "lives": [LIVE_BLOCK],
        "forecasts": [{"city": "上海市", "adcode": "310000", "casts": casts}],
    }


def _settings() -> Settings:
    # 小写字段名：大写 init kwargs 会被 pydantic-settings 静默忽略（以前靠 .env 掩盖）。
    return Settings(
        weather_provider="amap",
        amap_api_key="test-key",
        weather_default_city="上海",
    )


class AmapWeatherExtensionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        client._AMAP_GEOCODE_CACHE.clear()

    async def test_weather_request_uses_extensions_all(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def fake_amap_get(url: str, params: dict) -> dict:
            calls.append((url, dict(params)))
            return GEO_PAYLOAD if url == client.AMAP_GEOCODE_URL else _forecast_payload()

        with patch.object(client, "_amap_get", new=fake_amap_get):
            await client.get_weather(_settings(), city="上海")

        weather_call = next(c for c in calls if c[0] == client.AMAP_WEATHER_URL)
        self.assertEqual(
            weather_call[1]["extensions"],
            client.AMAP_WEATHER_EXTENSIONS_ALL,
            "天气查询必须请求 extensions=all，否则永远拿不到逐日预报",
        )

    async def test_forecasts_are_normalized_for_every_day(self) -> None:
        async def fake_amap_get(url: str, params: dict) -> dict:
            return GEO_PAYLOAD if url == client.AMAP_GEOCODE_URL else _forecast_payload()

        with patch.object(client, "_amap_get", new=fake_amap_get):
            payload = await client.get_weather(_settings(), city="上海")

        casts = payload["forecasts"]
        self.assertEqual(len(casts), 3)
        self.assertEqual(casts[1]["day_weather"], "大雨")
        self.assertEqual(casts[1]["night_temp_c"], 12)
        self.assertTrue(casts[0]["date"].count("-") == 2)


class WeatherAnswerDateMatchingTests(unittest.TestCase):
    """`_weather_answer` 是纯函数：日期匹配与"暂无该日预报"的确定性守卫。"""

    def setUp(self) -> None:
        from services.life_service_client import _normalize_amap_casts

        self.payload = {
            "provider": "amap",
            "city": "上海市",
            "condition": "晴",
            "temperature_c": 23,
            "forecasts": _normalize_amap_casts(_forecast_payload()["forecasts"][0]),
        }

    def _answer(self, intent: str) -> str:
        from agents.life_agent_node import _weather_answer

        return _weather_answer(
            self.payload,
            intent,
            tz_name="Asia/Shanghai",
            city_label="上海市",
        )

    def test_tomorrow_uses_forecast_not_realtime(self) -> None:
        text = self._answer("上海市明天天气怎么样")
        self.assertIn("大雨", text)
        self.assertIn("19", text)
        self.assertIn("12", text)
        self.assertNotIn("当前实时天气", text)

    def test_today_uses_first_cast(self) -> None:
        text = self._answer("上海市今天天气")
        self.assertIn("多云", text)
        self.assertIn("24", text)

    def test_missing_day_says_no_forecast_and_marks_realtime_as_reference(self) -> None:
        # 高德只给未来 3 天；问 3 天后必须明说没有，而不是拿实况冒充。
        text = self._answer("上海市大后天天气")
        self.assertIn("暂无该日预报", text)
        self.assertIn("这是此刻实况", text)
        self.assertNotIn("2026-", text)

    def test_generic_future_question_lists_forecasts(self) -> None:
        text = self._answer("上海市未来几天天气")
        self.assertIn("多云", text)
        self.assertIn("大雨", text)

    def test_realtime_question_returns_current_condition(self) -> None:
        text = self._answer("上海市现在天气")
        self.assertIn("晴", text)


if __name__ == "__main__":
    unittest.main()
