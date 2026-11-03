"""Unit tests for LifePilot cross-scene and life-service helpers."""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from services.idempotency_service import check_idempotent, record_idempotent
from services.meeting_invite_extraction import extract_meeting_invite


class MeetingInviteExtractionTests(unittest.TestCase):
    def test_extracts_meeting_number_url_and_time(self) -> None:
        text = (
            "Join the Product Sync meeting.\n"
            "Meeting number: 123456789\n"
            "https://example.com/join/abc\n"
            "Oct 20 at 2:30 PM"
        )
        result = extract_meeting_invite(text)
        self.assertEqual(result["meeting_number"], "123456789")
        self.assertEqual(result["join_url"], "https://example.com/join/abc")
        self.assertIsNotNone(result["starts_at"])
        self.assertEqual(result["starts_at"].hour, 14)
        self.assertEqual(result["starts_at"].minute, 30)
        self.assertIsNotNone(result["ends_at"])

    def test_deterministic_extraction_keeps_source_snippet(self) -> None:
        text = "Reminder about tomorrow."
        result = extract_meeting_invite(text, source="email")
        self.assertEqual(result["source"], "email")
        self.assertIn("source_text_snippet", result["extra"])
        self.assertNotIn("meeting_number", result)


class IdempotencyServiceTests(unittest.TestCase):
    def test_record_and_read_roundtrip(self) -> None:
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        with patch("services.idempotency_service.get_redis", return_value=redis):
            cached = asyncio.run(check_idempotent("key-1"))
        self.assertIsNone(cached)

        payload = {"status": "success", "id": "abc"}
        with patch("services.idempotency_service.get_redis", return_value=redis):
            asyncio.run(record_idempotent("key-1", payload))
        self.assertTrue(redis.set.called)

        redis.get = AsyncMock(return_value=json.dumps(payload))
        with patch("services.idempotency_service.get_redis", return_value=redis):
            cached = asyncio.run(check_idempotent("key-1"))
        self.assertEqual(cached, payload)


class LifePreferenceCacheTests(unittest.TestCase):
    def test_upsert_invalidates_redis_cache(self) -> None:
        session = AsyncMock()
        existing = MagicMock()
        existing.habits = []
        existing.time_preferences = {}
        existing.work_hours = {}
        existing.priority_rules = []
        existing.raw_notes = None

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.delete = AsyncMock(return_value=True)

        from services import long_term_preference_service
        from services.long_term_preference_service import upsert_life_preference

        with (
            patch.object(
                long_term_preference_service,
                "get_user_life_preference",
                new=AsyncMock(return_value=existing),
            ),
            patch.object(
                long_term_preference_service,
                "get_redis",
                return_value=redis,
            ),
        ):
            row = asyncio.run(
                upsert_life_preference(session, "00000000-0000-0000-0000-000000000001", habits=[])
            )
        self.assertIs(row, existing)
        self.assertTrue(redis.delete.called)


if __name__ == "__main__":
    unittest.main()