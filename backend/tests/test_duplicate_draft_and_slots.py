"""Regression guards for duplicate HITL email drafts and cross-turn location slots.

Real badcases:
  1. "青岛团建天气" 这一个意图弹出了两张内容相近的邮件确认卡片（一份多了小贴士）。
     根因：同一轮 tool_calls 并发执行 propose_email_draft，两行 PendingAction 都落库；
     模型被打回重写后还会再调用一次。前端 onPendingAction 也无条件 append。
  2. "去成都" 之后用户补一句 "酒店定在太古里"，本轮 intent 没有城市，
     高德把"太古里"解析到了别的城市的同名地点。
"""

import asyncio
import unittest
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage

from agents.email_agent_node import _execute_tool_calls
from agents.life_agent_node import _session_destination
from agents.tools.life_tool import LifeTool
from services.pending_action_service import is_same_email_intent


def _action_row(**overrides: Any) -> MagicMock:
    row = MagicMock()
    row.id = overrides.pop("id", uuid.uuid4())
    row.title = overrides.pop("title", "发送邮件给 a@b.com")
    row.summary = overrides.pop("summary", "收件人：a@b.com")
    row.payload = overrides.pop("payload", {})
    row.updated_at = None
    row.created_at = None
    return row


class EmailIntentFingerprintTests(unittest.TestCase):
    """主题指纹决定两条草稿是不是同一个意图。"""

    def test_identical_subject_is_same_intent(self) -> None:
        self.assertTrue(is_same_email_intent("青岛团建天气提醒", "青岛团建天气提醒"))

    def test_subject_ignores_whitespace_and_punctuation(self) -> None:
        self.assertTrue(is_same_email_intent("青岛 团建：天气提醒", "青岛团建天气提醒"))

    def test_appended_tip_section_is_still_same_intent(self) -> None:
        # 实测里两份草稿只差一段"小贴士"，必须判成同一意图。
        self.assertTrue(is_same_email_intent("青岛团建天气提醒", "青岛团建天气提醒（含小贴士）"))

    def test_unrelated_subjects_are_different_intents(self) -> None:
        self.assertFalse(is_same_email_intent("青岛团建天气", "季度预算复盘"))

    def test_empty_subject_only_matches_empty(self) -> None:
        self.assertTrue(is_same_email_intent("", ""))
        self.assertFalse(is_same_email_intent("", "青岛团建天气"))


class ProposeToolSerializationTests(unittest.IsolatedAsyncioTestCase):
    """草案工具共享同一个 AsyncSession，绝不能被并发执行。"""

    async def test_propose_tools_run_serially_while_reads_run_concurrently(self) -> None:
        order: list[str] = []
        active_proposes = 0
        max_active_proposes = 0

        async def fake_execute(tools_by_name: dict, call: dict):
            nonlocal active_proposes, max_active_proposes
            name = call["name"]
            if name.startswith("propose_"):
                active_proposes += 1
                max_active_proposes = max(max_active_proposes, active_proposes)
                await asyncio.sleep(0.01)
                active_proposes -= 1
            order.append(name)
            return name, "{}", True, MagicMock()

        calls = [
            {"name": "list_emails", "args": {}},
            {"name": "propose_email_draft", "args": {}},
            {"name": "get_email", "args": {}},
            {"name": "propose_email_reply", "args": {}},
        ]

        with patch("agents.email_agent_node._execute_tool", new=fake_execute):
            results = await _execute_tool_calls({}, calls)

        self.assertEqual(len(results), len(calls))
        self.assertEqual(max_active_proposes, 1)
        for index, call in enumerate(calls):
            self.assertEqual(results[index][0], call["name"])


class PendingActionDedupeTests(unittest.IsolatedAsyncioTestCase):
    """同一收件人 + 同主题在窗口内必须复用同一行。"""

    async def test_duplicate_draft_reuses_existing_row(self) -> None:
        from lib.enums import PendingActionStatus, PendingActionType
        from services import pending_action_service

        existing = _action_row(
            payload={"to": "a@b.com", "subject": "青岛团建天气提醒", "body": "旧正文"},
        )
        existing.status = PendingActionStatus.PENDING

        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.refresh = AsyncMock()

        created: list[Any] = []

        def fake_add(row: Any) -> None:
            created.append(row)

        session.add.side_effect = fake_add

        with patch.object(
            pending_action_service,
            "_find_duplicate_email_draft",
            AsyncMock(return_value=existing),
        ):
            row = await pending_action_service.create_pending_action(
                session,
                user_id=uuid.uuid4(),
                action_type=PendingActionType.EMAIL_SEND,
                title="发送邮件给 a@b.com",
                summary="新摘要",
                payload={
                    "to": "a@b.com",
                    "subject": "青岛团建天气提醒（含小贴士）",
                    "body": "新正文，多了小贴士",
                },
            )

        self.assertIs(row, existing)
        self.assertEqual(existing.payload["body"], "新正文，多了小贴士")
        self.assertEqual(existing.summary, "新摘要")
        # 关键：没有新插入第二行。
        self.assertEqual(created, [])


