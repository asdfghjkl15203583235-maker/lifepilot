"""SQLAlchemy ORM models (PostgreSQL). Import this module so metadata includes all tables."""

from lib.enums import MemoryCategory, TaskStatus
from models.pending_action import PendingAction
from models.background_job import BackgroundJob
from models.background_job_run import BackgroundJobRun
from models.conversation import Conversation
from models.daily_briefing import DailyBriefing
from models.life_service_request import LifeServiceRequest
from models.task import Task
from models.user import User
from models.user_calendar_event import UserCalendarEvent
from models.user_calendar_integration import UserCalendarIntegration
from models.user_gmail_integration import UserGmailIntegration
from models.user_integration import UserIntegration
from models.user_life_preference import UserLifePreference
from models.user_meeting_invite import UserMeetingInvite
from models.user_memory import UserMemory
from models.user_notion_integration import UserNotionIntegration
from models.user_notification import UserNotification
from models.user_profile import UserProfile
from models.user_reminder import UserReminder

__all__ = [
    "BackgroundJob",
    "BackgroundJobRun",
    "Conversation",
    "DailyBriefing",
    "LifeServiceRequest",
    "MemoryCategory",
    "PendingAction",
    "Task",
    "TaskStatus",
    "User",
    "UserCalendarEvent",
    "UserCalendarIntegration",
    "UserGmailIntegration",
    "UserIntegration",
    "UserLifePreference",
    "UserMeetingInvite",
    "UserMemory",
    "UserNotionIntegration",
    "UserNotification",
    "UserProfile",
    "UserReminder",
]
