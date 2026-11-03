"""APScheduler entrypoint: deliver due reminders as in-app notifications."""

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from services.reminder_service import process_due_reminders

logger = logging.getLogger(__name__)


async def run_reminder_tick(session: AsyncSession, settings: Settings) -> int:
    """Deliver all reminders whose `remind_at` has passed. Returns delivered count."""
    if not settings.reminder_tick_enabled:
        return 0
    now = datetime.now(timezone.utc)
    delivered = await process_due_reminders(session, now)
    if delivered:
        logger.info("Reminder tick delivered %d reminder(s)", delivered)
    return delivered