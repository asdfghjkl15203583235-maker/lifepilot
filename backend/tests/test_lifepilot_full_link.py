"""Full-link tests for LifePilot cross-scene flows.

These cover email meeting extraction, automatic pre-meeting reminder creation,
planner task persistence, deadline reminder scheduling, reminder delivery into
notifications, and idempotent life-service calls.
"""

import asyncio
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from lib.enums import (
    NotificationCategory,
    ReminderStatus,
    ReminderTriggerType,
    TaskPriority,
    TaskStatus,
)
from schemas.meeting_invite import MeetingInviteCreateRequest
from schemas.task import TaskCreateRequest
from services.idempotency_service import check_idempotent, record_idempotent
from services.meeting_invite_extraction import extract_meeting_invite
from services.meeting_invite_service import save_extracted_meeting_invite
from services.reminder_service import process_due_reminders, schedule_deadline_reminder, snooze_reminder
from services.task_service import create_task_for_user

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _execute_result(*, rows=(), scalar=None, rowcount=0) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.rowcount = rowcount
    result.scalars.return_value.all.return_value = list(rows)
    return result


def _async_session(*, rows=(), scalar=None, rowcount=0) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_execute_result(rows=rows, scalar=scalar, rowcount=rowcount),
    )
    session.flush = AsyncMock()
    session.refresh = AsyncMock(side_effect=lambda row: None)
    session.add = MagicMock()
    return session


class FullLinkMeetingInviteTests(unittest.TestCase):
    def test_email_invite_extraction_persists_meeting_reminder(self) -> None:
        text = (
            "Join the Product Sync meeting.\n"
            "Meeting number: 123456789\n"
            "https://example.com/join/abc\n"
            "Oct 20 at 2:30 PM"
        )
        extracted = extract_meeting_invite(text, source="email")
        self.assertEqual(extracted["meeting_number"], "123456789")
        self.assertEqual(extracted["join_url"], "https://example.com/join/abc")
        self.assertIsNotNone(extracted.get("starts_at"))
        self.assertEqual(extracted["starts_at"].hour, 14)
        self.assertEqual(extracted["starts_at"].minute, 30)

        session = _async_session()
        row = asyncio.run(
            save_extracted_meeting_invite(
                session,
                USER_ID,
                MeetingInviteCreateRequest(
                    title=extracted.get("title") or "Product Sync",
                    description=extracted.get("description") or "",
                    starts_at=extracted["starts_at"],
                    ends_at=extracted.get("ends_at"),
                    meeting_number=extracted.get("meeting_number"),
                    join_url=extracted.get("join_url"),
                    source="email",
                    external_id="msg-123",
                    reminder_minutes_before=15,
                    extra=extracted.get("extra") or {},
                ),
                found_in_email=True,
                email_message_id="msg-123",
            )
        )

        self.assertEqual(row.title, "Product Sync")
        self.assertEqual(row.meeting_number, "123456789")
        self.assertEqual(row.found_in_email, True)
        adds = [call.args[0] for call in session.add.call_args_list]
        self.assertEqual(len(adds), 2)
        reminder = adds[1]
        self.assertEqual(reminder.user_id, USER_ID)
        self.assertEqual(reminder.title, f"Meeting soon: {row.title}")
        self.assertEqual(reminder.trigger_type, ReminderTriggerType.MEETING)
        self.assertEqual(reminder.status, ReminderStatus.PENDING)
        self.assertEqual(reminder.payload["minutes_before"], 15)
        self.assertEqual(
            reminder.remind_at,
            extracted["starts_at"] - timedelta(minutes=15),
        )


