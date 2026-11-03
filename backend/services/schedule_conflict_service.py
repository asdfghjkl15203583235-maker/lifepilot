"""Schedule conflict detection for planner-created tasks.

A task with a due time can collide with an existing calendar event or with
another task due around the same moment. We surface that collision
deterministically in the reply instead of silently double-booking the user.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lib.enums import TaskStatus
from models.task import Task
from models.user_calendar_event import UserCalendarEvent

# Two items are "in the same slot" when their start times are this close, or
# when a calendar event spans the new task's start time.
CONFLICT_WINDOW_MINUTES = 60

_FINISHED_TASK_STATUSES = {TaskStatus.DONE.value}


async def _overlapping_events(
    session: AsyncSession,
    user_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
) -> list[UserCalendarEvent]:
    result = await session.execute(
        select(UserCalendarEvent).where(
            UserCalendarEvent.user_id == user_id,
            UserCalendarEvent.starts_at <= window_end,
            or_(
                UserCalendarEvent.ends_at.is_(None),
                UserCalendarEvent.ends_at >= window_start,
            ),
        ),
    )
    return list(result.scalars().all())


async def _nearby_tasks(
    session: AsyncSession,
    user_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
) -> list[Task]:
    result = await session.execute(
        select(Task).where(
            Task.user_id == user_id,
            Task.due_at.is_not(None),
            Task.due_at >= window_start,
            Task.due_at <= window_end,
            Task.source != "system",
        ),
    )
    return [
        task
        for task in result.scalars().all()
        if str(getattr(task.status, "value", task.status)) not in _FINISHED_TASK_STATUSES
    ]


async def find_schedule_conflicts(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    due_at: datetime | None,
    exclude_task_ids: set[uuid.UUID] | None = None,
) -> list[dict[str, str]]:
    """Return the existing items that occupy the slot around *due_at*.

    Each conflict is ``{"source": "calendar"|"task", "title": str}``. An empty
    list means the slot is free (or the task has no due time, which cannot
    conflict).
    """
    if due_at is None:
        return []

    window_start = due_at - timedelta(minutes=CONFLICT_WINDOW_MINUTES)
    window_end = due_at + timedelta(minutes=CONFLICT_WINDOW_MINUTES)
    excluded = exclude_task_ids or set()

    conflicts: list[dict[str, str]] = []
    for event in await _overlapping_events(session, user_id, window_start, window_end):
        # A null-end event is a single instant: only collide when its start
        # actually falls inside the window.
        if event.ends_at is None and not (window_start <= event.starts_at <= window_end):
            continue
        conflicts.append({"source": "calendar", "title": event.title})

    for task in await _nearby_tasks(session, user_id, window_start, window_end):
        if task.id in excluded:
            continue
        conflicts.append({"source": "task", "title": task.title})

    return conflicts


def render_conflict_notice(title: str, conflicts: list[dict[str, str]]) -> str:
    """User-facing warning shared by every surface that reports conflicts."""
    if not conflicts:
        return ""
    names = "、".join(f"【{item.get('title') or '未命名'}】" for item in conflicts)
    return f"时间冲突：你在这个时段已有{names}（新任务：{title}）。"
