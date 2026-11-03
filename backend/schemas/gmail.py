from pydantic import BaseModel


class GmailAuthUrlResponse(BaseModel):
    auth_url: str


class GmailStatusResponse(BaseModel):
    is_connected: bool
    provider_account_id: str | None = None
