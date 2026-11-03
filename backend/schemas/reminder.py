import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from lib.enums import ReminderStatus, ReminderTriggerType


class ReminderCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    note: str | None = None
    task_id: uuid.UUID | None = None
    remind_at: datetime
    trigger_type: ReminderTriggerType = ReminderTriggerType.MANUAL
    payload: dict[str, Any] = Field(default_factory=dict)


class ReminderAckRequest(BaseModel):
    status: ReminderStatus = ReminderStatus.ACKNOWLEDGED


class ReminderSnoozeRequest(BaseModel):
    minutes: int = Field(default=15, ge=1, le=10080)


class ReminderResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID
    task_id: uuid.UUID | None
    title: str
    note: str | None
    remind_at: datetime
    trigger_type: ReminderTriggerType
    status: ReminderStatus
    sent_at: datetime | None
    created_at: datetime
    payload: dict[str, Any]