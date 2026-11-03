"""Turn meeting-invite emails into meeting records and board tasks.

The deterministic path is deliberate: an inbox scan runs unattended, so it must
not depend on an LLM deciding whether a message is an invite. Anything that
survives the keyword filter and yields a real start time is upserted by message
id, then projected onto exactly one task (matched by `meeting_invite_id`).
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from lib.enums import TaskPriority
from models.task import Task
from models.user_meeting_invite import UserMeetingInvite
from schemas.meeting_invite import MeetingInviteCreateRequest
from schemas.task import TaskCreateRequest
from services import task_service
from services.mail163_client import Mail163Config, Mail163Session, get_email, list_emails
from services.meeting_invite_extraction import extract_meeting_invite, extract_meeting_invite_with_llm
from services.meeting_invite_service import upsert_meeting_invite
from services.notification_service import format_notification_time

logger = logging.getLogger(__name__)

# 会议邀请邮件的常见标题/摘要关键词。只用来筛候选，命中后仍必须解析出真实时间。
INVITE_KEYWORDS = (
    "会议", "邀请", "日程", "预约", "议程", "参会", "入会", "腾讯会议", "钉钉会议",
    "飞书", "zoom", "teams", "webinar", "meeting", "invite", "invitation",
    "calendar", "schedule", "rsvp",
)

# 已经开始的会议不再建任务（时间窗只为容忍扫描间隔和时区换算误差）。
PAST_MEETING_GRACE = timedelta(hours=2)
DEFAULT_REMINDER_MINUTES = 30
MAX_TITLE_CHARS = 200


@dataclass
class ScanResult:
    scanned: int = 0
    invites: int = 0
    tasks_created: int = 0
    tasks_updated: int = 0
    skipped: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "invites": self.invites,
            "tasks_created": self.tasks_created,
            "tasks_updated": self.tasks_updated,
            "skipped": self.skipped,
            "items": self.items[:20],
            "errors": self.errors[:5],
        }


def looks_like_meeting_invite(subject: str, snippet: str = "") -> bool:
    haystack = f"{subject}\n{snippet}".lower()
    return any(keyword in haystack for keyword in INVITE_KEYWORDS)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _clean_title(subject: str, fallback: str) -> str:
    title = " ".join(subject.split()) or fallback
    for prefix in ("回复：", "转发：", "回复:", "转发:", "RE:", "Re:", "FW:", "Fw:", "Invitation:", "邀请："):
        if title.startswith(prefix):
            title = title[len(prefix):].strip()
    return title[:MAX_TITLE_CHARS] or fallback


def _task_title(invite: UserMeetingInvite) -> str:
    title = (invite.title or "").strip()
    if title.lower().startswith(("会议", "meeting")):
        return title[:MAX_TITLE_CHARS]
    return f"会议：{title}"[:MAX_TITLE_CHARS]


def _task_description(invite: UserMeetingInvite) -> str:
    lines = [f"时间：{format_notification_time(invite.starts_at)}"]
    if invite.ends_at:
        lines.append(f"结束：{format_notification_time(invite.ends_at)}")
    if invite.meeting_number:
        lines.append(f"会议号：{invite.meeting_number}")
    if invite.join_url:
        lines.append(f"入会链接：{invite.join_url}")
    if invite.description:
        lines.append("")
        lines.append(invite.description.strip()[:1500])
    return "\n".join(lines)


async def ensure_task_for_meeting_invite(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    invite: UserMeetingInvite,
    email_subject: str | None = None,
) -> tuple[Task | None, bool]:
    """Create the board task for an invite, or refresh it if already created.

    Matching on `source_metadata->>'meeting_invite_id'` keeps repeated scans from
    producing duplicate cards; an invite update (time moved) refreshes the task.
    """
    existing = await session.execute(
        select(Task).where(
            Task.user_id == user_id,
            Task.source_metadata.op("->>")("meeting_invite_id") == str(invite.id),
        )
    )
    task = existing.scalars().first()
    title = _task_title(invite)
    description = _task_description(invite)

    if task is not None:
        task.title = title
        task.description = description
        task.due_at = invite.starts_at
        await session.flush()
        return task, False

    metadata: dict[str, Any] = {
        "origin": "meeting_invite",
        "meeting_invite_id": str(invite.id),
    }
    if invite.email_message_id:
        metadata["email_message_id"] = invite.email_message_id
    if email_subject:
        metadata["email_subject"] = email_subject[:300]

    task = await task_service.create_task_for_user(
        session,
        user_id=user_id,
        body=TaskCreateRequest(
            title=title,
            description=description,
            due_at=invite.starts_at,
            priority=TaskPriority.HIGH,
            assigned_agent="calendar_agent",
            plan_source="email_invite",
            source="user",
            source_metadata=metadata,
        ),
    )
    if task is not None:
        # 后台扫描是主动行为：不通知用户，任务出现在看板上也不会有人注意到。
        await _notify_new_meeting_task(session, user_id, invite, task)
    return task, task is not None


async def _notify_new_meeting_task(
    session: AsyncSession,
    user_id: uuid.UUID,
    invite: UserMeetingInvite,
    task: Task,
) -> None:
    from services.notification_service import add_email_event_notification

    body_parts = [f"时间：{format_notification_time(invite.starts_at)}"]
    if invite.meeting_number:
        body_parts.append(f"会议号：{invite.meeting_number}")
    if invite.join_url:
        body_parts.append(f"入会链接：{invite.join_url}")
    try:
        await add_email_event_notification(
            session,
            user_id,
            title=f"邮件已加入日程：{invite.title[:60]}",
            body="；".join(body_parts),
            task_id=task.id,
            email_message_id=invite.email_message_id,
        )
    except Exception as exc:
        # 通知失败不能回滚已经建好的会议任务。
        logger.warning("Failed to notify new meeting task %s: %s", getattr(task, "id", None), exc)


async def _known_external_ids(
    session: AsyncSession,
    user_id: uuid.UUID,
    message_ids: list[str],
) -> set[str]:
    if not message_ids:
        return set()
    result = await session.execute(
        select(UserMeetingInvite.external_id).where(
            UserMeetingInvite.user_id == user_id,
            UserMeetingInvite.external_id.in_(message_ids),
        )
    )
    return {value for value in result.scalars().all() if value}


async def scan_mail163_inbox_for_meeting_invites(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    config: Mail163Config,
    settings: Settings | None = None,
    lookback_days: int = 3,
    max_results: int = 30,
    use_llm: bool = False,
    mail_session: Mail163Session | None = None,
) -> ScanResult:
    """Scan recent inbox messages and project invites onto invites + tasks."""
    result = ScanResult()
    listing = await list_emails(
        config,
        query=f"newer_than:{max(1, lookback_days)}d",
        max_results=max(1, max_results),
        session=mail_session,
    )
    messages = listing.get("messages") or []
    candidates = [
        message
        for message in messages
        if message.get("id") and looks_like_meeting_invite(
            str(message.get("subject") or ""), str(message.get("snippet") or "")
        )
    ]
    if not candidates:
        return result

    known = await _known_external_ids(
        session, user_id, [str(message["id"]) for message in candidates]
    )
    now = datetime.now(timezone.utc)

    for message in candidates:
        message_id = str(message.get("id"))
        if message_id in known:
            result.skipped += 1
            continue
        result.scanned += 1
        try:
            full = await get_email(config, message_id, session=mail_session)
        except Exception as exc:  # network / IMAP issues must not abort the scan
            logger.warning("Meeting invite scan could not read message %s: %s", message_id, exc)
            result.errors.append(f"{message_id}: {exc}")
            continue

        subject = str(full.get("subject") or message.get("subject") or "")
        body = str(full.get("body") or full.get("snippet") or "")
        text = f"{subject}\n{body}".strip()
        # 邮件级幂等键统一用 Message-ID：新邮件提醒和任务去重都按它对齐。
        email_message_id = str(full.get("messageId") or message.get("messageId") or message_id)
        extracted = extract_meeting_invite(text, source="email")

        if use_llm and settings is not None and settings.openrouter_api_key and "starts_at" not in extracted:
            try:
                from config import chat_llm

                extracted = await extract_meeting_invite_with_llm(text, "email", chat_llm(settings))
            except Exception as exc:
                logger.warning("LLM invite extraction failed for %s: %s", message_id, exc)

        starts_at = _as_utc(extracted.get("starts_at"))
        if starts_at is None or starts_at < now - PAST_MEETING_GRACE:
            result.skipped += 1
            continue

        invite_request = MeetingInviteCreateRequest(
            title=_clean_title(subject, "会议邀请"),
            description=body.strip()[:2000] or None,
            starts_at=starts_at,
            ends_at=_as_utc(extracted.get("ends_at")),
            meeting_number=extracted.get("meeting_number"),
            join_url=extracted.get("join_url"),
            source="email",
            external_id=message_id,
            reminder_minutes_before=DEFAULT_REMINDER_MINUTES,
            extra={
                "email_subject": subject[:300],
                "email_from": str(full.get("from") or ""),
                "source_text_snippet": str(extracted.get("extra", {}).get("source_text_snippet") or "")[:2000],
            },
        )
        invite, _created = await upsert_meeting_invite(
            session,
            user_id,
            invite_request,
            found_in_email=True,
            email_message_id=email_message_id,
        )
        result.invites += 1
        task, task_created = await ensure_task_for_meeting_invite(
            session,
            user_id=user_id,
            invite=invite,
            email_subject=subject,
        )
        if task_created:
            result.tasks_created += 1
        elif task is not None:
            result.tasks_updated += 1
        result.items.append({
            "title": invite.title,
            "starts_at": invite.starts_at.isoformat(),
            "task_created": task_created,
            # 会议邀请已经推送过"已加入日程"，新邮件提醒按同一幂等键跳过。
            "email_message_id": email_message_id,
        })

    return result
