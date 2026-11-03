from pydantic import BaseModel, Field

HEALTH_STATUS_OK = "ok"


class HealthResponse(BaseModel):
    """API health check payload returned by GET /health."""
    status: str = Field(default=HEALTH_STATUS_OK, description="Service availability")
