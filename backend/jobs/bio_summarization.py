"""Daily background job — update user bios and deduplicate memory facts.

Runs once per day via APScheduler. For each user with recent conversations:
1. Extract new facts from the past 24 hours of conversations.
2. Diff against the existing bio and regenerate via LLM only if changed.
3. Cluster-deduplicate semantically similar memory rows.
4. Invalidate Redis bio cache so the next session picks up the new bio.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Sequence

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, chat_llm
from models.conversation import Conversation
from models.user import User
from models.user_profile import UserProfile
from services.memory_service import deduplicate_memories, invalidate_bio_cache

logger = logging.getLogger(__name__)

LOOKBACK_HOURS = 24
MAX_EXCERPT_CHARS = 4_000

BIO_SYSTEM_PROMPT = (
    "You are a memory summarization agent. Given a user's existing bio "
    "and new conversation excerpts, return an updated bio that is 150–250 words. "
    "Only include stable, meaningful facts. Do not include one-off statements. "
    "Do not repeat what is already accurately captured in the existing bio."
)


async def run_bio_summarization(session: AsyncSession, settings: Settings) -> None:
    """Entry point called by the scheduler — processes every user."""
    llm = chat_llm(settings)

    result = await session.execute(select(User.id))
    user_ids: list[uuid.UUID] = [row[0] for row in result.all()]

    logger.info("Bio summarization job started — %d user(s) to process", len(user_ids))

    for user_id in user_ids:
        try:
            await _process_single_user(session, settings, llm, user_id)
        except Exception:
            logger.exception("Bio summarization failed for user %s", user_id)

    logger.info("Bio summarization job complete")


async def _process_single_user(
    session: AsyncSession,
    settings: Settings,
    llm: ChatOpenAI,
    user_id: uuid.UUID,
) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    conversations = await _recent_conversations(session, user_id, cutoff)
    if not conversations:
        return

    excerpts = _extract_excerpts(conversations)
    if not excerpts:
        return

    profile = await _get_or_create_profile(session, user_id)
    current_bio = profile.bio or ""

    new_bio = await _generate_updated_bio(llm, current_bio, excerpts)
    has_meaningful_change = (
        new_bio.strip() and new_bio.strip() != current_bio.strip()
    )

    if has_meaningful_change:
        profile.bio = new_bio.strip()
        profile.bio_updated_at = datetime.now(timezone.utc)
        await session.flush()
        logger.info("Updated bio for user %s", user_id)

    dedup_count = await deduplicate_memories(session, user_id)
    if dedup_count:
        logger.info("Deduplicated %d memories for user %s", dedup_count, user_id)

    await invalidate_bio_cache(user_id)


# ── Internal helpers ──────────────────────────────────────────────────────


async def _recent_conversations(
    session: AsyncSession,
    user_id: uuid.UUID,
    since: datetime,
) -> Sequence[Conversation]:
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id, Conversation.updated_at >= since),
    )
    return result.scalars().all()


def _extract_excerpts(conversations: Sequence[Conversation]) -> str:
    """Build a single string of recent user/assistant turns, respecting size limits."""
    parts: list[str] = []
    total = 0
    for conversation in conversations:
        for message in conversation.messages or []:
            role = message.get("role", "")
            content = message.get("content", "")
            if role not in ("user", "assistant"):
                continue
            line = f"{role}: {content}"
            if total + len(line) > MAX_EXCERPT_CHARS:
                return "\n".join(parts)
            parts.append(line)
            total += len(line)
    return "\n".join(parts)


async def _generate_updated_bio(
    llm: ChatOpenAI,
    current_bio: str,
    excerpts: str,
) -> str:
    user_payload = (
        f"[EXISTING BIO]\n{current_bio or 'No existing bio.'}\n\n"
        f"[NEW CONVERSATION EXCERPTS]\n{excerpts}"
    )
    response = await llm.ainvoke([
        SystemMessage(content=BIO_SYSTEM_PROMPT),
        HumanMessage(content=user_payload),
    ])
    return response.content if isinstance(response.content, str) else ""


async def _get_or_create_profile(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> UserProfile:
    result = await session.execute(
        select(UserProfile).where(UserProfile.user_id == user_id),
    )
    profile = result.scalar_one_or_none()
    if profile is not None:
        return profile

    profile = UserProfile(user_id=user_id)
    session.add(profile)
    await session.flush()
    await session.refresh(profile)
    return profile
