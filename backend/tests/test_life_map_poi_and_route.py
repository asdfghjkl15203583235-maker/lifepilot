"""高德地图：POI 城市限定 + 路径规划。

真实 badcase（上海两日游）：
  - 「外滩」不限城市 → 匹配到武汉/福州/上海浦东；
  - 「上海博物馆」「豫园」走地理编码 → 连坐标都查不到；
  - 模型只能说「我没有路线规划能力」。

这里锁住三件事：POI 搜索必须带 city/citylimit、没命中才退回地理编码、
路径规划能返回距离与耗时。
"""

import unittest
from unittest.mock import MagicMock, patch

from config import Settings
from services import life_service_client as client

POI_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "count": "2",
    "pois": [
        {
            "name": "外滩",
            "address": "中山东一路",
            "cityname": "上海市",
            "adname": "黄浦区",
            "pname": "上海市",
            "adcode": "310101",
            "location": "121.490317,31.239666",
            "type": "风景名胜;风景名胜",
            "typecode": "110000",
            "tag": "夜景,江景",
            # extensions=all 才会带回 biz_ext；base 模式下这些字段全缺。
            "biz_ext": {
                "rating": "4.7",
                "cost": "0.00",
                "open_time": "全天开放",
            },
            "photos": [{"url": "https://store.is.autonavi.com/example.jpg"}],
        },
        {
            "name": "外滩观光隧道",
            "address": "南京东路",
            "cityname": "上海市",
            "adname": "黄浦区",
            "pname": "上海市",
            "adcode": "310101",
            "location": "121.497000,31.238000",
            "type": "风景名胜",
            "typecode": "110000",
            "biz_ext": [],
        },
    ],
}

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
        }
    ],
}

WALKING_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "route": {
        "paths": [
            {
                "distance": "1200",
                "duration": "900",
                "steps": [
                    {
                        "instruction": "沿中山东一路步行 600 米",
                        "road": "中山东一路",
                        "distance": "600",
                        "duration": "450",
                    }
                ],
            }
        ]
    },
}

TRANSIT_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "route": {
        "transits": [
            {
                "distance": "5400",
                "duration": "1800",
                "segments": [
                    {"walking": {"distance": "400", "duration": "300"}},
                    {
                        "bus": {
                            "buslines": [
                                {
                                    "name": "地铁2号线",
                                    "distance": "5000",
                                    "duration": "1500",
                                    "departure_stop": {"name": "南京东路"},
                                }
                            ]
                        }
                    },
                ],
            }
        ]
    },
}

REGEO_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "regeocode": {
        "formatted_address": "上海市黄浦区",
        "addressComponent": {
            "province": "上海市",
            "city": "上海市",
            "district": "黄浦区",
            "adcode": "310101",
        },
    },
}


def _settings(**overrides) -> Settings:
    # 小写字段名：大写 init kwargs 会被 pydantic-settings 静默忽略。
    values = {"map_provider": "amap", "amap_api_key": "test-key"}
    values.update(overrides)
    return Settings(**values)


class MapCityScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        client._AMAP_GEOCODE_CACHE.clear()

    def _patch(self, calls: list[tuple[str, dict]], *, poi_payload=None) -> None:
        async def fake_amap_get(url: str, params: dict) -> dict:
            calls.append((url, dict(params)))
            if url == client.AMAP_POI_TEXT_URL:
                return poi_payload if poi_payload is not None else POI_PAYLOAD
            if url == client.AMAP_GEOCODE_URL:
                return GEO_PAYLOAD
            return WALKING_PAYLOAD

        patcher = patch.object(client, "_amap_get", new=fake_amap_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_poi_search_sends_city_and_citylimit(self) -> None:
        calls: list[tuple[str, dict]] = []
        self._patch(calls)

        result = await client.get_map_context(_settings(), query="外滩", city="上海")

        poi_calls = [c for c in calls if c[0] == client.AMAP_POI_TEXT_URL]
        self.assertEqual(len(poi_calls), 1)
        params = poi_calls[0][1]
        self.assertEqual(params["city"], "上海")
        self.assertEqual(params["citylimit"], "true", "必须服务端硬限定城市，否则会全国乱匹配")
        self.assertEqual(params["keywords"], "外滩")
        self.assertEqual(result["scope"], "poi")
        self.assertEqual(params["extensions"], "all", "评分/人均在 biz_ext 里，必须用 all")
        self.assertEqual(result["places"][0]["name"], "外滩")
        self.assertEqual(result["places"][0]["city"], "上海市")
        self.assertAlmostEqual(result["places"][0]["latitude"], 31.239666)

        # 评分 / 人均 / 营业时间：能拿到就透出，模型才有真实数据可引用。
        place = result["places"][0]
        self.assertEqual(place["rating"], 4.7)
        self.assertEqual(place["cost"], 0.0)
        self.assertEqual(place["open_time"], "全天开放")
        self.assertEqual(place["tag"], "夜景,江景")
        self.assertIn("autonavi", place["cover_url"])

        # biz_ext 为空列表时不能崩，也不能编造评分。
        self.assertIsNone(result["places"][1]["rating"])
        # 命中 POI 就不再走地理编码
        self.assertEqual([c for c in calls if c[0] == client.AMAP_GEOCODE_URL], [])

    async def test_geocode_fallback_still_scoped_to_city(self) -> None:
        calls: list[tuple[str, dict]] = []
        self._patch(calls, poi_payload={"status": "1", "info": "OK", "pois": []})

        result = await client.get_map_context(_settings(), query="人民广场", city="上海")

        geocode_calls = [c for c in calls if c[0] == client.AMAP_GEOCODE_URL]
        self.assertEqual(len(geocode_calls), 1)
        self.assertEqual(geocode_calls[0][1]["city"], "上海")
        self.assertEqual(result["scope"], "geocode")

    async def test_geocode_cache_is_per_city(self) -> None:
        calls: list[tuple[str, dict]] = []
        self._patch(calls, poi_payload={"status": "1", "info": "OK", "pois": []})

        await client._amap_geocode(_settings(), "人民广场", city="上海")
        await client._amap_geocode(_settings(), "人民广场", city="南京")

        geocode_calls = [c for c in calls if c[0] == client.AMAP_GEOCODE_URL]
        self.assertEqual(len(geocode_calls), 2, "不同城市的同名地址不能共用缓存")

    async def test_missing_places_report_note(self) -> None:
        async def fake_amap_get(url: str, params: dict) -> dict:
            if url == client.AMAP_POI_TEXT_URL:
                return {"status": "1", "info": "OK", "pois": []}
            return {"status": "1", "info": "OK", "geocodes": []}

        with patch.object(client, "_amap_get", new=fake_amap_get):
            result = await client.get_map_context(_settings(), query="火星博物馆", city="上海")

        self.assertEqual(result["places"], [])
        self.assertIn("未找到", result["note"])


class RoutePlanningTests(unittest.IsolatedAsyncioTestCase):
    def _patch(self, calls: list[tuple[str, dict]], payload: dict) -> None:
        async def fake_amap_get(url: str, params: dict) -> dict:
            calls.append((url, dict(params)))
            return payload

        patcher = patch.object(client, "_amap_get", new=fake_amap_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_walking_route_returns_distance_and_duration(self) -> None:
        calls: list[tuple[str, dict]] = []
        self._patch(calls, WALKING_PAYLOAD)

        result = await client.get_route_planning(
            _settings(),
            origin_latitude=31.2397,
            origin_longitude=121.4903,
            destination_latitude=31.2323,
            destination_longitude=121.4750,
            mode="walking",
        )

        self.assertEqual(calls[0][0], client.AMAP_DIRECTION_WALKING_URL)
        # 高德要 "lon,lat"
        self.assertEqual(calls[0][1]["origin"], "121.490300,31.239700")
        self.assertEqual(result["distance_m"], 1200)
        self.assertEqual(result["duration_s"], 900)
        self.assertEqual(result["duration_text"], "约 15 分钟")
        self.assertIn("步行", result["summary"])
        self.assertEqual(result["steps"][0]["road"], "中山东一路")

    async def test_transit_route_requires_city_and_reports_lines(self) -> None:
        calls: list[tuple[str, dict]] = []
        self._patch(calls, TRANSIT_PAYLOAD)

        result = await client.get_route_planning(
            _settings(),
            origin_latitude=31.2397,
            origin_longitude=121.4903,
            destination_latitude=31.2323,
            destination_longitude=121.4750,
            mode="transit",
            city="上海",
        )

        self.assertEqual(calls[0][0], client.AMAP_DIRECTION_TRANSIT_URL)
        self.assertEqual(calls[0][1]["city"], "上海")
        self.assertEqual(result["duration_text"], "约 30 分钟")
        self.assertIn("地铁2号线", result["steps"][1]["instruction"])

    async def test_transit_without_city_uses_origin_city(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def fake_amap_get(url: str, params: dict) -> dict:
            calls.append((url, dict(params)))
            if url == client.AMAP_REGEO_URL:
                return REGEO_PAYLOAD
            return TRANSIT_PAYLOAD

        with patch.object(client, "_amap_get", new=fake_amap_get):
            result = await client.get_route_planning(
                _settings(),
                origin_latitude=31.2397,
                origin_longitude=121.4903,
                destination_latitude=31.2323,
                destination_longitude=121.4750,
                mode="transit",
            )

        transit_calls = [c for c in calls if c[0] == client.AMAP_DIRECTION_TRANSIT_URL]
        self.assertEqual(transit_calls[0][1]["city"], "上海市")
        self.assertEqual(result["mode"], "transit")

    async def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(client.LifeServiceError):
            await client.get_route_planning(
                _settings(),
                origin_latitude=31.2,
                origin_longitude=121.4,
                destination_latitude=31.3,
                destination_longitude=121.5,
                mode="teleport",
            )

    async def test_empty_path_raises(self) -> None:
        self._patch([], {"status": "1", "info": "OK", "route": {"paths": []}})
        with self.assertRaises(client.LifeServiceError):
            await client.get_route_planning(
                _settings(),
                origin_latitude=31.2,
                origin_longitude=121.4,
                destination_latitude=31.3,
                destination_longitude=121.5,
                mode="walking",
            )


class LifeToolWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_map_tool_passes_session_city(self) -> None:
        from unittest.mock import AsyncMock

        from agents.tools.life_tool import LifeTool

        settings = _settings()
        tool = LifeTool(None, settings, default_city="上海")
        with patch(
            "agents.tools.life_tool.get_map_context",
            AsyncMock(return_value={"places": []}),
        ) as map_call:
            await tool._map("外滩")
        map_call.assert_awaited_once_with(settings, query="外滩", city="上海")

    async def test_route_tool_passes_mode_and_city(self) -> None:
        from unittest.mock import AsyncMock

        from agents.tools.life_tool import LifeTool

        settings = _settings()
        tool = LifeTool(None, settings, default_city="上海")
        with patch(
            "agents.tools.life_tool.get_route_planning",
            AsyncMock(return_value={"summary": "步行 1.2 公里，约 15 分钟"}),
        ) as route_call:
            output = await tool._route(31.2397, 121.4903, 31.2323, 121.4750, "walking")
        self.assertIn("15 分钟", output)
        route_call.assert_awaited_once_with(
            settings,
            origin_latitude=31.2397,
            origin_longitude=121.4903,
            destination_latitude=31.2323,
            destination_longitude=121.4750,
            mode="walking",
            city="上海",
        )


class ItineraryTaskToolTests(unittest.IsolatedAsyncioTestCase):
    """行程能真正落库：life 代理也需要 create_task（此前只有 direct 路由有）。"""

    def _settings(self) -> Settings:
        return Settings(map_provider="amap", amap_api_key="test-key")

    def test_create_task_tool_requires_session_and_user(self) -> None:
        from agents.tools.life_tool import LifeTool

        without_session = LifeTool(None, self._settings(), user_id="user-1")
        self.assertNotIn("create_task", [t.name for t in without_session.tools()])

        with_session = LifeTool(MagicMock(), self._settings(), user_id="user-1")
        self.assertIn("create_task", [t.name for t in with_session.tools()])

    async def test_create_task_delegates_to_task_action_tool(self) -> None:
        from unittest.mock import AsyncMock

        from agents.tools.life_tool import LifeTool

        tool = LifeTool(MagicMock(), self._settings(), user_id="user-1")
        created = {"id": "task-1", "title": "上海两日游"}
        with patch(
            "agents.tools.task_action_tools.create_task_for_user",
            AsyncMock(return_value=type("Row", (), {"id": "task-1", "title": "上海两日游"})()),
        ) as create_call:
            output = await tool._create_task(
                "上海两日游",
                due_at="2026-11-07T09:00:00+08:00",
                description="D1 外滩 → 豫园",
            )

        self.assertIn("created", output)
        self.assertIn("上海两日游", output)
        create_call.assert_awaited_once()

    async def test_create_task_without_session_reports_error(self) -> None:
        from agents.tools.life_tool import LifeTool

        tool = LifeTool(None, self._settings(), user_id=None)
        output = await tool._create_task("上海两日游")
        self.assertIn("error", output)


if __name__ == "__main__":
    unittest.main()
