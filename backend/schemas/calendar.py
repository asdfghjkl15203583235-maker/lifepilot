from pydantic import BaseModel


class CalendarAuthUrlResponse(BaseModel):
    auth_url: str


class CalendarStatusResponse(BaseModel):
    is_connected: bool
    provider_account_id: str | None = None
