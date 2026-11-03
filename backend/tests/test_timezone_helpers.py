"""Tests for the planner tz helpers that keep naive wall-clock times aligned with
the user's local timezone (regression guard for "上午 10 点任务下午才提醒")."""

import unittest
from datetime import datetime, timezone

from agents.common import resolve_local_datetime, tz_name_of


class TzNameOfTests(unittest.TestCase):
    def test_uses_planner_timezone(self) -> None:
        settings = type("S", (), {"planner_timezone": "Asia/Shanghai"})()
        self.assertEqual(tz_name_of(settings), "Asia/Shanghai")

    def test_falls_back_to_utc_for_non_string_settings(self) -> None:
        settings = type("S", (), {"planner_timezone": 42})()
        self.assertEqual(tz_name_of(settings), "UTC")

    def test_falls_back_when_attr_missing(self) -> None:
        self.assertEqual(tz_name_of(object()), "UTC")


class ResolveLocalDatetimeTests(unittest.TestCase):
    def test_naive_time_is_interpreted_in_local_tz(self) -> None:
        # "上午 10 点" in Shanghai must be 02:00Z, not 10:00Z.
        got = resolve_local_datetime("2026-10-25T10:00:00", "Asia/Shanghai")
        self.assertEqual(got, datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc))

    def test_aware_z_time_is_not_double_shifted(self) -> None:
        got = resolve_local_datetime("2026-10-25T10:00:00Z", "Asia/Shanghai")
        self.assertEqual(got, datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc))

    def test_empty_value_returns_none(self) -> None:
        self.assertIsNone(resolve_local_datetime(None, "Asia/Shanghai"))
        self.assertIsNone(resolve_local_datetime("", "Asia/Shanghai"))

    def test_invalid_value_returns_none(self) -> None:
        self.assertIsNone(resolve_local_datetime("not-a-date", "Asia/Shanghai"))

    def test_naive_datetime_object_interpreted_in_local_tz(self) -> None:
        naive = datetime(2026, 10, 25, 8, 0)
        got = resolve_local_datetime(naive, "Asia/Shanghai")
        self.assertEqual(got, datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
