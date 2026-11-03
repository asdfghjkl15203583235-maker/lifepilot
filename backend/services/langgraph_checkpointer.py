"""Async Postgres LangGraph checkpointer lifecycle.

The checkpointer stores graph thread state in Postgres so a conversation's
intermediate planner/tool state can be resumed across requests and restarts.

Windows limitation: psycopg's async driver requires a SelectorEventLoop, while
Windows defaults to the ProactorEventLoop. Start the backend with `python run.py`,
which switches the policy before the loop is created. If a Proactor loop is still
in use, or the DB is down, the process degrades to an in-memory checkpointer so
the API still starts. Durable conversation history is independently persisted in
the `conversations` table by the conversations API.

On Linux (production), `LANGGRAPH_CHECKPOINTER=postgres` uses the durable pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from langgraph.checkpoint.memory import InMemorySaver

from config import Settings

logger = logging.getLogger(__name__)

# psycopg uses a libpq conninfo; strip the SQLAlchemy dialect prefix (+asyncpg).
def _psycopg_dsn(database_url: str | None) -> str | None:
    if not database_url:
        return None
    base = database_url
    if "+" in database_url.split("://", 1)[0]:
        scheme, rest = database_url.split("://", 1)
        base = f"{scheme.split('+', 1)[0]}://{rest}"
    parsed = urlparse(base)
    if not parsed.scheme or not parsed.hostname:
        return base
    port = f":{parsed.port}" if parsed.port else ""
    userinfo = ""
    if parsed.username:
        userinfo = unquote(parsed.username)
        if parsed.password:
            userinfo += f":{unquote(parsed.password)}"
        userinfo += "@"
    path = parsed.path or "/postgres"
    params = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{userinfo}{parsed.hostname}{port}{path}{params}"


@dataclass
class _CheckpointerHolder:
    pool: Any | None = None
    checkpointer: Any | None = None
    memory_checkpointer: InMemorySaver | None = None
    _postgres_attempted: bool = False


_holder = _CheckpointerHolder()
_setup_lock: asyncio.Lock | None = None


def checkpointer_enabled(settings: Settings) -> bool:
    return settings.langgraph_checkpointer == "postgres" and bool(settings.database_url)


def _is_proactor_loop() -> bool:
    """True when the running loop is a Windows Proactor loop (psycopg can't use it)."""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return False
    return type(running).__name__ == "ProactorEventLoop"


async def setup_postgres_checkpointer(settings: Settings) -> None:
    """Create the AsyncPostgresSaver and its tables once per process.

    If the Postgres saver cannot be created (unsupported event loop, DB down),
    the process falls back to an in-memory saver and logs a warning.
    """
    global _setup_lock
    if not checkpointer_enabled(settings):
        return

    if _holder.checkpointer is not None or _holder._postgres_attempted:
        return

    if _setup_lock is None:
        _setup_lock = asyncio.Lock()

    async with _setup_lock:
        if _holder.checkpointer is not None or _holder._postgres_attempted:
            return

        pool: Any = None
        try:
            dsn = _psycopg_dsn(settings.database_url)
            if dsn is None:
                raise RuntimeError("DATABASE_URL is not configured")

            # psycopg async needs the SelectorEventLoop; on Windows uvicorn only
            # falls back here if the launcher (run.py) did not switch the policy.
            if sys.platform == "win32" and _is_proactor_loop():
                raise RuntimeError(
                    "async psycopg requires WindowsSelectorEventLoopPolicy on Windows; "
                    "start the backend with `python run.py` so the policy is applied "
                    "before the event loop is created"
                )

            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg_pool import AsyncConnectionPool

            kwargs: dict[str, Any] = {
                "conninfo": dsn,
                "max_size": settings.database_pool_size,
                "min_size": 1,
                "open": False,
                "timeout": 5,
                "kwargs": {
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": None,
                    "connect_timeout": 5,
                },
            }
            pool = AsyncConnectionPool(**kwargs)
            await pool.open()
            checkpointer = AsyncPostgresSaver(conn=pool)
            await checkpointer.setup()

            _holder.pool = pool
            _holder.checkpointer = checkpointer
            _holder._postgres_attempted = True
            logger.info("LangGraph Postgres checkpointer initialised")
        except Exception as exc:
            if pool is not None:
                with contextlib.suppress(Exception):
                    await pool.close()
            _holder._postgres_attempted = True
            _holder.checkpointer = None
            logger.warning(
                "Postgres checkpointer unavailable (%s: %s); falling back to in-memory. "
                "Session history stays durable via the conversations table.",
                type(exc).__name__,
                exc,
            )


def get_memory_checkpointer() -> InMemorySaver:
    """Return one process-wide in-memory saver for thread state recovery."""
    if _holder.memory_checkpointer is None:
        _holder.memory_checkpointer = InMemorySaver()
    return _holder.memory_checkpointer


async def get_postgres_checkpointer(settings: Settings) -> Any | None:
    """Return the durable Postgres saver, or None when unavailable."""
    if _holder.checkpointer is None and not _holder._postgres_attempted:
        await setup_postgres_checkpointer(settings)
    return _holder.checkpointer


async def close_postgres_checkpointer() -> None:
    """Close the checkpointer pool during app shutdown."""
    if _holder.pool is not None:
        with contextlib.suppress(Exception):
            await _holder.pool.close()
    _holder.pool = None
    _holder.checkpointer = None