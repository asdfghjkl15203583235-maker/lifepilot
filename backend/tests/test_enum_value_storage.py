"""Regression tests: SQLAlchemy SqlEnum columns must store/read the enum .value (lowercase).

The DB columns are plain VARCHAR (native_enum=False). SQLAlchemy's SqlEnum default
lookup is the enum *name* (uppercase) unless `values_callable` is provided, which
diverges from the lowercase `.value` used everywhere else in the codebase and leads
to LookupError on read and wrong-case INSERTs on write.

These tests assert the columns are configured to round-trip `.value` strings.
"""

import unittest

from lib.enums import (
    BackgroundJobRunOutcome,
    BackgroundJobStatus,
    IntegrationProvider,
    NotificationCategory,
    ReminderStatus,
    ReminderTriggerType,
    TaskPriority,
    TaskStatus,
)
from models.background_job import BackgroundJob
from models.background_job_run import BackgroundJobRun
from models.task import Task
from models.user_integration import UserIntegration
from models.user_notification import UserNotification
from models.user_reminder import UserReminder


def _values_callable(column) -> object:
    return column.type.values_callable or (lambda enums: [e.name for e in enums])


class EnumColumnsStoreValueTests(unittest.TestCase):
    def _assert_lowercase_roundtrip(self, enum_cls, column) -> None:
        values_callable = _values_callable(column)
        # The effective DB string for each enum member must be its .value.
        for member in enum_cls:
            self.assertEqual(
                member.value,
                values_callable(enum_cls)[list(enum_cls).index(member)],
                f"{enum_cls.__name__}.{member.name} must map to {member.value!r}",
            )

    def test_task_status_column_maps_to_lowercase_values(self) -> None:
        self._assert_lowercase_roundtrip(TaskStatus, Task.__table__.c.status)

    def test_task_priority_column_maps_to_lowercase_values(self) -> None:
        self._assert_lowercase_roundtrip(TaskPriority, Task.__table__.c.priority)

    def test_background_job_status_column_maps_to_lowercase_values(self) -> None:
        self._assert_lowercase_roundtrip(BackgroundJobStatus, BackgroundJob.__table__.c.status)

    def test_background_job_run_outcome_column_maps_to_lowercase_values(self) -> None:
        self._assert_lowercase_roundtrip(BackgroundJobRunOutcome, BackgroundJobRun.__table__.c.outcome)

    def test_integration_provider_column_maps_to_lowercase_values(self) -> None:
        self._assert_lowercase_roundtrip(IntegrationProvider, UserIntegration.__table__.c.provider)

    def test_notification_category_column_maps_to_lowercase_values(self) -> None:
        self._assert_lowercase_roundtrip(NotificationCategory, UserNotification.__table__.c.category)

    def test_reminder_trigger_type_column_maps_to_lowercase_values(self) -> None:
        self._assert_lowercase_roundtrip(ReminderTriggerType, UserReminder.__table__.c.trigger_type)

    def test_reminder_status_column_maps_to_lowercase_values(self) -> None:
        self._assert_lowercase_roundtrip(ReminderStatus, UserReminder.__table__.c.status)


if __name__ == "__main__":
    unittest.main()