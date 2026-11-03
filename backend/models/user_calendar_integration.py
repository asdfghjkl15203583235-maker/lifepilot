import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from models.user import User

CALENDAR_PROVIDER = "google_calendar"
CALENDAR_SCOPES = " ".join([
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/userinfo.email",
])
MAX_PROVIDER_ACCOUNT_ID_LENGTH = 254
MAX_SCOPE_LENGTH = 1024


class UserCalendarIntegration(Base):
    __tablename__ = "user_calendar_integrations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, unique=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default=CALENDAR_PROVIDER)
    provider_account_id: Mapped[str | None] = mapped_column(String(MAX_PROVIDER_ACCOUNT_ID_LENGTH), nullable=True)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expiry: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    scope: Mapped[str | None] = mapped_column(String(MAX_SCOPE_LENGTH), nullable=True, default=CALENDAR_SCOPES)
    is_connected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped["User"] = relationship("User", back_populates="calendar_integration")
