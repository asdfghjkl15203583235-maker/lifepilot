import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from lib.enums import PendingActionStatus, PendingActionType


class PendingActionResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: str | None
    action_type: PendingActionType
    status: PendingActionStatus
    title: str
    summary: str | None
    payload: dict[str, Any]
    result: dict[str, Any] | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    executed_at: datetime | None