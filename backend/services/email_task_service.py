"""把一封邮件（经由它的站内通知）变成一个看板任务。

截止时间优先取自邮件正文里写着的时间（会议 / 邀约 / 预约），提取不到才落到
"明天 09:00"。原文按 IMAP UID 回读，UID 缺失时按 Message-ID 搜索兜底；
回读或抽取失败都不影响建卡，只是退回默认截止时间。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, chat_llm
from db.constants import MAX_TASK_DESCRIPTION_LENGTH, MAX_TASK_TITLE_LENGTH
from lib.enums import NotificationCategory, TaskPriority
from models.task import Task
from models.user_notification import UserNotification
from schemas.task import TaskCreateRequest
from services import task_service
from services.inbox_event_extraction import extract_inbox_signal_with_llm
from services.mail163_client import (
    Mail163Config,
    Mail163Session,
    get_email,
    get_email_by_message_id,
)
from services.notification_service import NEW_MAIL_TITLE_PREFIX, format_notification_time
from services.reminder_service import schedule_deadline_reminder
from services.task_project_classifier import classify_task_project

logger = logging.getLogger(__name__)

# 没有明确时间的邮件，默认明天这个点到期；有 due_at 才会进提醒体系。
DEFAULT_DUE_HOUR = 9
FALLBACK_TIMEZONE = "UTC"
PLAN_SOURCE = "email_notification"
FALLBACK_TASK_TITLE = "处理邮件"
SNIPPET_CHARS = 200


class EmailNotificationNotActionableError(ValueError):
    """This notification is not an email alert we can turn into a task."""


def _resolve_timezone(timezone_name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown planner timezone %r, falling back to UTC", timezone_name)
        return timezone.utc


def next_default_due_at(settings: Settings, *, now: datetime | None = None) -> datetime:
    """Tomorrow at 09:00 in the planner timezone."""
    tz = _resolve_timezone(settings.planner_timezone)
    local_now = (now.astimezone(tz) if now is not None else datetime.now(tz))
    tomorrow = local_now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(hour=DEFAULT_DUE_HOUR), tzinfo=tz)


def _subject_from_notification(notification: UserNotification) -> str:
    title = (notification.title or "").strip()
    if title.startswith(NEW_MAIL_TITLE_PREFIX):
        title = title[len(NEW_MAIL_TITLE_PREFIX):].strip()
    return " ".join(title.split())[:MAX_TASK_TITLE_LENGTH] or FALLBACK_TASK_TITLE


def _build_mail_config(settings: Settings) -> Mail163Config | None:
    """Mailbox credentials for re-reading the original message, if configured."""
    try:
        from services.agent_runner import build_mail163_config

        return build_mail163_config(settings)
    except Exception as exc:
        logger.warning("Email task: mailbox is not configured (%s)", exc)
        return None


async def _load_email(
    notification: UserNotification,
    config: Mail163Config,
) -> dict[str, Any] | None:
    """Read the original message back from the mailbox."""
    mail_session = Mail163Session(config)
    try:
        if notification.email_uid:
            return await get_email(config, notification.email_uid, session=mail_session)
        if notification.email_message_id:
            return await get_email_by_message_id(
                config, notification.email_message_id, session=mail_session
            )
        return None
    except Exception as exc:
        logger.warning("Email task: could not read the original message: %s", exc)
        return None
    finally:
        await asyncio.to_thread(mail_session.close)


async def _extract_event(mail: dict[str, Any], settings: Settings) -> dict[str, Any] | None:
    """Ask the LLM whether this message contains a dated event."""
    text = "\n".join(
        part for part in (str(mail.get("subject") or ""), str(mail.get("body") or "")) if part.strip()
    )
    if not text.strip():
        return None
    llm = chat_llm(settings)
    if llm is None:
        return None
    signal = await extract_inbox_signal_with_llm(text, llm)
    if signal is None:
        return None
    return signal["event"]


def _format_mail_date(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return format_notification_time(parsed)


def _description_from_email(mail: dict[str, Any], event: dict[str, Any] | None) -> str:
    lines = [f"发件人：{str(mail.get('from') or '未知发件人').strip()}"]
    received = _format_mail_date(mail.get("date"))
    if received:
        lines.append(f"收件时间：{received}")
    if event is not None:
        starts_at = event.get("starts_at")
        if starts_at is not None:
            lines.append(f"会议时间：{format_notification_time(starts_at)}")
        if event.get("location"):
            lines.append(f"地点：{event['location']}")
    subject = " ".join(str(mail.get("subject") or "").split())[:80]
    if subject:
        lines.append(f"来源邮件：《{subject}》")
    snippet = str(mail.get("snippet") or "").strip()
    if snippet:
        lines.extend(["", snippet[:SNIPPET_CHARS]])
    return "\n".join(lines)[:MAX_TASK_DESCRIPTION_LENGTH]


def _description_from_notification(notification: UserNotification) -> str:
    body = (notification.body or "").strip()
    footer = "来源：邮件提醒（原文已不在收件箱，可在通知页查看）"
    text = f"{body}\n\n{footer}" if body else footer
    return text[:MAX_TASK_DESCRIPTION_LENGTH]


async def create_task_from_email_notification(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    notification: UserNotification,
    settings: Settings,
) -> tuple[Task, bool]:
    """Create (or reuse) the board task for this email alert.

    Returns (task, created). Repeated clicks and repeated polls land on the same
    card: the notification already carries `task_id`, and the email's
    `message_id` is the second guard.
    """
    if notification.category != NotificationCategory.EMAIL or not notification.email_message_id:
        raise EmailNotificationNotActionableError("这条通知不是可转为任务的邮件提醒。")

    if notification.task_id is not None:
        existing_and_linked = await session.get(Task, notification.task_id)
        if existing_and_linked is not None:
            return existing_and_linked, False

    from services.inbox_listener import find_task_for_email_message

    duplicated = await find_task_for_email_message(
        session,
        user_id=user_id,
        message_id=notification.email_message_id,
    )
    if duplicated is not None:
        notification.task_id = duplicated.id
        await session.flush()
        return duplicated, False

    mail: dict[str, Any] | None = None
    event: dict[str, Any] | None = None
    config = _build_mail_config(settings)
    if config is not None:
        mail = await _load_email(notification, config)
        if mail is not None:
            try:
                event = await _extract_event(mail, settings)
            except Exception as exc:
                logger.warning("Email task: event extraction failed: %s", exc)

    due_at = next_default_due_at(settings)
    deadline_source = "default"
    if event is not None and event.get("starts_at") is not None:
        meeting_at: datetime = event["starts_at"]
        if meeting_at > datetime.now(timezone.utc):
            due_at = meeting_at
            deadline_source = "email_event"
            logger.info("Email task: using the meeting time %s as the deadline", meeting_at)
        else:
            logger.info("Email task: meeting time %s already passed, using default", meeting_at)

    title = _subject_from_notification(notification)
    description = (
        _description_from_email(mail, event)
        if mail is not None
        else _description_from_notification(notification)
    )

    task = await task_service.create_task_for_user(
        session,
        user_id=user_id,
        body=TaskCreateRequest(
            title=title,
            description=description,
            due_at=due_at,
            priority=TaskPriority.MEDIUM,
            project=classify_task_project(title),
            plan_source=PLAN_SOURCE,
            source="user",
            source_metadata={
                "origin": PLAN_SOURCE,
                "email_message_id": notification.email_message_id,
                "notification_id": str(notification.id),
                "deadline_source": deadline_source,
            },
        ),
    )
    if task is None:
        raise EmailNotificationNotActionableError("创建任务失败：找不到对应用户。")

    await schedule_deadline_reminder(
        session,
        user_id,
        task.id,
        task.title,
        due_at,
        minutes_before=settings.reminder_default_lead_minutes,
    )
    notification.task_id = task.id
    await session.flush()
    logger.info("Created task %s from email notification %s", task.id, notification.id)
    return task, True
