"""Completing a task must silence its still-pending reminders.

`get_due_reminders` delivers anything with `status == PENDING` whose `remind_at`
has passed, regardless of the linked task's state. Deadline reminders are armed
30 minutes early, so without this cleanup the user ticks the task off and is
reminded again half an hour later.
"""

import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from schemas.task import TaskUpdateRequest
from services.task_service import update_task

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_task_row() -> MagicMock:
    task = MagicMock()
    task.id = uuid.uuid4()
    task.user_id = USER_ID
    task.completed_at = None
    task.started_at = None
    return task


class CompleteTaskCancelsRemindersTests(unittest.IsolatedAsyncioTestCase):
    async def test_marking_task_done_cancels_pending_reminders(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        task = _make_task_row()

        with patch(
            "services.task_service.get_task_for_user",
            new=AsyncMock(return_value=task),
        ), patch(
            "services.reminder_service.cancel_task_reminders",
            new=AsyncMock(return_value=2),
        ) as mock_cancel:
            result = await update_task(
                session,
                task_id=task.id,
                user_id=USER_ID,
                body=TaskUpdateRequest(status="done"),
            )

        self.assertIs(result, task)
        mock_cancel.assert_awaited_once_with(session, task.id)
        self.assertIsNotNone(task.completed_at)

    async def test_title_only_update_leaves_reminders_armed(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        task = _make_task_row()

        with patch(
            "services.task_service.get_task_for_user",
            new=AsyncMock(return_value=task),
        ), patch(
            "services.reminder_service.cancel_task_reminders",
            new=AsyncMock(),
        ) as mock_cancel:
            await update_task(
                session,
                task_id=task.id,
                user_id=USER_ID,
                body=TaskUpdateRequest(title="改个标题"),
            )

        mock_cancel.assert_not_awaited()

    async def test_in_progress_status_leaves_reminders_armed(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        task = _make_task_row()

        with patch(
            "services.task_service.get_task_for_user",
            new=AsyncMock(return_value=task),
        ), patch(
            "services.reminder_service.cancel_task_reminders",
            new=AsyncMock(),
        ) as mock_cancel:
            await update_task(
                session,
                task_id=task.id,
                user_id=USER_ID,
                body=TaskUpdateRequest(status="in_progress"),
            )

        mock_cancel.assert_not_awaited()

    async def test_cancel_failure_still_marks_task_done(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        task = _make_task_row()

        with patch(
            "services.task_service.get_task_for_user",
            new=AsyncMock(return_value=task),
        ), patch(
            "services.reminder_service.cancel_task_reminders",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await update_task(
                session,
                task_id=task.id,
                user_id=USER_ID,
                body=TaskUpdateRequest(status="done"),
            )

        self.assertIs(result, task)
        self.assertIsNotNone(task.completed_at)


if __name__ == "__main__":
    unittest.main()
