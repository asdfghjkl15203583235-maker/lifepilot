"""One execution of a `BackgroundJob` with persisted parameters and trace payload."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from lib.enums import BackgroundJobRunOutcome

if TYPE_CHECKING:
    from models.background_job import BackgroundJob
    from models.user import User


class BackgroundJobRun(Base):
    """Snapshot of a single scheduler-triggered (or future: manual) graph run."""

    __tablename__ = "background_job_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("background_jobs.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    job_title: Mapped[str] = mapped_column(String(500))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[BackgroundJobRunOutcome] = mapped_column(
        SqlEnum(BackgroundJobRunOutcome, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
    )
    detail: Mapped[str] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    traces: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    run_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    job: Mapped["BackgroundJob"] = relationship("BackgroundJob", back_populates="runs")
    user: Mapped["User"] = relationship("User", back_populates="background_job_runs")
