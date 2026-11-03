"""Memory tool — retrieve and add user memories.

Given to the orchestrator agent only. Sub-agents must never import this.
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from lib.enums import MemoryCategory
from services.memory_service import add_memory, retrieve_memory

logger = logging.getLogger(__name__)


async def retrieve_memory_tool(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    query: str,
) -> list[str]:
    """Retrieve relevant memory chunks for the current conversation context."""
    chunks = await retrieve_memory(session, settings, user_id, query)
    logger.info("Retrieved %d memory chunks for user %s", len(chunks), user_id)
    return chunks


async def add_memory_tool(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    text: str,
    category: str,
) -> dict[str, str]:
    """Save a new memory fact for the user.

    Returns a confirmation dict with 'status' and 'memory_id' or error detail.
    """
    try:
        parsed_category = MemoryCategory(category)
    except ValueError:
        valid = ", ".join(c.value for c in MemoryCategory)
        return {"status": "error", "detail": f"Invalid category '{category}'. Valid: {valid}"}

    memory = await add_memory(session, settings, user_id, text, parsed_category)
    if memory is None:
        return {"status": "skipped", "detail": "该内容只是记忆指令，没有可保存的事实"}
    return {"status": "saved", "memory_id": str(memory.id)}