class SessionDestinationTests(unittest.TestCase):
    """core_intent 里已确认的目的地必须能传给 LifeTool。"""

    def test_reads_destination_from_core_intent(self) -> None:
        state = {"core_intent": {"entities": {"destination": "成都"}}}
        self.assertEqual(_session_destination(state), "成都")

    def test_missing_destination_returns_none(self) -> None:
        self.assertIsNone(_session_destination({}))
        self.assertIsNone(_session_destination({"core_intent": {"entities": {}}}))
        self.assertIsNone(_session_destination({"core_intent": {"entities": {"destination": "  "}}}))

    def test_map_uses_session_city_as_scope(self) -> None:
        # 不再把城市拼进关键词，而是作为 city 参数下发（高德 citylimit 才真正生效）。
        tool = LifeTool(None, MagicMock(), default_city="成都")
        self.assertEqual(tool._scope_city(None), "成都")

    def test_map_explicit_city_wins(self) -> None:
        tool = LifeTool(None, MagicMock(), default_city="成都")
        self.assertEqual(tool._scope_city("上海"), "上海")

    def test_map_without_any_city_is_unscoped(self) -> None:
        tool = LifeTool(None, MagicMock(), default_city=None)
        self.assertIsNone(tool._scope_city(None))

    def test_weather_falls_back_to_session_city(self) -> None:
        settings = MagicMock()
        tool = LifeTool(None, settings, default_city="成都")
        with patch(
            "agents.tools.life_tool.get_weather",
            AsyncMock(return_value={"city": "成都"}),
        ) as weather:
            asyncio.run(tool._weather())
        weather.assert_awaited_once_with(settings, city="成都")


class EmailAgentDuplicateReceiptTests(unittest.IsolatedAsyncioTestCase):
    """同一个 action_id 只上报一次，前端就不会渲染两张卡片。"""

    async def test_same_action_id_reported_once(self) -> None:
        from agents import email_agent_node

        action_id = str(uuid.uuid4())
        receipt = (
            '{"status": "pending", "action_id": "%s", "action_type": "email_send", "to": "a@b.com"}'
            % action_id
        )

        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "propose_email_draft", "args": {}, "id": "call-1"},
                    {"name": "propose_email_draft", "args": {}, "id": "call-2"},
                ],
            ),
            AIMessage(content="草稿已准备好，等待你确认。"),
        ]

        class FakeAgent:
            def __init__(self) -> None:
                self._round = 0

            async def ainvoke(self, messages: list[Any]) -> AIMessage:
                response = responses[min(self._round, len(responses) - 1)]
                self._round += 1
                return response

        fake_agent = FakeAgent()

        class FakeLLM:
            def bind_tools(self, tools: list[Any]) -> "FakeLLM":
                return self

            async def ainvoke(self, messages: list[Any]) -> AIMessage:
                return await fake_agent.ainvoke(messages)

        runtime = SimpleNamespace(
            context={
                "settings": MagicMock(),
                "db_session": MagicMock(),
                "email_credentials": MagicMock(),
                "gmail_access_token": None,
            }
        )
        state = {
            "current_task": {"intent": "给 a@b.com 发送青岛团建天气邮件", "task_id": "t1"},
            "user_id": uuid.uuid4(),
            "messages": [],
        }

        with patch.object(email_agent_node, "astream_invoke", AsyncMock(side_effect=[
            responses[0], responses[1],
        ])), patch.object(
            email_agent_node, "make_mail163_email_tools", return_value=[]
        ), patch.object(
            email_agent_node, "make_propose_email_tools", return_value=[]
        ), patch.object(
            email_agent_node, "_execute_tool", AsyncMock(return_value=(
                "propose_email_draft", receipt, True, MagicMock(),
            ))
        ):
            result = await email_agent_node.email_agent_node(
                state, FakeLLM(), runtime,
            )

        self.assertEqual(len(result["pending_actions"]), 1)
        self.assertEqual(result["pending_actions"][0]["action_id"], action_id)


if __name__ == "__main__":
    unittest.main()
