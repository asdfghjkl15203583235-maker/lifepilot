"""Unit tests for next-run math (no DB)."""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from lib.enums import BackgroundJobStatus
from services.background_job_service import advance_next_run, compute_next_run_utc, parse_cron_fields


class TestBackgroundJobSchedule(unittest.TestCase):
    def test_parse_cron_rejects_wrong_arity(self) -> None:
        with self.assertRaises(ValueError):
            parse_cron_fields("0 0 * *")

    def test_every_minute_next(self) -> None:
        # Mid-minute — next fire should be the start of the following minute.
        start = datetime(2026, 1, 1, 12, 0, 30, tzinfo=timezone.utc)
        nxt = compute_next_run_utc("* * * * *", "UTC", start)
        self.assertIsNotNone(nxt)
        assert nxt is not None
        self.assertGreater(nxt, start)
        self.assertEqual(nxt, datetime(2026, 1, 1, 12, 1, 0, tzinfo=timezone.utc))

    def test_advance_next_run_sets_ended_when_next_not_before_end(self) -> None:
        job = MagicMock()
        job.cron_expression = "* * * * *"
        job.timezone_label = "UTC"
        job.schedule_ends_at = datetime(2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc)
        job.status = BackgroundJobStatus.ACTIVE
        tick = datetime(2026, 1, 1, 12, 4, 15, tzinfo=timezone.utc)
        after = datetime(2026, 1, 1, 12, 4, 1, tzinfo=timezone.utc)
        advance_next_run(job, after_utc=after, tick_now=tick)
        self.assertIsNone(job.next_run_at)
        self.assertEqual(job.status, BackgroundJobStatus.ENDED)
        self.assertEqual(job.ended_at, tick)

    def test_advance_next_run_leaves_next_when_before_exclusive_end(self) -> None:
        job = MagicMock()
        job.cron_expression = "* * * * *"
        job.timezone_label = "UTC"
        job.schedule_ends_at = datetime(2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc)
        job.status = BackgroundJobStatus.ACTIVE
        tick = datetime(2026, 1, 1, 12, 2, 0, tzinfo=timezone.utc)
        after = datetime(2026, 1, 1, 12, 1, 1, tzinfo=timezone.utc)
        advance_next_run(job, after_utc=after, tick_now=tick)
        self.assertEqual(job.next_run_at, datetime(2026, 1, 1, 12, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(job.status, BackgroundJobStatus.ACTIVE)


if __name__ == "__main__":
    unittest.main()
