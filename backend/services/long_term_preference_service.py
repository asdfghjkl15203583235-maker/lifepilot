"""Structured preference/habit store for personalized planning.

Kept separate from the vector memory pipeline so planner prompts can consume
machine-readable habits, work hours, and priority rules without expensive
embedding retrieval.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.user_life_preference import UserLifePreference
from services.redis_client import get_redis

logger = logging.getLogger(__name__)

PREFERENCE_CACHE_TTL_SECONDS = 86_400


def _preference_key(user_id: uuid.UUID) -> str:
    return f"lifepilot:preferences:{user_id}"


async def get_user_life_preference(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> UserLifePreference | None:
    result = await session.execute(
        select(UserLifePreference).where(UserLifePreference.user_id == user_id),
    )
    return result.scalar_one_or_none()


async def upsert_life_preference(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    habits: list[dict[str, Any]] | None = None,
    time_preferences: dict[str, Any] | None = None,
    work_hours: dict[str, Any] | None = None,
    priority_rules: list[dict[str, Any]] | None = None,
    raw_notes: str | None = None,
) -> UserLifePreference:
    row = await get_user_life_preference(session, user_id)
    if row is None:
        row = UserLifePreference(user_id=user_id)
        session.add(row)
    if habits is not None:
        row.habits = habits
    if time_preferences is not None:
        row.time_preferences = time_preferences
    if work_hours is not None:
        row.work_hours = work_hours
    if priority_rules is not None:
        row.priority_rules = priority_rules
    if raw_notes is not None:
        row.raw_notes = raw_notes
    await session.flush()
    await session.refresh(row)
    await _invalidate_preference_cache(user_id)
    return row


async def invalidate_preference_cache(user_id: uuid.UUID) -> None:
    await _invalidate_preference_cache(user_id)


async def _invalidate_preference_cache(user_id: uuid.UUID) -> None:
    redis_client = get_redis()
    if redis_client is not None:
        try:
            await redis_client.delete(_preference_key(user_id))
        except Exception as exc:
            logger.warning("Preference cache delete failed for %s: %s", user_id, exc)