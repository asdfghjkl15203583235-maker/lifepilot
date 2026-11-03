"""Persistence and scheduling helpers for user background jobs (no HTTP)."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agents.state import AgentState
from config import Settings
from lib.enums import BackgroundJobRunOutcome, BackgroundJobStatus
from models.background_job import BackgroundJob
from models.background_job_run import BackgroundJobRun
from services.background_job_serialization import _iso, job_to_api_dict, run_to_api_dict

logger = logging.getLogger(__name__)

CRON_FIELD_COUNT = 5
VALID_ACTION_TYPES = frozenset({"email", "calendar", "notion", "custom"})


class BackgroundJobNotFoundError(ValueError):
    """No `BackgroundJob` row for the given id and user (caller maps to HTTP 404)."""


class BackgroundJobInvalidStateError(ValueError):
    """Operation not allowed for this job status (caller may map to HTTP 400)."""


def parse_cron_fields(expr: str) -> list[str]:
    """Return five cron parts or raise ValueError."""
    parts = expr.strip().split()
    if len(parts) != CRON_FIELD_COUNT:
        raise ValueError(
            f"Expected {CRON_FIELD_COUNT} cron fields (minute hour day month weekday), got {len(parts)}",
        )
    return parts


def compute_next_run_utc(
    cron_expression: str,
    timezone_name: str,
    after_utc: datetime,
) -> datetime | None:
    """Next fire time strictly after `after_utc` (APScheduler crontab in user's zone)."""
    try:
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        logger.warning("Invalid timezone for schedule: %s (%s)", timezone_name, exc)
        return None

    try:
        parse_cron_fields(cron_expression)
    except ValueError as exc:
        logger.warning("Invalid cron: %s", exc)
        return None

    trigger = CronTrigger.from_crontab(cron_expression.strip(), timezone=tz)
    if after_utc.tzinfo is None:
        after_utc = after_utc.replace(tzinfo=timezone.utc)
    next_local = trigger.get_next_fire_time(None, after_utc.astimezone(tz))
    if next_local is None:
        return None
    return next_local.astimezone(timezone.utc)


def _validate_actions(raw: list[dict[str, str]]) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for item in raw:
        action_type = (item.get("action_type") or item.get("actionType") or "").strip()
        label = (item.get("label") or "").strip()
        if not action_type or not label:
            continue
        if action_type not in VALID_ACTION_TYPES:
            raise ValueError(
                f"Invalid action_type '{action_type}'. Allowed: {', '.join(sorted(VALID_ACTION_TYPES))}",
            )
        cleaned.append({"action_type": action_type, "label": label})
    if not cleaned:
        raise ValueError("At least one action with action_type and label is required.")
    return cleaned


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def create_scheduled_job(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    title: str,
    instruction: str,
    schedule_description: str,
    timezone_name: str,
    schedule_starts_at: datetime,
    schedule_ends_at: datetime,
    cron_expression: str,
    actions: list[dict[str, str]],
) -> BackgroundJob:
    """Insert a new active job and compute the first `next_run_at` (UTC)."""
    title_clean = title.strip()
    if not title_clean:
        raise ValueError("必须提供标题")
    if not instruction.strip():
        raise ValueError("必须提供指令")

    _ = parse_cron_fields(cron_expression)
    _validate_zone(timezone_name)
    validated_actions = _validate_actions(actions)
    start_bound = _as_utc(schedule_starts_at)
    end_bound = _as_utc(schedule_ends_at)
    if end_bound <= start_bound:
        raise ValueError("schedule_ends_at 必须晚于 schedule_starts_at。")
    now_utc = datetime.now(timezone.utc)
    if end_bound <= now_utc:
        raise ValueError("schedule_ends_at 必须晚于当前时间。")

    start_after = max(now_utc, start_bound)
    next_run = compute_next_run_utc(cron_expression, timezone_name, start_after)
    if next_run is None or next_run >= end_bound:
        raise ValueError(
            "给定时间窗口内没有可执行的运行：请检查 cron、schedule_starts_at 和 schedule_ends_at。",
        )

    job = BackgroundJob(
        user_id=user_id,
        title=title_clean,
        instruction=instruction.strip(),
        schedule_description=schedule_description.strip() or "自定义计划",
        timezone_label=timezone_name.strip(),
        cron_expression=cron_expression.strip(),
        schedule_starts_at=start_bound,
        schedule_ends_at=end_bound,
        actions=validated_actions,
        status=BackgroundJobStatus.ACTIVE,
        next_run_at=next_run,
    )
    session.add(job)
    await session.flush()
    return job


def _validate_zone(tz_name: str) -> None:
    try:
        ZoneInfo(tz_name.strip())
    except Exception as exc:
        raise ValueError(f"Invalid IANA timezone: {tz_name!r}") from exc


def build_run_parameters_snapshot(
    job: BackgroundJob,
    settings: Settings,
    *,
    agent_state: AgentState | None,
) -> dict[str, Any]:
    """Everything needed to audit what the scheduler triggered with."""
    job_payload = {
        "id": str(job.id),
        "user_id": str(job.user_id),
        "title": job.title,
        "instruction": job.instruction,
        "schedule_description": job.schedule_description,
        "timezone_label": job.timezone_label,
        "cron_expression": job.cron_expression,
        "actions": job.actions,
        "status": job.status.value if isinstance(job.status, BackgroundJobStatus) else str(job.status),
        "schedule_starts_at_iso": _iso(job.schedule_starts_at),
        "schedule_ends_at_iso": _iso(job.schedule_ends_at),
    }
    out: dict[str, Any] = {
        "job": job_payload,
        "default_model": settings.openrouter_default_model,
    }
    if agent_state is not None:
        out["graph_state"] = {
            "tasks": agent_state.get("tasks"),
            "agent_results": agent_state.get("agent_results"),
            "error": agent_state.get("error"),
        }
    return out


def build_traces_from_agent_state(
    state: AgentState,
    started_at: datetime,
) -> list[dict[str, Any]]:
    """Heuristic trace list compatible with the UI trace modal (no raw chain-of-thought)."""
    traces: list[dict[str, Any]] = []
    base_ms = int(started_at.timestamp() * 1000)
    for index, tr in enumerate(state.get("agent_results") or [], 1):
        at_ms = base_ms + index * 1000
        at_dt = datetime.fromtimestamp(at_ms / 1000.0, tz=timezone.utc)
        traces.append(
            {
                "id": f"trace-{index}",
                "agentLabel": "编排器",
                "stepKind": "emit",
                "title": f"结果 {index} ({tr.get('route', '任务')})",
                "detail": str(tr.get("result", ""))[:4000],
                "atIso": at_dt.isoformat().replace("+00:00", "Z"),
            }
        )
    if not traces:
        fin = (state.get("final_answer") or state.get("error") or "没有结果。")[:4000]
        traces.append(
            {
                "id": "trace-1",
                "agentLabel": "编排器",
                "stepKind": "emit",
                "title": "定时运行",
                "detail": fin,
                "atIso": started_at.isoformat().replace("+00:00", "Z"),
            }
        )
    return traces


def decide_run_outcome(state: AgentState) -> tuple[BackgroundJobRunOutcome, str]:
    """Map graph state to a run outcome and human-readable detail string."""
    err = state.get("error")
    if err:
        return BackgroundJobRunOutcome.FAILED, str(err)[:8000]
    final = (state.get("final_answer") or "").strip()
    if not final:
        return BackgroundJobRunOutcome.FAILED, "The agent returned an empty result."
    return BackgroundJobRunOutcome.SUCCESS, final[:8000]


async def get_background_job_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
) -> BackgroundJob | None:
    result = await session.execute(
        select(BackgroundJob).where(
            BackgroundJob.id == job_id,
            BackgroundJob.user_id == user_id,
        ),
    )
    return result.scalar_one_or_none()


