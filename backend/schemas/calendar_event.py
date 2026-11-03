import uuid
from datetime import datetime

from pydantic import BaseModel

from schemas.task import TaskResponse


class LocalCalendarEventResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    uid: str
    source_file: str | None
    title: str
    description: str | None
    location: str | None
    starts_at: datetime
    ends_at: datetime | None
    all_day: bool


class CalendarDayResponse(BaseModel):
    """Merged view of one day: local events + user tasks with due_at."""
    events: list[LocalCalendarEventResponse]
    tasks: list[TaskResponse]


class CalendarMonthResponse(BaseModel):
    """Merged view of a date range: local events + user tasks with due_at."""
    events: list[LocalCalendarEventResponse]
    tasks: list[TaskResponse]
