import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession

from services.task_planner_service import analyze_tasks, scan_user_task_plan


def _task_model(
    *,
    task_id: str,
    title: str,
    status: str,
    priority: str = "medium",
    due_at: datetime | None = None,
    dependencies: list[object] | None = None,
    last_tracked_at: datetime | None = None,
    assigned_agent: str | None = "direct",
    plan_id: str | None = "plan-1",
) -> MagicMock:
    task = MagicMock()
    task.id = task_id
    task.title = title
    task.status.value = status
    task.priority.value = priority
    task.due_at = due_at
    task.dependencies = dependencies or []
    task.last_tracked_at = last_tracked_at
    task.assigned_agent = assigned_agent
    task.plan_id = plan_id
    return task


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


class AnalyzeTaskPlanTests(unittest.TestCase):
    def test_flags_overdue_task(self) -> None:
        tasks = [
            _task_model(
                task_id="t-overdue",
                title="提交周报",
                status="not_started",
                due_at=NOW - timedelta(hours=1),
            )
        ]
        result = analyze_tasks(tasks, NOW)
        self.assertEqual(result["summary"]["overdue"], 1)
        self.assertEqual(result["overdue"][0]["id"], "t-overdue")
        self.assertIn("已逾期", result["recommendations"][0])

    def test_flags_upcoming_deadline_in_window(self) -> None:
        tasks = [
            _task_model(
                task_id="t-upcoming",
                title="准备演示",
                status="not_started",
                due_at=NOW + timedelta(hours=12),
            )
        ]
        result = analyze_tasks(tasks, NOW)
        self.assertEqual(result["summary"]["upcoming"], 1)
        self.assertEqual(result["upcoming"][0]["id"], "t-upcoming")
        self.assertIn("临近截止", result["recommendations"][0])

    def test_ready_task_when_dependencies_are_done(self) -> None:
        tasks = [
            _task_model(task_id="parent", title="收集资料", status="done"),
            _task_model(
                task_id="child",
                title="撰写摘要",
                status="not_started",
                dependencies=[{"id": "parent"}],
            ),
        ]
        result = analyze_tasks(tasks, NOW)
        self.assertEqual(result["summary"]["ready"], 1)
        self.assertEqual(result["ready"][0]["id"], "child")

    def test_marks_in_progress_blocked_task_when_dependency_pending(self) -> None:
        tasks = [
            _task_model(task_id="parent", title="母任务", status="not_started"),
            _task_model(
                task_id="child",
                title="子任务",
                status="in_progress",
                dependencies=[{"id": "parent"}],
            ),
        ]
        result = analyze_tasks(tasks, NOW)
        self.assertEqual(result["summary"]["blocked"], 1)
        self.assertEqual(result["blocked"][0]["id"], "child")
        self.assertTrue(
            any("依赖" in recommendation for recommendation in result["recommendations"]),
            "Expected a dependency-block recommendation",
        )

    def test_flags_stalled_in_progress_task(self) -> None:
        tasks = [
            _task_model(
                task_id="t-stale",
                title="长驻任务",
                status="in_progress",
                last_tracked_at=NOW - timedelta(days=2),
            )
        ]
        result = analyze_tasks(tasks, NOW)
        self.assertEqual(result["summary"]["ready"], 0)
        self.assertEqual(result["stalled"][0]["id"], "t-stale")
        self.assertIn("停滞", result["recommendations"][0])

    def test_detects_conflicting_deadlines(self) -> None:
        due = NOW + timedelta(hours=20)
        tasks = [
            _task_model(task_id="a", title="甲", status="in_progress", priority="high", due_at=due),
            _task_model(task_id="b", title="乙", status="in_progress", priority="high", due_at=due),
        ]
        result = analyze_tasks(tasks, NOW)
        self.assertGreaterEqual(len(result["conflicts"]), 1)
        self.assertIn("撞期", result["recommendations"][-1])


class ScanUserTaskPlanTests(unittest.TestCase):
    def test_service_uses_user_tasks(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        tasks = [
            _task_model(
                task_id="t1",
                title="整理日历",
                status="not_started",
                due_at=NOW + timedelta(hours=6),
            )
        ]

        import services.task_planner_service as service
        from unittest.mock import patch

        with patch.object(
            service,
            "list_tasks_for_user",
            new=AsyncMock(return_value=tasks),
        ):
            result = asyncio.run(service.scan_user_task_plan(session, user_id="u1", now=NOW))

        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(result["upcoming"][0]["id"], "t1")


if __name__ == "__main__":
    unittest.main()