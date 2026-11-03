"""One-off reminder tool for the orchestrator `direct_reply` agent.

Turns a natural-language "remind me at <time>" request into a persisted
`user_reminders` row plus a `tasks` row so it surfaces in the task board,
calendar, and daily briefing.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lib.enums import ReminderStatus, ReminderTriggerType, TaskStatus
from models.user_reminder import UserReminder
from schemas.reminder import ReminderCreateRequest
from schemas.task import TaskCreateRequest
from services.reminder_service import create_reminder
from services.task_project_classifier import classify_task_project
from services.task_service import create_task_for_user

logger = logging.getLogger(__name__)

CREATE_REMINDER_TOOL_NAME = "create_reminder"

CREATE_REMINDER_TOOL_DESCRIPTION = """\
Create a one-off reminder for the user at a specific date/time.
Call this when the user asks to be reminded at a specific time, e.g.
"remind me tomorrow at 8am to update my resume" or "提醒我明天早上八点改简历".
You must pass `remind_at` as an ISO-8601 instant resolved from the user's local
timezone (today's date is <today>), a concise `title`, and an optional `note`.
This creates both a reminder notification and a visible task with that due time.
Do NOT call this for recurring automations — use create_scheduled_job instead.
"""


class ReminderToolArgs(BaseModel):
    title: str = Field(description="Short reminder title, e.g. '改简历'.")
    remind_at: datetime = Field(
        description="ISO-8601 instant when to remind, resolved to the user's local timezone."
    )
    note: str | None = Field(default=None, description="Optional extra note for the reminder.")


def _resolve_remind_at(settings: Any, remind_at: datetime) -> datetime:
    """Force *remind_at* into the user's planner timezone as an aware UTC instant."""
    tz_name = getattr(settings, "planner_timezone", None) or "UTC"
    tz = ZoneInfo(tz_name)
    if remind_at.tzinfo is None:
        remind_at = remind_at.replace(tzinfo=tz)
    return remind_at.astimezone(timezone.utc)


async def _find_duplicate_pending_reminder(
    session: AsyncSession,
    user_id: uuid.UUID,
    title: str,
    remind_at: datetime,
    *,
    window_minutes: int = 10,
) -> UserReminder | None:
    """Return an existing PENDING reminder matching user + title within a time window.

    Guards against parallel/duplicate tool invocations persisting the same reminder
    more than once (e.g. two fan-out `direct_reply` nodes both calling create_reminder).
    """
    lower = remind_at - timedelta(minutes=window_minutes)
    upper = remind_at + timedelta(minutes=window_minutes)
    result = await session.execute(
        select(UserReminder)
        .where(
            UserReminder.user_id == user_id,
            UserReminder.status == ReminderStatus.PENDING,
            UserReminder.title == title,
            UserReminder.remind_at >= lower,
            UserReminder.remind_at <= upper,
        )
        .order_by(UserReminder.remind_at.asc())
        .limit(1),
    )
    return result.scalar_one_or_none()


def make_create_reminder_tool(
    session: AsyncSession,
    user_id: uuid.UUID,
    settings: Any,
) -> StructuredTool:
    """Bound tool — one per chat request, shares the request DB session."""

    async def _invoke(**kwargs: Any) -> str:
        title = str(kwargs.get("title") or "").strip()
        remind_at_raw = kwargs.get("remind_at")
        note = kwargs.get("note")
        if not title or not remind_at_raw:
            return json.dumps({"status": "error", "message": "缺少必要参数 title 或 remind_at。"})

        try:
            remind_at = _resolve_remind_at(settings, remind_at_raw)
        except (TypeError, ValueError):
            return json.dumps({"status": "error", "message": f"remind_at 格式无效: {remind_at_raw}"})

        # 幂等去重：同用户存在相同的 pending 提醒（title 相同且 remind_at 相差 < 10 分钟）
        # 则直接返回已有，避免并行 direct / 重复工具调用造成重复落库。
        existing = await _find_duplicate_pending_reminder(session, user_id, title, remind_at)
        if existing is not None:
            return json.dumps(
                {
                    "status": "exists",
                    "reminder_id": str(existing.id),
                    "task_id": str(existing.task_id) if existing.task_id else None,
                    "title": title,
                    "remind_at": remind_at.isoformat(),
                    "note": note,
                    "duplicate": True,
                },
                ensure_ascii=False,
            )

        row = await create_reminder(
            session,
            user_id,
            ReminderCreateRequest(
                title=title[:240],
                note=note,
                remind_at=remind_at,
                trigger_type=ReminderTriggerType.MANUAL,
                payload={"source": "chat"},
            ),
        )

        # Mirror as a visible task with the same due time so it shows up in the
        # task board / calendar / daily briefing. Source metadata links it back.
        task = await create_task_for_user(
            session,
            user_id=user_id,
            body=TaskCreateRequest(
                title=title[:500],
                description=f"来自聊天的定时提醒：{remind_at.isoformat()}",
                status=TaskStatus.NOT_STARTED,
                project=classify_task_project(title[:500]),
                priority="medium",
                due_at=remind_at,
                source="user",
                source_metadata={"reminder_id": str(row.id), "source": "chat_reminder"},
            ),
        )
        if task is not None:
            row.task_id = task.id

        return json.dumps(
            {
                "status": "created",
                "reminder_id": str(row.id),
                "task_id": str(task.id) if task else None,
                "title": title,
                "remind_at": remind_at.isoformat(),
                "note": note,
            },
            ensure_ascii=False,
        )

    return StructuredTool.from_function(
        name=CREATE_REMINDER_TOOL_NAME,
        description=CREATE_REMINDER_TOOL_DESCRIPTION,
        coroutine=_invoke,
        args_schema=ReminderToolArgs,
        infer_schema=False,
    )
