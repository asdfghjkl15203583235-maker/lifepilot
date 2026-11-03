"""新邮件提醒：基线、噪音、幂等、去重与"只提醒最新一封"。"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services import new_mail_notifier
from services.new_mail_notifier import notify_new_inbox_mail

USER_ID = uuid.uuid4()
BASELINE = datetime.now(timezone.utc)


def _message(**overrides) -> dict:
    message = {
        "id": "101",
        "messageId": "<m1@example.com>",
        "subject": "周六一起吃个饭？",
        "from": "张伟 <zhang@example.com>",
        "date": (BASELINE + timedelta(minutes=1)).isoformat(),
        "snippet": "老地方见",
    }
    message.update(overrides)
    return message


class NewMailNotifierTests(unittest.IsolatedAsyncioTestCase):
    def _session(self) -> AsyncMock:
        session = AsyncMock()
        session.add = MagicMock()
        return session

    async def _run(
        self,
        *,
        messages: list[dict],
        skip_message_ids: set[str] | None = None,
        created: bool = True,
        existing_task=None,
    ):
        session = self._session()
        created_row = SimpleNamespace(id=uuid.uuid4()) if created else None
        with patch.object(
            new_mail_notifier,
            "list_emails",
            new=AsyncMock(return_value={"messages": messages}),
        ), patch.object(
            new_mail_notifier,
            "add_new_mail_notification",
            new=AsyncMock(return_value=created_row),
        ) as add, patch.object(
            new_mail_notifier,
            "find_task_for_email_message",
            new=AsyncMock(return_value=existing_task),
        ):
            result = await notify_new_inbox_mail(
                session,
                user_id=USER_ID,
                config=MagicMock(),
                baseline=BASELINE,
                skip_message_ids=skip_message_ids,
            )
        return result, add

    async def test_new_message_is_announced(self):
        result, add = await self._run(messages=[_message()])

        self.assertEqual(result.notified, 1)
        self.assertEqual(result.skipped, 0)
        add.assert_awaited_once()
        self.assertEqual(add.await_args.kwargs["email_message_id"], "<m1@example.com>")

    async def test_message_older_than_baseline_is_ignored(self):
        old = _message(date=(BASELINE - timedelta(days=1)).isoformat())

        result, add = await self._run(messages=[old])

        self.assertEqual(result.notified, 0)
        self.assertEqual(result.skipped, 1)
        add.assert_not_awaited()

    async def test_noise_sender_is_ignored(self):
        noise = _message(**{"from": "no-reply@shop.example.com", "subject": "限时优惠"})

        result, add = await self._run(messages=[noise])

        self.assertEqual(result.notified, 0)
        add.assert_not_awaited()

    async def test_message_handled_by_event_path_is_skipped(self):
        result, add = await self._run(
            messages=[_message()],
            skip_message_ids={"<m1@example.com>"},
        )

        self.assertEqual(result.notified, 0)
        add.assert_not_awaited()

    async def test_message_that_already_has_a_task_is_not_announced(self):
        result, add = await self._run(
            messages=[_message()],
            existing_task=SimpleNamespace(id=uuid.uuid4()),
        )

        self.assertEqual(result.notified, 0)
        self.assertEqual(result.duplicates, 1)
        add.assert_not_awaited()

    async def test_already_announced_message_counts_as_duplicate(self):
        result, add = await self._run(messages=[_message()], created=False)

        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.notified, 0)
        add.assert_awaited_once()

    async def test_only_the_newest_message_is_announced(self):
        older = _message(id="1", messageId="<old@example.com>", date=(BASELINE + timedelta(minutes=1)).isoformat())
        newer = _message(id="2", messageId="<new@example.com>", date=(BASELINE + timedelta(minutes=6)).isoformat())

        result, add = await self._run(messages=[older, newer])

        self.assertEqual(result.notified, 1)
        add.assert_awaited_once()
        self.assertEqual(add.await_args.kwargs["email_message_id"], "<new@example.com>")

    async def test_missing_message_id_falls_back_to_uid(self):
        result, add = await self._run(messages=[_message(id="77", messageId="")])

        self.assertEqual(result.notified, 1)
        self.assertEqual(add.await_args.kwargs["email_message_id"], "uid:77")

    async def test_message_without_date_is_skipped(self):
        result, add = await self._run(messages=[_message(date=None)])

        self.assertEqual(result.notified, 0)
        self.assertEqual(result.skipped, 1)
        add.assert_not_awaited()

    async def test_listing_failure_keeps_baseline_and_reports_error(self):
        session = self._session()
        with patch.object(
            new_mail_notifier,
            "list_emails",
            new=AsyncMock(side_effect=RuntimeError("connection reset")),
        ), patch.object(
            new_mail_notifier,
            "add_new_mail_notification",
            new=AsyncMock(),
        ) as add:
            result = await notify_new_inbox_mail(
                session,
                user_id=USER_ID,
                config=MagicMock(),
                baseline=BASELINE,
            )

        self.assertEqual(result.notified, 0)
        self.assertTrue(result.errors)
        add.assert_not_awaited()
