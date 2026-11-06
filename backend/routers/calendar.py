import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from config import Settings, get_settings
from db.dependencies import verify_database_ready
from db.session import get_session
from schemas.calendar import CalendarStatusResponse
from schemas.calendar_event import (
    CalendarDayResponse,
    LocalCalendarEventResponse,
)
from schemas.task import TaskResponse
from services import calendar_integration_service, local_calendar_service, task_service

calendar_router = APIRouter(prefix="/calendar", tags=["calendar"])


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
