"""Memory management API for the authenticated user's long-term memory store."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from config import Settings, get_settings
from db.dependencies import verify_database_ready
from db.session import get_session
from lib.enums import MemoryCategory
from models.user_memory import UserMemory
from services.memory_service import (
    add_memory,
    deduplicate_memories,
    invalidate_chunks_cache,
    retrieve_memory,
)

memory_router = APIRouter(
    prefix="/memory",
    tags=["memory"],
    dependencies=[Depends(verify_database_ready)],
)


class MemoryCreateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    category: MemoryCategory = MemoryCategory.PREFERENCE


class MemoryResponse(BaseModel):
    id: str
    text: str
    category: str
    is_active: bool
    created_at: str | None


@memory_router.get("", response_model=list[MemoryResponse])
async def get_memories(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[MemoryResponse]:
    result = await session.execute(
        select(UserMemory)
        .where(UserMemory.user_id == user_id, UserMemory.is_active.is_(True))
        .order_by(UserMemory.created_at.desc())
        .limit(limit),
    )
    return [
        MemoryResponse(
            id=str(row.id),
            text=row.text,
            category=row.category,
            is_active=row.is_active,
            created_at=row.created_at.isoformat() if row.created_at else None,
        )
        for row in result.scalars().all()
    ]


@memory_router.post("", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
async def post_memory(
    body: MemoryCreateRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> MemoryResponse:
    row = await add_memory(session, settings, user_id, body.text, body.category)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="该内容只是记忆指令，没有可保存的事实",
        )
    return MemoryResponse(
        id=str(row.id),
        text=row.text,
        category=row.category,
        is_active=row.is_active,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


@memory_router.post("/query")
async def post_memory_query(
    body: dict[str, str],
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, list[str]]:
    query = str(body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="缺少 query 参数")
    chunks = await retrieve_memory(session, settings, user_id, query)
    return {"chunks": chunks}


@memory_router.post("/deduplicate")
async def post_deduplicate_memories(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    deactivated = await deduplicate_memories(session, user_id)
    await invalidate_chunks_cache(user_id)
    return {"deactivated": deactivated}


@memory_router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> None:
    result = await session.execute(
        select(UserMemory).where(
            UserMemory.id == memory_id,
            UserMemory.user_id == user_id,
            UserMemory.is_active.is_(True),
        ),
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到记忆")
    row.is_active = False
    await session.flush()
    await invalidate_chunks_cache(user_id)