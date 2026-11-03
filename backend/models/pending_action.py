"""Human-in-the-loop confirmation drafts for high-risk write operations.

AI generates a frozen, structured draft (payload); the user confirms or rejects
it in the frontend; only an explicit /approve call executes the payload. The
execute path never re-parses conversation text, so email bodies cannot inject
commands.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.constants import MAX_PENDING_ACTION_SUMMARY_LENGTH, MAX_PENDING_ACTION_TITLE_LENGTH
from lib.enums import PendingActionStatus, PendingActionType


class PendingAction(Base):
    __tablename__ = "pending_actions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action_type: Mapped[PendingActionType] = mapped_column(
        SqlEnum(PendingActionType, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    status: Mapped[PendingActionStatus] = mapped_column(
        SqlEnum(PendingActionStatus, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
        default=PendingActionStatus.PENDING,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(MAX_PENDING_ACTION_TITLE_LENGTH))
    summary: Mapped[str | None] = mapped_column(String(MAX_PENDING_ACTION_SUMMARY_LENGTH), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)