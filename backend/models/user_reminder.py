"""Proactive reminders created by the planner, scheduler, or manual task editing."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.constants import MAX_REMINDER_NOTE_LENGTH, MAX_REMINDER_TITLE_LENGTH
from lib.enums import ReminderStatus, ReminderTriggerType

if TYPE_CHECKING:
    pass


class UserReminder(Base):
    __tablename__ = "user_reminders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    title: Mapped[str] = mapped_column(String(MAX_REMINDER_TITLE_LENGTH))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    trigger_type: Mapped[ReminderTriggerType] = mapped_column(
        SqlEnum(ReminderTriggerType, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
        default=ReminderTriggerType.MANUAL,
    )
    status: Mapped[ReminderStatus] = mapped_column(
        SqlEnum(ReminderStatus, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
        default=ReminderStatus.PENDING,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())