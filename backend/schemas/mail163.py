from pydantic import BaseModel


class Mail163StatusResponse(BaseModel):
    is_connected: bool
    provider_account_id: str | None = None
    provider: str = "mail163"