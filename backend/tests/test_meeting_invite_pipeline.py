"""Meeting-invite pipeline: extraction → inbox scan → board task → detail API."""

import json
import unittest
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from auth.dependencies import get_current_user_id
from db.dependencies import verify_database_ready
from db.session import get_session
from lib.enums import TaskPriority, TaskStatus
from main import app
from models.task import Task
from services.meeting_invite_extraction import CN_TZ, extract_meeting_invite
from services.meeting_invite_scanner import (
    ScanResult,
    _as_utc,
    ensure_task_for_meeting_invite,
    looks_like_meeting_invite,
    scan_mail163_inbox_for_meeting_invites,
)

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class MeetingInviteExtractionTests(unittest.TestCase):
    def test_chinese_invite_uses_utc_plus_8(self) -> None:
        text = (
            "会议主题：产品评审\n"
            "时间：2030年1月2日 14:30-15:30\n"
            "会议号：123456789\n"
            "入会链接：https://meeting.tencent.com/dm/abc"
        )
        result = extract_meeting_invite(text, source="email")

        self.assertEqual(result["starts_at"], datetime(2030, 1, 2, 14, 30, tzinfo=CN_TZ))
        self.assertEqual(result["ends_at"], datetime(2030, 1, 2, 15, 30, tzinfo=CN_TZ))
        self.assertEqual(result["meeting_number"], "123456789")
        self.assertEqual(result["join_url"], "https://meeting.tencent.com/dm/abc")
        self.assertTrue(result["found_in_email"])

    def test_iso_style_is_local_time(self) -> None:
        result = extract_meeting_invite("项目同步 2030-01-02 09:15", source="manual")
        self.assertEqual(result["starts_at"], datetime(2030, 1, 2, 9, 15, tzinfo=CN_TZ))
        self.assertEqual(result["ends_at"], datetime(2030, 1, 2, 10, 15, tzinfo=CN_TZ))

    def test_english_invite_keeps_utc_behavior(self) -> None:
        result = extract_meeting_invite("Design review Oct 20, 2030 at 2:30 PM")
        self.assertEqual(
            result["starts_at"],
            datetime(2030, 10, 20, 14, 30, tzinfo=timezone.utc),
        )

    def test_text_without_time_has_no_start(self) -> None:
        result = extract_meeting_invite("下周三开个会，记得参加")
        self.assertNotIn("starts_at", result)

    def test_keyword_filter(self) -> None:
        self.assertTrue(looks_like_meeting_invite("Invitation: sprint retro", ""))
        self.assertTrue(looks_like_meeting_invite("【会议邀请】季度评审", ""))
        self.assertFalse(looks_like_meeting_invite("Quarterly numbers", "revenue up"))

    def test_as_utc_normalizes(self) -> None:
        self.assertEqual(
            _as_utc("2030-01-02T03:04:05Z"),
            datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            _as_utc(datetime(2030, 1, 2, 3, 4, 5)),
            datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        self.assertIsNone(_as_utc("not-a-date"))


def _invite(**overrides) -> SimpleNamespace:
    values = {
        "id": uuid.uuid4(),
        "title": "产品评审",
        "description": None,
        "starts_at": datetime(2030, 1, 2, 6, 30, tzinfo=timezone.utc),
        "ends_at": None,
        "meeting_number": None,
        "join_url": None,
        "email_message_id": "m1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class EnsureTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_task_is_refreshed_not_duplicated(self) -> None:
        session = AsyncMock()
        existing = MagicMock()
        scalars = MagicMock()
        scalars.first.return_value = existing
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars
        session.execute = AsyncMock(return_value=execute_result)

        invite = _invite()
        task, created = await ensure_task_for_meeting_invite(
            session, user_id=USER_ID, invite=invite
        )

        self.assertFalse(created)
        self.assertIs(task, existing)
        self.assertEqual(existing.title, "会议：产品评审")
        self.assertEqual(existing.due_at, invite.starts_at)
        session.flush.assert_awaited()

    async def test_new_task_carries_meeting_metadata(self) -> None:
        session = AsyncMock()
        scalars = MagicMock()
        scalars.first.return_value = None
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars
        session.execute = AsyncMock(return_value=execute_result)

        new_task = MagicMock()
        invite = _invite(join_url="https://meeting.tencent.com/dm/abc")
        with patch(
            "services.meeting_invite_scanner.task_service.create_task_for_user",
            new=AsyncMock(return_value=new_task),
        ) as mock_create:
            task, created = await ensure_task_for_meeting_invite(
                session,
                user_id=USER_ID,
                invite=invite,
                email_subject="会议邀请：产品评审",
            )

        self.assertTrue(created)
        self.assertIs(task, new_task)
        body = mock_create.call_args.kwargs["body"]
        self.assertEqual(body.title, "会议：产品评审")
        self.assertEqual(body.priority, TaskPriority.HIGH)
        self.assertEqual(body.assigned_agent, "calendar_agent")
        self.assertEqual(body.plan_source, "email_invite")
        self.assertEqual(body.source_metadata["meeting_invite_id"], str(invite.id))
        self.assertEqual(body.source_metadata["email_message_id"], "m1")
        self.assertEqual(body.source_metadata["email_subject"], "会议邀请：产品评审")
        self.assertIn("入会链接", body.description)


def _message(message_id: str, subject: str) -> dict:
    return {"id": message_id, "subject": subject, "snippet": ""}


class InboxScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_filters_skips_and_projects(self) -> None:
        listing = {
            "messages": [
                _message("m1", "会议邀请：产品评审"),
                _message("m2", "本周周报"),  # 无邀请关键词，不进入候选
                _message("m3", "Meeting invite: retro"),
                _message("m4", "会议邀请：已结束"),
                _message("m5", "会议邀请：已知"),
            ]
        }
        bodies = {
            "m1": ("产品评审", "会议主题：产品评审\n时间：2030年1月2日 14:30-15:30"),
            "m3": ("Retro", "Retro\nOct 20, 2030 at 2:30 PM"),
            "m4": ("旧会议", "旧会议\n时间：2020年1月1日 10:00"),
        }

        async def fake_get_email(_config, message_id: str, **kwargs: object) -> dict:
            subject, body = bodies[message_id]
            return {"subject": subject, "body": body, "from": "boss@example.com"}

        def fake_upsert(session, user_id, body, *, found_in_email=False, email_message_id=None):
            row = SimpleNamespace(
                id=uuid.uuid4(),
                title=body.title,
                starts_at=body.starts_at,
                ends_at=body.ends_at,
                meeting_number=body.meeting_number,
                join_url=body.join_url,
                description=body.description,
                email_message_id=email_message_id,
            )
            return row, True

        session = AsyncMock()
        with patch(
            "services.meeting_invite_scanner.list_emails",
            new=AsyncMock(return_value=listing),
        ), patch(
            "services.meeting_invite_scanner.get_email",
            new=AsyncMock(side_effect=fake_get_email),
        ), patch(
            "services.meeting_invite_scanner._known_external_ids",
            new=AsyncMock(return_value={"m5"}),
        ), patch(
            "services.meeting_invite_scanner.upsert_meeting_invite",
            new=AsyncMock(side_effect=fake_upsert),
        ) as mock_upsert, patch(
            "services.meeting_invite_scanner.ensure_task_for_meeting_invite",
            new=AsyncMock(return_value=(MagicMock(), True)),
        ) as mock_ensure:
            result = await scan_mail163_inbox_for_meeting_invites(
                session,
                user_id=USER_ID,
                config=MagicMock(),
            )

        self.assertEqual(result.scanned, 3)  # m1 / m3 / m4
        self.assertEqual(result.invites, 2)  # m1 / m3
        self.assertEqual(result.tasks_created, 2)
        self.assertEqual(result.skipped, 2)  # m5 已知 + m4 已过期
        self.assertEqual([item["title"] for item in result.items], ["产品评审", "Retro"])
        self.assertEqual(mock_upsert.await_count, 2)
        self.assertEqual(mock_ensure.await_count, 2)
        for call in mock_upsert.await_args_list:
            self.assertTrue(call.kwargs["found_in_email"])
        self.assertEqual(
            sorted(call.kwargs["email_message_id"] for call in mock_upsert.await_args_list),
            ["m1", "m3"],
        )

    async def test_empty_inbox_returns_zeroes(self) -> None:
        with patch(
            "services.meeting_invite_scanner.list_emails",
            new=AsyncMock(return_value={"messages": []}),
        ) as mock_list, patch(
            "services.meeting_invite_scanner.get_email", new=AsyncMock()
        ) as mock_get:
            result = await scan_mail163_inbox_for_meeting_invites(
                AsyncMock(), user_id=USER_ID, config=MagicMock()
            )

        self.assertEqual(result.as_dict()["scanned"], 0)
        self.assertEqual(result.as_dict()["items"], [])
        mock_list.assert_awaited_once()
        mock_get.assert_not_awaited()


class ScanToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_runs_scanner_and_reports_items(self) -> None:
        from agents.tools.email_agent_tools import make_meeting_invite_tools

        canned = ScanResult(scanned=1, invites=1, tasks_created=1)
        canned.items.append(
            {"title": "产品评审", "starts_at": "2030-01-02T06:30:00+00:00", "task_created": True}
        )
        with patch(
            "services.meeting_invite_scanner.scan_mail163_inbox_for_meeting_invites",
            new=AsyncMock(return_value=canned),
        ) as mock_scan:
            tool = make_meeting_invite_tools(AsyncMock(), USER_ID, MagicMock())[0]
            payload = json.loads(await tool.ainvoke({"lookback_days": 5}))

        self.assertEqual(tool.name, "scan_meeting_invites")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["items"][0]["title"], "产品评审")
        self.assertEqual(mock_scan.await_args.kwargs["lookback_days"], 5)


class TaskDetailApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = AsyncMock()
        self.client = TestClient(app)

        async def _session_override():
            yield self.session

        app.dependency_overrides[verify_database_ready] = lambda: None
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID
        app.dependency_overrides[get_session] = _session_override

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.client.close()

    def _task(self) -> Task:
        now = datetime.now(timezone.utc)
        return Task(
            id=uuid.uuid4(),
            user_id=USER_ID,
            title="会议：产品评审",
            description=None,
            status=TaskStatus.NOT_STARTED,
            project=None,
            assigned_agent="calendar_agent",
            priority=TaskPriority.HIGH,
            dependencies=[],
            plan_id=None,
            due_at=now,
            started_at=None,
            completed_at=None,
            plan_source="email_invite",
            source="user",
            source_metadata={"origin": "meeting_invite"},
            last_tracked_at=None,
            created_at=now,
        )

    def test_get_task_returns_row(self) -> None:
        task = self._task()
        with patch(
            "routers.tasks.task_service.get_task_for_user",
            new=AsyncMock(return_value=task),
        ):
            response = self.client.get(f"/tasks/{task.id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(task.id))
        self.assertEqual(response.json()["title"], "会议：产品评审")

    def test_get_task_missing_returns_404(self) -> None:
        with patch(
            "routers.tasks.task_service.get_task_for_user",
            new=AsyncMock(return_value=None),
        ):
            response = self.client.get(f"/tasks/{uuid.uuid4()}")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
