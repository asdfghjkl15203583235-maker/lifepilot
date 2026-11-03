import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from db.constants import (
    MAX_TASK_ASSIGNED_AGENT_LENGTH,
    MAX_TASK_DEPENDENCY_LENGTH,
    MAX_TASK_PLAN_ID_LENGTH,
    MAX_TASK_PROJECT_LENGTH,
    MAX_TASK_SOURCE_LENGTH,
    MAX_TASK_TITLE_LENGTH,
)
from lib.enums import TaskPriority, TaskStatus

if TYPE_CHECKING:
    from models.user import User


class Task(Base):
    """Task owned by a single user, with planner metadata for autonomous work."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(MAX_TASK_TITLE_LENGTH))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        SqlEnum(TaskStatus, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
        default=TaskStatus.NOT_STARTED,
    )
    project: Mapped[str | None] = mapped_column(String(MAX_TASK_PROJECT_LENGTH), nullable=True)
    assigned_agent: Mapped[str | None] = mapped_column(String(MAX_TASK_ASSIGNED_AGENT_LENGTH), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Planner fields
    plan_id: Mapped[str | None] = mapped_column(String(MAX_TASK_PLAN_ID_LENGTH), nullable=True, index=True)
    priority: Mapped[TaskPriority] = mapped_column(
        SqlEnum(TaskPriority, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
        default=TaskPriority.MEDIUM,
    )
    dependencies: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    plan_source: Mapped[str | None] = mapped_column(String(MAX_TASK_SOURCE_LENGTH), nullable=True)
    source: Mapped[str] = mapped_column(
        String(MAX_TASK_SOURCE_LENGTH),
        default="user",
        server_default="user",
    )
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    last_tracked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user: Mapped["User"] = relationship("User", back_populates="tasks")
