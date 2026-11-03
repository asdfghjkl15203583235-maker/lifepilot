"""Map ORM rows to the JSON the frontend expects (camelCase in nested trace keys)."""

from datetime import datetime, timezone
from typing import Any

from models.background_job import BackgroundJob
from models.background_job_run import BackgroundJobRun


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def job_to_api_dict(job: BackgroundJob) -> dict[str, Any]:
    status = job.status.value if hasattr(job.status, "value") else str(job.status)
    return {
        "id": str(job.id),
        "title": job.title,
        "instruction": job.instruction,
        "schedule_description": job.schedule_description,
        "timezone_label": job.timezone_label,
        "cron_expression": job.cron_expression,
        "actions": list(job.actions or []),
        "status": status,
        "next_run_iso": _iso(job.next_run_at),
        "schedule_starts_at_iso": _iso(job.schedule_starts_at),
        "schedule_ends_at_iso": _iso(job.schedule_ends_at),
        "created_at_iso": _iso(job.created_at) or "",
        "ended_at_iso": _iso(job.ended_at),
    }


def run_to_api_dict(row: BackgroundJobRun) -> dict[str, Any]:
    out = {
        "id": str(row.id),
        "job_id": str(row.job_id),
        "job_title": row.job_title,
        "started_at_iso": _iso(row.started_at) or "",
        "finished_at_iso": _iso(row.finished_at) or "",
        "outcome": row.outcome.value if hasattr(row.outcome, "value") else str(row.outcome),
        "detail": row.detail,
        "duration_ms": row.duration_ms,
        "traces": list(row.traces or []),
        "run_parameters": dict(row.run_parameters or {}),
    }
    return out
