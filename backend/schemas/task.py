import uuid
from datetime import datetime

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from db.constants import (
    MAX_TASK_ASSIGNED_AGENT_LENGTH,
    MAX_TASK_DEPENDENCY_LENGTH,
    MAX_TASK_DESCRIPTION_LENGTH,
    MAX_TASK_PLAN_ID_LENGTH,
    MAX_TASK_PROJECT_LENGTH,
    MAX_TASK_SOURCE_LENGTH,
    MAX_TASK_TITLE_LENGTH,
)
from lib.enums import TaskPriority, TaskStatus


class TaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TASK_TITLE_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_TASK_DESCRIPTION_LENGTH)
    status: TaskStatus = TaskStatus.NOT_STARTED
    project: str | None = Field(default=None, max_length=MAX_TASK_PROJECT_LENGTH)
    assigned_agent: str | None = Field(default=None, max_length=MAX_TASK_ASSIGNED_AGENT_LENGTH)
    priority: TaskPriority = TaskPriority.MEDIUM
    due_at: datetime | None = None
    dependencies: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_TASK_DEPENDENCY_LENGTH)
    plan_id: str | None = Field(default=None, max_length=MAX_TASK_PLAN_ID_LENGTH)
    plan_source: str | None = Field(default=None, max_length=MAX_TASK_SOURCE_LENGTH)
    source: str = Field(default="user", max_length=MAX_TASK_SOURCE_LENGTH)
    source_metadata: dict[str, Any] | None = None


class TaskUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TASK_TITLE_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_TASK_DESCRIPTION_LENGTH)
    status: TaskStatus | None = None
    project: str | None = Field(default=None, max_length=MAX_TASK_PROJECT_LENGTH)
    assigned_agent: str | None = Field(default=None, max_length=MAX_TASK_ASSIGNED_AGENT_LENGTH)
    priority: TaskPriority | None = None
    due_at: datetime | None = None
    dependencies: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_TASK_DEPENDENCY_LENGTH)
    plan_id: str | None = Field(default=None, max_length=MAX_TASK_PLAN_ID_LENGTH)
    plan_source: str | None = Field(default=None, max_length=MAX_TASK_SOURCE_LENGTH)
    source: str | None = Field(default=None, max_length=MAX_TASK_SOURCE_LENGTH)
    source_metadata: dict[str, Any] | None = None


class TaskResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    description: str | None
    status: TaskStatus
    project: str | None
    assigned_agent: str | None
    priority: TaskPriority
    dependencies: list[dict[str, Any]]
    plan_id: str | None
    due_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    plan_source: str | None
    source: str | None
    source_metadata: dict[str, Any] | None
    last_tracked_at: datetime | None
    created_at: datetime
