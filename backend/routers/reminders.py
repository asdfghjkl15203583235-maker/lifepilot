"""Reminder API for the authenticated user."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from db.dependencies import verify_database_ready
from db.session import get_session
from schemas.reminder import (
    ReminderAckRequest,
    ReminderCreateRequest,
    ReminderResponse,
    ReminderSnoozeRequest,
)
from services.reminder_service import (
    ReminderNotFoundError,
    ack_reminder,
    create_reminder,
    list_reminders_for_user,
    snooze_reminder,
)

reminders_router = APIRouter(
    prefix="/reminders",
    tags=["reminders"],
    dependencies=[Depends(verify_database_ready)],
)


@reminders_router.get("", response_model=list[ReminderResponse])
async def get_reminders(
    include_sent: bool = Query(default=False),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> list[ReminderResponse]:
    rows = await list_reminders_for_user(session, user_id, include_sent=include_sent)
    return [ReminderResponse.model_validate(r) for r in rows]


@reminders_router.post("", response_model=ReminderResponse, status_code=status.HTTP_201_CREATED)
async def post_create_reminder(
    body: ReminderCreateRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> ReminderResponse:
    row = await create_reminder(session, user_id, body)
    return ReminderResponse.model_validate(row)


@reminders_router.post("/{reminder_id}/snooze", response_model=ReminderResponse)
async def post_snooze_reminder(
    reminder_id: uuid.UUID,
    body: ReminderSnoozeRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> ReminderResponse:
    try:
        row = await snooze_reminder(session, user_id, reminder_id, minutes=body.minutes)
    except ReminderNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到提醒")
    return ReminderResponse.model_validate(row)


@reminders_router.post("/{reminder_id}/ack", response_model=ReminderResponse)
async def post_ack_reminder(
    reminder_id: uuid.UUID,
    body: ReminderAckRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> ReminderResponse:
    try:
        row = await ack_reminder(session, user_id, reminder_id)
    except ReminderNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ReminderResponse.model_validate(row)