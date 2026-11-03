import base64
import uuid
import urllib.parse
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from auth.jwt import create_access_token
from auth.oauth_redirect import oauth_success_redirect
from config import Settings, get_settings
from db.dependencies import verify_database_ready
from db.session import get_session
from schemas.notion import NotionAuthUrlResponse, NotionStatusResponse
from services import notion_integration_service, user_service

NOTION_AUTH_ENDPOINT = "https://api.notion.com/v1/oauth/authorize"
NOTION_TOKEN_ENDPOINT = "https://api.notion.com/v1/oauth/token"

notion_router = APIRouter(prefix="/notion", tags=["notion"])


def _require_notion_credentials(settings: Settings) -> tuple[str, str]:
    if not settings.notion_client_id or not settings.notion_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Notion OAuth 凭据尚未配置",
        )
    return settings.notion_client_id, settings.notion_client_secret


@notion_router.get("/auth-url", response_model=NotionAuthUrlResponse)
async def get_notion_auth_url(settings: Settings = Depends(get_settings)) -> NotionAuthUrlResponse:
    client_id, _ = _require_notion_credentials(settings)

    params = {
        "client_id": client_id,
        "redirect_uri": settings.notion_redirect_uri,
        "response_type": "code",
        "owner": "user",
    }
    auth_url = f"{NOTION_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"
    return NotionAuthUrlResponse(auth_url=auth_url)


@notion_router.get("/callback", dependencies=[Depends(verify_database_ready)])
async def notion_oauth_callback(
    code: Annotated[str, Query()],
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    client_id, client_secret = _require_notion_credentials(settings)

    if not settings.jwt_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="JWT_SECRET 尚未配置")

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            NOTION_TOKEN_ENDPOINT,
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.notion_redirect_uri,
            },
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/json",
            },
        )

    if token_response.status_code != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="与 Notion 交换授权码失败")

    token_data: dict[str, Any] = token_response.json()
    access_token: str = token_data["access_token"]
    workspace_id: str | None = token_data.get("workspace_id")
    workspace_name: str | None = token_data.get("workspace_name")
    bot_id: str | None = token_data.get("bot_id")

    owner = token_data.get("owner", {})
    owner_user = owner.get("user", {}) if isinstance(owner, dict) else {}
    email: str | None = owner_user.get("person", {}).get("email") if isinstance(owner_user, dict) else None

    if not email:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="无法从 Notion OAuth 响应中确定用户邮箱",
        )

    user = await user_service.get_or_create_user_by_email(session, email)

    await notion_integration_service.upsert_notion_integration(
        session,
        user_id=user.id,
        access_token=access_token,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        bot_id=bot_id,
        provider_account_id=email,
    )

    jwt_token = create_access_token(user.id, email, settings.jwt_secret)

    return oauth_success_redirect(settings.frontend_url, jwt_token)


@notion_router.get(
    "/status",
    response_model=NotionStatusResponse,
    dependencies=[Depends(verify_database_ready)],
)
async def get_notion_status(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> NotionStatusResponse:
    integration = await notion_integration_service.get_notion_integration(session, user_id)
    if integration is None:
        return NotionStatusResponse(is_connected=False)

    return NotionStatusResponse(
        is_connected=integration.is_connected,
        provider_account_id=integration.provider_account_id,
        workspace_name=integration.workspace_name,
    )


@notion_router.post(
    "/disconnect",
    dependencies=[Depends(verify_database_ready)],
)
async def post_disconnect_notion(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Clear stored Notion OAuth data and mark the integration disconnected."""
    from services.agent_runner import invalidate_token_cache
    cleared = await notion_integration_service.disconnect_notion_integration(session, user_id)
    await invalidate_token_cache(user_id, "notion")
    return {"disconnected": cleared}
