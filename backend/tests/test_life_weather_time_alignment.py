"""模块三：天气数据的时间对齐（回归守卫）。

真实 badcase：用户问"明天天气怎么样"，高德实时接口只返回**当前**实时天气，
模型却把它当成明天的预报敷衍用户——"明天多云转晴"其实是此刻的实况。

修复：Life Agent 必须能识别"用户问的是未来时间"，当工具只返回实时数据时，
明确标注"这是当前实时天气，暂无明日预报"，严禁把实时当预报。
"""

import json
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage

from agents.life_agent_node import _asks_future_time, life_agent_node
from agents.state import AgentState


class AsksFutureTimeTests(unittest.TestCase):
    def test_future_words_detected(self) -> None:
        for text in ("明天天气怎么样", "后天呢", "下周天气", "未来几天天气", "明早会下雨吗"):
            self.assertTrue(_asks_future_time(text), text)

    def test_realtime_words_not_future(self) -> None:
        for text in ("现在天气怎么样", "今天天气", "此刻天气", "现在气温多少"):
            self.assertFalse(_asks_future_time(text), text)


class WeatherRealtimeLabelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = MagicMock()
        self.settings.planner_timezone = "Asia/Shanghai"
        self.user_id = uuid.uuid4()

    def _state(self, intent: str) -> AgentState:
        return AgentState(
            current_task={"intent": intent, "route": "life"},
            user_id=self.user_id,
        )

    def _runtime(self) -> SimpleNamespace:
        return SimpleNamespace(context={"settings": self.settings})

    async def test_future_question_labels_realtime_data(self) -> None:
        payload = {
            "provider": "amap",
            "city": "山西榆次",
            "condition": "晴",
            "temperature_c": 18,
            "as_of_iso": "2026-10-29 09:00:00",
        }
        with patch(
            "agents.life_agent_node.get_weather",
            new=AsyncMock(return_value=payload),
        ):
            result = await life_agent_node(
                self._state("山西榆次明天天气怎么样"),
                llm=MagicMock(),
                runtime=self._runtime(),  # type: ignore[arg-type]
            )
        text = result["agent_results"][0]["result"]
        self.assertIn("当前实时天气", text)
        self.assertIn("暂无", text)
        # 仍要给出实时数据本身，只是不伪装成预报。
        self.assertIn("18", text)

    async def test_realtime_question_is_not_labeled(self) -> None:
        payload = {
            "provider": "amap",
            "city": "山西榆次",
            "condition": "晴",
            "temperature_c": 18,
            "as_of_iso": "2026-10-29 09:00:00",
        }
        with patch(
            "agents.life_agent_node.get_weather",
            new=AsyncMock(return_value=payload),
        ):
            result = await life_agent_node(
                self._state("山西榆次现在天气怎么样"),
                llm=MagicMock(),
                runtime=self._runtime(),  # type: ignore[arg-type]
            )
        text = result["agent_results"][0]["result"]
        self.assertNotIn("暂无", text)


class LifePromptCarriesTimeRuleTests(unittest.IsolatedAsyncioTestCase):
    """通用工具循环里的 system prompt 也要带时间对齐死命令。"""

    async def test_system_prompt_has_time_alignment_rule(self) -> None:
        settings = MagicMock()
        settings.planner_timezone = "Asia/Shanghai"
        captured: dict[str, str] = {}

        async def fake_astream_invoke(agent, messages, *, stream=True, **kwargs):
            captured["system"] = messages[0].content
            return AIMessage(content="好的。")

        state = AgentState(
            current_task={"intent": "上海附近有什么好玩的地方", "route": "life"},
            user_id=uuid.uuid4(),
        )
        with patch("agents.life_agent_node.astream_invoke", new=fake_astream_invoke):
            await life_agent_node(
                state,
                llm=MagicMock(),
                runtime=SimpleNamespace(context={"settings": settings}),  # type: ignore[arg-type]
            )
        self.assertIn("实时", captured["system"])
        self.assertIn("预报", captured["system"])


if __name__ == "__main__":
    unittest.main()
