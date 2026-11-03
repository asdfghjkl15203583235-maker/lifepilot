"""Calendar agent tools — thin wrappers around services.calendar_client.

Each standalone ``tool_*`` function accepts an access_token and delegates to the Calendar
client. ``make_calendar_tools(access_token)`` returns a list of LangChain tools with the
token closed over so it never appears in the LLM's tool schema.
"""

import json
from typing import Any

from langchain_core.tools import tool

from services.calendar_client import (
    create_event as _create_event,
    delete_event as _delete_event,
    get_event as _get_event,
    list_all_events as _list_all_events,
    list_calendars as _list_calendars,
    list_events as _list_events,
    update_event as _update_event,
)

DEFAULT_MAX_RESULTS = 10


# ── Standalone tool functions (kept for backward compat) ─────────────────


async def tool_list_events(
    access_token: str,
    *,
    calendar_id: str = "primary",
    time_min: str | None = None,
    time_max: str | None = None,
    query: str = "",
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """List events from a calendar.

    When a time range is specified, automatically paginates to fetch all
    matching events instead of returning only the first page.
    """
    if time_min or time_max:
        return await _list_all_events(
            access_token,
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
            query=query,
        )
    return await _list_events(
        access_token,
        calendar_id=calendar_id,
        time_min=time_min,
        time_max=time_max,
        query=query,
        max_results=max_results,
    )


async def tool_get_event(
    access_token: str,
    event_id: str,
    *,
    calendar_id: str = "primary",
) -> dict[str, Any]:
    """Fetch a single calendar event by its ID."""
    return await _get_event(access_token, event_id, calendar_id=calendar_id)


async def tool_create_event(
    access_token: str,
    summary: str,
    start: str,
    end: str,
    *,
    calendar_id: str = "primary",
    description: str = "",
    location: str = "",
    attendees: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new calendar event."""
    return await _create_event(
        access_token,
        summary,
        start,
        end,
        calendar_id=calendar_id,
        description=description,
        location=location,
        attendees=attendees,
    )


async def tool_update_event(
    access_token: str,
    event_id: str,
    *,
    calendar_id: str = "primary",
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    """Update (patch) an existing calendar event."""
    return await _update_event(
        access_token,
        event_id,
        calendar_id=calendar_id,
        summary=summary,
        start=start,
        end=end,
        description=description,
        location=location,
    )


async def tool_delete_event(
    access_token: str,
    event_id: str,
    *,
    calendar_id: str = "primary",
) -> dict[str, Any]:
    """Delete a calendar event."""
    return await _delete_event(access_token, event_id, calendar_id=calendar_id)


async def tool_list_calendars(access_token: str) -> dict[str, Any]:
    """List all calendars available to the authenticated user."""
    return await _list_calendars(access_token)


# ── LangChain tool factory ────────────────────────────────────────────────


def make_calendar_tools(access_token: str) -> list:
    """Return LangChain tools for Google Calendar with the access token closed over."""

    @tool
    async def list_events(
        calendar_id: str = "primary",
        time_min: str = "",
        time_max: str = "",
        query: str = "",
        max_results: int = 50,
    ) -> str:
        """List calendar events. time_min and time_max use RFC3339 format (e.g. 2025-01-15T09:00:00-05:00).
        For a specific day set time_min to 00:00:00 and time_max to 23:59:59. Automatically fetches all pages when a time range is given."""
        t_min = time_min or None
        t_max = time_max or None
        result = await tool_list_events(
            access_token,
            calendar_id=calendar_id,
            time_min=t_min,
            time_max=t_max,
            query=query,
            max_results=max_results,
        )
        return json.dumps(result, default=str)

    @tool
    async def get_event(event_id: str, calendar_id: str = "primary") -> str:
        """Fetch a single calendar event by its ID."""
        return json.dumps(
            await _get_event(access_token, event_id, calendar_id=calendar_id), default=str
        )

    @tool
    async def create_event(
        summary: str,
        start: str,
        end: str,
        calendar_id: str = "primary",
        description: str = "",
        location: str = "",
        attendees: list[str] | None = None,
    ) -> str:
        """Create a new calendar event. start and end use RFC3339 format."""
        return json.dumps(
            await _create_event(
                access_token,
                summary,
                start,
                end,
                calendar_id=calendar_id,
                description=description,
                location=location,
                attendees=attendees,
            ),
            default=str,
        )

    @tool
    async def update_event(
        event_id: str,
        calendar_id: str = "primary",
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        description: str | None = None,
        location: str | None = None,
    ) -> str:
        """Update an existing calendar event."""
        return json.dumps(
            await _update_event(
                access_token,
                event_id,
                calendar_id=calendar_id,
                summary=summary,
                start=start,
                end=end,
                description=description,
                location=location,
            ),
            default=str,
        )

    @tool
    async def delete_event(event_id: str, calendar_id: str = "primary") -> str:
        """Delete a calendar event by its ID."""
        return json.dumps(
            await _delete_event(access_token, event_id, calendar_id=calendar_id), default=str
        )

    @tool
    async def list_calendars() -> str:
        """List all Google Calendars available to the user."""
        return json.dumps(await _list_calendars(access_token), default=str)

    return [list_events, get_event, create_event, update_event, delete_event, list_calendars]