async def stop_scheduled_job(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
) -> BackgroundJob:
    """Set an **active** job to paused; clear next run. Idempotent if already paused."""
    job = await get_background_job_for_user(session, user_id, job_id)
    if job is None:
        raise BackgroundJobNotFoundError("No schedule with that id exists for your account.")
    if job.status == BackgroundJobStatus.ENDED:
        raise BackgroundJobInvalidStateError("That schedule has already ended; delete it to remove it from the list.")
    if job.status == BackgroundJobStatus.PAUSED:
        return job
    job.status = BackgroundJobStatus.PAUSED
    job.next_run_at = None
    return job


async def delete_scheduled_job(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
) -> None:
    """Permanently delete a job and its runs (CASCADE) for the owning user."""
    job = await get_background_job_for_user(session, user_id, job_id)
    if job is None:
        raise BackgroundJobNotFoundError("No schedule with that id exists for your account.")
    await session.delete(job)


async def list_jobs_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> list[dict[str, Any]]:
    result = await session.execute(
        select(BackgroundJob)
        .where(BackgroundJob.user_id == user_id)
        .order_by(BackgroundJob.created_at.desc()),
    )
    rows = result.scalars().all()
    return [job_to_api_dict(j) for j in rows]


async def list_runs_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    job_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = select(BackgroundJobRun).where(BackgroundJobRun.user_id == user_id)
    if job_id is not None:
        query = query.where(BackgroundJobRun.job_id == job_id)
    query = query.order_by(BackgroundJobRun.started_at.desc()).limit(limit)
    result = await session.execute(query)
    rows = result.scalars().all()
    return [run_to_api_dict(r) for r in rows]


