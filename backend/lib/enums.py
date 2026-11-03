from enum import Enum


class TaskStatus(str, Enum):
    """Lifecycle state for a task."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class IntegrationProvider(str, Enum):
    """Third-party account the user may connect for Gmail, Calendar, or Notion."""

    GMAIL = "gmail"
    GOOGLE_CALENDAR = "google_calendar"
    NOTION = "notion"


class MemoryCategory(str, Enum):
    """Classification for stored user memory facts."""

    FACT = "fact"
    RULE = "rule"
    IDENTITY = "identity"
    PREFERENCE = "preference"
    GOAL = "goal"
    WORK = "work"
    EPISODIC = "episodic"


class BackgroundJobStatus(str, Enum):
    """User-visible lifecycle for a scheduled background job (matches UI copy)."""

    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"


class BackgroundJobRunOutcome(str, Enum):
    """Result of a single scheduled execution."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class NotificationCategory(str, Enum):
    """In-app notification bucket (mirrors the web app `NotificationCategory`)."""

    EMAIL = "email"
    CALENDAR = "calendar"
    AGENT = "agent"
    SYSTEM = "system"
    BACKGROUND_JOB = "background_job"
    LIFE = "life"
    REMINDER = "reminder"


class TaskPriority(str, Enum):
    """User-facing priority used by the planner and task board."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TaskProject(str, Enum):
    """Planner-assigned semantic category for a task (drives the project filter chips)."""

    WORK = "工作"
    LIFE = "生活"
    STUDY = "学习"
    OTHER = "其他"


class ReminderStatus(str, Enum):
    """Lifecycle for proactive reminders."""

    PENDING = "pending"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    CANCELLED = "cancelled"


class ReminderTriggerType(str, Enum):
    """What caused a reminder to be scheduled."""

    DEADLINE = "deadline"
    MEETING = "meeting"
    TASK_PROGRESS = "task_progress"
    SCHEDULED_RUN = "scheduled_run"
    MANUAL = "manual"


class PendingActionType(str, Enum):
    """High-risk write operations gated behind an explicit user confirm (HITL)."""

    EMAIL_SEND = "email_send"
    EMAIL_REPLY = "email_reply"
    TASK_UPDATE = "task_update"
    TASK_DELETE = "task_delete"


class PendingActionStatus(str, Enum):
    """Lifecycle of a HITL confirmation draft."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    EXPIRED = "expired"
