"""Authenticated conversation/thread CRUD API.

Every single-conversation operation is scoped by both user_id and the
conversation id so a user can never read or mutate another user's thread.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from config import Settings, get_settings
from db.session import get_session
from models.conversation import Conversation
from schemas.conversation import ConversationCreateRequest, ConversationResponse, ConversationUpdateRequest
from schemas.pending_action import PendingActionResponse
from services import conversation_service
from services.pending_action_service import list_open_pending_actions_for_conversations

conversations_router = APIRouter(prefix="/conversations", tags=["conversations"])


async def _pending_by_conversation(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversations: list[Conversation],
) -> dict[str, list[PendingActionResponse]]:
    """Unconfirmed HITL drafts grouped by the conversation they belong to."""
    keys: set[str] = set()
    for conversation in conversations:
        keys.add(str(conversation.id))
        if conversation.client_conversation_id:
            keys.add(conversation.client_conversation_id)
    grouped = await list_open_pending_actions_for_conversations(
        session, user_id=user_id, conversation_ids=sorted(keys),
    )
    return {
        key: [PendingActionResponse.model_validate(row) for row in rows]
        for key, rows in grouped.items()
    }


def _to_response(
    conversation: Conversation,
    drafts: dict[str, list[PendingActionResponse]],
) -> ConversationResponse:
    response = ConversationResponse.model_validate(conversation)
    merged: list[PendingActionResponse] = []
    seen: set[uuid.UUID] = set()
    for key in (str(conversation.id), conversation.client_conversation_id):
        for action in drafts.get(key, []) if key else []:
            if action.id in seen:
                continue
            seen.add(action.id)
            merged.append(action)
    response.pending_actions = merged
    return response


@conversations_router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> list[ConversationResponse]:
    conversations = list(
        await conversation_service.list_conversations_for_user(session, user_id=user_id)
    )
    drafts = await _pending_by_conversation(
        session, user_id=user_id, conversations=conversations,
    )
    return [_to_response(conversation, drafts) for conversation in conversations]


@conversations_router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreateRequest,
    settings: Settings = Depends(get_settings),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> ConversationResponse:
    if body.client_conversation_id:
        conversation = await conversation_service.upsert_conversation_by_client_id(
            session,
            user_id=user_id,
            client_conversation_id=body.client_conversation_id,
            messages=[m.model_dump() for m in body.messages],
            settings=settings,
            title=body.title,
        )
        if conversation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到用户")
        return ConversationResponse.model_validate(conversation)

    conversation = await conversation_service.create_conversation(
        session,
        user_id=user_id,
        title=body.title,
        messages=body.messages,
        settings=settings,
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到用户")
    return ConversationResponse.model_validate(conversation)


@conversations_router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> ConversationResponse:
    conversation = await conversation_service.get_conversation(
        session,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到会话")
    drafts = await _pending_by_conversation(
        session, user_id=user_id, conversations=[conversation],
    )
    return _to_response(conversation, drafts)


@conversations_router.patch("/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: uuid.UUID,
    body: ConversationUpdateRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> ConversationResponse:
    conversation = await conversation_service.update_conversation(
        session,
        user_id=user_id,
        conversation_id=conversation_id,
        title=body.title,
        messages=body.messages,
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到会话")
    return ConversationResponse.model_validate(conversation)


@conversations_router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> Response:
    deleted = await conversation_service.delete_conversation(
        session,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到会话")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
