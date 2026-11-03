"""Daily Briefing — aggregate today's tasks, calendar events, and recent emails
into a short LLM-generated digest, cached one row per user per calendar date.
"""

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, chat_llm
from models.daily_briefing import DailyBriefing

logger = logging.getLogger(__name__)

EMAIL_DETAIL_CAP = 6  # bound get_email calls — avoid token/rate blowups
EMAIL_FALLBACK_QUERY_DAYS = 7

# 晨报固定分两段展示任务：未完成 / 已完成。标题必须确定性生成，
# 不能交给 LLM 自由发挥——模型一省略，用户就看不到“已完成”那一段。
PENDING_TASK_HEADING = "【今日待办】"
DONE_TASK_HEADING = "【已完成】"
SCHEDULE_HEADING = "【日程】"
EMAIL_HEADING = "【邮件】"
EMPTY_PENDING_HINT = "今天没有未完成的待办，可以安心推进新计划。"
EMPTY_DONE_HINT = "今天还没有完成的任务，完成一项后会自动出现在这里。"
EMPTY_SCHEDULE_HINT = "今天没有会议或日程安排。"
EMPTY_EMAIL_HINT = "今天没有需要关注的新邮件。"


# ── Email helpers ─────────────────────────────────────────────────────────


def _msg_header(message: dict[str, Any], name: str) -> str:
    headers = (message.get("payload") or {}).get("headers") or []
    for header in headers:
        if (header.get("name") or "").lower() == name.lower():
            return str(header.get("value") or "")
    return ""


def _is_noise_email(message: dict[str, Any]) -> bool:
    """Skip ads / newsletters / no-reply daemons."""
    sender = (_msg_header(message, "From") or "").lower()
    subject = (_msg_header(message, "Subject") or "").lower()
    if re.search(r"no-?reply|donotreply|no_reply|mailer-daemon|noreply", sender):
        return True
    if re.search(r"订阅|退订|unsubscribe|newsletter|promo|优惠|促销|广告|weekly|digest", subject):
        return True
    if any(word in sender for word in ("@newsletter.", "mailinglist", "marketing.")):
        return True
    return False


def _message_fingerprint(message: dict[str, Any]) -> str:
    return str(message.get("id") or _msg_header(message, "Message-ID"))


def _summarize_email(message: dict[str, Any]) -> dict[str, str] | None:
    sender = _msg_header(message, "From")
    subject = _msg_header(message, "Subject")
    if not subject and not sender:
        return None
    return {
        "from": sender or "未知发件人",
        "subject": subject or "(无主题)",
        "snippet": (message.get("snippet") or "")[:200],
        "important": "IMPORTANT" in (message.get("labelIds") or []),
        "unread": "UNREAD" in (message.get("labelIds") or []),
    }


# ── Aggregation ───────────────────────────────────────────────────────────