class FullLinkPlannerTests(unittest.TestCase):
    def test_planner_task_persists_lifepilot_metadata_and_deadline_reminder(self) -> None:
        due_at = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
        session = _async_session(scalar=MagicMock(), rowcount=0)
        task = asyncio.run(
            create_task_for_user(
                session,
                user_id=USER_ID,
                body=TaskCreateRequest(
                    title="Submit expense report",
                    description="Auto-planned step",
                    project="工作",
                    assigned_agent="direct",
                    priority=TaskPriority.MEDIUM,
                    due_at=due_at,
                    dependencies=[{"id": "step-1", "step": "step-1"}],
                    plan_id="plan-2",
                    plan_source="planner",
                    source_metadata={"planner": "life_pilot", "plan_summary": "Plan the week"},
                ),
            )
        )

        self.assertEqual(task.project, "工作")
        self.assertEqual(task.plan_id, "plan-2")
        self.assertEqual(task.plan_source, "planner")
        self.assertEqual(task.assigned_agent, "direct")
        self.assertEqual(task.priority, TaskPriority.MEDIUM)
        self.assertEqual(task.dependencies, [{"id": "step-1", "step": "step-1"}])
        self.assertEqual(task.source_metadata["planner"], "life_pilot")

        reminder = asyncio.run(
            schedule_deadline_reminder(
                session,
                USER_ID,
                task.id,
                task.title,
                due_at,
                minutes_before=30,
            )
        )
        self.assertEqual(reminder.title, "Deadline: Submit expense report")
        self.assertEqual(reminder.trigger_type, ReminderTriggerType.DEADLINE)
        self.assertEqual(reminder.status, ReminderStatus.PENDING)
        self.assertEqual(reminder.payload["task_id"], str(task.id))
        self.assertEqual(
            reminder.remind_at,
            due_at - timedelta(minutes=30),
        )


class FullLinkReminderDeliveryTests(unittest.TestCase):
    def test_due_reminder_tick_creates_notification_and_marks_sent(self) -> None:
        reminder = MagicMock()
        reminder.user_id = USER_ID
        reminder.title = "Meeting soon: Product Sync"
        reminder.note = "Product Sync starts soon"
        reminder.status = ReminderStatus.PENDING
        reminder.sent_at = None
        reminder.task_id = uuid.UUID("99999999-0000-0000-0000-000000000099")

        session = _async_session(rows=[reminder])
        now = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
        delivered = asyncio.run(process_due_reminders(session, now))

        self.assertEqual(delivered, 1)
        self.assertEqual(reminder.status, ReminderStatus.SENT)
        self.assertEqual(reminder.sent_at, now)
        self.assertEqual(session.add.call_count, 1)
        notification = session.add.call_args.args[0]
        self.assertEqual(notification.user_id, USER_ID)
        self.assertEqual(notification.category, NotificationCategory.REMINDER)
        self.assertEqual(notification.is_read, False)
        self.assertEqual(notification.reminder_id, reminder.id)
        self.assertEqual(notification.task_id, reminder.task_id)


class FullLinkReminderSnoozeTests(unittest.TestCase):
    def test_snooze_reauth_reminder_in_pending_state(self) -> None:
        reminder = MagicMock()
        reminder.id = uuid.UUID("88888888-0000-0000-0000-000000000088")
        reminder.user_id = USER_ID
        reminder.title = "改简历"
        reminder.remind_at = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
        reminder.status = ReminderStatus.SENT
        reminder.sent_at = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)

        select_result = MagicMock()
        select_result.scalar_one_or_none.return_value = reminder
        update_result = MagicMock()
        update_result.rowcount = 1

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[select_result, update_result])
        session.flush = AsyncMock()
        session.refresh = AsyncMock()

        now = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
        with patch("services.reminder_service.datetime", wraps=datetime) as mock_dt:
            # Freeze `datetime.now(timezone.utc)` for the new due time.
            mock_dt.now.return_value = now
            updated = asyncio.run(snooze_reminder(session, USER_ID, reminder.id, minutes=15))

        self.assertIsNotNone(updated)
        self.assertEqual(reminder.status, ReminderStatus.PENDING)
        self.assertIsNone(reminder.sent_at)
        self.assertEqual(reminder.remind_at, now + timedelta(minutes=15))


