"""Tests for the orchestrator one-off reminder tool (`create_reminder`)."""

import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from agents.tools.reminder_tool import make_create_reminder_tool
from lib.enums import ReminderStatus, ReminderTriggerType, TaskStatus


USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
# A fixed UTC-ish instant so tests don't depend on the clock for default tz.
_NOW = datetime(2026, 10, 24, 2, 0, 0, tzinfo=timezone.utc)


def _make_reminder_row(**overrides):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.user_id = USER_ID
    row.title = "改简历"
    row.remind_at = _NOW
    row.trigger_type = ReminderTriggerType.MANUAL
    row.status = ReminderStatus.PENDING
    row.payload = {}
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _make_task_row():
    task = MagicMock()
    task.id = uuid.uuid4()
    task.user_id = USER_ID
    return task


class ReminderToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.session = AsyncMock()
        self.session.flush = AsyncMock()
        # The tool's dedup pre-check runs `session.execute(...).scalar_one_or_none()`.
        # Default to no duplicate so the "created" path is exercised.
        dedup_result = MagicMock()
        dedup_result.scalar_one_or_none.return_value = None
        self.session.execute = AsyncMock(return_value=dedup_result)
        create_reminder_patcher = patch(
            "agents.tools.reminder_tool.create_reminder",
            new=AsyncMock(return_value=_make_reminder_row()),
        )
        create_task_patcher = patch(
            "agents.tools.reminder_tool.create_task_for_user",
            new=AsyncMock(return_value=_make_task_row()),
        )
        self.mock_create_reminder = create_reminder_patcher.start()
        self.mock_create_task = create_task_patcher.start()
        self.addCleanup(create_reminder_patcher.stop)
        self.addCleanup(create_task_patcher.stop)

    def _make_settings(self, timezone: str = "Asia/Shanghai"):
        settings = MagicMock()
        settings.planner_timezone = timezone
        return settings

    async def test_creates_reminder_and_task(self) -> None:
        tool = make_create_reminder_tool(self.session, USER_ID, self._make_settings())
        now_utc = datetime.now(timezone.utc)
        remind_at = (now_utc + timedelta(hours=1)).isoformat()

        raw = await tool.ainvoke({"title": "改简历", "remind_at": remind_at})
        result = json.loads(raw)

        self.assertEqual(result["status"], "created")
        self.assertTrue(result["reminder_id"])
        self.assertTrue(result["task_id"])

        # Reminder must have trigger_type=MANUAL and payload source chat.
        request = self.mock_create_reminder.call_args.args[2]
        self.assertEqual(request.trigger_type, ReminderTriggerType.MANUAL)
        self.assertEqual(request.payload, {"source": "chat"})

        # Task must be a user-visible task with due_at == remind_at.
        task_body = self.mock_create_task.call_args.kwargs["body"]
        self.assertEqual(task_body.source, "user")
        self.assertEqual(task_body.status, TaskStatus.NOT_STARTED)
        self.assertIsNotNone(task_body.due_at)
        self.assertIn("reminder_id", (task_body.source_metadata or {}))

    async def test_missing_args_raise_validation_error(self) -> None:
        from pydantic import ValidationError

        tool = make_create_reminder_tool(self.session, USER_ID, self._make_settings())
        with self.assertRaises(ValidationError):
            await tool.ainvoke({"title": "", "remind_at": None})
        self.assertFalse(self.mock_create_reminder.called)

    async def test_rejects_bad_remind_at(self) -> None:
        from pydantic import ValidationError

        tool = make_create_reminder_tool(self.session, USER_ID, self._make_settings())
        with self.assertRaises(ValidationError):
            await tool.ainvoke({"title": "开会", "remind_at": "not-a-date"})
        self.assertFalse(self.mock_create_reminder.called)

    async def test_normalizes_naive_remind_at_to_utc(self) -> None:
        tool = make_create_reminder_tool(self.session, USER_ID, self._make_settings())
        # Naive local time in Shanghai: 2026-10-25 08:00 local == 2026-10-25 00:00 UTC.
        naive = "2026-10-25T08:00:00"
        raw = await tool.ainvoke({"title": "改简历", "remind_at": naive})
        result = json.loads(raw)
        self.assertEqual(result["status"], "created")
        request = self.mock_create_reminder.call_args.args[2]
        self.assertEqual(request.remind_at, datetime(2026, 10, 25, 0, 0, 0, tzinfo=timezone.utc))

    async def test_duplicate_pending_reminder_returns_exists(self) -> None:
        # Simulate an existing PENDING reminder matching (user, title, remind_at window).
        existing = _make_reminder_row()
        dedup_result = MagicMock()
        dedup_result.scalar_one_or_none.return_value = existing
        self.session.execute = AsyncMock(return_value=dedup_result)

        tool = make_create_reminder_tool(self.session, USER_ID, self._make_settings())
        raw = await tool.ainvoke({"title": "改简历", "remind_at": "2026-10-25T08:00:00"})
        result = json.loads(raw)

        self.assertEqual(result["status"], "exists")
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["reminder_id"], str(existing.id))
        # No new row / task must be created on the duplicate path.
        self.assertFalse(self.mock_create_reminder.called)
        self.assertFalse(self.mock_create_task.called)


if __name__ == "__main__":
    unittest.main()