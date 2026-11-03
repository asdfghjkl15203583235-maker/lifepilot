import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.conversation import Conversation
from schemas.chat import ChatMessage
from schemas.conversation import ConversationSaveRequest
from services.user_service import get_user_by_id


def _messages_to_jsonable(messages: Sequence[ChatMessage]) -> list[dict[str, str]]:
    # exclude_none keeps optional fields (timestamp) from leaking "timestamp": null
    # into the persisted JSONB, so the dict stays identical for callers that omit it.
    return [message.model_dump(exclude_none=True) for message in messages]


def _first_user_text_from_messages(messages: Sequence[ChatMessage]) -> str:
    for message in messages:
        if message.role == "user" and message.content.strip():
            return message.content.strip()
    return ""


async def create_conversation(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    title: str | None,
    messages: list[ChatMessage],
    settings: Any = None,
) -> Conversation | None:
    """Create a conversation owned by *user_id*.

    When *title* is missing or still a placeholder, derives a real title from the
    first user message (LLM-concise with a truncation fallback).
    """
    owner = await get_user_by_id(session, user_id)
    if owner is None:
        return None

    from services.conversation_title_service import is_placeholder_title, truncate_title_from_text

    first_user_text = _first_user_text_from_messages(messages)
    resolved_title: str | None = None
    if title is not None and not is_placeholder_title(title):
        resolved_title = title.strip()
    else:
        truncated = truncate_title_from_text(first_user_text)
        if truncated:
            resolved_title = truncated

    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=user_id,
        title=resolved_title,
        messages=_messages_to_jsonable(messages),
    )
    session.add(conversation)
    await session.flush()
    await session.refresh(conversation)
    return conversation


async def _conversation_by_client_id_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    client_conversation_id: str,
) -> Conversation | None:
    if not client_conversation_id:
        return None
    result = await session.execute(
        select(Conversation).where(
            Conversation.client_conversation_id == client_conversation_id,
            Conversation.user_id == user_id,
        ),
    )
    row = result.scalar_one_or_none()
    if row is not None:
        return row
    try:
        id_as_uuid = uuid.UUID(client_conversation_id)
    except (ValueError, AttributeError, TypeError):
        return None
    return await session.get(Conversation, id_as_uuid)


async def _first_user_text(messages: Sequence[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"].strip()
    return ""


async def upsert_conversation_by_client_id(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    client_conversation_id: str,
    messages: list[dict[str, Any]],
    settings: Any = None,
    title: str | None = None,
) -> Conversation | None:
    """Idempotently persist a chat transcript keyed by the client-generated id.

    Uses the client tab id (which the frontend later PUTs/PATCHes against) as the
    primary key when it is a valid UUID, so the SSE stream and the frontend both
    write to the same row. Falls back to a fresh UUID when the client id is a
    non-UUID placeholder (e.g. the initial "conv-initial" tab).
    """
    from services.conversation_title_service import is_placeholder_title, truncate_title_from_text

    messages = [m for m in messages if m.get("content")]
    jsonable = _messages_to_jsonable([ChatMessage(role=m["role"], content=m["content"]) for m in messages])
    if not jsonable:
        return None

    owner = await get_user_by_id(session, user_id)
    if owner is None:
        return None

    id_as_uuid: uuid.UUID | None = None
    try:
        id_as_uuid = uuid.UUID(client_conversation_id)
    except (ValueError, AttributeError, TypeError):
        id_as_uuid = None

    existing: Conversation | None = None
    if id_as_uuid is not None:
        existing = await session.get(Conversation, id_as_uuid)
        if existing is not None and existing.user_id != user_id:
            # id collision with another user's row — fall back to a fresh row
            id_as_uuid = None
            existing = None
    if existing is None:
        result = await session.execute(
            select(Conversation).where(
                Conversation.client_conversation_id == client_conversation_id,
                Conversation.user_id == user_id,
            ),
        )
        existing = result.scalar_one_or_none()

    first_user_text = await _first_user_text(jsonable)
    resolved_title: str | None = None
    if title is not None and not is_placeholder_title(title):
        resolved_title = title.strip()
    elif first_user_text:
        truncated = truncate_title_from_text(first_user_text)
        if truncated:
            resolved_title = truncated

    if existing is not None:
        existing.messages = jsonable
        existing.client_conversation_id = client_conversation_id
        # Title is fixed from the first message; never clobber it on subsequent
        # upserts. Only explicit user rename (save/update_conversation) changes it.
        await session.flush()
        await session.refresh(existing)
        return existing

    conversation = Conversation(
        id=id_as_uuid or uuid.uuid4(),
        user_id=user_id,
        title=resolved_title,
        messages=jsonable,
        client_conversation_id=client_conversation_id,
    )
    session.add(conversation)
    await session.flush()
    await session.refresh(conversation)
    return conversation


async def save_conversation(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    body: ConversationSaveRequest,
) -> Conversation | None:
    """Insert a new conversation or update an existing one for this user."""
    owner = await get_user_by_id(session, user_id)
    if owner is None:
        return None

    payload = _messages_to_jsonable(body.messages)
    new_id = conversation_id or uuid.uuid4()

    if conversation_id is not None:
        existing = await get_conversation(session, user_id=user_id, conversation_id=conversation_id)
        if existing is not None:
            existing.messages = payload
            if body.title is not None:
                existing.title = body.title.strip() or None
            await session.flush()
            await session.refresh(existing)
            return existing

        row_with_same_id = await session.get(Conversation, conversation_id)
        if row_with_same_id is not None:
            # ids are global; another user already owns this conversation id
            return None

    title = body.title.strip() if body.title else None
    conversation = Conversation(
        id=new_id,
        user_id=user_id,
        title=title,
        messages=payload,
    )
    session.add(conversation)
    await session.flush()
    await session.refresh(conversation)
    return conversation


async def get_conversation(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> Conversation | None:
    """Return a single conversation if it belongs to the user."""
    result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        ),
    )
    return result.scalar_one_or_none()


async def update_conversation(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    title: str | None,
    messages: list[ChatMessage] | None,
) -> Conversation | None:
    """Update title/messages only after ownership is verified."""
    conversation = await get_conversation(session, user_id=user_id, conversation_id=conversation_id)
    if conversation is None:
        return None
    if messages is not None:
        conversation.messages = _messages_to_jsonable(messages)
    if title is not None:
        conversation.title = title.strip() or None
    await session.flush()
    await session.refresh(conversation)
    return conversation


async def delete_conversation(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> bool:
    """Delete a conversation only if it belongs to *user_id*."""
    conversation = await get_conversation(session, user_id=user_id, conversation_id=conversation_id)
    if conversation is None:
        return False
    await session.delete(conversation)
    await session.flush()
    return True


async def list_conversations_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> Sequence[Conversation]:
    """List all conversations for a user, most recently updated first."""
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc()),
    )
    return result.scalars().all()
