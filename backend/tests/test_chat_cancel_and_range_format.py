import asyncio
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from agents.common import NUMERIC_RANGE_RULE
from agents.life_agent_node import _render_forecast
from agents.travel_planning import TRAVEL_SYNTHESIS_RULES, build_weather_advice
from auth.dependencies import get_current_user_id
from db.dependencies import verify_database_ready
from main import app
from services import agent_runner

CONVERSATION_KEY = "conv-cancel-test"


def _register_stream(task: asyncio.Task) -> agent_runner._ConversationStream:
    stream = agent_runner._ConversationStream()
    stream.producer_task = task
    agent_runner._STREAM_REGISTRY[CONVERSATION_KEY] = stream
    return stream


class CancelConversationTurnTests(unittest.IsolatedAsyncioTestCase):
    """停止按钮的后端语义：取消生产端、摘掉缓冲、发 cancelled 事件。"""

    async def asyncSetUp(self) -> None:
        agent_runner.reset_stream_registry()

    async def asyncTearDown(self) -> None:
        agent_runner.reset_stream_registry()

    async def test_cancel_running_turn_stops_producer_and_clears_registry(self) -> None:
        producer_started = asyncio.Event()

        async def long_running_turn() -> None:
            producer_started.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(long_running_turn())
        stream = _register_stream(task)
        await producer_started.wait()

        self.assertTrue(agent_runner.cancel_conversation_turn(CONVERSATION_KEY))

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(CONVERSATION_KEY, agent_runner._STREAM_REGISTRY)
        self.assertTrue(stream.done)
        self.assertEqual([event for _, event in stream.events][-1]["type"], "cancelled")

    async def test_cancel_unknown_conversation_is_noop(self) -> None:
        self.assertFalse(agent_runner.cancel_conversation_turn("not-running"))

    async def test_cancel_finished_turn_reports_not_running(self) -> None:
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        _register_stream(task)

        self.assertFalse(agent_runner.cancel_conversation_turn(CONVERSATION_KEY))
        self.assertNotIn(CONVERSATION_KEY, agent_runner._STREAM_REGISTRY)

    async def test_silent_disconnect_cancels_producer(self) -> None:
        task = asyncio.create_task(asyncio.sleep(60))
        stream = _register_stream(task)

        with patch.object(agent_runner, "DISCONNECT_CANCEL_GRACE_SECONDS", 0):
            await agent_runner._cancel_turn_if_silent(stream, CONVERSATION_KEY)

        # cancel() 只是排入取消请求，任务在下一个事件循环 tick 才真正结束。
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(CONVERSATION_KEY, agent_runner._STREAM_REGISTRY)

    async def test_reconnected_subscriber_keeps_generation_alive(self) -> None:
        task = asyncio.create_task(asyncio.sleep(0.2))
        stream = _register_stream(task)
        stream.subscribe()  # 前端自动重连已在宽限期内重新挂上

        with patch.object(agent_runner, "DISCONNECT_CANCEL_GRACE_SECONDS", 0):
            await agent_runner._cancel_turn_if_silent(stream, CONVERSATION_KEY)

        self.assertFalse(task.cancelled())
        self.assertIn(CONVERSATION_KEY, agent_runner._STREAM_REGISTRY)
        await task


class NumericRangeFormatTests(unittest.TestCase):
    """`17~19℃` 的两个波浪号会被 Markdown 当成删除线，后端不能再输出 `~`。"""

    def test_forecast_uses_zhi_instead_of_tilde(self) -> None:
        text = _render_forecast(
            {"day_temp_c": 17, "night_temp_c": 19, "day_weather": "晴"},
            "上海",
            "明天",
        )

        self.assertIn("17至19°C", text)
        self.assertNotIn("~", text)

    def test_weather_advice_uses_zhi_instead_of_tilde(self) -> None:
        results = [
            {
                "route": "life",
                "intent": "查询上海两天的天气与气温",
                "result": "上海明天多云，气温 13~19°C。",
            }
        ]

        advice = build_weather_advice(results)

        self.assertIn("13至19°C", advice)
        self.assertNotIn("~", advice)

    def test_travel_synthesis_rules_share_the_range_rule(self) -> None:
        self.assertIn(NUMERIC_RANGE_RULE, TRAVEL_SYNTHESIS_RULES)


class ChatCancelRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app.dependency_overrides[verify_database_ready] = lambda: None
        app.dependency_overrides[get_current_user_id] = lambda: uuid.UUID(int=1)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        agent_runner.reset_stream_registry()

    def test_cancel_reports_cancelled_while_turn_runs(self) -> None:
        with TestClient(app) as client, patch(
            "routers.chat.cancel_conversation_turn", return_value=True
        ):
            response = client.post(
                "/chat/cancel", json={"client_conversation_id": CONVERSATION_KEY}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "cancelled"})

    def test_cancel_reports_idle_when_nothing_runs(self) -> None:
        with TestClient(app) as client, patch(
            "routers.chat.cancel_conversation_turn", return_value=False
        ):
            response = client.post(
                "/chat/cancel", json={"client_conversation_id": CONVERSATION_KEY}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "idle"})

    def test_cancel_requires_conversation_id(self) -> None:
        with TestClient(app) as client:
            response = client.post("/chat/cancel", json={})

        self.assertEqual(response.status_code, 422)
