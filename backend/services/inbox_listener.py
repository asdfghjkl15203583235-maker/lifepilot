"""主动邮箱监听 — IMAP 轮询 → LLM 识别会议/邀约 → 建任务 + 推送通知。

背景：`meeting_invite_scanner` 只认关键词模板（会议/邀请/Zoom…），朋友一句
「周六一起吃个饭？」永远不会触发。这里补上语义层：对**非会议模板**的新邮件
跑一次 LLM 抽取，识别出带绝对时间的会议 / 朋友邀约 / 预约，落成看板任务并
发一条站内通知，让用户知道"有人约了你"。

幂等：以邮件的 `Message-ID` 为键，Redis 记已处理（30 天）+ 数据库按
`tasks.source_metadata->>'email_message_id'` 兜底，重复轮询不会建第二条任务。
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, chat_llm
from lib.enums import TaskPriority
from models.task import Task
from schemas.task import TaskCreateRequest
from services import task_service
from services.inbox_event_extraction import extract_inbox_signal_with_llm
from services.mail163_client import Mail163Config, Mail163Session, get_email, list_emails
from services.notification_service import add_email_event_notification, format_notification_time
from services.redis_client import get_redis
from services.task_project_classifier import classify_task_project

logger = logging.getLogger(__name__)

POLL_LOOKBACK_DAYS = 2
MAX_MESSAGES_PER_TICK = 20
# LLM 调用有成本：每轮最多解析 8 封，剩下的留到下一轮（5 分钟后）。
MAX_LLM_CALLS_PER_TICK = 8
SEEN_TTL_SECONDS = 30 * 24 * 60 * 60
SEEN_KEY_PREFIX = "lifepilot:inbox-event-seen"
# 已经开始的邀约不再建任务（容忍轮询间隔与时区换算误差）。
PAST_EVENT_GRACE = timedelta(hours=2)

NOISE_SENDER_RE = re.compile(r"no-?reply|donotreply|no_reply|mailer-daemon|noreply", re.IGNORECASE)
NOISE_SUBJECT_RE = re.compile(
    r"订阅|退订|unsubscribe|newsletter|优惠|促销|广告|验证码|verification code",
    re.IGNORECASE,
)

_KIND_PREFIX = {
    "meeting": "会议：",
    "social": "邀约：",
    "appointment": "预约：",
}
_KIND_PRIORITY = {
    "meeting": TaskPriority.HIGH,
    "social": TaskPriority.MEDIUM,
    "appointment": TaskPriority.MEDIUM,
}


@dataclass
class InboxPollResult:
    scanned: int = 0
    extracted: int = 0
    tasks_created: int = 0
    duplicates: int = 0
    skipped: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "extracted": self.extracted,
            "tasks_created": self.tasks_created,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "items": self.items[:20],
            "errors": self.errors[:5],
        }


def _seen_key(user_id: uuid.UUID, message_id: str) -> str:
    digest = hashlib.sha256(f"{user_id}|{message_id}".encode("utf-8")).hexdigest()[:32]
    return f"{SEEN_KEY_PREFIX}:{digest}"


def is_noise_message(sender: str, subject: str) -> bool:
    return bool(NOISE_SENDER_RE.search(sender or "") or NOISE_SUBJECT_RE.search(subject or ""))


async def _is_marked_seen(user_id: uuid.UUID, message_id: str) -> bool:
    redis_client = get_redis()
    if redis_client is None:
        return False
    try:
        return bool(await redis_client.exists(_seen_key(user_id, message_id)))
    except Exception as exc:
        logger.warning("Inbox listener: seen-cache read failed (%s): %s", message_id, exc)
        return False


async def _mark_seen(user_id: uuid.UUID, message_id: str) -> None:
    redis_client = get_redis()
    if redis_client is None:
        return
    try:
        await redis_client.set(_seen_key(user_id, message_id), "1", ex=SEEN_TTL_SECONDS)
    except Exception as exc:
        logger.warning("Inbox listener: seen-cache write failed (%s): %s", message_id, exc)


async def find_task_for_email_message(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    message_id: str,
) -> Task | None:
    """DB 侧幂等兜底：这封邮件是否已经建过任务。"""
    result = await session.execute(
        select(Task)
        .where(
            Task.user_id == user_id,
            Task.source_metadata.op("->>")("email_message_id") == message_id,
        )
        .limit(1)
    )
    return result.scalars().first()


def _event_task_title(event: dict[str, Any]) -> str:
    prefix = _KIND_PREFIX.get(str(event.get("kind")), "邀约：")
    title = str(event.get("title") or "").strip()
    return f"{prefix}{title}"[:200]


def _event_task_description(
    event: dict[str, Any],
    *,
    subject: str,
    sender: str,
    snippet: str,
) -> str:
    lines = [
        f"来源邮件：《{subject}》",
        f"发件人：{sender}",
        f"时间：{format_notification_time(event['starts_at'])}",
    ]
    if event.get("location"):
        lines.append(f"地点：{event['location']}")
    if event.get("reason"):
        lines.append(f"识别依据：{event['reason']}")
    if snippet:
        lines.extend(["", snippet[:800]])
    return "\n".join(lines)


async def poll_mail163_inbox_for_events(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    config: Mail163Config,
    settings: Settings | None,
    lookback_days: int = POLL_LOOKBACK_DAYS,
    max_results: int = MAX_MESSAGES_PER_TICK,
    max_llm_calls: int = MAX_LLM_CALLS_PER_TICK,
    mail_session: Mail163Session | None = None,
) -> InboxPollResult:
    """Scan recent inbox messages for actionable events and project them onto tasks."""
    result = InboxPollResult()
    if settings is None or not getattr(settings, "openrouter_api_key", None):
        logger.info("Inbox listener skipped: LLM is not configured")
        return result

    listing = await list_emails(
        config,
        query=f"newer_than:{max(1, lookback_days)}d",
        max_results=max(1, max_results),
        session=mail_session,
    )
    messages = listing.get("messages") or []
    llm = chat_llm(settings)
    now = datetime.now(timezone.utc)
    llm_calls = 0

    for message in messages:
        subject = str(message.get("subject") or "")
        snippet = str(message.get("snippet") or "")
        sender = str(message.get("from") or "")

        if is_noise_message(sender, subject):
            result.skipped += 1
            continue

        # Message-ID 是全局幂等键；极少数邮件没有该 header 时退回 UID（带前缀，
        # 避免与真正的 Message-ID 撞键）。
        message_id = str(message.get("messageId") or "").strip()
        if not message_id:
            message_id = f"uid:{message.get('id')}"
        if not message_id or message_id == "uid:None":
            result.skipped += 1
            continue

        if await _is_marked_seen(user_id, message_id):
            result.duplicates += 1
            continue
        if await find_task_for_email_message(session, user_id=user_id, message_id=message_id):
            result.duplicates += 1
            await _mark_seen(user_id, message_id)
            continue
        if llm_calls >= max_llm_calls:
            break

        result.scanned += 1
        try:
            full = await get_email(config, str(message.get("id")), session=mail_session)
        except Exception as exc:
            logger.warning("Inbox listener could not read message %s: %s", message_id, exc)
            result.errors.append(f"{message_id}: {exc}")
            continue

        full_subject = str(full.get("subject") or subject)
        body = str(full.get("body") or full.get("snippet") or snippet)
        text = f"{full_subject}\n\n{body}".strip()

        llm_calls += 1
        signal = await extract_inbox_signal_with_llm(text, llm, now=now)
        if signal is not None and signal["action_required"]:
            # Shadow 阶段：只记录"这封邮件需要用户行动"，暂不建任务。
            # 攒一周真实数据看误判率，确认后再接自动建卡。
            logger.info(
                "Inbox listener action-required (shadow) user=%s message=%s subject=%r reason=%r",
                user_id,
                message_id,
                full_subject[:80],
                signal["action_reason"],
            )
        event = signal["event"] if signal is not None else None
        if event is None:
            await _mark_seen(user_id, message_id)
            result.skipped += 1
            continue

        starts_at: datetime = event["starts_at"]
        if starts_at < now - PAST_EVENT_GRACE:
            logger.info("Inbox listener skipped past event %r at %s", event["title"], starts_at)
            await _mark_seen(user_id, message_id)
            result.skipped += 1
            continue

        title = _event_task_title(event)
        task = await task_service.create_task_for_user(
            session,
            user_id=user_id,
            body=TaskCreateRequest(
                title=title,
                description=_event_task_description(
                    event, subject=full_subject, sender=str(full.get("from") or sender), snippet=body
                ),
                due_at=starts_at,
                priority=_KIND_PRIORITY.get(str(event.get("kind")), TaskPriority.MEDIUM),
                project=classify_task_project(title),
                assigned_agent="calendar_agent",
                plan_source="email_listener",
                source="user",
                source_metadata={
                    "origin": "email_event",
                    "email_message_id": message_id,
                    "email_subject": full_subject[:300],
                    "event_kind": str(event.get("kind")),
                },
            ),
        )
        result.extracted += 1
        if task is not None:
            result.tasks_created += 1
            await add_email_event_notification(
                session,
                user_id,
                title=f"邮件已加入待办：{event['title'][:60]}",
                body=(
                    f"来自《{full_subject}》"
                    + (f"，地点：{event['location']}" if event.get("location") else "")
                    + f"，时间：{format_notification_time(starts_at)}"
                ),
                task_id=task.id,
                email_message_id=message_id,
            )
        result.items.append({
            "title": event["title"],
            "starts_at": starts_at.isoformat(),
            "kind": event.get("kind"),
            "task_id": str(task.id) if task is not None else None,
            # 同一封邮件已经弹过"已加入待办"，新邮件提醒要跳过它。
            "email_message_id": message_id,
        })
        await _mark_seen(user_id, message_id)

    return result
