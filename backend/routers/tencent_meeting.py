"""Tencent Meeting REST API adapter with optional idempotent create."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from auth.dependencies import get_current_user_id
from config import Settings, get_settings
from schemas.tencent_meeting import (
    TencentMeetingCreateRequest,
    TencentMeetingUpdateRequest,
)
from services.idempotency_service import check_idempotent, record_idempotent
from services.tencent_meeting_client import (
    TencentMeetingApiError,
    TencentMeetingConfigError,
    build_tencent_meeting_client,
)

logger = logging.getLogger(__name__)

tencent_meeting_router = APIRouter(
    prefix="/tencent-meeting",
    tags=["tencent-meeting"],
)


class IdempotentMeetingCreateRequest(TencentMeetingCreateRequest):
    idempotency_key: str | None = None


class MeetingPayloadResponse(BaseModel):
    data: dict[str, Any]
    idempotency_key: str | None = None


def _require_client(settings: Settings):
    try:
        return build_tencent_meeting_client(settings)
    except TencentMeetingConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


def _handle_provider_error(exc: Exception) -> None:
    if isinstance(exc, TencentMeetingApiError):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    logger.warning("Tencent Meeting request failed: %s", exc)
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@tencent_meeting_router.get("/meetings")
async def list_tencent_meetings(
    settings: Settings = Depends(get_settings),
    _user_id: Any = Depends(get_current_user_id),
) -> MeetingPayloadResponse:
    client = _require_client(settings)
    try:
        data = await client.list_meetings()
    except Exception as exc:
        _handle_provider_error(exc)
    return MeetingPayloadResponse(data=data)


@tencent_meeting_router.get("/meetings/{meeting_id}")
async def get_tencent_meeting(
    meeting_id: str,
    settings: Settings = Depends(get_settings),
    _user_id: Any = Depends(get_current_user_id),
) -> MeetingPayloadResponse:
    client = _require_client(settings)
    try:
        data = await client.get_meeting(meeting_id)
    except Exception as exc:
        _handle_provider_error(exc)
    return MeetingPayloadResponse(data=data)


@tencent_meeting_router.post(
    "/meetings",
    response_model=MeetingPayloadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_tencent_meeting(
    body: IdempotentMeetingCreateRequest,
    settings: Settings = Depends(get_settings),
    _user_id: Any = Depends(get_current_user_id),
) -> MeetingPayloadResponse:
    if body.idempotency_key:
        cached = await check_idempotent(body.idempotency_key)
        if cached is not None:
            return MeetingPayloadResponse(**cached)

    client = _require_client(settings)
    try:
        data = await client.create_meeting(
            title=body.title,
            start_time=body.start_time,
            end_time=body.end_time,
            participants=body.participants,
            settings=body.settings,
        )
    except Exception as exc:
        _handle_provider_error(exc)

    response = MeetingPayloadResponse(data=data, idempotency_key=body.idempotency_key)
    if body.idempotency_key:
        await record_idempotent(body.idempotency_key, response.model_dump(mode="json"))
    return response


@tencent_meeting_router.put("/meetings/{meeting_id}", response_model=MeetingPayloadResponse)
async def update_tencent_meeting(
    meeting_id: str,
    body: TencentMeetingUpdateRequest,
    settings: Settings = Depends(get_settings),
    _user_id: Any = Depends(get_current_user_id),
) -> MeetingPayloadResponse:
    client = _require_client(settings)
    try:
        data = await client.update_meeting(
            meeting_id,
            title=body.title,
            start_time=body.start_time,
            end_time=body.end_time,
        )
    except Exception as exc:
        _handle_provider_error(exc)
    return MeetingPayloadResponse(data=data)