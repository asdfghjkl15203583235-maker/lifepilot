"""Unit tests for notification API mapping (no DB)."""

import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

from lib.enums import NotificationCategory
from services.notification_service import user_notification_to_api_dict


class TestUserNotificationSerialization(unittest.TestCase):
    def test_user_notification_to_api_dict_uses_zulu_suffix(self) -> None:
        row = MagicMock()
        row.id = uuid.UUID("b6c0c0c0-0000-0000-0000-000000000001")
        row.title = "Schedule completed: Test"
        row.body = "Done."
        row.category = NotificationCategory.BACKGROUND_JOB
        row.is_read = False
        row.created_at = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        out = user_notification_to_api_dict(row)
        self.assertEqual(out["id"], "b6c0c0c0-0000-0000-0000-000000000001")
        self.assertEqual(out["category"], "background_job")
        self.assertEqual(out["is_read"], False)
        self.assertEqual(out["created_at_iso"], "2026-04-24T12:00:00Z")


if __name__ == "__main__":
    unittest.main()
