"""Shared harness for evaluation scenarios.

Every scenario creates a throwaway user, runs the real graph (real LLM + real
Postgres), asserts on persisted rows, and deletes the user afterwards so the
evaluation is repeatable and never touches a real account.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from config import Settings, get_settings
from db.session import create_async_engine_from_settings
from services.agent_runner import invoke_chat_agent_state
from services.embedding_service import init_embedding_client


def require_eval_env(settings: Settings, *, memory: bool = False) -> None:
    """Skip (not fail) when the local machine cannot run a real evaluation."""
    missing: list[str] = []
    if not settings.database_url:
        missing.append("DATABASE_URL")
    if not settings.openrouter_api_key:
        missing.append("OPENROUTER_API_KEY")
    if memory and not settings.voyageai_api_key:
        missing.append("VOYAGEAI_API_KEY")
    if missing:
        import unittest

        raise unittest.SkipTest(f"评测环境缺少配置: {', '.join(missing)}")


@asynccontextmanager
async def eval_session(*, memory: bool = False) -> AsyncIterator[tuple[Settings, Any]]:
    settings = get_settings()
    require_eval_env(settings, memory=memory)
    # The evaluation bypasses the FastAPI lifespan, so initialise the VoyageAI
    # client here — otherwise memory writes fail with "client not initialised".
    init_embedding_client(settings)
    engine = create_async_engine_from_settings(settings)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            yield settings, session
    finally:
        await engine.dispose()


async def create_eval_user(session: Any) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, email, name) VALUES (:id, :email, :name)"),
        {
            "id": user_id,
            "email": f"eval+{user_id.hex[:12]}@chief-of-staff.test",
            "name": "评测用户",
        },
    )
    await session.commit()
    return user_id


async def delete_eval_user(session: Any, user_id: uuid.UUID, *, thread_ids: list[str] | None = None) -> None:
    """Remove the throwaway user (FK cascades handle tasks/reminders/memories)."""
    # The graph leaves ORM instances (reminders, tasks) in the identity map. A
    # Core DELETE bypasses that map, so a later commit would try to UPDATE rows
    # that the cascade already removed → StaleDataError. Drop the identity map
    # first, roll back any uncommitted graph-side changes, then delete.
    try:
        await session.rollback()
        session.expire_all()
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    session.expire_all()
    for thread_id in thread_ids or []:
        for table in ("checkpoint_writes", "checkpoints", "checkpoint_blobs"):
            try:
                await session.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = :thread_id"),
                    {"thread_id": thread_id},
                )
                await session.commit()
            except Exception:
                await session.rollback()


async def run_chat(
    settings: Settings,
    session: Any,
    user_id: uuid.UUID,
    content: str,
    *,
    inline_memory_extraction: bool = False,
    thread_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Run one real graph turn; returns (terminal state, final answer)."""
    thread_id = thread_id or f"eval-{uuid.uuid4()}"
    state = await invoke_chat_agent_state(
        settings=settings,
        session=session,
        user_id=user_id,
        message_payloads=[{"role": "user", "content": content}],
        model=settings.openrouter_default_model,
        inline_memory_extraction=inline_memory_extraction,
        client_conversation_id=thread_id,
    )
    await session.commit()
    answer = state.get("final_answer") or state.get("error") or ""
    return state, answer


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
