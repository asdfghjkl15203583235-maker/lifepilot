"""Create, deliver, and acknowledge proactive reminders."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.constants import MAX_NOTIFICATION_TITLE_LENGTH
from lib.enums import NotificationCategory, ReminderStatus, ReminderTriggerType
from models.user_notification import UserNotification
from models.user_reminder import UserReminder
from schemas.reminder import ReminderCreateRequest

logger = logging.getLogger(__name__)

MAX_DUE_REMINDERS_PER_TICK = 100


class ReminderNotFoundError(ValueError):
    """No reminder row for this user and id."""


async def create_reminder(
    session: AsyncSession,
    user_id: uuid.UUID,
    body: ReminderCreateRequest,
) -> UserReminder:
    row = UserReminder(
        user_id=user_id,
        task_id=body.task_id,
        title=body.title.strip(),
        note=body.note,
        remind_at=body.remind_at,
        trigger_type=body.trigger_type,
        status=ReminderStatus.PENDING,
        payload=body.payload,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_reminders_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int = 200,
    include_sent: bool = False,
) -> list[UserReminder]:
    stmt = (
        select(UserReminder)
        .where(UserReminder.user_id == user_id)
        .order_by(UserReminder.remind_at.desc())
        .limit(limit)
    )
    if not include_sent:
        stmt = stmt.where(UserReminder.status != ReminderStatus.CANCELLED)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def ack_reminder(
    session: AsyncSession,
    user_id: uuid.UUID,
    reminder_id: uuid.UUID,
) -> UserReminder:
    result = await session.execute(
        select(UserReminder).where(
            UserReminder.id == reminder_id,
            UserReminder.user_id == user_id,
        ),
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise ReminderNotFoundError("Reminder not found.")
    row.status = ReminderStatus.ACKNOWLEDGED
    await session.flush()
    await session.refresh(row)
    return row


async def get_due_reminders(
    session: AsyncSession,
    now_utc: datetime,
    *,
    max_rows: int = MAX_DUE_REMINDERS_PER_TICK,
) -> list[UserReminder]:
    result = await session.execute(
        select(UserReminder)
        .where(
            UserReminder.status == ReminderStatus.PENDING,
            UserReminder.remind_at <= now_utc,
        )
        .order_by(UserReminder.remind_at.asc())
        .limit(max_rows)
        .with_for_update(skip_locked=True),
    )
    return list(result.scalars().all())


async def process_due_reminders(session: AsyncSession, now_utc: datetime) -> int:
    """Deliver due in-app notifications and mark reminders sent. Returns delivered count."""
    rows = await get_due_reminders(session, now_utc)
    for row in rows:
        session.add(
            UserNotification(
                user_id=row.user_id,
                category=NotificationCategory.REMINDER,
                title=(row.title or "Reminder")[:MAX_NOTIFICATION_TITLE_LENGTH],
                body=(row.note or row.title or "You have a scheduled reminder.")[:4000],
                is_read=False,
                reminder_id=row.id,
                task_id=row.task_id,
            ),
        )
        row.status = ReminderStatus.SENT
        row.sent_at = now_utc
        logger.info("Delivered reminder %s for user %s", row.id, row.user_id)
    await session.flush()
    return len(rows)


async def snooze_reminder(
    session: AsyncSession,
    user_id: uuid.UUID,
    reminder_id: uuid.UUID,
    *,
    minutes: int,
) -> UserReminder | None:
    """Re-arm a sent reminder to fire again `minutes` from now (and un-read its alert)."""
    result = await session.execute(
        select(UserReminder).where(
            UserReminder.id == reminder_id,
            UserReminder.user_id == user_id,
        ),
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    new_due = datetime.now(timezone.utc) + timedelta(minutes=max(1, minutes))
    row.remind_at = new_due
    row.status = ReminderStatus.PENDING
    row.sent_at = None
    await session.execute(
        update(UserNotification)
        .where(
            UserNotification.user_id == user_id,
            UserNotification.reminder_id == reminder_id,
            UserNotification.is_read.is_(False),
        )
        .values(is_read=True),
    )
    await session.flush()
    await session.refresh(row)
    return row


async def cancel_task_reminders(session: AsyncSession, task_id: uuid.UUID) -> int:
    result = await session.execute(
        update(UserReminder)
        .where(
            UserReminder.task_id == task_id,
            UserReminder.status == ReminderStatus.PENDING,
        )
        .values(status=ReminderStatus.CANCELLED),
    )
    await session.flush()
    return int(result.rowcount or 0)


async def schedule_deadline_reminder(
    session: AsyncSession,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    task_title: str,
    due_at: datetime,
    *,
    minutes_before: int = 30,
) -> UserReminder | None:
    """Add a deadline reminder when a task gets a due date (idempotent per task+due)."""
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    remind_at = due_at.astimezone(timezone.utc) - timedelta(minutes=minutes_before)
    await cancel_task_reminders(session, task_id)
    row = await create_reminder(
        session,
        user_id,
        ReminderCreateRequest(
            title=f"Deadline: {task_title}",
            note=f"Task due at {due_at.isoformat()}",
            task_id=task_id,
            remind_at=remind_at,
            trigger_type=ReminderTriggerType.DEADLINE,
            payload={"task_id": str(task_id), "due_at_iso": due_at.isoformat()},
        ),
    )
    return row