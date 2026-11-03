"""List scheduled jobs and run history for the authenticated user."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from db.session import get_session
from services.background_job_service import (
    BackgroundJobInvalidStateError,
    BackgroundJobNotFoundError,
    delete_scheduled_job,
    list_jobs_for_user,
    list_runs_for_user,
    stop_scheduled_job,
)
from services.background_job_serialization import job_to_api_dict

background_jobs_router = APIRouter(tags=["background-jobs"])


@background_jobs_router.get("/background-jobs")
async def get_background_jobs(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Return all jobs for the user (active, paused, ended)."""
    return await list_jobs_for_user(session, user_id)


@background_jobs_router.get("/background-jobs/runs")
async def get_background_job_runs(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    job_id: uuid.UUID | None = Query(default=None, description="Filter to a single schedule id."),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """Return recent run history, optionally filtered by job."""
    return await list_runs_for_user(session, user_id, job_id=job_id, limit=limit)


@background_jobs_router.post("/background-jobs/{job_id}/stop")
async def post_stop_background_job(
    job_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Pause an active schedule (or no-op if already paused)."""
    try:
        job = await stop_scheduled_job(session, user_id, job_id)
    except BackgroundJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BackgroundJobInvalidStateError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return job_to_api_dict(job)


@background_jobs_router.delete("/background-jobs/{job_id}")
async def delete_background_job(
    job_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Permanently delete a schedule and its run history."""
    try:
        await delete_scheduled_job(session, user_id, job_id)
    except BackgroundJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
