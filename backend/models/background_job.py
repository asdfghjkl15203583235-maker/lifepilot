"""Recurring assistant work the user schedules (matches background jobs UI)."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from db.constants import MAX_BACKGROUND_JOB_TITLE_LENGTH
from lib.enums import BackgroundJobStatus

if TYPE_CHECKING:
    from models.background_job_run import BackgroundJobRun
    from models.user import User


class BackgroundJob(Base):
    """A cron-like schedule plus natural-language instruction for the agent graph."""

    __tablename__ = "background_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(MAX_BACKGROUND_JOB_TITLE_LENGTH))
    instruction: Mapped[str] = mapped_column(Text)
    schedule_description: Mapped[str] = mapped_column(String(512))
    timezone_label: Mapped[str] = mapped_column(String(128))
    cron_expression: Mapped[str] = mapped_column(String(128))
    actions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    status: Mapped[BackgroundJobStatus] = mapped_column(
        SqlEnum(BackgroundJobStatus, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
        default=BackgroundJobStatus.ACTIVE,
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # Inclusive start; exclusive end (no run at or after `schedule_ends_at`).
    schedule_starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schedule_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    runs: Mapped[list["BackgroundJobRun"]] = relationship(
        "BackgroundJobRun",
        back_populates="job",
        cascade="all, delete-orphan",
    )
    user: Mapped["User"] = relationship("User", back_populates="background_jobs")
