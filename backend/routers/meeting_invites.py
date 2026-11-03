"""Meeting invite API: extract, persist, and auto-schedule reminders."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from config import Settings, get_settings
from db.dependencies import verify_database_ready
from db.session import get_session
from models.user_meeting_invite import UserMeetingInvite
from schemas.meeting_invite import (
    MeetingInviteCreateRequest,
    MeetingInviteExtractRequest,
    MeetingInviteFromEmailRequest,
    MeetingInviteResponse,
    MeetingInviteUpsertResponse,
)
from services.meeting_invite_extraction import (
    extract_meeting_invite,
    extract_meeting_invite_with_llm,
)
from services.meeting_invite_service import (
    delete_meeting_invite_for_user,
    get_meeting_invite_for_user,
    list_meeting_invites_for_user,
    save_extracted_meeting_invite,
    upsert_meeting_invite,
)

logger = logging.getLogger(__name__)

meeting_invites_router = APIRouter(
    prefix="/meeting-invites",
    tags=["meeting-invites"],
    dependencies=[Depends(verify_database_ready)],
)


@meeting_invites_router.get("", response_model=list[MeetingInviteResponse])
async def get_meeting_invites(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    upcoming_only: bool = False,
) -> list[MeetingInviteResponse]:
    rows = await list_meeting_invites_for_user(
        session,
        user_id,
        upcoming_only=upcoming_only,
        limit=100,
    )
    return [MeetingInviteResponse.model_validate(r) for r in rows]


@meeting_invites_router.post("", response_model=MeetingInviteUpsertResponse, status_code=status.HTTP_201_CREATED)
async def post_create_meeting_invite(
    body: MeetingInviteCreateRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> MeetingInviteUpsertResponse:
    row, created = await upsert_meeting_invite(
        session,
        user_id,
        body,
        found_in_email=False,
        email_message_id=None,
    )
    return MeetingInviteUpsertResponse(
        invite=MeetingInviteResponse.model_validate(row),
        created=created,
    )


@meeting_invites_router.post("/extract", response_model=MeetingInviteCreateRequest)
async def post_extract_meeting_invite(
    body: MeetingInviteExtractRequest,
    settings: Settings = Depends(get_settings),
) -> MeetingInviteCreateRequest:
    base = extract_meeting_invite(body.text, source=body.source)
    if body.use_llm and settings.openrouter_api_key:
        try:
            from config import chat_llm

            base = await extract_meeting_invite_with_llm(body.text, body.source, chat_llm(settings))
        except Exception as exc:
            logger.warning("LLM extraction failed, using deterministic result: %s", exc)
    return MeetingInviteCreateRequest(
        title=str(base.get("title") or "已提取的会议"),
        description=str(base.get("description") or ""),
        starts_at=base.get("starts_at"),
        ends_at=base.get("ends_at"),
        meeting_number=base.get("meeting_number"),
        join_url=base.get("join_url"),
        source=body.source,
        extra=base.get("extra") or {},
    )


@meeting_invites_router.post(
    "/from-email",
    response_model=MeetingInviteUpsertResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_meeting_invite_from_email(
    body: MeetingInviteFromEmailRequest,
    settings: Settings = Depends(get_settings),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> MeetingInviteUpsertResponse:
    extracted = extract_meeting_invite(body.text, source="email")
    if settings.openrouter_api_key:
        try:
            from config import chat_llm

            extracted = await extract_meeting_invite_with_llm(body.text, "email", chat_llm(settings))
        except Exception as exc:
            logger.warning("LLM email extraction failed, using deterministic result: %s", exc)
    create_body = MeetingInviteCreateRequest(
        title=str(extracted.get("title") or "从邮件提取的会议"),
        description=str(extracted.get("description") or ""),
        starts_at=extracted.get("starts_at"),
        ends_at=extracted.get("ends_at"),
        meeting_number=extracted.get("meeting_number"),
        join_url=extracted.get("join_url"),
        source="email",
        external_id=body.external_id or body.message_id,
        reminder_minutes_before=body.reminder_minutes_before,
        extra=extracted.get("extra") or {},
    )
    if create_body.starts_at is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="无法从邮件中提取会议开始时间",
        )
    row, created = await upsert_meeting_invite(
        session,
        user_id,
        create_body,
        found_in_email=True,
        email_message_id=body.message_id,
    )
    return MeetingInviteUpsertResponse(
        invite=MeetingInviteResponse.model_validate(row),
        created=created,
    )


@meeting_invites_router.post("/scan-inbox")
async def post_scan_inbox_for_meeting_invites(
    settings: Settings = Depends(get_settings),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Scan recent inbox mail and create tasks for meeting invites.

    The scheduler runs the same service on an interval; this endpoint is the
    on-demand path (and what the UI/tests can call).
    """
    from services.agent_runner import build_mail163_config
    from services.meeting_invite_scanner import scan_mail163_inbox_for_meeting_invites

    config = build_mail163_config(settings)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="未配置 163 邮箱，无法扫描会议邀请",
        )
    result = await scan_mail163_inbox_for_meeting_invites(
        session,
        user_id=user_id,
        config=config,
        settings=settings,
    )
    await session.commit()
    return result.as_dict()


@meeting_invites_router.get("/{invite_id}", response_model=MeetingInviteResponse)
async def get_meeting_invite(
    invite_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> MeetingInviteResponse:
    row = await get_meeting_invite_for_user(session, user_id, invite_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到会议邀请")
    return MeetingInviteResponse.model_validate(row)


@meeting_invites_router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting_invite(
    invite_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> None:
    deleted = await delete_meeting_invite_for_user(session, user_id, invite_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到会议邀请")