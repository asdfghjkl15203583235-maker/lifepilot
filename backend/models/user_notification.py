import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from db.constants import MAX_NOTIFICATION_BODY_LENGTH, MAX_NOTIFICATION_TITLE_LENGTH
from lib.enums import NotificationCategory

if TYPE_CHECKING:
    from models.user import User


class UserNotification(Base):
    """A single in-app alert for a user (e.g. scheduled job finished or failed)."""

    __tablename__ = "user_notifications"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[NotificationCategory] = mapped_column(
        SqlEnum(NotificationCategory, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
    )
    title: Mapped[str] = mapped_column(String(MAX_NOTIFICATION_TITLE_LENGTH))
    body: Mapped[str] = mapped_column(String(MAX_NOTIFICATION_BODY_LENGTH))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    background_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("background_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reminder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_reminders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 邮件类通知的幂等键：一封邮件只提醒一次，轮询重复命中不会弹第二遍。
    email_message_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    # IMAP UID：转成任务时要回读原文抽取会议时间，按 UID 取比按 Message-ID 搜索可靠。
    email_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="notifications")
