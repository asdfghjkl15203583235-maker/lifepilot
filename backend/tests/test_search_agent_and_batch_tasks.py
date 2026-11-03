"""联网攻略搜索 + 行程批量落库 + HITL 确认落地。

真实 badcase（北京周末两日游）：
  1. 推荐理由全是模型常识 —— 需要 search 分支把真实网页结果带进合成；
  2. 结尾问「需要我记成日程或待办吗？」，用户回「好的，帮我加上」后什么都没发生 ——
     `hitl_reply=approve` 抢在工具循环之前返回"没有待决操作"，把请求吞掉了；
  3. 一次对话要落 4~6 条行程节点，单条 create_task 不够用。
"""

import json
import unittest
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession

from agents.common import render_date_reference
from agents.orchestrator_node import _is_successful_receipt, direct_reply_node
from agents.planner_agent_node import expand_travel_tasks
from agents.search_agent_node import search_agent_node
from agents.state import AgentState
from agents.travel_planning import build_travel_steps
from agents.tools.task_action_tools import (
    CREATE_TASK_TOOL_NAME,
    CREATE_TASKS_TOOL_NAME,
    make_task_action_tools,
)
from services.web_search_client import WebSearchError

USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")

SEARCH_PAYLOAD = {
    "provider": "tavily",
    "query": "北京中轴线最新旅游攻略",
    "answer": "中轴线建议上午从永定门进、下午到钟鼓楼。",
    "results": [
        {
            "title": "北京中轴线一日游路线",
            "url": "https://example.com/axis",
            "content": "故宫需提前 7 天预约，周一闭馆。",
            "score": 0.92,
        }
    ],
}


class TravelSearchStepTests(unittest.TestCase):
    def test_search_step_is_parallel_and_optional(self) -> None:
        without_search = build_travel_steps("北京", "两天", plan_id="p1")
        self.assertEqual(len(without_search), 3)

        with_search = build_travel_steps("北京", "两天", plan_id="p1", include_search=True)
        self.assertEqual(len(with_search), 4)
        search_step = with_search[-1]
        self.assertEqual(search_step["route"], "search")
        self.assertEqual(search_step["id"], "step-tips")
        self.assertEqual(search_step["depends_on"], [])
        self.assertIn("攻略", search_step["intent"])

    def test_planner_expansion_adds_search_step(self) -> None:
        expanded = expand_travel_tasks(
            [{"id": "step-1", "route": "direct", "intent": "北京周末两日游"}],
            latest_user_text="北京周末两日游",
            core_intent=None,
            plan_id="p2",
            include_search=True,
        )
        self.assertEqual([step["route"] for step in expanded].count("search"), 1)


class SearchAgentTests(unittest.IsolatedAsyncioTestCase):
    def _state(self, intent: str = "检索北京中轴线最新旅游攻略") -> AgentState:
        return AgentState(current_task={"intent": intent, "route": "search", "task_key": "tips"})

    def _runtime(self) -> SimpleNamespace:
        return SimpleNamespace(context={"settings": MagicMock(), "db_session": None})

    async def test_success_renders_sources(self) -> None:
        with patch(
            "agents.search_agent_node.search_web",
            AsyncMock(return_value=SEARCH_PAYLOAD),
        ):
            result = await search_agent_node(self._state(), llm=MagicMock(), runtime=self._runtime())

        entry = result["agent_results"][0]
        self.assertEqual(entry["route"], "search")
        self.assertEqual(entry["task_status"], "success")
        self.assertIn("故宫需提前 7 天预约", entry["result"])
        self.assertIn("https://example.com/axis", entry["result"])

    async def test_missing_key_is_an_honest_failure(self) -> None:
        with patch(
            "agents.search_agent_node.search_web",
            AsyncMock(side_effect=WebSearchError("未配置 TAVILY_API_KEY")),
        ):
            result = await search_agent_node(self._state(), llm=MagicMock(), runtime=self._runtime())

        entry = result["agent_results"][0]
        self.assertEqual(entry["task_status"], "failed")
        self.assertIn("没有成功", entry["result"])
        self.assertIn("TAVILY_API_KEY", entry["result"])


class BatchCreateTasksTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.session = AsyncMock(spec=AsyncSession)

    def _tool(self):
        tools = make_task_action_tools(self.session, USER_ID, MagicMock(planner_timezone="Asia/Shanghai"))
        return {tool.name: tool for tool in tools}

    async def test_batch_creates_each_node(self) -> None:
        created = [SimpleNamespace(id=uuid.uuid4(), title=title) for title in ("天安门广场", "故宫")]
        with patch(
            "agents.tools.task_action_tools.create_task_for_user",
            AsyncMock(side_effect=created),
        ) as create_call:
            output = await self._tool()[CREATE_TASKS_TOOL_NAME].ainvoke(
                {
                    "tasks": [
                        {"title": "天安门广场", "due_at": "2026-11-07T09:00:00+08:00"},
                        {"title": "故宫", "due_at": "2026-11-07T11:00:00+08:00"},
                    ]
                }
            )

        receipt = json.loads(output)
        self.assertEqual(receipt["status"], "created")
        self.assertEqual(receipt["count"], 2)
        self.assertEqual(len(receipt["task_ids"]), 2)
        self.assertEqual(create_call.await_count, 2)
        self.assertTrue(_is_successful_receipt(receipt))

    async def test_partial_failure_keeps_successful_rows(self) -> None:
        with patch(
            "agents.tools.task_action_tools.create_task_for_user",
            AsyncMock(side_effect=[SimpleNamespace(id=uuid.uuid4(), title="天安门广场"), None]),
        ):
            output = await self._tool()[CREATE_TASKS_TOOL_NAME].ainvoke(
                {"tasks": [{"title": "天安门广场"}, {"title": "故宫"}]}
            )

        receipt = json.loads(output)
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["count"], 1)
        self.assertEqual(len(receipt["failed"]), 1)

    async def test_single_tool_still_works(self) -> None:
        with patch(
            "agents.tools.task_action_tools.create_task_for_user",
            AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), title="写周报")),
        ):
            output = await self._tool()[CREATE_TASK_TOOL_NAME].ainvoke({"title": "写周报"})
        self.assertEqual(json.loads(output)["status"], "created")


class HitlConfirmationFallthroughTests(unittest.IsolatedAsyncioTestCase):
    """「好的，帮我加上」没有待确认草案时，必须继续走工具循环而不是回一句空话。"""

    def setUp(self) -> None:
        self.session = AsyncMock(spec=AsyncSession)
        self.settings = MagicMock()
        self.settings.reminder_default_lead_minutes = 30

    def _runtime(self) -> SimpleNamespace:
        return SimpleNamespace(context={"db_session": self.session, "settings": self.settings})

    def _state(self) -> AgentState:
        return AgentState(
            current_task={"intent": "好的，帮我加上", "route": "direct", "hitl_reply": "approve"},
            messages=[
                HumanMessage(content="北京周末两日游"),
                AIMessage(content="第一天：09:00 天安门，11:00 故宫。需要我记成待办吗？"),
                HumanMessage(content="好的，帮我加上"),
            ],
            user_id=USER_ID,
        )

    async def test_confirm_without_pending_action_creates_tasks(self) -> None:
        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": CREATE_TASKS_TOOL_NAME,
                        "args": {
                            "tasks": [
                                {"title": "天安门广场", "due_at": "2026-11-07T09:00:00+08:00"},
                                {"title": "故宫", "due_at": "2026-11-07T11:00:00+08:00"},
                            ]
                        },
                        "id": "c1",
                    }
                ],
            ),
            AIMessage(content="已创建 2 条待办：周六 09:00 天安门广场、11:00 故宫。"),
        ]

        async def fake_astream(agent, messages, *, stream: bool = True, **kwargs):
            return responses.pop(0)

        created = [SimpleNamespace(id=uuid.uuid4(), title="天安门广场"), SimpleNamespace(id=uuid.uuid4(), title="故宫")]
        with patch("agents.orchestrator_node.astream_invoke", fake_astream), patch(
            "services.pending_action_service.list_pending_actions",
            AsyncMock(return_value=[]),
        ), patch(
            "agents.tools.task_action_tools.create_task_for_user",
            AsyncMock(side_effect=created),
        ) as create_call:
            result = await direct_reply_node(self._state(), llm=_FakeLLM(), runtime=self._runtime())

        text = result["agent_results"][0]["result"]
        self.assertNotIn("没有找到需要你确认的待决操作", text)
        self.assertIn("2 条", text)
        self.assertEqual(create_call.await_count, 2)


class _FakeLLM:
    def bind_tools(self, tools: list[Any]) -> "_FakeLLM":
        return self


class DateReferenceTests(unittest.TestCase):
    def test_date_reference_maps_weekdays(self) -> None:
        text = render_date_reference("Asia/Shanghai")
        self.assertIn("今天：", text)
        self.assertIn("周六", text)


if __name__ == "__main__":
    unittest.main()
