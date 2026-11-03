from pydantic import BaseModel


class NotionAuthUrlResponse(BaseModel):
    auth_url: str


class NotionStatusResponse(BaseModel):
    is_connected: bool
    provider_account_id: str | None = None
    workspace_name: str | None = None
