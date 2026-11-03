"""Regression guards for two production HITL failures.

Bug 1 — 切页后确认卡片丢失:
  PendingAction rows were never tagged with a conversation_id, so the
  transcript endpoint had no way to restore an unconfirmed draft. Reloading
  the page kept the chat text but silently dropped the confirm card.

Bug 2 — 口头"ok"被当成卡片确认:
  A bare "ok" in the chat box was routed to direct_reply, whose claim gate had
  no email markers at all, so the model answered "邮件已按草稿发送" with no
  draft and no card. The email node had a similar hole: it treated *having a
  pending draft* as permission to claim success.
"""

import unittest
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, SystemMessage

from agents.common import contains_send_claim
from agents.email_agent_node import email_agent_node
from agents.orchestrator_node import direct_reply_node
from agents.state import AgentState
from lib.enums import PendingActionStatus, PendingActionType
from services import pending_action_service
from services.mail163_client import Mail163Config


class SendClaimDetectorTests(unittest.TestCase):
    """发送声称识别要能抓住真实幻觉，又不能误伤"看已发送文件夹"。"""

    def test_real_claim_from_badcase_is_detected(self) -> None:
        self.assertTrue(contains_send_claim("邮件已按草稿发送"))
        self.assertTrue(contains_send_claim("已经发送邮件给 a@b.com"))
        self.assertTrue(contains_send_claim("已发送"))
        self.assertTrue(contains_send_claim("已帮你发送"))
        self.assertTrue(contains_send_claim("邮件发送成功"))
        self.assertTrue(contains_send_claim("已回复给对方"))

    def test_draft_pending_wording_is_not_a_claim(self) -> None:
        # 这正是合法回复的样子：草案好了、等确认。
        self.assertFalse(contains_send_claim("草稿已准备好，等待你确认后发送"))
        self.assertFalse(contains_send_claim("我已经准备好草稿了，请点击确认卡片"))
        self.assertFalse(contains_send_claim("我没有发送任何邮件"))
        self.assertFalse(contains_send_claim("发送失败，请稍后重试"))

    def test_readonly_sent_folder_wording_is_not_a_claim(self) -> None:
        # 用户查"已发送"文件夹时，正常回答必然含这些词。
        self.assertFalse(contains_send_claim("查看已发送邮件列表"))
        self.assertFalse(contains_send_claim("已发送的邮件有三封"))


class EmailGatesNeverExposeSendToolsTests(unittest.IsolatedAsyncioTestCase):
    """图内不提供真实发送工具：发送只能由用户点卡片后 /approve 执行。"""

    async def test_send_tools_are_stripped_before_binding(self) -> None:
        seen: list[list[str]] = []

        class FakeLLM:
            def bind_tools(self, tools: list[Any]) -> "FakeLLM":
                seen.append([t.name for t in tools])
                return self

        class NamedTool:
            def __init__(self, name: str) -> None:
                self.name = name

            async def ainvoke(self, args: dict[str, Any]) -> str:
                return "{}"

        async def fake_astream(agent: Any, messages: Any, *, stream: bool = True, **kw: Any) -> AIMessage:
            return AIMessage(content="草稿已准备好，等待你确认后发送。")

        state = AgentState(
            current_task={"intent": "帮我发一封邮件给 user@example.com，说周末见"},
            messages=[],
            user_id=uuid.uuid4(),
            email_credentials=Mail163Config(email="user@163.com", auth_code="code"),
        )
        runtime = SimpleNamespace(context={
            "settings": MagicMock(),
            "db_session": MagicMock(),
            "email_credentials": Mail163Config(email="user@163.com", auth_code="code"),
        })

        with patch(
            "agents.email_agent_node.make_mail163_email_tools",
            return_value=[NamedTool("list_emails"), NamedTool("send_email"), NamedTool("reply_to_email")],
        ), patch(
            "agents.email_agent_node.make_propose_email_tools",
            return_value=[NamedTool("propose_email_draft")],
        ), patch(
            "agents.email_agent_node.astream_invoke", fake_astream,
        ):
            await email_agent_node(state, FakeLLM(), runtime)  # type: ignore[arg-type]

        self.assertEqual(len(seen), 1)
        self.assertNotIn("send_email", seen[0])
        self.assertNotIn("reply_to_email", seen[0])
        self.assertIn("list_emails", seen[0])
        self.assertIn("propose_email_draft", seen[0])


