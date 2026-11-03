"""Persistence for in-app user notifications."""

import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.constants import MAX_NOTIFICATION_TITLE_LENGTH
from lib.enums import BackgroundJobRunOutcome, NotificationCategory
from models.user_notification import UserNotification

NOTIFICATION_JOB_BODY_SNIPPET_CHARS = 900

# 邮件类通知统一的标题前缀：「转成任务」时用它把原始主题还原出来。
NEW_MAIL_TITLE_PREFIX = "新邮件："

# 通知正文里给人看的时间，不能用 UTC ISO 原文（用户看到的会是 2026-11-04T10:00:00+00:00）。
DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
DISPLAY_TIME_FORMAT = "%Y-%m-%d %H:%M"


def format_notification_time(value: datetime) -> str:
    """Format a timestamp for humans in notifications (e.g. 2026-11-04 18:00)."""
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(DISPLAY_TIMEZONE).strftime(DISPLAY_TIME_FORMAT)


def user_notification_to_api_dict(row: UserNotification) -> dict[str, Any]:
    """Shape expected by the web app notifications list."""
    cat = row.category.value if hasattr(row.category, "value") else str(row.category)
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    iso = created.isoformat().replace("+00:00", "Z")
    return {
        "id": str(row.id),
        "title": row.title,
        "body": row.body,
        "category": cat,
        "is_read": row.is_read,
        "created_at_iso": iso,
        "reminder_id": str(row.reminder_id) if row.reminder_id else None,
        "task_id": str(row.task_id) if row.task_id else None,
    }


class UserNotificationNotFoundError(ValueError):
    """No notification row for this user and id."""


def _truncate_body(text: str) -> str:
    text = text.strip()
    if len(text) <= NOTIFICATION_JOB_BODY_SNIPPET_CHARS:
        return text
    return text[: NOTIFICATION_JOB_BODY_SNIPPET_CHARS - 1].rstrip() + "…"


async def add_background_job_run_notification(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    job_title: str,
    outcome: BackgroundJobRunOutcome,
    detail: str,
) -> UserNotification:
    """Record a schedule run result the user can read in the notifications inbox."""
    safe_title = job_title.strip() or "定时任务"
    if outcome == BackgroundJobRunOutcome.SUCCESS:
        title = f"定时任务已完成：{safe_title}"
        body = _truncate_body(detail) if detail.strip() else "运行已成功完成。"
    else:
        title = f"定时任务运行失败：{safe_title}"
        body = _truncate_body(detail) if detail.strip() else "运行失败，没有更多详情。"

    row = UserNotification(
        user_id=user_id,
        category=NotificationCategory.BACKGROUND_JOB,
        title=title[:MAX_NOTIFICATION_TITLE_LENGTH],
        body=body,
        is_read=False,
        background_job_id=job_id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def add_email_event_notification(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    title: str,
    body: str,
    task_id: uuid.UUID | None = None,
    email_message_id: str | None = None,
) -> UserNotification:
    """Tell the user an email was turned into a task while they were away.

    后台 IMAP 监听是主动行为：如果只静默建任务，用户根本不会知道有人约了他。
    记下 `email_message_id` 后，同一封邮件的"新邮件"提醒就不会再弹一次。
    """
    if email_message_id:
        already_announced = await find_notification_for_email_message(
            session, user_id, email_message_id
        )
        if already_announced is not None:
            return already_announced

    row = UserNotification(
        user_id=user_id,
        category=NotificationCategory.EMAIL,
        title=(title.strip() or "邮件提醒")[:MAX_NOTIFICATION_TITLE_LENGTH],
        body=_truncate_body(body or ""),
        is_read=False,
        task_id=task_id,
        email_message_id=(email_message_id or None),
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def find_notification_for_email_message(
    session: AsyncSession,
    user_id: uuid.UUID,
    email_message_id: str,
) -> UserNotification | None:
    """Return the alert already raised for this email, if any."""
    result = await session.execute(
        select(UserNotification)
        .where(
            UserNotification.user_id == user_id,
            UserNotification.email_message_id == email_message_id,
        )
        .limit(1),
    )
    return result.scalars().first()


async def add_new_mail_notification(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    email_message_id: str,
    subject: str,
    sender: str,
    snippet: str,
    received_at: datetime | None = None,
    email_uid: str | None = None,
) -> UserNotification | None:
    """Raise the "a new email arrived" alert the web toast pops up.

    Returns None when this message was already announced: the caller polls every
    five minutes, so announcing twice would toast twice.
    """
    existing = await find_notification_for_email_message(session, user_id, email_message_id)
    if existing is not None:
        return None

    title = f"{NEW_MAIL_TITLE_PREFIX}{(subject.strip() or '（无主题）')[:200]}"
    body_lines = [f"发件人：{sender.strip() or '未知发件人'}"]
    if received_at is not None:
        body_lines.append(f"收件时间：{format_notification_time(received_at)}")
    if snippet.strip():
        body_lines.extend(["", snippet.strip()[:200]])

    row = UserNotification(
        user_id=user_id,
        category=NotificationCategory.EMAIL,
        title=title[:MAX_NOTIFICATION_TITLE_LENGTH],
        body=_truncate_body("\n".join(body_lines)),
        is_read=False,
        email_uid=(email_uid or None),
        email_message_id=email_message_id[:512],
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_notifications_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int = 100,
) -> list[UserNotification]:
    result = await session.execute(
        select(UserNotification)
        .where(UserNotification.user_id == user_id)
        .order_by(UserNotification.created_at.desc())
        .limit(limit),
    )
    return list(result.scalars().all())


async def get_notification_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    notification_id: uuid.UUID,
) -> UserNotification:
    """Load one notification, scoped to its owner."""
    result = await session.execute(
        select(UserNotification).where(
            UserNotification.id == notification_id,
            UserNotification.user_id == user_id,
        ),
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise UserNotificationNotFoundError("Notification not found.")
    return row


async def mark_notification_read(
    session: AsyncSession,
    user_id: uuid.UUID,
    notification_id: uuid.UUID,
) -> None:
    row = await get_notification_for_user(session, user_id, notification_id)
    if not row.is_read:
        row.is_read = True


async def mark_all_notifications_read(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    """Set is_read on all unread rows; returns number of rows matched (best-effort)."""
    result = await session.execute(
        update(UserNotification)
        .where(UserNotification.user_id == user_id, UserNotification.is_read.is_(False))
        .values(is_read=True),
    )
    return int(result.rowcount or 0)


async def count_unread_for_user(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(UserNotification)
        .where(UserNotification.user_id == user_id, UserNotification.is_read.is_(False)),
    )
    return int(result.scalar_one() or 0)
