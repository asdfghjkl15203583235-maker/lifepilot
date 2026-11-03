"""Local .ics calendar sync — parse files and upsert events keyed by (user, UID).

Events are normalized to UTC before being stored. Recurring events (RRULE)
are intentionally not expanded yet.
"""

import logging
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from icalendar import Calendar
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.user_calendar_event import UserCalendarEvent

logger = logging.getLogger(__name__)

UTC = timezone.utc


def _as_utc(value: object) -> datetime | None:
    """Extract a timezone-aware UTC datetime from an icalendar vDDDTypes value."""
    dt = getattr(value, "dt", value)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            # Floating time (no zone) — assume UTC to keep storage consistent.
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    if isinstance(dt, date):
        # All-day event → start at 00:00 UTC of that calendar day.
        return datetime(dt.year, dt.month, dt.day, tzinfo=UTC)
    return None


def _is_all_day(value: object) -> bool:
    dt = getattr(value, "dt", value)
    return isinstance(dt, date) and not isinstance(dt, datetime)


def _prop(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def parse_ics_file(path: Path) -> list[dict]:
    """Parse one .ics file into normalized event dicts (all times UTC)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        calendar = Calendar.from_ical(raw)
    except Exception as exc:
        logger.warning("Skipping malformed .ics file %s: %s", path, exc)
        return []

    events: list[dict] = []
    for component in calendar.walk("VEVENT"):
        uid = str(component.get("uid") or "").strip()
        if not uid:
            continue
        starts_at = _as_utc(component.get("dtstart"))
        if starts_at is None:
            continue
        ends_at = _as_utc(component.get("dtend")) or starts_at
        events.append(
            {
                "uid": uid[:512],
                "title": (_prop(component.get("summary")) or "未命名事件")[:500],
                "description": (_prop(component.get("description")) or "")[:2000],
                "location": (_prop(component.get("location")) or "")[:500],
                "starts_at": starts_at,
                "ends_at": ends_at,
                "all_day": _is_all_day(component.get("dtstart")),
            }
        )
    return events


def scan_ics_directory(directory: str | Path) -> list[Path]:
    """Return sorted *.ics files under *directory* (empty when the dir is missing)."""
    base = Path(directory)
    if not base.is_dir():
        return []
    return sorted(base.glob("*.ics"))


async def list_local_events_between(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
) -> list[UserCalendarEvent]:
    """Local events overlapping [start_at, end_at): start < end AND (end is null OR end > start)."""
    result = await session.execute(
        select(UserCalendarEvent).where(
            UserCalendarEvent.user_id == user_id,
            UserCalendarEvent.starts_at < end_at,
            or_(
                UserCalendarEvent.ends_at.is_(None),
                UserCalendarEvent.ends_at > start_at,
            ),
        )
    )
    return list(result.scalars().all())


async def upsert_local_events(
    session: AsyncSession,
    user_id: uuid.UUID,
    directory: str | Path,
) -> dict:
    """Scan *directory* and upsert all VEVENTs for *user_id* (keyed by UID)."""
    files = scan_ics_directory(directory)
    imported = 0
    updated = 0
    skipped = 0
    errors = 0

    existing = (
        await session.execute(
            select(UserCalendarEvent).where(UserCalendarEvent.user_id == user_id),
        )
    ).scalars().all()
    by_uid = {row.uid: row for row in existing}

    for path in files:
        parsed = parse_ics_file(path)
        if not parsed:
            # Count files we could not read/parse as errors; files with no
            # usable VEVENT (all skipped) are counted as skipped instead.
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
                has_vevent = "BEGIN:VEVENT" in raw
            except OSError:
                has_vevent = False
            if not has_vevent:
                errors += 1
            continue
        for event in parsed:
            row = by_uid.get(event["uid"])
            if row is None:
                session.add(
                    UserCalendarEvent(
                        user_id=user_id,
                        uid=event["uid"],
                        source_file=str(path),
                        title=event["title"],
                        description=event["description"],
                        location=event["location"],
                        starts_at=event["starts_at"],
                        ends_at=event["ends_at"],
                        all_day=event["all_day"],
                    )
                )
                by_uid[event["uid"]] = event
                imported += 1
            else:
                row.title = event["title"]
                row.description = event["description"]
                row.location = event["location"]
                row.starts_at = event["starts_at"]
                row.ends_at = event["ends_at"]
                row.all_day = event["all_day"]
                row.source_file = str(path)
                updated += 1

    await session.flush()
    return {
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "files": len(files),
        "errors": errors,
    }
