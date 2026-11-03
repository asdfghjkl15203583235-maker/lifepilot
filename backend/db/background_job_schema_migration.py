"""One-shot PostgreSQL column adds for `background_jobs` (create_all does not alter existing tables)."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


SCHEDULE_STARTS_ADD = text(
    "ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS "
    "schedule_starts_at TIMESTAMP WITH TIME ZONE",
)
SCHEDULE_ENDS_ADD = text(
    "ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS "
    "schedule_ends_at TIMESTAMP WITH TIME ZONE",
)
BACKFILL_STARTS = text(
    "UPDATE background_jobs SET schedule_starts_at = created_at "
    "WHERE schedule_starts_at IS NULL",
)
BACKFILL_ENDS = text(
    """
    UPDATE background_jobs
    SET schedule_ends_at = COALESCE(ended_at, created_at + INTERVAL '5 years')
    WHERE schedule_ends_at IS NULL
    """
)
SET_NOT_NULL = (
    text("ALTER TABLE background_jobs ALTER COLUMN schedule_starts_at SET NOT NULL"),
    text("ALTER TABLE background_jobs ALTER COLUMN schedule_ends_at SET NOT NULL"),
)


async def ensure_background_job_schedule_window_columns(
    connection: AsyncConnection,
) -> None:
    await connection.execute(SCHEDULE_STARTS_ADD)
    await connection.execute(SCHEDULE_ENDS_ADD)
    await connection.execute(BACKFILL_STARTS)
    await connection.execute(BACKFILL_ENDS)
    for stmt in SET_NOT_NULL:
        await connection.execute(stmt)
