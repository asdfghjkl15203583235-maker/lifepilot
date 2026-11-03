import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MeetingInviteCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    meeting_number: str | None = Field(default=None, max_length=128)
    join_url: str | None = Field(default=None, max_length=1024)
    source: str = "manual"
    external_id: str | None = Field(default=None, max_length=512)
    reminder_minutes_before: int = Field(default=30, ge=0, le=10080)
    extra: dict[str, Any] = Field(default_factory=dict)


class MeetingInviteExtractRequest(BaseModel):
    text: str = Field(min_length=1, max_length=30000)
    source: str = "manual"
    use_llm: bool = True


class MeetingInviteFromEmailRequest(BaseModel):
    text: str = Field(min_length=1, max_length=30000)
    message_id: str | None = Field(default=None, max_length=512)
    external_id: str | None = Field(default=None, max_length=512)
    reminder_minutes_before: int = Field(default=30, ge=0, le=10080)


class MeetingInviteUpsertResponse(BaseModel):
    invite: "MeetingInviteResponse"
    created: bool


class MeetingInviteResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID
    source: str
    external_id: str | None
    title: str
    description: str | None
    starts_at: datetime
    ends_at: datetime | None
    meeting_number: str | None
    join_url: str | None
    found_in_email: bool
    reminder_sent_at: datetime | None
    created_at: datetime
    extra: dict[str, Any]