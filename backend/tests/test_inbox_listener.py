"""模块五：邮箱主动监听（IMAP 轮询 → LLM 抽取 → 任务 + 通知）。

关键约束：
  - 朋友邀约不含「会议」关键词，也必须能被识别；
  - 同一封邮件（Message-ID）绝不重复建任务/重复通知。
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services import inbox_listener
from services.inbox_listener import is_noise_message, poll_mail163_inbox_for_events

USER_ID = uuid.uuid4()
FUTURE = datetime.now(timezone.utc) + timedelta(days=3)


def _listing(**overrides) -> dict:
    message = {
        "id": "101",
        "messageId": "<invite-1@example.com>",
        "subject": "周六一起吃个饭？",
        "snippet": "",
        "from": "张伟 <zhang@example.com>",
    }
    message.update(overrides)
    return {"messages": [message]}


EVENT = {
    "kind": "social",
    "title": "和张伟吃饭",
    "starts_at": FUTURE,
    "ends_at": None,
    "location": "南京西路",
    "reason": "周六晚 7 点",
}


class NoiseFilterTests(unittest.TestCase):
    def test_no_reply_and_newsletter_are_noise(self) -> None:
        self.assertTrue(is_noise_message("no-reply@shop.com", "你的订单已发货"))
        self.assertTrue(is_noise_message("news@brand.com", "本周订阅推荐"))
        self.assertFalse(is_noise_message("zhang@example.com", "周六一起吃个饭？"))


class InboxListenerTests(unittest.IsolatedAsyncioTestCase):
    def _session(self) -> AsyncMock:
        session = AsyncMock()
        session.add = MagicMock()
        return session

    async def _run(
        self,
        *,
        seen: bool = False,
        existing_task=None,
        signal: dict | None = None,
        **listing_overrides,
    ):
        session = self._session()
        created_task = SimpleNamespace(id=uuid.uuid4())
        signal_payload = signal or {
            "is_event": True,
            "event": dict(EVENT),
            "action_required": False,
            "action_reason": None,
        }
        with patch.object(
            inbox_listener, "list_emails", new=AsyncMock(return_value=_listing(**listing_overrides))
        ), patch.object(
            inbox_listener,
            "get_email",
            new=AsyncMock(
                return_value={
                    "subject": "周六一起吃个饭？",
                    "body": "周六晚上 7 点，南京西路，老地方见。",
                    "from": "张伟 <zhang@example.com>",
                }
            ),
        ), patch.object(
            inbox_listener, "chat_llm", return_value=MagicMock()
        ), patch.object(
            inbox_listener,
            "extract_inbox_signal_with_llm",
            new=AsyncMock(return_value=signal_payload),
        ) as extract, patch.object(
            inbox_listener.task_service,
            "create_task_for_user",
            new=AsyncMock(return_value=created_task),
        ) as create_task, patch.object(
            inbox_listener, "add_email_event_notification", new=AsyncMock()
        ) as notify, patch.object(
            inbox_listener, "_is_marked_seen", new=AsyncMock(return_value=seen)
        ), patch.object(
            inbox_listener, "_mark_seen", new=AsyncMock()
        ) as mark_seen, patch.object(
            inbox_listener,
            "find_task_for_email_message",
            new=AsyncMock(return_value=existing_task),
        ):
            result = await poll_mail163_inbox_for_events(
                session,
                user_id=USER_ID,
                config=MagicMock(),
                settings=MagicMock(openrouter_api_key="test-key"),
            )
        return result, create_task, notify, mark_seen, extract

    async def test_social_invite_creates_task_and_notification(self) -> None:
        result, create_task, notify, mark_seen, _extract = await self._run()

        self.assertEqual(result.tasks_created, 1)
        self.assertEqual(result.items[0]["title"], "和张伟吃饭")
        body = create_task.await_args.kwargs["body"]
        self.assertEqual(body.title, "邀约：和张伟吃饭")
        self.assertEqual(body.due_at, FUTURE)
        self.assertEqual(body.source_metadata["email_message_id"], "<invite-1@example.com>")
        notify.assert_awaited_once()
        self.assertIn("加入待办", notify.await_args.kwargs["title"])
        mark_seen.assert_awaited_once()

    async def test_seen_message_id_is_not_processed_twice(self) -> None:
        result, create_task, notify, _mark_seen, extract = await self._run(seen=True)

        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.tasks_created, 0)
        create_task.assert_not_awaited()
        notify.assert_not_awaited()
        extract.assert_not_awaited()

    async def test_existing_task_in_db_blocks_repeat(self) -> None:
        result, create_task, _notify, _mark_seen, _extract = await self._run(
            existing_task=SimpleNamespace(id=uuid.uuid4())
        )

        self.assertEqual(result.duplicates, 1)
        create_task.assert_not_awaited()

    async def test_noise_sender_is_skipped(self) -> None:
        result, create_task, _notify, _mark_seen, _extract = await self._run(
            **{"from": "no-reply@shop.com", "subject": "你的订单已发货"}
        )

        self.assertEqual(result.skipped, 1)
        create_task.assert_not_awaited()

    async def test_action_required_is_logged_without_creating_a_task(self) -> None:
        signal = {
            "is_event": False,
            "event": None,
            "action_required": True,
            "action_reason": "要求周五前交报表",
        }

        with self.assertLogs("services.inbox_listener", level="INFO") as captured:
            result, create_task, _notify, _mark_seen, _extract = await self._run(signal=signal)

        self.assertEqual(result.tasks_created, 0)
        create_task.assert_not_awaited()
        self.assertTrue(any("action-required" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
