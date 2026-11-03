import uuid
from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from db.constants import MAX_EMAIL_LENGTH, MAX_USER_NAME_LENGTH

if TYPE_CHECKING:
    from models.background_job import BackgroundJob
    from models.background_job_run import BackgroundJobRun
    from models.conversation import Conversation
    from models.task import Task
    from models.user_calendar_integration import UserCalendarIntegration
    from models.user_gmail_integration import UserGmailIntegration
    from models.user_integration import UserIntegration
    from models.user_notion_integration import UserNotionIntegration
    from models.user_notification import UserNotification


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(MAX_EMAIL_LENGTH), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(MAX_USER_NAME_LENGTH))

    tasks: Mapped[list["Task"]] = relationship("Task", back_populates="user")
    conversations: Mapped[list["Conversation"]] = relationship(
        "Conversation",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    integrations: Mapped[list["UserIntegration"]] = relationship(
        "UserIntegration",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    gmail_integration: Mapped["UserGmailIntegration | None"] = relationship(
        "UserGmailIntegration",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )
    calendar_integration: Mapped["UserCalendarIntegration | None"] = relationship(
        "UserCalendarIntegration",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )
    notion_integration: Mapped["UserNotionIntegration | None"] = relationship(
        "UserNotionIntegration",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )
    background_jobs: Mapped[list["BackgroundJob"]] = relationship(
        "BackgroundJob",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    background_job_runs: Mapped[list["BackgroundJobRun"]] = relationship(
        "BackgroundJobRun",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    notifications: Mapped[list["UserNotification"]] = relationship(
        "UserNotification",
        back_populates="user",
        cascade="all, delete-orphan",
    )
