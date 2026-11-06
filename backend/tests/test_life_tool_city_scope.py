"""LifeTool 同轮城市一致性回归测试。

真实 badcase（2026-11-04 截图）：用户问"看看今天的天气，并找一个适合专注的好去处"，
天气解析出北京，地点查询却回落成配置默认城市上海（返回上海图书馆东馆），
回复里两个城市互相打架，模型只好把"这个分支没有…推荐工具把地点当成上海…没有采用"
这类内部纠结原样念给用户。

修复：LifeTool 实例按请求创建，新增 `_turn_city` 把本轮第一次确定的城市钉住，
后续同轮天气/地点/路线调用一律复用；再配合 CITY_CONSISTENCY_RULE 提示词。
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.common import CITY_CONSISTENCY_RULE
from agents.tools.life_tool import LifeTool


def _settings() -> MagicMock:
    return MagicMock()


class TurnCityScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_weather_resolved_city_is_reused_by_map(self) -> None:
        """天气解析出北京后，同轮不带城市的地点查询必须也查北京。"""
        box = LifeTool(None, _settings())  # 无会话目的地

        with patch(
            "agents.tools.life_tool.get_weather",
            AsyncMock(return_value={"city": "北京", "temperature_c": 13}),
        ) as fake_weather, patch(
            "agents.tools.life_tool.get_map_context",
            AsyncMock(return_value='{"places": []}'),
        ) as fake_map:
            await box._weather()
            await box._map(query="图书馆")

        fake_weather.assert_awaited_once()
        self.assertEqual(fake_map.await_args.kwargs["city"], "北京")

    async def test_map_city_is_reused_by_weather(self) -> None:
        """先查地点钉住城市后，同轮不带城市的天气也必须同城。"""
        settings = _settings()
        box = LifeTool(None, settings)

        with patch(
            "agents.tools.life_tool.get_map_context",
            AsyncMock(return_value='{"places": []}'),
        ), patch(
            "agents.tools.life_tool.get_weather",
            AsyncMock(return_value={"city": "北京"}),
        ) as fake_weather:
            await box._map(query="图书馆", city="北京")
            await box._weather()

        fake_weather.assert_awaited_once_with(settings, city="北京")

    async def test_scope_city_priority(self) -> None:
        """本轮点名 > 会话已确认目的地 > 本轮已确定城市。"""
        box = LifeTool(None, _settings(), default_city="成都")
        self.assertEqual(box._scope_city(None), "成都")

        pinned = LifeTool(None, _settings())
        pinned._pin_turn_city("北京")
        self.assertEqual(pinned._scope_city(None), "北京")
        self.assertEqual(pinned._scope_city("上海"), "上海")

    async def test_default_city_beats_pinned_turn_city(self) -> None:
        """会话目的地（跨轮确认过）比本轮天气解析出的城市更可信。"""
        box = LifeTool(None, _settings(), default_city="成都")
        box._pin_turn_city("北京")
        self.assertEqual(box._scope_city(None), "成都")

    def test_prompt_rule_forbids_internal_narration(self) -> None:
        """提示词必须挡住“这个分支/推荐工具/没有采用”这类内部独白。"""
        for phrase in ("城市一致性", "实际所在城市", "内部决策", "这个分支"):
            self.assertIn(phrase, CITY_CONSISTENCY_RULE)


if __name__ == "__main__":
    unittest.main()
