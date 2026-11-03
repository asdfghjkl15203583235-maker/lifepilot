import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from db.constants import (
    MAX_INTEGRATION_DISPLAY_NAME_LENGTH,
    MAX_INTEGRATION_EXTERNAL_ACCOUNT_ID_LENGTH,
    MAX_OAUTH_SCOPES_JSON_LENGTH,
)
from lib.enums import IntegrationProvider

if TYPE_CHECKING:
    from models.user import User


class UserIntegration(Base):
    """OAuth/API connection for one user to Gmail, Google Calendar, or Notion."""

    __tablename__ = "user_integrations"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_integrations_user_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[IntegrationProvider] = mapped_column(
        SqlEnum(IntegrationProvider, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
    )

    # OAuth tokens — encrypt at rest in production; stored here so MCP/tool layers can refresh calls.
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Provider account identity (Google subject, Notion workspace id, etc.) and UI label.
    external_account_id: Mapped[str | None] = mapped_column(
        String(MAX_INTEGRATION_EXTERNAL_ACCOUNT_ID_LENGTH),
        nullable=True,
    )
    display_name: Mapped[str | None] = mapped_column(
        String(MAX_INTEGRATION_DISPLAY_NAME_LENGTH),
        nullable=True,
    )

    # JSON array string of granted OAuth scopes (stored as text for portability).
    oauth_scopes_json: Mapped[str | None] = mapped_column(String(MAX_OAUTH_SCOPES_JSON_LENGTH), nullable=True)

    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped["User"] = relationship("User", back_populates="integrations")
