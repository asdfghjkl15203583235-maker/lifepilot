"""Thin async wrapper around the Google Calendar REST API using httpx.

All methods accept an access_token and call the Calendar API directly.
Token refresh is handled by `get_valid_access_token` before calling any method.
"""

import time
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from models.user_calendar_integration import UserCalendarIntegration

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

TOKEN_EXPIRY_BUFFER_SECONDS = 300
DEFAULT_MAX_RESULTS = 10
EVENTS_PER_PAGE = 250
MAX_PAGINATION_PAGES = 10
HTTP_TIMEOUT_SECONDS = 30


class CalendarApiError(Exception):
    """Raised when the Google Calendar API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Calendar API {status_code}: {detail}")


async def get_valid_access_token(
    session: AsyncSession,
    integration: UserCalendarIntegration,
    client_id: str,
    client_secret: str,
) -> str:
    """Return a valid access token, refreshing if expired."""
    is_expired = (
        integration.token_expiry is not None
        and integration.token_expiry < int(time.time()) + TOKEN_EXPIRY_BUFFER_SECONDS
    )

    if not is_expired and integration.access_token:
        return integration.access_token

    if not integration.refresh_token:
        raise CalendarApiError(401, "没有可用的刷新令牌，用户需要重新授权")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": integration.refresh_token,
                "grant_type": "refresh_token",
            },
        )

    if response.status_code != 200:
        raise CalendarApiError(response.status_code, "令牌刷新失败")

    token_data = response.json()
    integration.access_token = token_data["access_token"]
    expires_in: int | None = token_data.get("expires_in")
    if expires_in:
        integration.token_expiry = int(time.time()) + expires_in

    await session.flush()
    return integration.access_token


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def _calendar_request(
    method: str,
    path: str,
    access_token: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{CALENDAR_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.request(
            method,
            url,
            headers=_auth_headers(access_token),
            params=params,
            json=json_body,
        )

    if not response.is_success:
        raise CalendarApiError(response.status_code, response.text)

    if response.status_code == 204:
        return {}
    return response.json()


# ── List events ──────────────────────────────────────────────────────────

async def list_events(
    access_token: str,
    *,
    calendar_id: str = "primary",
    time_min: str | None = None,
    time_max: str | None = None,
    query: str = "",
    max_results: int = DEFAULT_MAX_RESULTS,
    page_token: str | None = None,
) -> dict[str, Any]:
    """List events from a calendar. time_min/time_max use RFC3339 format."""
    params: dict[str, Any] = {
        "maxResults": max_results,
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if time_min:
        params["timeMin"] = time_min
    if time_max:
        params["timeMax"] = time_max
    if query:
        params["q"] = query
    if page_token:
        params["pageToken"] = page_token
    return await _calendar_request(
        "GET", f"/calendars/{calendar_id}/events", access_token, params=params,
    )


# ── List ALL events (paginated) ──────────────────────────────────────────

async def list_all_events(
    access_token: str,
    *,
    calendar_id: str = "primary",
    time_min: str | None = None,
    time_max: str | None = None,
    query: str = "",
) -> dict[str, Any]:
    """Paginate through all events for a time range, returning them in one list."""
    all_items: list[dict[str, Any]] = []
    page_token: str | None = None
    summary = ""

    for _ in range(MAX_PAGINATION_PAGES):
        result = await list_events(
            access_token,
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
            query=query,
            max_results=EVENTS_PER_PAGE,
            page_token=page_token,
        )

        all_items.extend(result.get("items", []))
        summary = result.get("summary", summary)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return {
        "kind": "calendar#events",
        "summary": summary,
        "items": all_items,
        "total_events": len(all_items),
    }


# ── Get single event ─────────────────────────────────────────────────────

async def get_event(
    access_token: str,
    event_id: str,
    *,
    calendar_id: str = "primary",
) -> dict[str, Any]:
    return await _calendar_request(
        "GET", f"/calendars/{calendar_id}/events/{event_id}", access_token,
    )


# ── Create event ─────────────────────────────────────────────────────────

async def create_event(
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
    """Create a calendar event. start/end are RFC3339 datetime strings."""
    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": email} for email in attendees]
    return await _calendar_request(
        "POST", f"/calendars/{calendar_id}/events", access_token, json_body=body,
    )


# ── Update event ─────────────────────────────────────────────────────────

async def update_event(
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
    """Patch (partial update) an existing calendar event."""
    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if start is not None:
        body["start"] = {"dateTime": start}
    if end is not None:
        body["end"] = {"dateTime": end}
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    return await _calendar_request(
        "PATCH", f"/calendars/{calendar_id}/events/{event_id}", access_token, json_body=body,
    )


# ── Delete event ─────────────────────────────────────────────────────────

async def delete_event(
    access_token: str,
    event_id: str,
    *,
    calendar_id: str = "primary",
) -> dict[str, Any]:
    return await _calendar_request(
        "DELETE", f"/calendars/{calendar_id}/events/{event_id}", access_token,
    )


# ── List calendars ───────────────────────────────────────────────────────

async def list_calendars(access_token: str) -> dict[str, Any]:
    """List all calendars the authenticated user has access to."""
    return await _calendar_request("GET", "/users/me/calendarList", access_token)
