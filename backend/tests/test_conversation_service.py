"""Conversation CRUD ownership and auth tests.

All per-conversation handlers must scope by both user_id and conversation id
so one user cannot read or mutate another user's thread.
"""

import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models.conversation import Conversation
from schemas.chat import ChatMessage
from services.conversation_service import (
    _conversation_by_client_id_for_user,
    create_conversation,
    delete_conversation,
    get_conversation,
    update_conversation,
    upsert_conversation_by_client_id,
)

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
CONV_A = uuid.uuid4()


def _fake_owner(session: AsyncMock) -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = object()
    session.execute = AsyncMock(return_value=result)


def _fake_matching_conversation() -> Conversation:
    conversation = Conversation(
        id=CONV_A,
        user_id=USER_A,
        title="user A chat",
        messages=[],
    )
    return conversation


class ConversationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_conversation_assigns_owner(self) -> None:
        session = AsyncMock()
        _fake_owner(session)
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.refresh = AsyncMock(side_effect=lambda row: None)

        conversation = await create_conversation(
            session,
            user_id=USER_A,
            title="  New chat  ",
            messages=[
                ChatMessage(role="user", content="hello"),
                ChatMessage(role="assistant", content="hi"),
            ],
        )

        self.assertIsNotNone(conversation)
        self.assertEqual(conversation.user_id, USER_A)
        self.assertEqual(conversation.title, "New chat")
        self.assertEqual(conversation.messages[0]["role"], "user")
        self.assertEqual(conversation.messages[1]["content"], "hi")

    async def test_get_conversation_requires_user_ownership(self) -> None:
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        other_user_conv = await get_conversation(
            session,
            user_id=USER_B,
            conversation_id=CONV_A,
        )

        self.assertIsNone(other_user_conv)

    async def test_update_conversation_rejects_other_users_thread(self) -> None:
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        result = await update_conversation(
            session,
            user_id=USER_B,
            conversation_id=CONV_A,
            title="hijacked",
            messages=[],
        )

        self.assertIsNone(result)

    async def test_delete_conversation_rejects_other_users_thread(self) -> None:
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        deleted = await delete_conversation(
            session,
            user_id=USER_B,
            conversation_id=CONV_A,
        )

        self.assertFalse(deleted)
        session.delete.assert_not_called()

    async def test_update_conversation_updates_owned_thread(self) -> None:
        session = AsyncMock()
        existing = _fake_matching_conversation()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing)),
        )
        session.flush = AsyncMock()
        session.refresh = AsyncMock(side_effect=lambda row: None)

        updated = await update_conversation(
            session,
            user_id=USER_A,
            conversation_id=CONV_A,
            title="  updated title  ",
            messages=[ChatMessage(role="user", content="hi")],
        )

        self.assertEqual(updated.title, "updated title")
        self.assertEqual(updated.messages, [{"role": "user", "content": "hi"}])

    async def test_delete_conversation_deletes_owned_thread(self) -> None:
        session = AsyncMock()
        existing = _fake_matching_conversation()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing)),
        )
        session.delete = AsyncMock()
        session.flush = AsyncMock()

        deleted = await delete_conversation(session, user_id=USER_A, conversation_id=CONV_A)

        self.assertTrue(deleted)
        session.delete.assert_awaited_once_with(existing)


class UpsertConversationTests(unittest.IsolatedAsyncioTestCase):
    """Idempotent upsert keyed by the client-generated conversation id."""

    def setUp(self) -> None:
        self.owner_patcher = patch(
            "services.conversation_service.get_user_by_id",
            new=AsyncMock(return_value=object()),
        )
        self.owner_patcher.start()
        self.addCleanup(self.owner_patcher.stop)

    def _session_with_get(self, existing: object | None = None):
        session = AsyncMock()
        conv_result = MagicMock()
        conv_result.scalar_one_or_none.return_value = existing
        patched = {
            "execute": AsyncMock(return_value=conv_result),
            "get": AsyncMock(return_value=existing),
            "add": MagicMock(),
            "flush": AsyncMock(),
            "refresh": AsyncMock(side_effect=lambda row: None),
        }
        session.configure_mock(**patched)
        return session

    async def test_creates_when_no_existing_row(self) -> None:
        session = self._session_with_get(existing=None)
        client_id = str(uuid.uuid4())

        conversation = await upsert_conversation_by_client_id(
            session,
            user_id=USER_A,
            client_conversation_id=client_id,
            messages=[{"role": "user", "content": "安排明天会议"}],
            settings=None,
        )

        self.assertIsNotNone(conversation)
        self.assertEqual(conversation.client_conversation_id, client_id)
        self.assertEqual(conversation.id, uuid.UUID(client_id))
        self.assertEqual(conversation.messages[0]["content"], "安排明天会议")

    async def test_explicit_real_title_is_preserved(self) -> None:
        session = self._session_with_get(existing=None)
        conversation = await upsert_conversation_by_client_id(
            session,
            user_id=USER_A,
            client_conversation_id=str(uuid.uuid4()),
            messages=[{"role": "user", "content": "帮我订周五的电影票"}],
            settings=None,
            title=" 周末出游  ",
        )
        self.assertEqual(conversation.title, "周末出游")

    async def test_placeholder_title_uses_truncated_first_user_text(self) -> None:
        session = self._session_with_get(existing=None)
        long_text = "帮我安排下周全部会议的会议室预订并同步给所有参会人"
        conversation = await upsert_conversation_by_client_id(
            session,
            user_id=USER_A,
            client_conversation_id=str(uuid.uuid4()),
            messages=[{"role": "user", "content": long_text}],
            settings=None,
            title=None,
        )
        self.assertIsNotNone(conversation.title)
        self.assertEqual(conversation.title, long_text[:30])

    async def test_lookup_by_client_id_finds_row(self) -> None:
        existing = _fake_matching_conversation()
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        session.execute = AsyncMock(return_value=result)

        found = await _conversation_by_client_id_for_user(
            session,
            user_id=USER_A,
            client_conversation_id=str(uuid.uuid4()),
        )
        self.assertIs(found, existing)

    async def test_existing_title_is_never_clobbered_by_upsert(self) -> None:
        """Bug 2 fix: a subsequent turn's upsert must not overwrite the title
        derived from the first message."""
        existing = _fake_matching_conversation()
        existing.title = "周末出游"
        session = self._session_with_get(existing=existing)

        conversation = await upsert_conversation_by_client_id(
            session,
            user_id=USER_A,
            client_conversation_id=str(existing.id),
            messages=[
                {"role": "user", "content": "第一句：帮我安排周末出游"},
                {"role": "assistant", "content": "好的。"},
                {"role": "user", "content": "第二句：再把行程发我"},
                {"role": "assistant", "content": "已发。"},
            ],
            settings=None,
            title="第二句：再把行程发我",
        )

        # Title stays as the original (first-message-derived) title, and messages update.
        self.assertIs(conversation, existing)
        self.assertEqual(conversation.title, "周末出游")
        self.assertEqual(conversation.messages[-1]["content"], "已发。")


if __name__ == "__main__":
    unittest.main()