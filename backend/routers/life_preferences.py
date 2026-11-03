"""LifePilot long-term preferences API.

Exposes the structured habit/time/work-hour/priority store so the planner and
frontend can read and update the personalization surface without going through
the chat graph.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from db.dependencies import verify_database_ready
from db.session import get_session
from services.long_term_preference_service import (
    get_user_life_preference,
    invalidate_preference_cache,
    upsert_life_preference,
)

preferences_router = APIRouter(
    prefix="/life-preferences",
    tags=["life-preferences"],
    dependencies=[Depends(verify_database_ready)],
)


class LifePreferenceResponse(BaseModel):
    habits: list[dict[str, object]]
    time_preferences: dict[str, object]
    work_hours: dict[str, object]
    priority_rules: list[dict[str, object]]
    raw_notes: str | None
    updated_at: str | None


class LifePreferenceUpsertRequest(BaseModel):
    habits: list[dict[str, object]] | None = None
    time_preferences: dict[str, object] | None = None
    work_hours: dict[str, object] | None = None
    priority_rules: list[dict[str, object]] | None = None
    raw_notes: str | None = Field(default=None, max_length=4000)


@preferences_router.get("", response_model=LifePreferenceResponse)
async def get_life_preferences(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> LifePreferenceResponse:
    row = await get_user_life_preference(session, user_id)
    if row is None:
        return LifePreferenceResponse(
            habits=[],
            time_preferences={},
            work_hours={},
            priority_rules=[],
            raw_notes=None,
            updated_at=None,
        )
    return LifePreferenceResponse(
        habits=row.habits or [],
        time_preferences=row.time_preferences or {},
        work_hours=row.work_hours or {},
        priority_rules=row.priority_rules or [],
        raw_notes=row.raw_notes,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@preferences_router.put("", response_model=LifePreferenceResponse)
async def put_life_preferences(
    body: LifePreferenceUpsertRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> LifePreferenceResponse:
    row = await upsert_life_preference(
        session,
        user_id,
        habits=body.habits,
        time_preferences=body.time_preferences,
        work_hours=body.work_hours,
        priority_rules=body.priority_rules,
        raw_notes=body.raw_notes,
    )
    return LifePreferenceResponse(
        habits=row.habits or [],
        time_preferences=row.time_preferences or {},
        work_hours=row.work_hours or {},
        priority_rules=row.priority_rules or [],
        raw_notes=row.raw_notes,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@preferences_router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_life_preferences(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await get_user_life_preference(session, user_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到偏好设置")
    await session.delete(row)
    await session.flush()
    await invalidate_preference_cache(user_id)