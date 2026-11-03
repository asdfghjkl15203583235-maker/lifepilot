"""Regression tests for `delete_task` orphan-cleanup of linked reminders."""

import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from services.task_service import delete_task


def _make_task_row() -> MagicMock:
    task = MagicMock()
    task.id = uuid.uuid4()
    task.user_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    return task


class DeleteTaskCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_task_cancels_linked_reminders(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        task_id = uuid.uuid4()
        user_id = uuid.UUID("00000000-0000-0000-0000-000000000002")

        # get_task_for_user returns the task row.
        with patch(
            "services.task_service.get_task_for_user",
            new=AsyncMock(return_value=_make_task_row()),
        ) as mock_get, patch(
            "services.reminder_service.cancel_task_reminders",
            new=AsyncMock(return_value=2),
        ) as mock_cancel:
            result = await delete_task(session, task_id=task_id, user_id=user_id)

        self.assertTrue(result)
        mock_get.assert_awaited_once()
        # Cancel must run before the task row is deleted.
        mock_cancel.assert_awaited_once_with(session, task_id)
        self.assertTrue(session.delete.called)
        self.assertTrue(session.flush.called)

    async def test_delete_task_missing_skips_cancel_and_delete(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        task_id = uuid.uuid4()
        user_id = uuid.UUID("00000000-0000-0000-0000-000000000002")

        with patch(
            "services.task_service.get_task_for_user",
            new=AsyncMock(return_value=None),
        ) as mock_get, patch(
            "services.reminder_service.cancel_task_reminders",
            new=AsyncMock(),
        ) as mock_cancel:
            result = await delete_task(session, task_id=task_id, user_id=user_id)

        self.assertFalse(result)
        mock_get.assert_awaited_once()
        mock_cancel.assert_not_awaited()
        session.delete.assert_not_called()

    async def test_delete_task_cancel_failure_still_deletes_task(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        task_id = uuid.uuid4()
        user_id = uuid.UUID("00000000-0000-0000-0000-000000000002")

        with patch(
            "services.task_service.get_task_for_user",
            new=AsyncMock(return_value=_make_task_row()),
        ), patch(
            "services.reminder_service.cancel_task_reminders",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await delete_task(session, task_id=task_id, user_id=user_id)

        self.assertTrue(result)
        self.assertTrue(session.delete.called)


if __name__ == "__main__":
    unittest.main()