def _today_bounds(settings: Settings) -> tuple[date, datetime, datetime]:
    tz = ZoneInfo(settings.planner_timezone or "UTC")
    now_local = datetime.now(tz)
    today = now_local.date()
    start_local = datetime.combine(today, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return today, start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def _fetch_recent_emails(
    session: AsyncSession,
    settings: Settings,
    user_id: object,
    start_utc: datetime,
) -> list[dict[str, str]]:
    from services import gmail_client as gmail_module
    from services import gmail_integration_service

    integration = await gmail_integration_service.get_gmail_integration(session, user_id)
    if integration is None or not integration.is_connected:
        return []
    if not (settings.google_client_id and settings.google_client_secret):
        return []

    try:
        token = await gmail_module.get_valid_access_token(
            session,
            integration,
            settings.google_client_id,
            settings.google_client_secret,
        )
    except Exception as exc:
        logger.warning("Daily briefing: gmail token unavailable for user %s: %s", user_id, exc)
        return []

    after_epoch = int(start_utc.timestamp())
    seen: set[str] = set()
    summaries: list[dict[str, str]] = []

    async def ingest(query: str, cap: int) -> None:
        try:
            listing = await gmail_module.list_emails(token, query=query, max_results=cap)
        except Exception as exc:
            logger.warning("Daily briefing: email list failed (%s): %s", query, exc)
            return
        stubs = listing.get("messages") or []
        for stub in stubs[:cap]:
            msg_id = stub.get("id")
            if not msg_id:
                continue
            try:
                message = await gmail_module.get_email(token, msg_id)
            except Exception:
                continue
            fp = _message_fingerprint(message)
            if fp in seen:
                continue
            seen.add(fp)
            if _is_noise_email(message):
                continue
            summary = _summarize_email(message)
            if summary:
                summaries.append(summary)

    # 1) today's unread or important emails first.
    await ingest(f"after:{after_epoch} (is:unread OR is:important)", EMAIL_DETAIL_CAP)
    if len(summaries) < 3:
        # 2) recent important emails (excluding noise), then recent general bucket.
        await ingest(f"newer_than:{EMAIL_FALLBACK_QUERY_DAYS}d is:important", EMAIL_DETAIL_CAP)
        if len(summaries) < 3:
            await ingest(f"newer_than:{EMAIL_FALLBACK_QUERY_DAYS}d", EMAIL_DETAIL_CAP)

    return summaries[:3]


def _task_summary(row: Any, tz: ZoneInfo) -> dict[str, Any]:
    due_label = None
    if row.due_at is not None:
        try:
            due_label = row.due_at.astimezone(tz).strftime("%H:%M")
        except (ValueError, OSError):
            due_label = None
    return {
        "id": str(row.id),
        "title": row.title,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "due_label": due_label,
        "status": getattr(row.status, "value", str(row.status)),
        "priority": getattr(row.priority, "value", None),
    }


async def _collect_context(
    session: AsyncSession,
    settings: Settings,
    user_id: object,
) -> tuple[date, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """Collect today's data, splitting tasks into 未完成 / 已完成.

    晨报必须分两段展示：今天要做的（未完成）和已经勾掉的（已完成）。
    已完成的任务可能不是今天到期（昨天到期今天才完成），因此单独按
    `completed_at` 查一次，再按 id 去重。
    """
    from services import local_calendar_service, task_service

    today, start_utc, end_utc = _today_bounds(settings)
    tz = ZoneInfo(settings.planner_timezone or "UTC")

    due_today = await task_service.list_user_tasks_between(
        session, user_id=user_id, start_at=start_utc, end_at=end_utc,
    )
    completed_today = await task_service.list_user_tasks_completed_between(
        session, user_id=user_id, start_at=start_utc, end_at=end_utc,
    )
    events_rows = await local_calendar_service.list_local_events_between(
        session, user_id=user_id, start_at=start_utc, end_at=end_utc,
    )
    emails = await _fetch_recent_emails(session, settings, user_id, start_utc)

    pending_tasks: list[dict[str, Any]] = []
    done_tasks: list[dict[str, Any]] = []
    seen_done_ids: set[str] = set()
    for row in due_today:
        summary = _task_summary(row, tz)
        if summary["status"] == "done":
            done_tasks.append(summary)
            seen_done_ids.add(summary["id"])
        else:
            pending_tasks.append(summary)
    for row in completed_today:
        summary = _task_summary(row, tz)
        if summary["id"] in seen_done_ids:
            continue
        done_tasks.append(summary)
        seen_done_ids.add(summary["id"])

    events = [
        {
            "title": row.title,
            "starts_at": row.starts_at.isoformat() if row.starts_at else None,
            "location": row.location,
        }
        for row in events_rows
    ]
    return today, pending_tasks, done_tasks, events, emails


# ── LLM generation ────────────────────────────────────────────────────────


_BRIEFING_SYSTEM_PROMPT = """\
你是一位贴心的个人助理,负责为使用者写一份"今日晨报"。
请根据提供的今日数据,写一段约 150 字的中文晨报。输出结构必须严格按下面顺序,
小标题必须一字不改(用小标题行,不要用 Markdown 加粗):

【今日待办】
- 今天还没完成的事,一条一行;没有就写「{empty_pending}」

【已完成】
- 今天已经完成的事,一条一行简要列出;没有就写「{empty_done}」

【日程】
- 今天的会议/事件(含时间);没有就写「{empty_schedule}」

【邮件】
- 只归纳最重要的 1-3 封(发件人+主题,一句话要点);没有就写「{empty_email}」

硬性要求:
- 四段必须全部出现,不允许省略任何一段,哪怕内容为空也要保留小标题和友好提示。
- 只基于给定事实,不要虚构任务、日程或邮件。
- 语气自然、简洁。
"""


def _task_line(task: dict) -> str:
    label = task.get("due_label")
    return f"- {task['title']}" + (f"（{label} 前）" if label else "")


def _render_task_section(
    heading: str,
    tasks: list[dict],
    empty_hint: str,
) -> str:
    if not tasks:
        return f"{heading}\n{empty_hint}"
    return "\n".join([heading, *(_task_line(task) for task in tasks)])


def _build_prompt_context(
    pending_tasks: list[dict],
    done_tasks: list[dict],
    events: list[dict],
    emails: list[dict],
) -> str:
    lines: list[str] = []
    lines.append(PENDING_TASK_HEADING)
    if pending_tasks:
        lines.extend(_task_line(task) for task in pending_tasks)
    else:
        lines.append(EMPTY_PENDING_HINT)

    lines.append(DONE_TASK_HEADING)
    if done_tasks:
        lines.extend(_task_line(task) for task in done_tasks)
    else:
        lines.append(EMPTY_DONE_HINT)

    lines.append(SCHEDULE_HEADING)
    if events:
        lines.extend(
            f"- {event['title']}" + (f" @ {event['location']}" if event.get("location") else "")
            for event in events
        )
    else:
        lines.append(EMPTY_SCHEDULE_HINT)

    lines.append(EMAIL_HEADING)
    if emails:
        for email in emails:
            marker = "★" if email.get("important") else ("未读" if email.get("unread") else "")
            lines.append(f"- {email['from']}：《{email['subject']}》{marker} {email['snippet']}")
    else:
        lines.append(EMPTY_EMAIL_HINT)
    return "\n".join(lines)


def _build_fallback_briefing(
    pending_tasks: list[dict],
    done_tasks: list[dict],
    events: list[dict],
    emails: list[dict],
) -> str:
    """确定性晨报：LLM 不可用时仍然两段齐全、空段也有提示语。"""
    sections = [
        _render_task_section(PENDING_TASK_HEADING, pending_tasks, EMPTY_PENDING_HINT),
        _render_task_section(DONE_TASK_HEADING, done_tasks, EMPTY_DONE_HINT),
    ]
    if events:
        sections.append(
            SCHEDULE_HEADING
            + "\n"
            + "\n".join(
                f"- {event['title']}"
                + (f"（{event['starts_at']}）" if event.get("starts_at") else "")
                for event in events
            )
        )
    else:
        sections.append(f"{SCHEDULE_HEADING}\n{EMPTY_SCHEDULE_HINT}")
    if emails:
        sections.append(
            EMAIL_HEADING
            + "\n"
            + "\n".join(f"- {email['from']}《{email['subject']}》" for email in emails)
        )
    else:
        sections.append(f"{EMAIL_HEADING}\n{EMPTY_EMAIL_HINT}")
    return "\n\n".join(sections)


def _ensure_task_sections(
    text: str,
    pending_tasks: list[dict],
    done_tasks: list[dict],
) -> str:
    """LLM 偶尔会漏段：缺哪段就用确定性内容补齐，保证两段一定出现。"""
    missing: list[str] = []
    if PENDING_TASK_HEADING not in text:
        missing.append(_render_task_section(PENDING_TASK_HEADING, pending_tasks, EMPTY_PENDING_HINT))
    if DONE_TASK_HEADING not in text:
        missing.append(_render_task_section(DONE_TASK_HEADING, done_tasks, EMPTY_DONE_HINT))
    if not missing:
        return text
    return "\n\n".join([text.strip(), *missing]).strip()


async def _generate_briefing(
    settings: Settings,
    pending_tasks: list[dict],
    done_tasks: list[dict],
    events: list[dict],
    emails: list[dict],
) -> str:
    context = _build_prompt_context(pending_tasks, done_tasks, events, emails)
    try:
        llm = chat_llm(settings)
        response = await llm.ainvoke(
            [
                SystemMessage(
                    content=_BRIEFING_SYSTEM_PROMPT.format(
                        empty_pending=EMPTY_PENDING_HINT,
                        empty_done=EMPTY_DONE_HINT,
                        empty_schedule=EMPTY_SCHEDULE_HINT,
                        empty_email=EMPTY_EMAIL_HINT,
                    )
                ),
                HumanMessage(content="以下为今天的数据:\n" + context + "\n\n请输出今日晨报。"),
            ],
        )
        text = response.content if isinstance(response.content, str) else ""
        if not text.strip():
            return _build_fallback_briefing(pending_tasks, done_tasks, events, emails)
        return _ensure_task_sections(text.strip(), pending_tasks, done_tasks)
    except Exception:
        logger.exception("Daily briefing LLM generation failed; using fallback")
        return _build_fallback_briefing(pending_tasks, done_tasks, events, emails)


# ── Public API (cache layer) ──────────────────────────────────────────────


async def get_cached_briefing(
    session: AsyncSession,
    user_id: object,
    briefing_date: date,
) -> str | None:
    result = await session.execute(
        select(DailyBriefing.content).where(
            DailyBriefing.user_id == user_id,
            DailyBriefing.briefing_date == briefing_date,
        )
    )
    row = result.first()
    return row[0] if row else None


async def generate_and_store_briefing(
    session: AsyncSession,
    settings: Settings,
    user_id: object,
) -> str:
    today, pending_tasks, done_tasks, events, emails = await _collect_context(
        session, settings, user_id
    )
    content = await _generate_briefing(settings, pending_tasks, done_tasks, events, emails)

    result = await session.execute(
        select(DailyBriefing).where(
            DailyBriefing.user_id == user_id,
            DailyBriefing.briefing_date == today,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        existing.content = content
    else:
        session.add(DailyBriefing(
            user_id=user_id,
            briefing_date=today,
            content=content,
        ))
    await session.flush()
    return content