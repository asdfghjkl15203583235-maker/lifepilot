"""Request/response payloads for the Tencent Meeting REST API adapter."""

from typing import Any

from pydantic import BaseModel, Field


class TencentMeetingCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    start_time: int | None = Field(
        default=None, description="Unix seconds; omitted defaults to provider now.",
    )
    end_time: int | None = Field(default=None, description="Unix seconds.")
    participants: list[str] | None = None
    settings: dict[str, Any] | None = Field(default_factory=dict)


class TencentMeetingUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    start_time: int | None = None
    end_time: int | None = None