async def get_due_jobs(
    session: AsyncSession,
    now_utc: datetime,
    *,
    max_rows: int = 50,
) -> list[BackgroundJob]:
    """Jobs that are active and at or past their next run time."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    result = await session.execute(
        select(BackgroundJob)
        .where(
            BackgroundJob.status == BackgroundJobStatus.ACTIVE,
            BackgroundJob.next_run_at.isnot(None),
            BackgroundJob.next_run_at < BackgroundJob.schedule_ends_at,
            BackgroundJob.next_run_at <= now_utc,
        )
        .order_by(BackgroundJob.next_run_at.asc())
        .limit(max_rows)
        .with_for_update(skip_locked=True),
    )
    return list(result.scalars().all())


def advance_next_run(
    job: BackgroundJob,
    after_utc: datetime,
    tick_now: datetime,
) -> None:
    """
    Set the next fire time strictly after `after_utc` (APScheduler crontab).
    If the next occurrence would fall at or after the exclusive `schedule_ends_at`, the job is ended.
    """
    if tick_now.tzinfo is None:
        tick_now = tick_now.replace(tzinfo=timezone.utc)
    end_bound = _as_utc(job.schedule_ends_at)
    nxt = compute_next_run_utc(job.cron_expression, job.timezone_label, after_utc)
    if nxt is None or nxt >= end_bound:
        job.next_run_at = None
        if job.status == BackgroundJobStatus.ACTIVE:
            job.status = BackgroundJobStatus.ENDED
            job.ended_at = tick_now
        return
    job.next_run_at = nxt


async def reconcile_past_window_schedules(
    session: AsyncSession,
    now_utc: datetime,
) -> None:
    """
    End ACTIVE jobs whose window has passed and which are not still due for a run this tick.
    Avoids stranding automations with an exclusive end in the past.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    not_still_due = or_(
        BackgroundJob.next_run_at.is_(None),
        BackgroundJob.next_run_at > now_utc,
        BackgroundJob.next_run_at >= BackgroundJob.schedule_ends_at,
    )
    await session.execute(
        update(BackgroundJob)
        .where(
            BackgroundJob.status == BackgroundJobStatus.ACTIVE,
            BackgroundJob.schedule_ends_at <= now_utc,
        )
        .where(not_still_due)
        .values(
            status=BackgroundJobStatus.ENDED,
            next_run_at=None,
            ended_at=now_utc,
        ),
    )
