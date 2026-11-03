"""LifePilot REST adapter for weather and map life services.

Each request is persisted to `life_service_requests` and protected by an
optional idempotency key so retries from the frontend or background jobs do not
repeatedly hit the external provider.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from config import Settings, get_settings
from db.dependencies import verify_database_ready
from db.session import get_session
from models.life_service_request import LifeServiceRequest
from services.idempotency_service import check_idempotent, record_idempotent
from services.life_service_client import get_map_context, get_weather

logger = logging.getLogger(__name__)

life_services_router = APIRouter(
    prefix="/life-services",
    tags=["life-services"],
    dependencies=[Depends(verify_database_ready)],
)


class LifeServiceResponse(BaseModel):
    id: str
    service_name: str
    status: str
    result: dict[str, object] | None
    idempotency_key: str | None


class WeatherRequest(BaseModel):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    city: str | None = Field(default=None, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=512)


class MapRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    # 城市限定：不传会按全国搜，同名地点（外滩）会命中别的城市。
    city: str | None = Field(default=None, max_length=100)
    idempotency_key: str | None = Field(default=None, max_length=512)


async def _persist_life_call(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    service_name: str,
    payload: dict[str, object],
    idempotency_key: str | None,
    status_code: str,
    result: dict[str, object] | None,
) -> LifeServiceRequest:
    row = LifeServiceRequest(
        user_id=user_id,
        service_name=service_name,
        payload=payload,
        status=status_code,
        result=result,
        idempotency_key=idempotency_key,
        is_processed=status_code == "success",
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


@life_services_router.post("/weather", response_model=LifeServiceResponse)
async def post_weather(
    body: WeatherRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> LifeServiceResponse:
    if body.idempotency_key:
        cached = await check_idempotent(body.idempotency_key)
        if cached is not None:
            return LifeServiceResponse.model_validate(cached)

    payload = {
        "city": body.city,
        "latitude": body.latitude,
        "longitude": body.longitude,
    }
    try:
        result = await get_weather(
            settings,
            latitude=body.latitude,
            longitude=body.longitude,
            city=body.city,
        )
        row = await _persist_life_call(
            session,
            user_id=user_id,
            service_name="weather",
            payload=payload,
            idempotency_key=body.idempotency_key,
            status_code="success",
            result=result,
        )
    except Exception as exc:
        logger.warning("Weather request failed: %s", exc)
        row = await _persist_life_call(
            session,
            user_id=user_id,
            service_name="weather",
            payload=payload,
            idempotency_key=body.idempotency_key,
            status_code="failed",
            result={"detail": str(exc)},
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    response = LifeServiceResponse(
        id=str(row.id),
        service_name=row.service_name,
        status=row.status,
        result=row.result,
        idempotency_key=row.idempotency_key,
    )
    if body.idempotency_key:
        await record_idempotent(body.idempotency_key, response.model_dump(mode="json"))
    return response


@life_services_router.post("/map", response_model=LifeServiceResponse)
async def post_map(
    body: MapRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> LifeServiceResponse:
    if body.idempotency_key:
        cached = await check_idempotent(body.idempotency_key)
        if cached is not None:
            return LifeServiceResponse.model_validate(cached)

    payload = {"query": body.query, "city": body.city}
    try:
        result = await get_map_context(settings, query=body.query, city=body.city)
        row = await _persist_life_call(
            session,
            user_id=user_id,
            service_name="map",
            payload=payload,
            idempotency_key=body.idempotency_key,
            status_code="success",
            result=result,
        )
    except Exception as exc:
        logger.warning("Map request failed: %s", exc)
        row = await _persist_life_call(
            session,
            user_id=user_id,
            service_name="map",
            payload=payload,
            idempotency_key=body.idempotency_key,
            status_code="failed",
            result={"detail": str(exc)},
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    response = LifeServiceResponse(
        id=str(row.id),
        service_name=row.service_name,
        status=row.status,
        result=row.result,
        idempotency_key=row.idempotency_key,
    )
    if body.idempotency_key:
        await record_idempotent(body.idempotency_key, response.model_dump(mode="json"))
    return response


@life_services_router.get("", response_model=list[LifeServiceResponse])
async def get_life_service_requests(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[LifeServiceResponse]:
    result = await session.execute(
        select(LifeServiceRequest)
        .where(LifeServiceRequest.user_id == user_id)
        .order_by(LifeServiceRequest.created_at.desc())
        .limit(limit),
    )
    return [
        LifeServiceResponse(
            id=str(row.id),
            service_name=row.service_name,
            status=row.status,
            result=row.result,
            idempotency_key=row.idempotency_key,
        )
        for row in result.scalars().all()
    ]