class EmailClaimNeedsApprovedReceiptTests(unittest.IsolatedAsyncioTestCase):
    """有 pending 草案也不许声称"已发送"——approved 只可能来自另一个请求。"""

    async def test_pending_draft_does_not_license_a_send_claim(self) -> None:
        receipt = (
            '{"status": "pending", "action_id": "aaa", "action_type": "email_send",'
            ' "to": "user@example.com", "subject": "周末见"}'
        )

        class FakeTool:
            name = "propose_email_draft"

            async def ainvoke(self, args: dict[str, Any]) -> str:
                return receipt

        responses = [
            AIMessage(content="", tool_calls=[{
                "name": "propose_email_draft",
                "args": {"to": "user@example.com", "subject": "周末见", "body": "好"},
                "id": "c1",
            }]),
            # 模型以为"提交过草案"就有资格宣告成功——必须被打回。
            AIMessage(content="邮件已按草稿发送。"),
            AIMessage(content="邮件已按草稿发送。"),
            AIMessage(content="邮件已按草稿发送。"),
            AIMessage(content="邮件已按草稿发送。"),
        ]

        async def fake_astream(agent: Any, messages: Any, *, stream: bool = True, **kw: Any) -> AIMessage:
            return responses.pop(0) if responses else AIMessage(content="草稿已准备好，等待你确认。")

        class FakeLLM:
            def bind_tools(self, tools: list[Any]) -> "FakeLLM":
                return self

        state = AgentState(
            current_task={"intent": "帮我发一封邮件给 user@example.com，说周末见"},
            messages=[],
            user_id=uuid.uuid4(),
            email_credentials=Mail163Config(email="user@163.com", auth_code="code"),
        )
        runtime = SimpleNamespace(context={
            "settings": MagicMock(),
            "db_session": MagicMock(),
            "email_credentials": Mail163Config(email="user@163.com", auth_code="code"),
        })

        with patch(
            "agents.email_agent_node.make_mail163_email_tools", return_value=[],
        ), patch(
            "agents.email_agent_node.make_propose_email_tools", return_value=[FakeTool()],
        ), patch(
            "agents.email_agent_node.astream_invoke", fake_astream,
        ):
            result = await email_agent_node(state, FakeLLM(), runtime)  # type: ignore[arg-type]

        final = result["agent_results"][0]["result"]
        self.assertNotIn("已发送", final)
        self.assertIn("确认", final)
        # 草案仍在，用户还有卡片可点。
        self.assertEqual(len(result["pending_actions"]), 1)


class DirectReplyBlocksSendClaimTests(unittest.IsolatedAsyncioTestCase):
    """用户只回一句"ok"：direct 分支必须打回"已发送"，绝不承认。"""

    def _runtime(self) -> SimpleNamespace:
        return SimpleNamespace(context={"db_session": AsyncMock(), "settings": MagicMock()})

    def _state(self) -> AgentState:
        return AgentState(
            current_task={"intent": "ok"},
            messages=[],
            user_id=uuid.uuid4(),
        )

    async def test_verbal_ok_never_yields_a_send_claim(self) -> None:
        injected: list[str] = []

        async def fake_astream(agent: Any, messages: Any, *, stream: bool = True, **kw: Any) -> AIMessage:
            for message in messages:
                if isinstance(message, SystemMessage):
                    injected.append(message.content)
            # 模型每一轮都跳过卡片直接宣告成功。
            return AIMessage(content="好的，邮件已按草稿发送。")

        class FakeLLM:
            def bind_tools(self, tools: list[Any]) -> "FakeLLM":
                return self

        with patch("agents.orchestrator_node.astream_invoke", fake_astream), patch(
            "agents.orchestrator_node.make_task_action_tools", return_value=[],
        ):
            result = await direct_reply_node(
                self._state(), FakeLLM(), self._runtime(),  # type: ignore[arg-type]
            )

        final = result["agent_results"][0]["result"]
        self.assertNotIn("已发送", final)
        self.assertIn("确认", final)
        # 必须注入过「口头确认无效」的纠正提示。
        self.assertTrue(any("口头" in text or "确认卡片" in text for text in injected))


