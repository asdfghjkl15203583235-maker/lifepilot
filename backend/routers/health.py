from fastapi import APIRouter

from schemas.health import HEALTH_STATUS_OK, HealthResponse

health_router = APIRouter(tags=["health"])


@health_router.get("/health", response_model=HealthResponse)
def read_health() -> HealthResponse:
    return HealthResponse(status=HEALTH_STATUS_OK)
