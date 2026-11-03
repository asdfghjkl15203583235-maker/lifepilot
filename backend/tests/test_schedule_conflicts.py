"""任务三：日程冲突检测（落库新日程/任务前查重）。

要求：新建带截止时间的任务前，查询目标时段内是否已有 `user_calendar_events`
或带 `due_at` 的 task；冲突时在回复中明确提示
「时间冲突：你在这个时段已有【XXX】」。
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from agents.state import AgentState
from models.task import Task
from models.user_calendar_event import UserCalendarEvent
from services.schedule_conflict_service import (
    CONFLICT_WINDOW_MINUTES,
    find_schedule_conflicts,
    render_conflict_notice,
)

USER_ID = uuid.uuid4()
NOW = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)


def _event(title: str, starts_at: datetime, ends_at: datetime | None) -> UserCalendarEvent:
    event = UserCalendarEvent()
    event.id = uuid.uuid4()
    event.user_id = USER_ID
    event.uid = f"uid-{uuid.uuid4().hex[:8]}"
    event.title = title
    event.starts_at = starts_at
    event.ends_at = ends_at
    event.all_day = False
    return event


def _task(title: str, due_at: datetime, status: str = "not_started") -> Task:
    task = Task()
    task.id = uuid.uuid4()
    task.user_id = USER_ID
    task.title = title
    task.due_at = due_at
    task.source = "user"
    task.status = status
    return task


def _session_with(events: list, tasks: list) -> AsyncMock:
    """Route two sequential session.execute() calls to the two queries."""
    event_result = MagicMock()
    event_result.scalars.return_value.all.return_value = events
    task_result = MagicMock()
    task_result.scalars.return_value.all.return_value = tasks
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[event_result, task_result])
    return session


class FindScheduleConflictTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_due_at_never_conflicts(self) -> None:
        session = AsyncMock()
        conflicts = await find_schedule_conflicts(session, user_id=USER_ID, due_at=None)
        self.assertEqual(conflicts, [])
        session.execute.assert_not_called()

    async def test_calendar_event_in_same_slot_is_reported(self) -> None:
        event = _event("产品评审会", NOW, NOW + timedelta(hours=1))
        session = _session_with([event], [])

        conflicts = await find_schedule_conflicts(
            session, user_id=USER_ID, due_at=NOW + timedelta(minutes=15),
        )

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["source"], "calendar")
        self.assertEqual(conflicts[0]["title"], "产品评审会")

    async def test_task_due_in_same_slot_is_reported(self) -> None:
        session = _session_with([], [_task("提交周报", NOW)])

        conflicts = await find_schedule_conflicts(
            session, user_id=USER_ID, due_at=NOW + timedelta(minutes=20),
        )

        self.assertEqual([c["title"] for c in conflicts], ["提交周报"])
        self.assertEqual(conflicts[0]["source"], "task")

    async def test_completed_task_is_not_a_conflict(self) -> None:
        session = _session_with([], [_task("已完成的旧任务", NOW, status="done")])

        conflicts = await find_schedule_conflicts(
            session, user_id=USER_ID, due_at=NOW,
        )

        self.assertEqual(conflicts, [])

    async def test_explicitly_excluded_task_is_skipped(self) -> None:
        existing = _task("同一计划的上一步", NOW)
        session = _session_with([], [existing])

        conflicts = await find_schedule_conflicts(
            session,
            user_id=USER_ID,
            due_at=NOW,
            exclude_task_ids={existing.id},
        )

        self.assertEqual(conflicts, [])

    async def test_zero_length_event_outside_window_is_ignored(self) -> None:
        """无结束时间的日程只有一个瞬间：落在窗口外就不算冲突。"""
        far = NOW + timedelta(minutes=CONFLICT_WINDOW_MINUTES * 3)
        event = _event("瞬时会面", far, None)
        session = _session_with([event], [])

        conflicts = await find_schedule_conflicts(
            session, user_id=USER_ID, due_at=NOW,
        )

        self.assertEqual(conflicts, [])


class RenderConflictNoticeTests(unittest.TestCase):
    def test_notice_matches_required_wording(self) -> None:
        notice = render_conflict_notice(
            "改简历",
            [{"source": "calendar", "title": "产品评审会"}],
        )
        self.assertIn("时间冲突", notice)
        self.assertIn("你在这个时段已有", notice)
        self.assertIn("【产品评审会】", notice)

    def test_empty_conflicts_render_nothing(self) -> None:
        self.assertEqual(render_conflict_notice("改简历", []), "")


class PlannerConflictWiringTests(unittest.IsolatedAsyncioTestCase):
    """planner 落库前必须调用冲突检测，并把提示写进 state。"""

    async def test_conflict_notice_is_added_to_state(self) -> None:
        from types import SimpleNamespace

        from agents import planner_agent_node as planner
        from sqlalchemy.ext.asyncio import AsyncSession

        settings = MagicMock()
        settings.planner_timezone = "Asia/Shanghai"
        settings.reminder_default_lead_minutes = 10
        # spec=AsyncSession so the planner's isinstance guard accepts it.
        db_session = MagicMock(spec=AsyncSession)
        nested = MagicMock()
        nested.__aenter__ = AsyncMock(return_value=None)
        nested.__aexit__ = AsyncMock(return_value=False)
        db_session.begin_nested.return_value = nested

        created = MagicMock()
        created.id = uuid.uuid4()
        created.title = "改简历"

        plan_json = (
            '{"plan_id": "p1", "summary": "改简历", "tasks": ['
            '{"id": "step-1", "intent": "改简历", "route": "calendar",'
            ' "priority": "medium", "project": "生活", "depends_on": [],'
            ' "due_at": "2026-10-31T08:00:00"}]}'
        )
        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=SimpleNamespace(content=plan_json))

        state = AgentState(
            user_id=USER_ID,
            messages=[SimpleNamespace(content="明天早上八点改简历，提醒我")],
            tasks=[{"intent": "改简历", "route": "calendar"}],
        )

        with patch.object(
            planner, "get_planner_rules", new=AsyncMock(return_value=[]),
        ), patch.object(
            planner, "find_schedule_conflicts",
            new=AsyncMock(return_value=[{"source": "calendar", "title": "产品评审会"}]),
        ), patch.object(
            planner, "create_task_for_user", new=AsyncMock(return_value=created),
        ), patch.object(
            planner, "schedule_deadline_reminder", new=AsyncMock(),
        ):
            result = await planner.planner_node(
                state,
                llm=llm,
                runtime=SimpleNamespace(
                    context={"db_session": db_session, "settings": settings},
                ),
            )

        notices = result.get("schedule_conflicts") or []
        self.assertEqual(len(notices), 1)
        self.assertIn("【产品评审会】", notices[0])
        self.assertIn("时间冲突", notices[0])


class MergeConflictFooterTests(unittest.IsolatedAsyncioTestCase):
    async def test_conflict_is_appended_to_single_branch_answer(self) -> None:
        from agents.orchestrator_node import merge_results_node

        state = AgentState(
            agent_results=[{"route": "calendar", "intent": "改简历", "result": "已创建任务。"}],
            tasks=[{"task_key": "calendar::改简历"}],
            schedule_conflicts=["时间冲突：你在这个时段已有【产品评审会】（新任务：改简历）。"],
        )
        merged = await merge_results_node(state, llm=MagicMock())
        self.assertIn("【产品评审会】", merged["final_answer"])
        self.assertIn("已创建任务", merged["final_answer"])

    async def test_no_conflict_keeps_answer_unchanged(self) -> None:
        from agents.orchestrator_node import merge_results_node

        state = AgentState(
            agent_results=[{"route": "calendar", "intent": "改简历", "result": "已创建任务。"}],
            tasks=[{"task_key": "calendar::改简历"}],
        )
        merged = await merge_results_node(state, llm=MagicMock())
        self.assertEqual(merged["final_answer"], "已创建任务。")


if __name__ == "__main__":
    unittest.main()
