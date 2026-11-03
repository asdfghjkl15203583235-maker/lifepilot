"""模块四：旅行规划的深度合成。

「去上海玩两天」必须被拆成天气 / 景点 / 交通三路并行步骤（现有 graph 的
fan-out 直接支持），最后由 merge_results 合成可执行行程，并且一定包含
「带伞/穿衣」提醒——不能只是罗列网上抄来的攻略。
"""

import unittest
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage

from agents import orchestrator_node as orchestrator
from agents.planner_agent_node import expand_travel_tasks
from agents.travel_planning import (
    build_travel_steps,
    build_weather_advice,
    extract_travel_destination,
    extract_travel_duration,
    is_travel_request,
)


class TravelDetectionTests(unittest.TestCase):
    def test_recognises_travel_requests(self) -> None:
        self.assertTrue(is_travel_request("去上海玩两天"))
        self.assertTrue(is_travel_request("帮我制定一个成都三日游攻略"))
        self.assertFalse(is_travel_request("帮我把周报写完"))

    def test_extracts_destination_and_duration(self) -> None:
        self.assertEqual(extract_travel_destination("去上海玩两天"), "上海")
        self.assertEqual(extract_travel_duration("去上海玩两天"), "两天")
        self.assertEqual(extract_travel_duration("成都三日游"), "三天")

    def test_destination_falls_back_to_session_prior(self) -> None:
        self.assertEqual(extract_travel_destination("帮我规划一下行程", "杭州"), "杭州")


class TravelStepTests(unittest.TestCase):
    def test_three_parallel_life_steps(self) -> None:
        steps = build_travel_steps("上海", "两天", plan_id="plan-1")
        self.assertEqual(len(steps), 3)
        self.assertTrue(all(step["route"] == "life" for step in steps))
        self.assertTrue(all(step["depends_on"] == [] for step in steps))
        self.assertTrue(all(step["plan_id"] == "plan-1" for step in steps))
        self.assertEqual(
            [step["id"] for step in steps],
            ["step-weather", "step-sights", "step-transport"],
        )

    def test_planner_expansion_replaces_direct_step(self) -> None:
        tasks = [{"id": "step-1", "route": "direct", "intent": "去上海玩两天"}]
        expanded = expand_travel_tasks(
            tasks,
            latest_user_text="去上海玩两天",
            core_intent=None,
            plan_id="plan-2",
        )
        self.assertEqual(len(expanded), 3)
        self.assertTrue(all(step["route"] == "life" for step in expanded))

    def test_explicit_other_routes_are_kept(self) -> None:
        tasks = [{"id": "step-1", "route": "calendar", "intent": "订会议室"}]
        expanded = expand_travel_tasks(
            tasks,
            latest_user_text="去上海玩两天，顺便订个会议室",
            core_intent={"entities": {"destination": "上海"}},
            plan_id="plan-3",
        )
        self.assertEqual(len(expanded), 4)
        self.assertEqual(expanded[-1]["route"], "calendar")


class WeatherAdviceTests(unittest.TestCase):
    def test_rain_and_temperature_produce_actionable_advice(self) -> None:
        advice = build_weather_advice([
            {
                "route": "life",
                "intent": "查询上海两天的天气与气温",
                "result": "上海明天小雨，气温 18~24°C。",
            }
        ])
        self.assertIn("带伞", advice)
        # 区间必须写「至」：`~` 会被前端 Markdown 当成删除线。
        self.assertIn("18至24", advice)
        self.assertNotIn("~", advice)

    def test_no_weather_branch_means_no_advice(self) -> None:
        self.assertEqual(build_weather_advice([{"route": "life", "intent": "查景点", "result": "外滩"}]), "")


class TravelSynthesisNodeTests(unittest.IsolatedAsyncioTestCase):
    def _state(self) -> dict:
        results = [
            {
                "route": "life",
                "intent": "查询上海两天的天气与气温",
                "result": "上海明天小雨，气温 18~24°C。",
                "task_status": "success",
                "task_key": "weather",
            },
            {
                "route": "life",
                "intent": "查询上海的热门景点与游客评价",
                "result": "外滩适合傍晚去，武康路适合citywalk。",
                "task_status": "success",
                "task_key": "sights",
            },
            {
                "route": "life",
                "intent": "查询上海的市内交通与景点间路线",
                "result": "地铁 2 号线可到外滩，单程约 30 分钟。",
                "task_status": "success",
                "task_key": "transport",
            },
        ]
        return {
            "messages": [HumanMessage(content="[最新请求] user: 去上海玩两天")],
            "agent_results": results,
            "tasks": [{"task_key": "weather"}, {"task_key": "sights"}, {"task_key": "transport"}],
        }

    async def test_travel_results_use_synthesis_prompt_and_advice_footer(self) -> None:
        captured: dict = {}

        async def fake_invoke(llm, messages):
            captured["system"] = messages[0].content
            return AIMessage(content="第一天：外滩 → 武康路。")

        with patch.object(orchestrator, "astream_invoke", new=fake_invoke):
            output = await orchestrator.merge_results_node(self._state(), MagicMock())

        self.assertIn("旅行规划", captured["system"])
        self.assertIn("出行提醒", output["final_answer"])
        self.assertIn("带伞", output["final_answer"])

    async def test_non_travel_merge_keeps_generic_prompt(self) -> None:
        captured: dict = {}

        async def fake_invoke(llm, messages):
            captured["system"] = messages[0].content
            return AIMessage(content="已处理。")

        state = self._state()
        state["messages"] = [HumanMessage(content="[最新请求] user: 看看我的邮件和日程")]
        with patch.object(orchestrator, "astream_invoke", new=fake_invoke):
            output = await orchestrator.merge_results_node(state, MagicMock())

        self.assertNotIn("旅行规划", captured["system"])
        self.assertNotIn("出行提醒", output["final_answer"])


if __name__ == "__main__":
    unittest.main()
