"""One-shot PostgreSQL schema fixes for `tasks` (create_all does not alter existing tables)."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

# ── Add columns that may be missing ──────────────────────────────────────────

_ADD_USER_ID = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS "
    "user_id UUID REFERENCES users(id) ON DELETE CASCADE"
)
_ADD_TITLE = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS title VARCHAR(500)"
)
_ADD_DESCRIPTION = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS description TEXT"
)
_ADD_STATUS = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS status VARCHAR(32) DEFAULT 'not_started'"
)
_SET_STATUS_DEFAULT_LOWERCASE = text(
    "ALTER TABLE tasks ALTER COLUMN status SET DEFAULT 'not_started'"
)
_ADD_PROJECT = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project VARCHAR(200)"
)
_ADD_ASSIGNED_AGENT = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assigned_agent VARCHAR(100)"
)
_ADD_CREATED_AT = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS "
    "created_at TIMESTAMP WITH TIME ZONE DEFAULT now()"
)
_ADD_PLAN_ID = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_id VARCHAR(100)"
)
_ADD_PRIORITY = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority VARCHAR(32) DEFAULT 'medium'"
)
_ADD_DEPENDENCIES = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS dependencies JSONB DEFAULT '[]'::jsonb"
)
_ADD_DUE_AT = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS due_at TIMESTAMP WITH TIME ZONE"
)
_ADD_STARTED_AT = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS started_at TIMESTAMP WITH TIME ZONE"
)
_ADD_COMPLETED_AT = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE"
)
_ADD_PLAN_SOURCE = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_source VARCHAR(200)"
)
_ADD_SOURCE = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS source VARCHAR(200) DEFAULT 'user'"
)
_ADD_SOURCE_METADATA = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS source_metadata JSONB"
)
_ADD_LAST_TRACKED_AT = text(
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_tracked_at TIMESTAMP WITH TIME ZONE"
)
_ADD_TASK_INDEXES = (
    text("CREATE INDEX IF NOT EXISTS ix_tasks_plan_id ON tasks (plan_id)"),
    text("CREATE INDEX IF NOT EXISTS ix_tasks_due_at ON tasks (due_at)"),
)

# ── 并发幂等兜底：同一用户 + 同一标题 + 同一截止时间只能有一行 ────────────────
# due_at 可为 NULL，PostgreSQL 的唯一索引把 NULL 视为互不相等，因此用
# COALESCE 把「无截止时间」折叠成同一常量，保证这类任务同样受约束。
# source='system' 的内部任务不参与去重。
_DEDUPE_DUPLICATE_TASKS = text(
    """
    WITH ranked AS (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY user_id, title, COALESCE(due_at, 'epoch'::timestamptz)
                   ORDER BY (status <> 'not_started') DESC, created_at ASC, id ASC
               ) AS row_rank
        FROM tasks
        WHERE source <> 'system'
    )
    DELETE FROM tasks
    WHERE id IN (SELECT id FROM ranked WHERE row_rank > 1)
    """
)
_ADD_TASK_UNIQUE_INDEX = text(
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_user_title_due "
    "ON tasks (user_id, title, COALESCE(due_at, 'epoch'::timestamptz)) "
    "WHERE source <> 'system'"
)

# ── Enforce NOT NULL after backfill ──────────────────────────────────────────

_DELETE_ORPHANS = text("DELETE FROM tasks WHERE user_id IS NULL")
_SET_USER_ID_NOT_NULL = text("ALTER TABLE tasks ALTER COLUMN user_id SET NOT NULL")
_SET_TITLE_NOT_NULL = text("ALTER TABLE tasks ALTER COLUMN title SET NOT NULL")
_ADD_USER_ID_INDEX = text(
    "CREATE INDEX IF NOT EXISTS ix_tasks_user_id ON tasks (user_id)"
)

# ── Drop NOT NULL on legacy columns not used by the current ORM ──────────────
# The pre-existing table may have business_id or updated_at as NOT NULL.
_NULLIFY_LEGACY_NOTNULL = text(
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'tasks' AND column_name = 'business_id'
              AND is_nullable = 'NO'
        ) THEN
            ALTER TABLE tasks ALTER COLUMN business_id DROP NOT NULL;
        END IF;

        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'tasks' AND column_name = 'updated_at'
              AND is_nullable = 'NO'
        ) THEN
            ALTER TABLE tasks ALTER COLUMN updated_at SET DEFAULT now();
            UPDATE tasks SET updated_at = now() WHERE updated_at IS NULL;
        END IF;
    END$$;
    """
)

