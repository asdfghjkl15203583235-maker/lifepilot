"""Persistence and reminder scheduling for extracted meeting invites."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lib.enums import ReminderStatus, ReminderTriggerType
from models.user_meeting_invite import UserMeetingInvite
from models.user_reminder import UserReminder
from schemas.meeting_invite import MeetingInviteCreateRequest

logger = logging.getLogger(__name__)


async def get_meeting_invite_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    invite_id: uuid.UUID,
) -> UserMeetingInvite | None:
    result = await session.execute(
        select(UserMeetingInvite).where(
            UserMeetingInvite.id == invite_id,
            UserMeetingInvite.user_id == user_id,
        ),
    )
    return result.scalar_one_or_none()


async def list_meeting_invites_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    upcoming_only: bool = False,
    limit: int = 100,
) -> list[UserMeetingInvite]:
    stmt = (
        select(UserMeetingInvite)
        .where(UserMeetingInvite.user_id == user_id)
        .order_by(UserMeetingInvite.starts_at.desc())
        .limit(limit)
    )
    if upcoming_only:
        stmt = (
            select(UserMeetingInvite)
            .where(
                UserMeetingInvite.user_id == user_id,
                UserMeetingInvite.starts_at >= datetime.now(timezone.utc),
            )
            .order_by(UserMeetingInvite.starts_at.asc())
            .limit(limit)
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def save_extracted_meeting_invite(
    session: AsyncSession,
    user_id: uuid.UUID,
    invite: MeetingInviteCreateRequest,
    *,
    found_in_email: bool = False,
    email_message_id: str | None = None,
) -> UserMeetingInvite:
    """Persist an invite and schedule an automatic pre-meeting reminder."""
    row = UserMeetingInvite(
        user_id=user_id,
        source=invite.source,
        external_id=invite.external_id,
        title=invite.title,
        description=invite.description,
        starts_at=invite.starts_at,
        ends_at=invite.ends_at,
        meeting_number=invite.meeting_number,
        join_url=invite.join_url,
        found_in_email=found_in_email,
        email_message_id=email_message_id,
        extra=invite.extra,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)

    lead_seconds = max(0, invite.reminder_minutes_before * 60)
    remind_at = row.starts_at - __import__("datetime").timedelta(seconds=lead_seconds)
    session.add(
        UserReminder(
            user_id=user_id,
            title=f"Meeting soon: {row.title}",
            note=(
                f"{row.title} starts at {row.starts_at.isoformat()}"
                + (f"\nMeeting number: {row.meeting_number}" if row.meeting_number else "")
                + (f"\nJoin: {row.join_url}" if row.join_url else "")
            ),
            remind_at=remind_at,
            trigger_type=ReminderTriggerType.MEETING,
            status=ReminderStatus.PENDING,
            payload={"meeting_invite_id": str(row.id), "minutes_before": invite.reminder_minutes_before},
        ),
    )
    await session.flush()
    return row


async def upsert_meeting_invite(
    session: AsyncSession,
    user_id: uuid.UUID,
    invite: MeetingInviteCreateRequest,
    *,
    found_in_email: bool = False,
    email_message_id: str | None = None,
) -> tuple[UserMeetingInvite, bool]:
    """Persist an invite, or update the existing row when `external_id` matches."""
    existing = None
    if invite.external_id:
        result = await session.execute(
            select(UserMeetingInvite).where(
                UserMeetingInvite.user_id == user_id,
                UserMeetingInvite.external_id == invite.external_id,
            ),
        )
        existing = result.scalar_one_or_none()
    if existing is not None:
        existing.source = invite.source
        existing.title = invite.title
        existing.description = invite.description
        existing.starts_at = invite.starts_at
        existing.ends_at = invite.ends_at
        existing.meeting_number = invite.meeting_number
        existing.join_url = invite.join_url
        existing.found_in_email = found_in_email or existing.found_in_email
        existing.email_message_id = email_message_id or existing.email_message_id
        existing.extra = {**dict(existing.extra or {}), **dict(invite.extra or {})}
        await _reschedule_meeting_reminder(session, existing, invite)
        await session.flush()
        await session.refresh(existing)
        return existing, False

    row = await save_extracted_meeting_invite(
        session,
        user_id,
        invite,
        found_in_email=found_in_email,
        email_message_id=email_message_id,
    )
    return row, True


async def _reschedule_meeting_reminder(
    session: AsyncSession,
    row: UserMeetingInvite,
    invite: MeetingInviteCreateRequest,
) -> None:
    from datetime import timedelta

    lead_seconds = max(0, invite.reminder_minutes_before * 60)
    remind_at = row.starts_at - timedelta(seconds=lead_seconds)
    result = await session.execute(
        select(UserReminder).where(
            UserReminder.user_id == row.user_id,
            UserReminder.payload.op("->>")("meeting_invite_id") == str(row.id),
            UserReminder.status.in_([ReminderStatus.PENDING, ReminderStatus.SENT]),
        ),
    )
    reminder = result.scalar_one_or_none()
    if reminder is None:
        session.add(
            UserReminder(
                user_id=row.user_id,
                title=f"Meeting soon: {row.title}",
                note=(
                    f"{row.title} starts at {row.starts_at.isoformat()}"
                    + (f"\nMeeting number: {row.meeting_number}" if row.meeting_number else "")
                    + (f"\nJoin: {row.join_url}" if row.join_url else "")
                ),
                remind_at=remind_at,
                trigger_type=ReminderTriggerType.MEETING,
                status=ReminderStatus.PENDING,
                payload={"meeting_invite_id": str(row.id), "minutes_before": invite.reminder_minutes_before},
            ),
        )
    else:
        reminder.remind_at = remind_at
        reminder.title = f"Meeting soon: {row.title}"
        reminder.status = ReminderStatus.PENDING


async def delete_meeting_invite_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    invite_id: uuid.UUID,
) -> bool:
    row = await get_meeting_invite_for_user(session, user_id, invite_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


def meeting_invite_to_api_dict(row: UserMeetingInvite) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "source": row.source,
        "external_id": row.external_id,
        "title": row.title,
        "description": row.description,
        "starts_at_iso": row.starts_at.isoformat().replace("+00:00", "Z") if row.starts_at else None,
        "ends_at_iso": row.ends_at.isoformat().replace("+00:00", "Z") if row.ends_at else None,
        "meeting_number": row.meeting_number,
        "join_url": row.join_url,
        "found_in_email": row.found_in_email,
        "reminder_sent_at_iso": (
            row.reminder_sent_at.isoformat().replace("+00:00", "Z") if row.reminder_sent_at else None
        ),
        "created_at_iso": row.created_at.isoformat().replace("+00:00", "Z"),
        "extra": dict(row.extra or {}),
    }