class FullLinkTaskStatusWriteBackTests(unittest.TestCase):
    """Agent results must be written back to the persisted tasks table."""

    def test_supervisor_marks_successful_task_done(self) -> None:
        from agents.supervisor_node import supervisor_node

        persisted_task = MagicMock()
        persisted_task.status = TaskStatus.NOT_STARTED
        persisted_task.started_at = None
        persisted_task.completed_at = None
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            return_value=_execute_result(scalar=persisted_task)
        )

        task = {
            "id": "t1",
            "task_key": "t1",
            "persisted_task_id": "11111111-1111-1111-1111-111111111111",
            "intent": "x",
            "depends_on": [],
            "route": "direct",
        }
        state = {
            "tasks": [task],
            "agent_results": [{"task_key": "t1", "result": "ok", "attempt": 0}],
            "completed_task_ids": [],
            "plan": {"iteration": 0},
            "settings": MagicMock(planner_max_iterations=2),
            "user_id": USER_ID,
            "db_session": session,
        }
        result = asyncio.run(supervisor_node(state, MagicMock()))
        self.assertEqual(result["supervisor_decision"]["action"], "done")
        self.assertIn("t1", result["completed_task_ids"])
        self.assertEqual(persisted_task.status, TaskStatus.DONE)
        self.assertTrue(session.flush.called)

    def test_supervisor_resets_blocked_task_to_not_started(self) -> None:
        from agents.supervisor_node import supervisor_node

        persisted_task = MagicMock()
        persisted_task.status = TaskStatus.IN_PROGRESS
        persisted_task.started_at = None
        persisted_task.completed_at = None
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            return_value=_execute_result(scalar=persisted_task)
        )

        task = {
            "id": "t1",
            "task_key": "t1",
            "persisted_task_id": "11111111-1111-1111-1111-111111111111",
            "intent": "x",
            "depends_on": [],
            "route": "direct",
        }
        state = {
            "tasks": [task],
            "agent_results": [{"task_key": "t1", "result": "failed", "attempt": 0}],
            "completed_task_ids": [],
            "plan": {"iteration": 0},
            "settings": MagicMock(planner_max_iterations=1),
            "user_id": USER_ID,
            "db_session": session,
        }
        result = asyncio.run(supervisor_node(state, MagicMock()))
        self.assertEqual(result["supervisor_decision"]["action"], "replan")
        self.assertTrue(result["force_replan"])
        # 失败步骤交回规划器重建，不再原样带着 retry_count 硬重试。
        self.assertEqual(result["tasks"], [])
        self.assertEqual(
            [t["task_key"] for t in result["replan_context"]["failed"]], ["t1"]
        )
        self.assertEqual(persisted_task.status, TaskStatus.NOT_STARTED)


class FullLinkTaskInProgressMarkTests(unittest.TestCase):
    """Agent nodes must mark the persisted task IN_PROGRESS when execution starts."""

    def test_mark_task_in_progress_marks_persisted_task(self) -> None:
        from agents.common import mark_task_in_progress_if_persisted

        persisted_task = MagicMock()
        persisted_task.status = TaskStatus.NOT_STARTED
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            return_value=_execute_result(scalar=persisted_task)
        )

        state = {"db_session": session, "user_id": USER_ID}
        task = {"persisted_task_id": "11111111-1111-1111-1111-111111111111"}
        asyncio.run(mark_task_in_progress_if_persisted(state, task))
        self.assertEqual(persisted_task.status, TaskStatus.IN_PROGRESS)

    def test_mark_task_in_progress_skips_without_session(self) -> None:
        from agents.common import mark_task_in_progress_if_persisted

        state = {}
        task = {"persisted_task_id": "11111111-1111-1111-1111-111111111111"}
        # Should not raise when db_session is missing.
        asyncio.run(mark_task_in_progress_if_persisted(state, task))


class FullLinkIdempotencyTests(unittest.TestCase):
    def test_life_service_idempotency_roundtrip(self) -> None:
        key = "weather-key-1"
        payload = {
            "id": "weather-1",
            "service_name": "weather",
            "status": "success",
            "result": {"temperature_c": 22.0},
        }
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        with patch("services.idempotency_service.get_redis", return_value=redis):
            self.assertIsNone(asyncio.run(check_idempotent(key)))

        with patch("services.idempotency_service.get_redis", return_value=redis):
            asyncio.run(record_idempotent(key, payload))
        self.assertTrue(redis.set.called)

        redis.get = AsyncMock(return_value=json.dumps(payload))
        with patch("services.idempotency_service.get_redis", return_value=redis):
            cached = asyncio.run(check_idempotent(key))
        self.assertEqual(cached, payload)


if __name__ == "__main__":
    unittest.main()