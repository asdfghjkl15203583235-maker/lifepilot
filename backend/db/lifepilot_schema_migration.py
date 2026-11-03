"""One-shot PostgreSQL table/column setup for LifePilot features.

Runs after `Base.metadata.create_all` so existing deploys without the new
tables get them without requiring a separate migration system.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_CREATE_MEETING_INVITES = text(
    """
    CREATE TABLE IF NOT EXISTS user_meeting_invites (
        id UUID PRIMARY KEY,
        user_id UUID REFERENCES users(id) ON DELETE CASCADE,
        source VARCHAR(64) NOT NULL DEFAULT 'manual',
        external_id VARCHAR(512),
        title VARCHAR(500) NOT NULL,
        description TEXT,
        starts_at TIMESTAMPTZ NOT NULL,
        ends_at TIMESTAMPTZ,
        meeting_number VARCHAR(128),
        join_url VARCHAR(1024),
        timezone_hint VARCHAR(128),
        found_in_email BOOLEAN NOT NULL DEFAULT FALSE,
        email_message_id VARCHAR(512),
        created_at TIMESTAMPTZ DEFAULT now(),
        reminder_sent_at TIMESTAMPTZ,
        extra JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """
)
_CREATE_MEETING_INVITE_INDEXES = (
    text("CREATE INDEX IF NOT EXISTS ix_user_meeting_invites_user_id ON user_meeting_invites (user_id)"),
    text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_meeting_invites_external_id "
        "ON user_meeting_invites (external_id)"
    ),
)
_CREATE_REMINDERS = text(
    """
    CREATE TABLE IF NOT EXISTS user_reminders (
        id UUID PRIMARY KEY,
        user_id UUID REFERENCES users(id) ON DELETE CASCADE,
        task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
        title VARCHAR(240) NOT NULL,
        note TEXT,
        remind_at TIMESTAMPTZ NOT NULL,
        trigger_type VARCHAR(32) NOT NULL DEFAULT 'manual',
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        sent_at TIMESTAMPTZ,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ DEFAULT now()
    )
    """
)
_CREATE_REMINDER_INDEXES = (
    text("CREATE INDEX IF NOT EXISTS ix_user_reminders_user_id ON user_reminders (user_id)"),
    text("CREATE INDEX IF NOT EXISTS ix_user_reminders_remind_at ON user_reminders (remind_at)"),
    text("CREATE INDEX IF NOT EXISTS ix_user_reminders_task_id ON user_reminders (task_id)"),
)
_CREATE_LIFE_PREFERENCES = text(
    """
    CREATE TABLE IF NOT EXISTS user_life_preferences (
        id UUID PRIMARY KEY,
        user_id UUID REFERENCES users(id) ON DELETE CASCADE,
        habits JSONB NOT NULL DEFAULT '[]'::jsonb,
        time_preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
        work_hours JSONB NOT NULL DEFAULT '{}'::jsonb,
        priority_rules JSONB NOT NULL DEFAULT '[]'::jsonb,
        raw_notes TEXT,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """
)
_CREATE_LIFE_PREFERENCE_INDEXES = (
    text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_life_preferences_user_id "
        "ON user_life_preferences (user_id)"
    ),
    text("CREATE INDEX IF NOT EXISTS ix_user_life_preferences_user_id ON user_life_preferences (user_id)"),
)
_CREATE_LIFE_SERVICE_REQUESTS = text(
    """
    CREATE TABLE IF NOT EXISTS life_service_requests (
        id UUID PRIMARY KEY,
        user_id UUID REFERENCES users(id) ON DELETE CASCADE,
        service_name VARCHAR(64) NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        result JSONB,
        idempotency_key VARCHAR(512),
        is_processed BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ DEFAULT now(),
        processed_at TIMESTAMPTZ
    )
    """
)
_CREATE_LIFE_SERVICE_REQUEST_INDEXES = (
    text("CREATE INDEX IF NOT EXISTS ix_life_service_requests_user_id ON life_service_requests (user_id)"),
    text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_life_service_requests_idempotency_key "
        "ON life_service_requests (idempotency_key)"
    ),
)
_CREATE_CALENDAR_EVENTS = text(
    """
    CREATE TABLE IF NOT EXISTS user_calendar_events (
        id UUID PRIMARY KEY,
        user_id UUID REFERENCES users(id) ON DELETE CASCADE,
        uid VARCHAR(512) NOT NULL,
        source_file VARCHAR(512),
        title VARCHAR(500) NOT NULL,
        description TEXT,
        location VARCHAR(500),
        starts_at TIMESTAMPTZ NOT NULL,
        ends_at TIMESTAMPTZ,
        all_day BOOLEAN NOT NULL DEFAULT FALSE,
        extra JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """
)
_CREATE_CALENDAR_EVENT_INDEXES = (
    text("CREATE INDEX IF NOT EXISTS ix_user_calendar_events_user_id ON user_calendar_events (user_id)"),
    text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_calendar_events_user_uid "
        "ON user_calendar_events (user_id, uid)"
    ),
)

# The ORM stores enum columns with values_callable (lowercase .value). Any rows
# written by older code used the SQLAlchemy default enum *name* (uppercase), so
# normalize those columns when the startup migration runs.
_NORMALIZE_ENUM_VALUES = text(
    """
    DO $$
    DECLARE
        rec RECORD;
    BEGIN
        FOR rec IN SELECT table_name, column_name, enum_values
                   FROM (
                       VALUES
                           ('tasks', 'status', 'not_started, in_progress, done'),
                           ('tasks', 'priority', 'low, medium, high, urgent'),
                           ('user_reminders', 'trigger_type', 'deadline, meeting, task_progress, scheduled_run, manual'),
                           ('user_reminders', 'status', 'pending, sent, acknowledged, cancelled'),
                           ('background_jobs', 'status', 'active, paused, ended'),
                           ('background_job_runs', 'outcome', 'success, failed, skipped'),
                           ('user_notifications', 'category', 'email, calendar, agent, system, background_job, life, reminder'),
                           ('user_integrations', 'provider', 'gmail, google_calendar, notion')
                   ) AS t(table_name, column_name, enum_values)
        LOOP
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = rec.table_name
                  AND column_name = rec.column_name
                  AND data_type IN ('character varying', 'text')
            ) THEN
                EXECUTE format(
                    'UPDATE %I SET %I = lower(%I) WHERE %I ~ ''^[A-Z_]+$''',
                    rec.table_name,
                    rec.column_name,
                    rec.column_name,
                    rec.column_name
                );
            END IF;
        END LOOP;
    END$$;
    """
)


# One-shot schema evolution for tables owned by SQLAlchemy metadata (create_all
# already ran, so new columns need an explicit idempotent ALTER TABLE).
_ADD_CONVERSATION_CLIENT_ID = text(
    """
    ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS client_conversation_id VARCHAR(128)
    """
)
_INDEX_CONVERSATION_CLIENT_ID = text(
    "CREATE INDEX IF NOT EXISTS ix_conversations_client_conversation_id "
    "ON conversations (client_conversation_id)"
)
_CREATE_DAILY_BRIEFINGS = text(
    """
    CREATE TABLE IF NOT EXISTS daily_briefings (
        id UUID PRIMARY KEY,
        user_id UUID REFERENCES users(id) ON DELETE CASCADE,
        briefing_date DATE NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """
)
_CREATE_DAILY_BRIEFING_INDEXES = (
    text("CREATE INDEX IF NOT EXISTS ix_daily_briefings_user_id ON daily_briefings (user_id)"),
    text("CREATE INDEX IF NOT EXISTS ix_daily_briefings_briefing_date ON daily_briefings (briefing_date)"),
    text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_briefings_user_date "
        "ON daily_briefings (user_id, briefing_date)"
    ),
)


# One-shot schema evolution for `user_notifications`: link reminder-origin alerts
# back to their `user_reminders` row so the web toast can snooze / act on them.
_ADD_NOTIFICATION_REMINDER_ID = text(
    """
    ALTER TABLE user_notifications
    ADD COLUMN IF NOT EXISTS reminder_id UUID
    REFERENCES user_reminders(id) ON DELETE SET NULL
    """
)
_ADD_NOTIFICATION_TASK_ID = text(
    """
    ALTER TABLE user_notifications
    ADD COLUMN IF NOT EXISTS task_id UUID
    REFERENCES tasks(id) ON DELETE SET NULL
    """
)
_INDEX_NOTIFICATION_REMINDER_ID = text(
    "CREATE INDEX IF NOT EXISTS ix_user_notifications_reminder_id "
    "ON user_notifications (reminder_id, user_id)"
)
# 邮件类通知按 Message-ID 幂等：后台每 5 分钟轮询一次收件箱，
# 没有这个唯一索引时同一封邮件会反复弹窗。
_ADD_NOTIFICATION_EMAIL_MESSAGE_ID = text(
    """
    ALTER TABLE user_notifications
    ADD COLUMN IF NOT EXISTS email_message_id VARCHAR(512)
    """
)
_INDEX_NOTIFICATION_EMAIL_MESSAGE_ID = text(
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_notifications_email_message_id "
    "ON user_notifications (user_id, email_message_id)"
)
# 转任务时要按 UID 回读邮件原文，才能把会议时间提取成截止时间。
_ADD_NOTIFICATION_EMAIL_UID = text(
    """
    ALTER TABLE user_notifications
    ADD COLUMN IF NOT EXISTS email_uid VARCHAR(64)
    """
)


async def ensure_lifepilot_schema(connection: AsyncConnection) -> None:
    await connection.execute(_ADD_NOTIFICATION_REMINDER_ID)
    await connection.execute(_ADD_NOTIFICATION_TASK_ID)
    await connection.execute(_INDEX_NOTIFICATION_REMINDER_ID)
    await connection.execute(_ADD_NOTIFICATION_EMAIL_MESSAGE_ID)
    await connection.execute(_INDEX_NOTIFICATION_EMAIL_MESSAGE_ID)
    await connection.execute(_ADD_NOTIFICATION_EMAIL_UID)
    await connection.execute(_CREATE_MEETING_INVITES)
    await connection.execute(_CREATE_DAILY_BRIEFINGS)
    for stmt in _CREATE_DAILY_BRIEFING_INDEXES:
        await connection.execute(stmt)
    await connection.execute(_ADD_CONVERSATION_CLIENT_ID)
    await connection.execute(_INDEX_CONVERSATION_CLIENT_ID)
    for stmt in _CREATE_MEETING_INVITE_INDEXES:
        await connection.execute(stmt)
    await connection.execute(_CREATE_REMINDERS)
    for stmt in _CREATE_REMINDER_INDEXES:
        await connection.execute(stmt)
    await connection.execute(_CREATE_LIFE_PREFERENCES)
    for stmt in _CREATE_LIFE_PREFERENCE_INDEXES:
        await connection.execute(stmt)
    await connection.execute(_CREATE_LIFE_SERVICE_REQUESTS)
    for stmt in _CREATE_LIFE_SERVICE_REQUEST_INDEXES:
        await connection.execute(stmt)
    await connection.execute(_CREATE_CALENDAR_EVENTS)
    for stmt in _CREATE_CALENDAR_EVENT_INDEXES:
        await connection.execute(stmt)
    await connection.execute(_NORMALIZE_ENUM_VALUES)