class PendingActionConversationBindingTests(unittest.IsolatedAsyncioTestCase):
    """草案必须记录 conversation_id，否则切页后无从恢复。"""

    async def test_context_conversation_id_is_persisted(self) -> None:
        added: list[Any] = []
        session = MagicMock()
        session.add.side_effect = added.append
        session.flush = AsyncMock()
        session.refresh = AsyncMock()

        token = pending_action_service.CURRENT_CONVERSATION_ID.set("conv-42")
        self.addCleanup(pending_action_service.CURRENT_CONVERSATION_ID.reset, token)

        with patch.object(
            pending_action_service, "_find_duplicate_email_draft", AsyncMock(return_value=None),
        ):
            row = await pending_action_service.create_pending_action(
                session,
                user_id=uuid.uuid4(),
                action_type=PendingActionType.EMAIL_SEND,
                title="发送邮件给 a@b.com",
                summary="摘要",
                payload={"to": "a@b.com", "subject": "青岛团建天气提醒", "body": "正文"},
            )

        self.assertEqual(row.conversation_id, "conv-42")
        self.assertEqual(len(added), 1)

    async def test_explicit_conversation_id_wins_over_context(self) -> None:
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.refresh = AsyncMock()

        token = pending_action_service.CURRENT_CONVERSATION_ID.set("conv-context")
        self.addCleanup(pending_action_service.CURRENT_CONVERSATION_ID.reset, token)

        with patch.object(
            pending_action_service, "_find_duplicate_email_draft", AsyncMock(return_value=None),
        ):
            row = await pending_action_service.create_pending_action(
                session,
                user_id=uuid.uuid4(),
                action_type=PendingActionType.EMAIL_SEND,
                title="t",
                summary="s",
                payload={"to": "a@b.com", "subject": "x", "body": "b"},
                conversation_id="conv-explicit",
            )

        self.assertEqual(row.conversation_id, "conv-explicit")

    async def test_duplicate_lookup_is_scoped_to_one_conversation(self) -> None:
        """两个标签页各写同主题邮件是两份草稿，不能互相吞掉。"""
        seen_stmt: list[Any] = []

        async def fake_execute(stmt: Any) -> Any:
            seen_stmt.append(stmt)
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=fake_execute)

        await pending_action_service._find_duplicate_email_draft(
            session,
            user_id=uuid.uuid4(),
            action_type=PendingActionType.EMAIL_SEND,
            to="a@b.com",
            subject="青岛团建天气提醒",
            conversation_id="conv-7",
        )

        self.assertEqual(len(seen_stmt), 1)
        self.assertIn("pending_actions.conversation_id", str(seen_stmt[0]))


async def _record_and_empty(bucket: list[Any], stmt: Any) -> Any:
    bucket.append(stmt)
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    return result


class OpenDraftQueryTests(unittest.IsolatedAsyncioTestCase):
    """恢复查询只返回 pending 且未过期的草案，并按会话分组。"""

    async def test_groups_pending_rows_by_conversation(self) -> None:
        row = MagicMock()
        row.id = uuid.uuid4()
        row.conversation_id = "conv-a"
        row.status = PendingActionStatus.PENDING
        row.action_type = PendingActionType.EMAIL_SEND
        row.title = "t"
        row.summary = "s"
        row.payload = {}
        row.result = None
        row.expires_at = None
        row.created_at = None
        row.updated_at = None
        row.executed_at = None

        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        session.execute = AsyncMock(return_value=result)

        grouped = await pending_action_service.list_open_pending_actions_for_conversations(
            session, user_id=uuid.uuid4(), conversation_ids=["conv-a", "conv-b"],
        )

        self.assertEqual(list(grouped), ["conv-a"])
        self.assertEqual(len(grouped["conv-a"]), 1)
        stmt = str(session.execute.await_args.args[0])
        self.assertIn("pending_actions.status", stmt)

    async def test_empty_id_list_skips_the_query(self) -> None:
        session = AsyncMock()
        grouped = await pending_action_service.list_open_pending_actions_for_conversations(
            session, user_id=uuid.uuid4(), conversation_ids=[],
        )
        self.assertEqual(grouped, {})
        session.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
