"""新邮件到达提醒 — 轮询收件箱，把**刚到的**邮件写成站内通知。

与 `inbox_listener`（语义抽取 → 建任务）分工不同：这里只回答"有没有新邮件"，
不调用 LLM、不建任务。两者靠邮件 `Message-ID` 互斥，同一封邮件只提醒一次。

基线（baseline）是**上一次成功轮询的时刻**，不是固定值：
  - 服务重启、IMAP 被拒导致漏轮询时，期间积压的邮件不会一次性补发；
  - 停顿超过 `BASELINE_MAX_LAG` 就重新起算，最多回看 10 分钟；
  - 一轮只提醒最新的一封（`MAX_ALERTS_PER_TICK`），用户要的是"来新邮件了"，
    不是把今天的收件箱翻出来。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from services.inbox_listener import find_task_for_email_message, is_noise_message
from services.mail163_client import Mail163Config, Mail163Session, list_emails
from services.notification_service import add_new_mail_notification
from services.redis_client import get_redis

logger = logging.getLogger(__name__)

POLL_LOOKBACK_DAYS = 2
MAX_MESSAGES_PER_TICK = 20
BASELINE_KEY_PREFIX = "lifepilot:new-mail-baseline"

# 漏轮询超过这个窗口就重新起算，不补发历史邮件。
BASELINE_MAX_LAG = timedelta(minutes=10)
# 一轮最多弹几封：按到达时间倒序，只提醒最新的那封。
MAX_ALERTS_PER_TICK = 1

# Redis 不可用时的兜底基线：上一次扫描的开始时间（进程内存）。
_last_scan_marker: datetime | None = None


@dataclass
class NewMailNotifyResult:
    scanned: int = 0
    notified: int = 0
    duplicates: int = 0
    skipped: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "notified": self.notified,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "items": self.items[:20],
            "errors": self.errors[:5],
        }


def _baseline_key(user_id: uuid.UUID) -> str:
    return f"{BASELINE_KEY_PREFIX}:{user_id}"


async def _read_baseline(user_id: uuid.UUID) -> datetime | None:
    redis_client = get_redis()
    if redis_client is None:
        return _last_scan_marker
    try:
        stored = await redis_client.get(_baseline_key(user_id))
        if not stored:
            return _last_scan_marker
        return datetime.fromisoformat(str(stored))
    except Exception as exc:
        logger.warning("New mail notifier: baseline read failed, using in-memory marker: %s", exc)
        return _last_scan_marker


async def _write_baseline(user_id: uuid.UUID, moment: datetime) -> None:
    global _last_scan_marker  # noqa: PLW0603 — 无 Redis 时的进程内兜底
    _last_scan_marker = moment

    redis_client = get_redis()
    if redis_client is None:
        return
    try:
        await redis_client.set(_baseline_key(user_id), moment.isoformat())
    except Exception as exc:
        logger.warning("New mail notifier: baseline write failed: %s", exc)


async def resolve_baseline(user_id: uuid.UUID, *, now: datetime | None = None) -> datetime:
    """Return the moment the next scan starts counting from.

    First ever run records "now" so the two-day backlog is treated as history;
    later runs continue from the previous successful scan, clamped so a long
    outage cannot flush hours of mail into the toast host.
    """
    moment = now or datetime.now(timezone.utc)
    stored = await _read_baseline(user_id)
    if stored is None:
        await _write_baseline(user_id, moment)
        return moment

    earliest = moment - BASELINE_MAX_LAG
    return stored if stored > earliest else earliest


def _parse_message_date(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _message_key(message: dict[str, Any]) -> str | None:
    message_id = str(message.get("messageId") or "").strip()
    if message_id:
        return message_id
    fallback_uid = message.get("id")
    if fallback_uid in (None, ""):
        return None
    return f"uid:{fallback_uid}"


async def notify_new_inbox_mail(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    config: Mail163Config,
    baseline: datetime | None = None,
    lookback_days: int = POLL_LOOKBACK_DAYS,
    max_results: int = MAX_MESSAGES_PER_TICK,
    skip_message_ids: Iterable[str] | None = None,
    mail_session: Mail163Session | None = None,
) -> NewMailNotifyResult:
    """Announce the newest inbox message that is neither noise nor already known."""
    result = NewMailNotifyResult()
    scan_started_at = datetime.now(timezone.utc)
    watched_since = baseline or await resolve_baseline(user_id, now=scan_started_at)
    handled_elsewhere = {value for value in (skip_message_ids or set()) if value}

    try:
        listing = await list_emails(
            config,
            query=f"newer_than:{max(1, lookback_days)}d",
            max_results=max(1, max_results),
            session=mail_session,
        )
    except Exception as exc:
        # 不推进基线：下一轮会重新覆盖这段时间，不会把邮件漏掉。
        logger.warning("New mail notifier could not list inbox: %s", exc)
        result.errors.append(f"list: {exc}")
        return result

    # IMAP SEARCH 返回的是按 UID 升序（最旧在前），这里改成最新的先看。
    messages = sorted(
        listing.get("messages") or [],
        key=lambda message: _parse_message_date(message.get("date"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    for message in messages:
        if result.notified >= MAX_ALERTS_PER_TICK:
            break

        key = _message_key(message)
        if key is None:
            result.skipped += 1
            continue

        if key in handled_elsewhere:
            result.skipped += 1
            continue

        subject = str(message.get("subject") or "")
        sender = str(message.get("from") or "")
        if is_noise_message(sender, subject):
            result.skipped += 1
            continue

        received_at = _parse_message_date(message.get("date"))
        if received_at is None or received_at < watched_since:
            result.skipped += 1
            continue

        result.scanned += 1

        # 这封邮件已经因为"会议/邀约"建过任务并推过通知了，不再弹第二条。
        if await find_task_for_email_message(session, user_id=user_id, message_id=key):
            result.duplicates += 1
            continue

        try:
            row = await add_new_mail_notification(
                session,
                user_id,
                email_message_id=key,
                subject=subject,
                sender=sender,
                snippet=str(message.get("snippet") or ""),
                received_at=received_at,
                email_uid=str(message.get("id") or "") or None,
            )
        except Exception as exc:
            logger.warning("New mail notifier could not record %s: %s", key, exc)
            result.errors.append(f"{key}: {exc}")
            continue

        if row is None:
            result.duplicates += 1
            continue

        result.notified += 1
        result.items.append({
            "message_id": key,
            "subject": subject[:120],
            "received_at": received_at.isoformat(),
        })

    # 只在这一轮成功读完收件箱后推进基线；失败时保持原值，下一轮补上。
    await _write_baseline(user_id, scan_started_at)
    return result
