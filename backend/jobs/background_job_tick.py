"""APScheduler entrypoint: find due `BackgroundJob` rows, run the graph, write `BackgroundJobRun` rows."""

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from lib.enums import BackgroundJobRunOutcome
from models.background_job import BackgroundJob
from models.background_job_run import BackgroundJobRun
from services.agent_runner import build_scheduled_job_user_payload, invoke_chat_agent_state
from services.background_job_service import (
    advance_next_run,
    build_run_parameters_snapshot,
    build_traces_from_agent_state,
    decide_run_outcome,
    get_due_jobs,
    reconcile_past_window_schedules,
)
from services.notification_service import add_background_job_run_notification

logger = logging.getLogger(__name__)


async def run_due_background_jobs(
    session: AsyncSession,
    settings: Settings,
) -> int:
    """Run every ready job, persist history, and advance `next_run_at`. Returns the count processed."""
    now = datetime.now(timezone.utc)
    await reconcile_past_window_schedules(session, now)
    due = await get_due_jobs(session, now)
    processed = 0
    for job in due:
        try:
            advance_next_run(job, after_utc=now, tick_now=now)
            await _execute_and_record_run(session, settings, job)
            processed += 1
        except Exception:
            logger.exception("Failed processing background job %s for user %s", job.id, job.user_id)
    return processed


async def _execute_and_record_run(
    session: AsyncSession,
    settings: Settings,
    job: BackgroundJob,
) -> None:
    started = datetime.now(timezone.utc)
    payloads = build_scheduled_job_user_payload(job)
    state = None
    try:
        state = await invoke_chat_agent_state(
            settings=settings,
            session=session,
            user_id=job.user_id,
            message_payloads=payloads,
            inline_memory_extraction=True,
            trace_tags=["background_job"],
            trace_metadata={
                "background_job_id": str(job.id),
                "background_job_title": job.title,
            },
        )
    except Exception as exc:
        await _record_failed_run(
            session,
            settings,
            job,
            started=started,
            error_message=str(exc),
        )
        return

    finished = datetime.now(timezone.utc)
    duration_ms = max(0, int((finished - started).total_seconds() * 1000))
    outcome, detail = decide_run_outcome(state)
    params = build_run_parameters_snapshot(job, settings, agent_state=state)
    traces = build_traces_from_agent_state(state, started)

    session.add(
        BackgroundJobRun(
            job_id=job.id,
            user_id=job.user_id,
            job_title=job.title,
            started_at=started,
            finished_at=finished,
            outcome=outcome,
            detail=detail,
            duration_ms=duration_ms,
            traces=traces,
            run_parameters=params,
        )
    )
    await add_background_job_run_notification(
        session,
        user_id=job.user_id,
        job_id=job.id,
        job_title=job.title,
        outcome=outcome,
        detail=detail,
    )


async def _record_failed_run(
    session: AsyncSession,
    settings: Settings,
    job: BackgroundJob,
    *,
    started: datetime,
    error_message: str,
) -> None:
    finished = datetime.now(timezone.utc)
    duration_ms = max(0, int((finished - started).total_seconds() * 1000))
    session.add(
        BackgroundJobRun(
            job_id=job.id,
            user_id=job.user_id,
            job_title=job.title,
            started_at=started,
            finished_at=finished,
            outcome=BackgroundJobRunOutcome.FAILED,
            detail=error_message[:8000],
            duration_ms=duration_ms,
            traces=[],
            run_parameters=build_run_parameters_snapshot(job, settings, agent_state=None),
        )
    )
    await add_background_job_run_notification(
        session,
        user_id=job.user_id,
        job_id=job.id,
        job_title=job.title,
        outcome=BackgroundJobRunOutcome.FAILED,
        detail=error_message,
    )
