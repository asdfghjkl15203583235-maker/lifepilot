"""Dashboard aggregation endpoints.

Today this is just the daily briefing, but the `/dashboard` prefix leaves room
for other home-page digests (e.g. a weekly wrap) later.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from config import Settings, get_settings
from db.dependencies import verify_database_ready
from db.session import get_session
from services.daily_briefing_service import (
    _today_bounds,
    generate_and_store_briefing,
    get_cached_briefing,
)

dashboard_router = APIRouter(
    prefix="/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(verify_database_ready)],
)


class DailyBriefingResponse(BaseModel):
    date: str
    content: str


@dashboard_router.get("/daily-briefing", response_model=DailyBriefingResponse)
async def get_daily_briefing(
    force_refresh: bool = Query(default=False),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DailyBriefingResponse:
    today, _start, _end = _today_bounds(settings)
    if not force_refresh:
        cached = await get_cached_briefing(session, user_id, today)
        if cached is not None:
            return DailyBriefingResponse(date=today.isoformat(), content=cached)

    content = await generate_and_store_briefing(session, settings, user_id)
    return DailyBriefingResponse(date=today.isoformat(), content=content)