# ── Normalise status column ───────────────────────────────────────────────────
# If the pre-existing table stored status as a native PostgreSQL ENUM with
# uppercase values (NOT_STARTED, IN_PROGRESS, DONE), cast it to plain VARCHAR
# and lower-case the values so they match the Python enum's .value strings.
_NORMALISE_STATUS = text(
    """
    DO $$
    DECLARE
        col_type TEXT;
    BEGIN
        SELECT data_type INTO col_type
        FROM information_schema.columns
        WHERE table_name = 'tasks' AND column_name = 'status';

        IF col_type IS NOT NULL AND col_type NOT IN ('character varying', 'text') THEN
            -- Cast native ENUM to VARCHAR
            ALTER TABLE tasks ALTER COLUMN status TYPE VARCHAR(32) USING status::text;
        END IF;

        -- Lower-case any uppercase legacy values
        UPDATE tasks
        SET status = lower(status)
        WHERE status ~ '^[A-Z_]+$';
    END$$;
    """
)


async def ensure_task_unique_signature_index(connection: AsyncConnection) -> None:
    """Add the `user_id + title + due_at` unique index after removing existing duplicates.

    The index is a defence-in-depth backstop for concurrent creators; the Redis
    lock is the primary guard. Failure here must not block startup (e.g. a
    read-only role or legacy data the dedupe could not clean), so it is logged
    loudly rather than raised.
    """
    try:
        # 放在 SAVEPOINT 里：索引建失败只回滚这一小段，不会把启动时的
        # schema 事务整体污染成 aborted 状态。
        async with connection.begin_nested():
            await connection.execute(_DEDUPE_DUPLICATE_TASKS)
            await connection.execute(_ADD_TASK_UNIQUE_INDEX)
    except Exception as exc:
        logger.warning(
            "Could not create tasks unique signature index; "
            "duplicate protection falls back to the Redis lock only: %s",
            exc,
        )


async def ensure_tasks_columns(connection: AsyncConnection) -> None:
    await connection.execute(_ADD_USER_ID)
    await connection.execute(_ADD_TITLE)
    await connection.execute(_ADD_DESCRIPTION)
    await connection.execute(_ADD_STATUS)
    await connection.execute(_ADD_PROJECT)
    await connection.execute(_ADD_ASSIGNED_AGENT)
    await connection.execute(_ADD_CREATED_AT)
    await connection.execute(_ADD_PLAN_ID)
    await connection.execute(_ADD_PRIORITY)
    await connection.execute(_ADD_DEPENDENCIES)
    await connection.execute(_ADD_DUE_AT)
    await connection.execute(_ADD_STARTED_AT)
    await connection.execute(_ADD_COMPLETED_AT)
    await connection.execute(_ADD_PLAN_SOURCE)
    await connection.execute(_ADD_SOURCE)
    await connection.execute(_ADD_SOURCE_METADATA)
    await connection.execute(_ADD_LAST_TRACKED_AT)
    for stmt in _ADD_TASK_INDEXES:
        await connection.execute(stmt)
    await connection.execute(_DELETE_ORPHANS)
    await connection.execute(_SET_USER_ID_NOT_NULL)
    await connection.execute(_SET_TITLE_NOT_NULL)
    await connection.execute(_ADD_USER_ID_INDEX)
    await connection.execute(_NULLIFY_LEGACY_NOTNULL)
    await connection.execute(_NORMALISE_STATUS)
    await connection.execute(_SET_STATUS_DEFAULT_LOWERCASE)
    await ensure_task_unique_signature_index(connection)
