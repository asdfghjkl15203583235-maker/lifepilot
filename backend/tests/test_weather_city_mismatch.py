"""真实 badcase：问上海天气，却返回广东佛山的天气。

用户原话："帮我查一下上海的天气，并帮我制定出行计划，两天"

两个独立缺陷叠加：
1. `_parse_city` 把整句去掉标点后当城市 →「上海并制定出行计划两天」；
2. 高德对这个字符串解析失败（或模糊撞到「广西…龙州县出行」），
   `_resolve_amap_location` 却静默回落到默认城市，把别处的数据当成成功答案。
"""

import unittest
from unittest.mock import patch

from agents.life_agent_node import _parse_city
from config import Settings
from services import life_service_client as client

BAD_CASE = "帮我查一下上海的天气，并帮我制定出行计划，两天"

# 高德对「出行计划」的模糊结果：地名里根本没有上海。
NANHAI_ROW = {
    "formatted_address": "广东省佛山市南海区桂城街道",
    "province": "广东省",
    "city": "佛山市",
    "district": "南海区",
    "adcode": "440605",
    "location": "113.14,23.03",
}

WUHAN_ROW = {
    "formatted_address": "上海市黄浦区人民广场",
    "province": "上海市",
    "city": "上海市",
    "district": "黄浦区",
    "adcode": "310000",
    "location": "121.473701,31.230416",
}


def _settings() -> Settings:
    return Settings(
        amap_api_key="k",
        weather_provider="amap",
        weather_default_city="Shanghai",
    )


class CityParsingBadCaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_city_is_not_the_whole_sentence(self) -> None:
        self.assertEqual(await _parse_city(BAD_CASE), "上海")

    async def test_common_phrasings(self) -> None:
        cases = {
            "查询上海天气": "上海",
            "上海明天天气": "上海",
            "北京天气怎么样": "北京",
            "帮我查一下明天上海的天气": "上海",
            "山西榆次明天天气怎么样？": "山西榆次",
            "weather in Paris": "Paris",
        }
        for text, expected in cases.items():
            self.assertEqual(await _parse_city(text), expected, text)

    async def test_no_city_returns_none(self) -> None:
        self.assertIsNone(await _parse_city("今天天气怎么样"))

    async def test_planner_rewritten_intents(self) -> None:
        """规划器会用自己的话重写 intent，不是用户原句。

        实测规划器把「帮我查一下上海的天气，并帮我制定出行计划，两天」
        改写成「查询上海近两天的天气情况」——旧正则只剥「两天」，
        留下「上海近」。这里覆盖规划器的真实措辞。
        """
        cases = {
            "查询上海近两天的天气情况": "上海",
            "根据上海天气制定两天出行计划": "上海",
            "结合上海天气安排行程": "上海",
            "上海最近一周的天气": "上海",
            "查询北京明天天气": "北京",
        }
        for text, expected in cases.items():
            self.assertEqual(await _parse_city(text), expected, text)


class WrongCityGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_fuzzy_match_to_unrelated_place_is_rejected(self) -> None:
        """高德把「出行计划」匹配到广西，必须报错而不是拿它当答案。"""
        with patch.object(client, "_amap_geocode", return_value=[NANHAI_ROW]):
            with self.assertRaises(client.LifeServiceError) as ctx:
                await client._resolve_amap_location(
                    _settings(), latitude=None, longitude=None, city="出行计划"
                )
        self.assertIn("拒绝", str(ctx.exception))

    async def test_named_city_never_falls_back_to_default(self) -> None:
        """用户点名了上海，解析失败也只能失败，不能回落到默认城市。"""
        with patch.object(client, "_amap_geocode", side_effect=client.LifeServiceError("amap", 0, "boom")):
            with self.assertRaises(client.LifeServiceError):
                await client._resolve_amap_location(
                    _settings(), latitude=None, longitude=None, city="上海"
                )

    async def test_completed_place_name_still_matches(self) -> None:
        """「山西榆次」→「山西省晋中市榆次区」是正确补全，不能被误拒。"""
        row = {
            "formatted_address": "山西省晋中市榆次区",
            "province": "山西省",
            "city": "晋中市",
            "district": "榆次区",
            "adcode": "140702",
            "location": "112.75,37.69",
        }
        with patch.object(client, "_amap_geocode", return_value=[row]):
            resolved = await client._resolve_amap_location(
                _settings(), latitude=None, longitude=None, city="山西榆次"
            )
        self.assertEqual(resolved["adcode"], "140702")

    async def test_correct_city_still_resolves(self) -> None:
        with patch.object(client, "_amap_geocode", return_value=[WUHAN_ROW]):
            resolved = await client._resolve_amap_location(
                _settings(), latitude=None, longitude=None, city="上海"
            )
        self.assertEqual(resolved["adcode"], "310000")


if __name__ == "__main__":
    unittest.main()
