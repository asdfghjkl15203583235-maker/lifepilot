"""Pydantic args for the orchestrator background-job tools."""

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


def parse_iso8601_utc_instant(value: object) -> datetime:
    """Parse tool input to an aware UTC instant (ISO-8601; 'Z' allowed)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    else:
        raise TypeError("Expected ISO-8601 string or datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class CreateScheduledJobToolArgs(BaseModel):
    """Arguments for the orchestrator `create_scheduled_job` tool."""

    title: str = Field(max_length=500, description="Short label shown in the Background jobs UI.")
    instruction: str = Field(
        description="Natural-language task for the agent on each run (what to do, constraints, goals).",
    )
    schedule_description: str = Field(
        description="Human-readable schedule, e.g. 'Weekdays at 10:00 AM' or 'Every Monday at 9:00 AM'.",
    )
    timezone: str = Field(
        description="IANA timezone for this schedule, e.g. America/Los_Angeles, Europe/London, UTC.",
    )
    schedule_starts_at: datetime = Field(
        description="ISO-8601 instant when the schedule may first run (inclusive), UTC or offset, e.g. 2026-04-25T00:00:00Z.",
    )
    schedule_ends_at: datetime = Field(
        description="ISO-8601 instant after which no more runs occur (exclusive). Must be after schedule_starts_at.",
    )
    cron_expression: str = Field(
        description=(
            "Standard 5-field cron: minute hour day month day_of_week. "
            "Example: 0 10 * * 1-5 = 10:00 Mon–Fri. Use * * * * * only for quick tests. "
            "Field order matches APScheduler: minute hour day month day_of_week."
        ),
    )
    actions: list[dict[str, Any]] = Field(
        description=(
            "Planned action chips for the UI: each item {action_type, label} where action_type is one of: "
            "email, calendar, notion, custom."
        ),
    )

    @field_validator("schedule_starts_at", "schedule_ends_at", mode="before")
    @classmethod
    def parse_schedule_instants(cls, value: object) -> datetime:
        return parse_iso8601_utc_instant(value)

    @field_validator("actions", mode="before")
    @classmethod
    def normalize_action_keys(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for raw in value:
            if not isinstance(raw, dict):
                continue
            at = raw.get("action_type") or raw.get("actionType")
            lab = raw.get("label")
            if at is not None and lab is not None:
                normalized.append({"action_type": str(at), "label": str(lab)})
        return normalized

    @model_validator(mode="after")
    def require_actions_and_window(self) -> "CreateScheduledJobToolArgs":
        if not self.actions:
            raise ValueError("At least one action (action_type + label) is required.")
        if self.schedule_ends_at <= self.schedule_starts_at:
            raise ValueError("schedule_ends_at must be strictly after schedule_starts_at.")
        now_utc = datetime.now(timezone.utc)
        if self.schedule_ends_at <= now_utc:
            raise ValueError("schedule_ends_at must be in the future.")
        return self


class StopScheduledJobToolArgs(BaseModel):
    """Arguments for `stop_scheduled_job` — pauses a running schedule (no more runs until resumed manually)."""

    job_id: uuid.UUID = Field(
        description="The background job id (UUID) from the Background jobs list; must belong to the user.",
    )


class DeleteScheduledJobToolArgs(BaseModel):
    """Arguments for `delete_scheduled_job` — permanently removes the schedule and its run history."""

    job_id: uuid.UUID = Field(
        description="The background job id (UUID) to delete; must belong to the user. Cannot be undone.",
    )
