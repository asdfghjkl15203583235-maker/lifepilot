"""邮件通知一键转任务：默认截止时间、幂等与错误边界。"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from lib.enums import NotificationCategory
from services import email_task_service


def _settings() -> SimpleNamespace:
    return SimpleNamespace(planner_timezone="Asia/Shanghai", reminder_default_lead_minutes=30)


def _notification(**overrides) -> SimpleNamespace:
    notification = SimpleNamespace(
        id=uuid.uuid4(),
        category=NotificationCategory.EMAIL,
        email_message_id="<m1@example.com>",
        email_uid="101",
        title="新邮件：请周五前交报表",
        body="发件人：李总\n时间：2026-11-03T10:00:00+08:00",
        task_id=None,
    )
    for key, value in overrides.items():
        setattr(notification, key, value)
    return notification


class DefaultDueTests(unittest.TestCase):
    def test_defaults_to_tomorrow_0900_in_planner_timezone(self) -> None:
        now = datetime(2026, 11, 3, 15, 30, tzinfo=timezone.utc)  # 23:30 +08:00

        due = email_task_service.next_default_due_at(_settings(), now=now)

        self.assertEqual(due.isoformat(), "2026-11-04T09:00:00+08:00")

    def test_unknown_timezone_falls_back_to_utc(self) -> None:
        settings = SimpleNamespace(planner_timezone="Mars/Olympus")
        now = datetime(2026, 11, 3, 15, 30, tzinfo=timezone.utc)

        due = email_task_service.next_default_due_at(settings, now=now)

        self.assertEqual(due.isoformat(), "2026-11-04T09:00:00+00:00")


class CreateTaskFromNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_email_notification_is_rejected(self) -> None:
        notification = _notification(category=NotificationCategory.REMINDER)

        with self.assertRaises(email_task_service.EmailNotificationNotActionableError):
            await email_task_service.create_task_from_email_notification(
                AsyncMock(),
                user_id=uuid.uuid4(),
                notification=notification,
                settings=_settings(),
            )

    async def test_linked_task_is_returned_without_creating_another(self) -> None:
        existing_task = SimpleNamespace(id=uuid.uuid4(), title="已有任务", due_at=None)
        session = AsyncMock()
        session.get = AsyncMock(return_value=existing_task)
        notification = _notification(task_id=existing_task.id)

        task, created = await email_task_service.create_task_from_email_notification(
            session,
            user_id=uuid.uuid4(),
            notification=notification,
            settings=_settings(),
        )

        self.assertFalse(created)
        self.assertEqual(task.id, existing_task.id)

    async def test_new_task_gets_tomorrow_0900_and_a_reminder(self) -> None:
        created_task = SimpleNamespace(id=uuid.uuid4(), title="请周五前交报表", due_at=None)
        notification = _notification()
        session = AsyncMock()
        session.get = AsyncMock(return_value=None)

        with patch(
            "services.inbox_listener.find_task_for_email_message",
            new=AsyncMock(return_value=None),
        ), patch.object(
            email_task_service.task_service,
            "create_task_for_user",
            new=AsyncMock(return_value=created_task),
        ) as create_task, patch.object(
            email_task_service,
            "schedule_deadline_reminder",
            new=AsyncMock(),
        ) as schedule:
            task, created = await email_task_service.create_task_from_email_notification(
                session,
                user_id=uuid.uuid4(),
                notification=notification,
                settings=_settings(),
            )

        self.assertTrue(created)
        self.assertEqual(task.id, created_task.id)

        body = create_task.await_args.kwargs["body"]
        self.assertEqual(body.title, "请周五前交报表")
        self.assertEqual((body.due_at.hour, body.due_at.minute), (9, 0))
        self.assertEqual(body.due_at, email_task_service.next_default_due_at(_settings()))
        self.assertEqual(body.source_metadata["email_message_id"], "<m1@example.com>")
        self.assertEqual(notification.task_id, created_task.id)
        schedule.assert_awaited_once()

    async def _run_with_email(self, *, event: dict | None, mail: dict | None = None):
        created_task = SimpleNamespace(id=uuid.uuid4(), title="评审会", due_at=None)
        notification = _notification()
        session = AsyncMock()
        session.get = AsyncMock(return_value=None)
        mail_payload = mail or {
            "subject": "评审会",
            "from": "李总 <li@example.com>",
            "date": "2026-11-03T02:00:00+00:00",
            "body": "12 月 1 日下午 2 点，会议室 A。",
            "snippet": "12 月 1 日下午 2 点",
        }

        with patch(
            "services.inbox_listener.find_task_for_email_message",
            new=AsyncMock(return_value=None),
        ), patch.object(
            email_task_service, "_build_mail_config", return_value=MagicMock()
        ), patch.object(
            email_task_service, "_load_email", new=AsyncMock(return_value=mail_payload)
        ), patch.object(
            email_task_service, "_extract_event", new=AsyncMock(return_value=event)
        ), patch.object(
            email_task_service.task_service,
            "create_task_for_user",
            new=AsyncMock(return_value=created_task),
        ) as create_task, patch.object(
            email_task_service,
            "schedule_deadline_reminder",
            new=AsyncMock(),
        ):
            await email_task_service.create_task_from_email_notification(
                session,
                user_id=uuid.uuid4(),
                notification=notification,
                settings=_settings(),
            )

        return create_task.await_args.kwargs["body"]

    async def test_meeting_time_in_email_becomes_the_deadline(self) -> None:
        meeting_at = datetime(2026, 12, 1, 14, 0, tzinfo=timezone(timedelta(hours=8)))

        body = await self._run_with_email(
            event={"starts_at": meeting_at, "location": "会议室 A", "title": "评审会"}
        )

        self.assertEqual(body.due_at, meeting_at)
        self.assertIn("会议时间：2026-12-01 14:00", body.description)
        self.assertIn("地点：会议室 A", body.description)

    async def test_meeting_time_already_passed_falls_back_to_tomorrow_0900(self) -> None:
        past = datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)

        body = await self._run_with_email(event={"starts_at": past})

        self.assertEqual(body.due_at, email_task_service.next_default_due_at(_settings()))

    async def test_mail_without_event_keeps_default_deadline(self) -> None:
        body = await self._run_with_email(event=None)

        self.assertEqual(body.due_at, email_task_service.next_default_due_at(_settings()))
        self.assertIn("收件时间：2026-11-03 10:00", body.description)
