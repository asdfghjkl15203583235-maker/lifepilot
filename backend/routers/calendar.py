import time
import urllib.parse
import uuid
from datetime import datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from auth.jwt import create_access_token
from auth.oauth_redirect import oauth_success_redirect
from config import Settings, get_settings
from db.dependencies import verify_database_ready
from db.session import get_session
from schemas.calendar import CalendarAuthUrlResponse, CalendarStatusResponse
from schemas.calendar_event import (
    CalendarDayResponse,
    LocalCalendarEventResponse,
)
from schemas.task import TaskResponse
from services import calendar_integration_service, local_calendar_service, task_service, user_service

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"

CALENDAR_SCOPES = " ".join([
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/userinfo.email",
])

calendar_router = APIRouter(prefix="/calendar", tags=["calendar"])


def _require_google_credentials(settings: Settings) -> tuple[str, str]:
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth 凭据尚未配置",
        )
    return settings.google_client_id, settings.google_client_secret


@calendar_router.get("/auth-url", response_model=CalendarAuthUrlResponse)
async def get_calendar_auth_url(settings: Settings = Depends(get_settings)) -> CalendarAuthUrlResponse:
    client_id, _ = _require_google_credentials(settings)

    params = {
        "client_id": client_id,
        "redirect_uri": settings.google_calendar_redirect_uri,
        "response_type": "code",
        "scope": CALENDAR_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{GOOGLE_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"
    return CalendarAuthUrlResponse(auth_url=auth_url)


@calendar_router.get("/callback", dependencies=[Depends(verify_database_ready)])
async def calendar_oauth_callback(
    code: Annotated[str, Query()],
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    client_id, client_secret = _require_google_credentials(settings)

    if not settings.jwt_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="JWT_SECRET 尚未配置")

    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": settings.google_calendar_redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_response.status_code != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="与 Google 交换授权码失败")

    token_data: dict[str, Any] = token_response.json()
    access_token: str = token_data["access_token"]
    refresh_token: str | None = token_data.get("refresh_token")
    expires_in: int | None = token_data.get("expires_in")
    token_expiry = int(time.time()) + expires_in if expires_in else None

    async with httpx.AsyncClient() as client:
        userinfo_response = await client.get(
            GOOGLE_USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if userinfo_response.status_code != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="无法从 Google 获取用户信息")

    email: str = userinfo_response.json()["email"]

    user = await user_service.get_or_create_user_by_email(session, email)

    await calendar_integration_service.upsert_calendar_integration(
        session,
        user_id=user.id,
        access_token=access_token,
        refresh_token=refresh_token,
        token_expiry=token_expiry,
        provider_account_id=email,
    )

    jwt_token = create_access_token(user.id, email, settings.jwt_secret)

    return oauth_success_redirect(settings.frontend_url, jwt_token)


@calendar_router.get(
    "/status",
    response_model=CalendarStatusResponse,
    dependencies=[Depends(verify_database_ready)],
)
async def get_calendar_status(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> CalendarStatusResponse:
    integration = await calendar_integration_service.get_calendar_integration(session, user_id)
    if integration is None:
        return CalendarStatusResponse(is_connected=False)

    return CalendarStatusResponse(
        is_connected=integration.is_connected,
        provider_account_id=integration.provider_account_id,
    )


@calendar_router.get(
    "/events",
    response_model=CalendarDayResponse,
    dependencies=[Depends(verify_database_ready)],
)
async def get_calendar_events(
    start_at: Annotated[datetime, Query()],
    end_at: Annotated[datetime, Query()],
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> CalendarDayResponse:
    """Merged view over [start_at, end_at): local ICS events + user tasks with due_at."""
    events = await local_calendar_service.list_local_events_between(
        session, user_id=user_id, start_at=start_at, end_at=end_at,
    )
    tasks = await task_service.list_user_tasks_between(
        session, user_id=user_id, start_at=start_at, end_at=end_at,
    )
    return CalendarDayResponse(
        events=[LocalCalendarEventResponse.model_validate(e) for e in events],
        tasks=[TaskResponse.model_validate(t) for t in tasks],
    )


@calendar_router.post(
    "/sync-local",
    dependencies=[Depends(verify_database_ready)],
)
async def post_sync_local_calendar(
    settings: Settings = Depends(get_settings),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Scan LOCAL_ICS_DIR and upsert all local .ics events for the user."""
    summary = await local_calendar_service.upsert_local_events(
        session, user_id=user_id, directory=settings.local_ics_dir,
    )
    return {"status": "ok", **summary}


@calendar_router.post(
    "/disconnect",
    dependencies=[Depends(verify_database_ready)],
)
async def post_disconnect_calendar(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Clear stored Google Calendar OAuth tokens and mark the integration disconnected."""
    from services.agent_runner import invalidate_token_cache
    cleared = await calendar_integration_service.disconnect_calendar_integration(session, user_id)
    await invalidate_token_cache(user_id, "calendar")
    return {"disconnected": cleared}
